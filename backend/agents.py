"""The planning team: List Reader -> Price Scout -> (tools + optimizer) -> Trip Advisor, plus the Plan Helper chat.

Agents read and choose; Python code fetches data and does all arithmetic. Every step emits an event
(for the live progress view) and an audit record.
"""

import asyncio
import json
import uuid
from typing import Any, Awaitable, Callable

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.exceptions import ModelHTTPError

from backend import audit, data_sources as ds
from backend.config import (
    CHAT_TOTAL_TOKENS_LIMIT,
    MAX_INPUT_CHARS,
    MAX_LIST_ITEMS,
    MAX_TOOL_RESULT_CHARS,
    PROMPTS_DIR,
    REQUEST_LIMIT,
    RUN_BUDGET_USD,
    TOTAL_TOKENS_LIMIT,
    build_model,
)
from backend.models import Advice, ChatAnswer, ParsedList, PlanRequest, PriceScoutResult
from backend.optimizer import build_plans
from backend.units import STEP, describe, ref_units_for, units_needed, variant_reason
from backend.safety import SpendMeter, allowed_amounts, fence, screen_text, strip_control, ungrounded_amounts

Emit = Callable[[dict], Awaitable[None]]
PLANS: dict[str, dict] = {}  # finished plans by id, for the chat helper (kept in memory)


def _prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _clip(value: Any, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + f"...[truncated {len(text) - limit} chars]"


def _compact_offer(o: dict) -> dict:
    return {k: o[k] for k in ("offer_id", "store_brand", "name", "price", "unit_text", "sale_story", "valid_to", "source") if o.get(k) not in (None, "")}


class Planner:
    """One planning run. Holds the shared spend meter, the offer registry, and the event emitter."""

    def __init__(self, req: PlanRequest, emit: Emit):
        self.req = req
        self.emit = emit
        self.run_id = uuid.uuid4().hex[:10]
        self.meter = SpendMeter(RUN_BUDGET_USD)
        self.offers: dict[str, dict] = {}  # every offer any tool returned in this run, by offer_id
        self.model = build_model()
        self.limits = UsageLimits(request_limit=REQUEST_LIMIT, total_tokens_limit=TOTAL_TOKENS_LIMIT)

    # ----------------------------------------------------------------------------- helpers
    async def _event(self, kind: str, **fields) -> None:
        audit.log(self.run_id, kind, **fields)
        await self.emit({"type": kind, **fields})

    def _allowed_brands(self) -> set[str]:
        brands = {s["brand"] for s in ds.STORES}
        if not self.req.has_costco:
            brands.discard("Costco")
        if not self.req.has_bjs:
            brands.discard("BJ's Wholesale Club")
        return brands

    def _register(self, offers: list[dict]) -> list[dict]:
        allowed = self._allowed_brands()
        kept = [o for o in offers if o["store_brand"] in allowed]
        for o in kept:
            self.offers[o["offer_id"]] = o
        return kept

    async def _search(self, keyword: str) -> dict:
        sales, base = await asyncio.gather(
            asyncio.to_thread(ds.search_weekly_sales, keyword),
            asyncio.to_thread(ds.search_base_prices, keyword),
        )
        # website prices first (one per store and staple), then this week's flyer deals
        offers = self._register(base["offers"]) + self._register(sales["offers"])
        await self._event("tool", agent="price_scout", tool="search_prices", args={"keyword": keyword},
                          summary=f"{len(offers)} offers", live=sales["data"].get("live"),
                          stale=sales["data"].get("stale", False))
        return {"keyword": keyword, "offers": offers, "freshness": sales["data"]}

    async def _run_agent(self, name: str, agent: Agent, prompt: str, **kw) -> Any:
        await self._event("agent_start", agent=name)
        try:
            result = await agent.run(prompt, usage_limits=self.limits, **kw)
        except ModelHTTPError as exc:
            if is_content_filter(exc):
                await self._event("guard", check="provider_content_filter", agent=name, blocked=True)
                raise PlanError("Your input was blocked by the AI provider's safety filter. "
                                "Please list only groceries, like '1 dozen eggs, milk, 2 lb chicken'.")
            raise
        usage = self.meter.record(name, result.usage)
        await self._event("agent_done", agent=name, usage=usage)
        return result.output

    # ------------------------------------------------------------------------------- steps
    async def run(self) -> dict:
        req = self.req
        address = strip_control(req.address, 200)
        shopping = strip_control(req.shopping_list, MAX_INPUT_CHARS)
        guard = screen_text(address + "\n" + shopping)
        await self._event("guard", check="input_screen", suspicious=guard["suspicious"], matches=guard["matches"])

        # 1) where is home?
        place = await asyncio.to_thread(ds.geocode, address)
        await self._event("tool", agent="trip_planner", tool="geocode", args={"address": address},
                          summary=place.get("label") or place.get("error"))
        if not place["ok"]:
            raise PlanError(place["error"])
        home = (place["lat"], place["lon"])

        # 2) List Reader: free text -> structured items
        intake = Agent(self.model, output_type=ParsedList, instructions=_prompt("intake"), name="list_reader")
        catalog = json.dumps(ds.reference_catalog(), ensure_ascii=False)
        parsed: ParsedList = await self._run_agent(
            "list_reader", intake,
            f"Reference catalog (ref_id, name, unit):\n{catalog}\n\n{fence('SHOPPING LIST', shopping)}",
        )
        ref_ids = {c["ref_id"] for c in ds.reference_catalog()}
        items = []
        for it in parsed.items[:MAX_LIST_ITEMS]:
            d = it.model_dump()
            if d["ref_id"] not in ref_ids:
                d["ref_id"], d["ref_units"] = None, None
            if not d["qty"] or not (0 < d["qty"] <= 500):
                d["qty"], d["qty_unit"] = 1, "pack"
            # the estimate's amount comes from code too, so it always matches the amount in the list
            ref = next((c for c in ds.reference_catalog() if c["ref_id"] == d["ref_id"]), None)
            if ref:
                coded = ref_units_for(d["qty"], d["qty_unit"], ref["unit"], d["search_keyword"])
                d["ref_units"] = coded if coded is not None else d["ref_units"]
            if d["ref_units"] is not None and not (0 < d["ref_units"] <= 50):
                d["ref_units"] = None
            d["weight_lb"] = min(max(d["weight_lb"] or 1.0, 0.1), 40)
            d["display"] = describe(d["qty"], d["qty_unit"], d["name"] or d["search_keyword"])
            d["step"] = STEP.get(d["qty_unit"], 1)
            items.append(d)
        if not items:
            raise PlanError("We could not find any groceries in that list. Try something like '1 dozen eggs, milk, 2 lb chicken breast'.")
        if parsed.suspicious or parsed.ignored:
            await self._event("guard", check="list_reader", suspicious=parsed.suspicious, ignored=parsed.ignored[:10])
        await self._event("items", items=[{"key": i["key"], "display": i["display"], "keyword": i["search_keyword"]} for i in items])

        # 3) look up every keyword at once (tools), then let the Price Scout match offers to items
        keywords = sorted({i["search_keyword"].lower().strip() for i in items})
        searches = await asyncio.gather(*(self._search(k) for k in keywords))
        by_kw = {s["keyword"]: s for s in searches}
        scout_input = [{
            "item_key": i["key"], "wants": i["display"],
            "offers": [_compact_offer(o) for o in by_kw[i["search_keyword"].lower().strip()]["offers"]][:34],
        } for i in items]

        scout = Agent(self.model, output_type=PriceScoutResult, instructions=_prompt("price_scout"), name="price_scout")

        @scout.tool_plain
        async def search_weekly_sales(keyword: str) -> str:
            """Search this week's New Haven flyer deals (and the Trader Joe's / Costco website price list) for a keyword."""
            res = await self._search(keyword[:40])
            return _clip([_compact_offer(o) for o in res["offers"]][:14])

        @scout.tool_plain
        async def search_website_prices(keyword: str) -> str:
            """Search the saved Trader Joe's and Costco website prices for a keyword."""
            res = await asyncio.to_thread(ds.search_base_prices, keyword[:40])
            offers = self._register(res["offers"])
            await self._event("tool", agent="price_scout", tool="search_website_prices", args={"keyword": keyword}, summary=f"{len(offers)} offers")
            return _clip([_compact_offer(o) for o in offers])

        has_any = any(x["offers"] for x in scout_input)
        if has_any:
            picks: PriceScoutResult = await self._run_agent(
                "price_scout", scout, fence("ITEMS AND OFFERS", json.dumps(scout_input, ensure_ascii=False)),
            )
        else:
            picks = PriceScoutResult(picks=[], no_match=[i["key"] for i in items])
            await self._event("agent_skip", agent="price_scout", reason="no store offers found for any item")

        # every match must point at an offer a tool really returned; the code (not the model) picks the cheapest
        choices: dict[tuple[str, str], list[dict]] = {}
        rejected, accepted = [], 0
        item_keys = {i["key"] for i in items}
        for p in picks.picks:
            offer = self.offers.get(p.offer_id)
            if not offer or p.item_key not in item_keys or not (0 < p.units <= 50):
                rejected.append(p.model_dump())
                continue
            bucket = choices.setdefault((p.item_key, offer["store_brand"]), [])
            if all(m["offer_id"] != p.offer_id for m in bucket):
                item = next(i for i in items if i["key"] == p.item_key)
                why = variant_reason(offer["name"], f"{item['display']} {item['search_keyword']}")
                bucket.append({"offer_id": p.offer_id, "units": p.units, "note": p.note,
                               "fit": "unsure" if why else "exact", "fit_reason": why or ""})
                accepted += 1
        await self._event("guard", check="offer_grounding", accepted=accepted, rejected=len(rejected),
                          rejected_examples=rejected[:3])

        # 4) routes and weather (tools), then the optimizer (pure Python)
        routes, weather = await asyncio.gather(
            asyncio.to_thread(ds.routes_from, home), asyncio.to_thread(ds.weather_week, home[0], home[1]),
        )
        await self._event("tool", agent="trip_planner", tool="routes", summary=routes["basis"])
        await self._event("tool", agent="trip_planner", tool="weather", summary=f"{len(weather['days'])}-day forecast")

        prefs = {**req.model_dump(), "home": home, "allowed_brands": self._allowed_brands(), "overrides": {}, "included": set()}
        ctx = {"items": items, "choices": choices, "offers": self.offers, "routes": routes["routes"],
               "weather": weather, "prefs": prefs}
        CONTEXTS[self.run_id] = ctx
        plan = compute_plan(ctx)
        await self._event("optimizer", options=len(plan["alternatives"]) + 1, best=plan["best"]["plan_id"],
                          total=plan["best"]["total"])

        # 5) Trip Advisor writes the explanation; every $ amount is checked against the plan
        # the advisor sees only the cleaned item names (for the reply language), never the raw input
        plan["advice"] = await self._advise(plan, ", ".join(i["display"] for i in items))

        plan.update({
            "id": self.run_id,
            "home": {"lat": home[0], "lon": home[1], "label": place["label"]},
            "items": items,
            "weather": weather["days"],
            "rates": ds.RATES,
            "shuttle": {"name": ds.SHUTTLE["name"], "basis": ds.SHUTTLE["basis"], "sources": ds.SHUTTLE["sources"],
                        "note": ds.SHUTTLE["note"]},
            "data_freshness": {
                "sales": [{"keyword": s["keyword"], **s["freshness"]} for s in searches],
                "routes": routes["data"], "weather": weather["data"], "routes_basis": routes["basis"],
            },
            "guards": {"input": guard, "list_ignored": parsed.ignored, "list_suspicious": parsed.suspicious,
                       "rejected_picks": len(rejected)},
            "usage": self.meter.summary(),
        })
        PLANS[self.run_id] = plan
        audit.log(self.run_id, "run_summary", usage=plan["usage"], best=plan["best"]["plan_id"], total=plan["best"]["total"])
        return plan

    async def _advise(self, plan: dict, item_names: str) -> dict:
        advisor = Agent(self.model, output_type=Advice, instructions=_prompt("advisor"), name="trip_advisor")
        allowed = allowed_amounts(plan)
        if "savings_vs_baseline" in plan:
            allowed.add(round(abs(plan["savings_vs_baseline"]), 2))
        summary = _plan_for_model(plan)
        prompt = (f"Reply in the language of these item names: {item_names[:120]!r}\n\n"
                  + fence("PLAN", json.dumps(summary, ensure_ascii=False)))
        for attempt in range(2):
            advice: Advice = await self._run_agent("trip_advisor", advisor, prompt)
            text = " ".join([advice.headline, *advice.reasons, *advice.tips, *advice.cautions])
            bad = ungrounded_amounts(text, allowed)
            await self._event("guard", check="price_grounding", attempt=attempt + 1, ungrounded=bad)
            if not bad:
                return {**advice.model_dump(), "verified": True}
            prompt += (f"\n\nYour last answer used dollar amounts that are not in the plan: {bad}. "
                       "Rewrite it using only amounts copied exactly from the plan.")
        return {**_template_advice(plan), "verified": True, "fallback": True}


class PlanError(Exception):
    """A problem the user can fix (bad address, empty list...)."""


CONTEXTS: dict[str, dict] = {}  # what each plan was built from, so a product swap can re-rank without the model


def compute_plan(ctx: dict) -> dict:
    """Run the optimizer (pure Python) and add the comparison figures. No model calls."""
    prefs = ctx["prefs"]
    plan = build_plans(ctx["items"], ctx["choices"], ctx["offers"], ctx["routes"], ctx["weather"], prefs)
    if not plan["best"]:
        raise PlanError("No store and transport combination fits these settings. Try allowing more transport modes or days.")
    plan["zipcar_hourly"] = prefs.get("zipcar_hourly") or ds.RATES["zipcar"]["hourly_usd"]
    if plan["baseline"]:
        plan["savings_vs_baseline"] = round(plan["baseline"]["total"] - plan["best"]["total"], 2)
        plan["minutes_saved_vs_baseline"] = plan["baseline"]["total_minutes"] - plan["best"]["total_minutes"]
    plan["time_value_per_hour"] = prefs.get("time_value_per_hour", 0)
    plan["break_even"] = _break_even(plan)
    plan["review"] = _review_groups(ctx, plan)
    return plan


def _break_even(plan: dict) -> dict:
    """At what value of an hour of your time a faster (or a cheaper) option would beat the best plan.

    For an option that costs more but saves time: switch above (extra $) / (hours saved).
    For an option that costs less but takes longer: switch below (dollars saved) / (extra hours).
    Pure arithmetic on the plan's own totals and minutes; rain, heavy-bag and estimate penalties are ignored.
    """
    best = plan["best"]
    options = [o for o in [*plan["alternatives"], plan.get("baseline")] if o and o["plan_id"] != best["plan_id"]]
    current = plan.get("time_value_per_hour", 0)

    def describe(o: dict, rate: float) -> dict:
        return {"plan_id": o["plan_id"], "stores": " + ".join(s["brand"] for s in o["stores"]),
                "mode": o["transport"]["mode"], "day": o["day_label"], "total": o["total"],
                "minutes": o["total_minutes"], "per_hour": round(rate, 2)}

    faster = [describe(o, (o["total"] - best["total"]) / ((best["total_minutes"] - o["total_minutes"]) / 60))
              for o in options if o["total_minutes"] < best["total_minutes"] and o["total"] > best["total"]]
    cheaper = [describe(o, (best["total"] - o["total"]) / ((o["total_minutes"] - best["total_minutes"]) / 60))
               for o in options if o["total_minutes"] > best["total_minutes"] and o["total"] < best["total"]]
    up = min((f for f in faster if f["per_hour"] > current), key=lambda f: f["per_hour"], default=None)
    down = max((c for c in cheaper if c["per_hour"] < current), key=lambda c: c["per_hour"], default=None)
    return {"faster_above": up, "cheaper_below": down}


def _preview(ctx: dict, forced: dict) -> dict | None:
    """What the best plan would be if these items had to be these exact products (pure Python)."""
    prefs = {**ctx["prefs"], "forced": forced}
    p = build_plans(ctx["items"], ctx["choices"], ctx["offers"], ctx["routes"], ctx["weather"], prefs)
    b = p["best"]
    if not b:
        return None
    return {"total": b["total"], "stores": " + ".join(s["brand"] for s in b["stores"]),
            "mode": b["transport"]["mode"], "day": b["day_label"], "lines": {l["key"]: l for l in b["lines"]}}


def _review_groups(ctx: dict, plan: dict) -> list[dict]:
    """For each item that has other versions (lactose-free milk, instant rice...), the choices A, B, C...:
    what the plan uses now and, for every other version, what the best plan would cost with it."""
    forced = ctx["prefs"].get("forced") or {}
    items = {i["key"]: i for i in ctx["items"]}
    variants: dict[str, list] = {}
    for (item_key, brand), matches in ctx["choices"].items():
        item = items.get(item_key)
        for m in matches:
            offer = ctx["offers"].get(m["offer_id"])
            if item and offer and m.get("fit", "exact") != "exact":
                variants.setdefault(item_key, []).append((brand, m, offer))
    groups = []
    for item_key, rows in variants.items():
        item = items[item_key]
        chosen = forced.get(item_key)
        others = {k: v for k, v in forced.items() if k != item_key}
        regular_preview = _preview(ctx, others) if chosen else None
        # option A is always the regular product: from this plan, or (if a version is chosen) from the plan without it
        line = ((regular_preview or {}).get("lines", {}).get(item_key) if chosen
                else next((l for l in plan["best"]["lines"] if l["key"] == item_key), None))
        current = {
            "product": line["product"] if line else None, "store_brand": line["store_brand"] if line else None,
            "cost": line["cost"] if line else None, "source": line["source"] if line else None,
            "selected": not chosen,
            "plan_total": plan["best"]["total"] if not chosen else (regular_preview or {}).get("total"),
            "plan_stores": (" + ".join(s["brand"] for s in plan["best"]["stores"]) if not chosen
                            else (regular_preview or {}).get("stores")),
        }
        options = []
        for brand, m, offer in sorted(rows, key=lambda r: (r[0], r[2]["price"])):
            conv = units_needed(item["qty"], item["qty_unit"], item["search_keyword"], offer["name"], offer.get("unit_text", ""))
            units = conv[0] if conv else m["units"]
            preview = None if chosen == offer["offer_id"] else _preview(ctx, {**others, item_key: offer["offer_id"]})
            options.append({
                "offer_id": offer["offer_id"], "product": offer["name"], "store_brand": brand,
                "unit_text": offer.get("unit_text", ""), "cost": round(offer["price"] * units, 2),
                "units_by": "code" if conv else "ai", "reason": m.get("fit_reason") or "other version",
                "selected": chosen == offer["offer_id"],
                "plan_total": plan["best"]["total"] if chosen == offer["offer_id"] else (preview or {}).get("total"),
                "plan_stores": (" + ".join(s["brand"] for s in plan["best"]["stores"]) if chosen == offer["offer_id"]
                                else (preview or {}).get("stores")),
            })
        groups.append({"item_key": item_key, "item": item["display"], "current": current, "options": options})
    return sorted(groups, key=lambda g: g["item"])


def set_version(plan_id: str, item_key: str, offer_id: str | None) -> dict:
    """The user picked which version of an item to buy (None = back to the regular product). Code only."""
    ctx, old = CONTEXTS.get(plan_id), PLANS.get(plan_id)
    if not ctx or not old:
        raise PlanError("That plan has expired. Please make a new plan first.")
    forced = ctx["prefs"].setdefault("forced", {})
    if offer_id:
        known = any(m["offer_id"] == offer_id for (k, _), ms in ctx["choices"].items() if k == item_key for m in ms)
        if not known:
            raise PlanError("That product is not one of the matches for this item.")
        forced[item_key] = offer_id
    else:
        forced.pop(item_key, None)
    plan = {**old, **compute_plan(ctx)}
    plan["advice"] = {**_template_advice(plan), "verified": True, "fallback": False,
                      "note": "Updated after your product choice (recalculated by code, no AI call)."}
    PLANS[plan_id] = plan
    audit.log(plan_id, "user_version", item=item_key, offer_id=offer_id, new_total=plan["best"]["total"])
    return plan


def set_quantity(plan_id: str, item_key: str, qty: float) -> dict:
    """The user changed an amount: every store is re-priced for the new amount by code, without the model."""
    ctx, old = CONTEXTS.get(plan_id), PLANS.get(plan_id)
    if not ctx or not old:
        raise PlanError("That plan has expired. Please make a new plan first.")
    item = next((i for i in ctx["items"] if i["key"] == item_key), None)
    if not item:
        raise PlanError("That item is not in this plan.")
    if not (0 < qty <= 500):
        raise PlanError("Please choose an amount above zero.")
    old_qty = item["qty"]
    item["qty"] = round(qty, 2)
    if item.get("ref_units"):
        item["ref_units"] = round(item["ref_units"] * qty / old_qty, 3)
    item["weight_lb"] = round(item["weight_lb"] * qty / old_qty, 2)
    item["display"] = describe(item["qty"], item["qty_unit"], item["name"] or item["search_keyword"])
    plan = {**old, **compute_plan(ctx), "items": ctx["items"]}
    plan["advice"] = {**_template_advice(plan), "verified": True, "fallback": False,
                      "note": "Updated after you changed an amount (recalculated by code, no AI call)."}
    PLANS[plan_id] = plan
    audit.log(plan_id, "user_quantity", item=item_key, qty=qty, unit=item["qty_unit"], new_total=plan["best"]["total"])
    return plan


def swap_product(plan_id: str, item_key: str, store_brand: str, offer_id: str) -> dict:
    """The user picked a different matching product: re-rank every option instantly, without the model."""
    ctx, old = CONTEXTS.get(plan_id), PLANS.get(plan_id)
    if not ctx or not old:
        raise PlanError("That plan has expired. Please make a new plan first.")
    matches = ctx["choices"].get((item_key, store_brand)) or []
    if not any(m["offer_id"] == offer_id for m in matches):
        raise PlanError("That product is not one of the matches for this item.")
    ctx["prefs"]["overrides"][(item_key, store_brand)] = offer_id
    core = compute_plan(ctx)
    plan = {**old, **core}
    plan["advice"] = {**_template_advice(plan), "verified": True, "fallback": False,
                      "note": "Updated after your product choice (recalculated by code, no AI call)."}
    PLANS[plan_id] = plan
    audit.log(plan_id, "user_swap", item=item_key, store=store_brand, offer_id=offer_id, new_total=plan["best"]["total"])
    return plan


def is_content_filter(exc: Exception) -> bool:
    """True when the model provider refused the prompt (its own jailbreak and abuse filter)."""
    return "content_filter" in str(getattr(exc, "body", "")) or "content_filter" in str(exc)


def _drop_brand(plan: dict, brand: str) -> None:
    keep = [o for o in [plan["best"], *plan["alternatives"]] if o and all(s["brand"] != brand for s in o["stores"])]
    plan["best"], plan["alternatives"] = (keep[0] if keep else None), keep[1:]


def _plan_for_model(plan: dict) -> dict:
    """A small view of the plan for the model: no coordinates, no raw data, just what it must explain."""

    def opt(o):
        if not o:
            return None
        return {
            "plan_id": o["plan_id"], "day": o["day_label"], "start_hour": o["start_hour"],
            "stores": [s["brand"] + (f" ({s['membership']})" if s["membership"] else "") for s in o["stores"]],
            "transport": {k: o["transport"][k] for k in ("mode", "cost", "kind", "detail") if k in o["transport"]},
            "basket": o["basket"], "transport_cost": o["transport_cost"], "total": o["total"],
            "minutes": o["total_minutes"], "travel_minutes": o["travel_minutes"], "shopping_minutes": o["shop_minutes"],
            "time_cost": o["time_cost"], "total_with_your_time": o["total_with_time"],
            "weather": o["weather"], "busy_at_start": o["busy_at_start"],
            "missing_prices": o["missing"],
            "items": [{"item": l["item"], "store": l["store_brand"], "price": l["price"], "units": l["units"],
                       "cost": l["cost"], "source": l["source"], "valid_to": l.get("valid_to")} for l in o["lines"]],
        }

    return {
        "best": opt(plan["best"]),
        "nearest_store_baseline": opt(plan["baseline"]),
        "baseline_is_best": bool(plan.get("baseline") and plan["baseline"]["plan_id"] == plan["best"]["plan_id"]),
        "money_saved_vs_baseline": plan.get("savings_vs_baseline"),
        "minutes_saved_vs_baseline": plan.get("minutes_saved_vs_baseline"),
        "user_values_time_at_per_hour": plan.get("time_value_per_hour"),
        "alternatives": [{k: v for k, v in opt(o).items() if k != "items"} for o in plan["alternatives"][:4]],
        "sale_deadlines": plan["sale_deadlines"],
        "heavy_bags": plan["heavy_bags"],
        "crowd_note": plan["busy_note"],
    }


def _template_advice(plan: dict) -> dict:
    b = plan["best"]
    stores = " + ".join(s["brand"] for s in b["stores"])
    mode = {"walk": "on foot", "shuttle": "by the free Yale shuttle", "uber": "by Uber", "zipcar": "by Zipcar"}[b["transport"]["mode"]]
    hour = f"{(b['start_hour'] + 11) % 12 + 1}{'am' if b['start_hour'] < 12 else 'pm'}"
    reasons = [f"Groceries ${b['basket']:.2f} plus getting there ${b['transport_cost']:.2f}."]
    save = plan.get("savings_vs_baseline")
    base = plan.get("baseline")
    if base and save and save > 0:
        reasons.append(f"${save:.2f} less than going to the nearest store ({base['stores'][0]['brand']}).")
    elif base and save and save < 0:
        reasons.append(f"${-save:.2f} more than the nearest store ({base['stores'][0]['brand']}), but "
                       f"{plan.get('minutes_saved_vs_baseline', 0)} minutes faster.")
    return {
        "headline": f"Go {b['day_label']} at {hour} to {stores} {mode}: ${b['total']:.2f} in total.",
        "reasons": reasons,
        "tips": [f"Sale on {d['item']} at {d['store_brand']} ends {d['valid_to']}." for d in plan["sale_deadlines"][:2]],
        "cautions": ["Uber and Zipcar costs are estimates."] + ([f"No price found for: {', '.join(b['missing'])}."] if b["missing"] else []),
    }


# ------------------------------------------------------------------------------------ chat
async def chat(plan_id: str, message: str, history: list[dict]) -> dict:
    plan = PLANS.get(plan_id)
    if not plan:
        raise PlanError("That plan has expired. Please make a new plan first.")
    run_id = f"{plan_id}-chat-{uuid.uuid4().hex[:4]}"
    message = strip_control(message, 600)
    guard = screen_text(message)
    audit.log(run_id, "guard", check="chat_input", **guard)

    meter = SpendMeter(RUN_BUDGET_USD)
    allowed = allowed_amounts(plan)
    if plan.get("savings_vs_baseline") is not None:
        allowed.add(round(abs(plan["savings_vs_baseline"]), 2))
    helper = Agent(build_model(), output_type=ChatAnswer, instructions=_prompt("chat"), name="plan_helper")
    tools_used: list[str] = []

    @helper.tool_plain
    def get_plan() -> str:
        """The current plan: best option, nearest-store baseline, alternatives, sale deadlines."""
        tools_used.append("get_plan")
        return _clip(_plan_for_model(plan), 8000)

    @helper.tool_plain
    async def search_weekly_sales(keyword: str) -> str:
        """This week's New Haven flyer deals for a keyword."""
        tools_used.append(f"search_weekly_sales({keyword})")
        res = await asyncio.to_thread(ds.search_weekly_sales, keyword[:40])
        for o in res["offers"]:
            allowed.add(round(o["price"], 2))
        return _clip([_compact_offer(o) for o in res["offers"]][:14])

    @helper.tool_plain
    def get_weather(date: str) -> str:
        """Forecast for one date (YYYY-MM-DD) in the next 7 days, with hourly rain chance."""
        tools_used.append(f"get_weather({date})")
        day = next((d for d in plan["weather"] if d["date"] == date), None)
        if not day:
            return "No forecast for that date (only the next 7 days are available)."
        return _clip({**day, "hours": [h for h in day["hours"] if 8 <= h["hour"] <= 21]})

    convo = "\n".join(f"{m.get('role', 'user')}: {strip_control(str(m.get('content', '')), 400)}" for m in history[-8:])
    prompt = (fence("EARLIER CONVERSATION", convo) + "\n\n" if convo else "") + fence("USER MESSAGE", message)
    limits = UsageLimits(request_limit=8, total_tokens_limit=CHAT_TOTAL_TOKENS_LIMIT)
    answer = None
    for attempt in range(2):
        try:
            result = await helper.run(prompt, usage_limits=limits)
        except ModelHTTPError as exc:
            if not is_content_filter(exc):
                raise
            audit.log(run_id, "guard", check="provider_content_filter", blocked=True)
            return {"answer": "I can't help with that request. I can only answer questions about your grocery plan, "
                              "like store choice, other days, sale dates or weather.",
                    "tools": [], "guard": {**guard, "provider_filter": True}, "grounding_blocked": False,
                    "usage": meter.summary()}
        meter.record("plan_helper", result.usage)
        bad = ungrounded_amounts(result.output.answer, allowed)
        audit.log(run_id, "guard", check="price_grounding", attempt=attempt + 1, ungrounded=bad)
        if not bad:
            answer = result.output.answer
            break
        prompt += f"\n\n(Your last answer used amounts not found in the plan or tools: {bad}. Answer again using only exact amounts from the data.)"
    blocked = answer is None
    if blocked:
        answer = ("I could not verify every price in my answer, so I won't guess. "
                  "Check the plan table, or re-plan with the settings you want to compare.")
    audit.log(run_id, "chat", message=message, answer=answer, tools=tools_used, usage=meter.summary())
    return {"answer": answer, "tools": tools_used, "guard": guard, "grounding_blocked": blocked, "usage": meter.summary()}
