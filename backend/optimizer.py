"""Pure-Python math: basket cost per store and day, trip cost per transport mode, and the ranking.

No model ever does arithmetic. The agents decide which offer matches which item; this module adds
up the money, checks every offer id against what the tools really returned, and ranks the options.
"""

import math
from datetime import date, datetime, timedelta

from backend.data_sources import RATES, SHUTTLE, STORES, miles_between, reference_price
from backend.units import units_needed

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ESTIMATE_PENALTY = 1.5  # ranking only (never added to any shown price): $ per item priced by a national average
BUSY_NOTE ="Estimated from common grocery patterns (weekend midday is busiest), not live store data."


# ------------------------------------------------------------------------------- transport
def uber_round_trip(miles: float) -> float:
    u = RATES["uber"]
    one_way = max(u["minimum_usd"], u["base_usd"] + u["per_mile_usd"] * miles)
    return round(2 * one_way, 2)


def zipcar_cost(total_drive_minutes: float, stops: int, hourly: float) -> tuple[float, float]:
    z = RATES["zipcar"]
    hours = (total_drive_minutes + stops * RATES["shopping_minutes_per_store"]) / 60
    hours = max(z["minimum_hours"], math.ceil(hours / z["increment_hours"]) * z["increment_hours"])
    return round(hours * hourly, 2), hours


def _loop_minutes_between(i: int, j: int) -> float:
    """Riding time from loop stop i forward to loop stop j: the loop time split by distance."""
    loop = SHUTTLE["loop"]
    legs = [miles_between((loop[k]["lat"], loop[k]["lon"]), (loop[(k + 1) % len(loop)]["lat"], loop[(k + 1) % len(loop)]["lon"]))
            for k in range(len(loop))]
    total, d, k = sum(legs), 0.0, i
    while k != j:
        d += legs[k]
        k = (k + 1) % len(loop)
    return d / total * SHUTTLE["loop_minutes"]


def shuttle_option(stops: list[dict], home: tuple[float, float], day: date, heavy: bool) -> dict | None:
    """Yale's free Weekend Grocery Shuttle, if it runs that day and serves every store in the trip.

    Going out: watch the tracker app and leave so you wait about 5 minutes; ride to the store stop.
    Coming back: the shuttle keeps looping, so it returns to your stop every loop. You wait whatever is
    left of the loop after walking to the store(s), shopping, and walking back to the stop.
    """
    sh, loop = SHUTTLE, SHUTTLE["loop"]
    if day.weekday() not in sh["days"]:
        return None
    walk_speed = RATES["walk"]["speed_mph"]
    near_stops = []
    for s in stops:
        idx = min((k for k, st in enumerate(loop) if st.get("store")),
                  key=lambda k: miles_between((loop[k]["lat"], loop[k]["lon"]), (s["lat"], s["lon"])))
        d = miles_between((loop[idx]["lat"], loop[idx]["lon"]), (s["lat"], s["lon"])) * 1.25
        if d > sh["max_walk_from_store_stop_miles"]:
            return None
        near_stops.append((idx, d))
    board = min((k for k, st in enumerate(loop) if st.get("board")),
                key=lambda k: miles_between(home, (loop[k]["lat"], loop[k]["lon"])))
    walk_to_stop_mi = miles_between(home, (loop[board]["lat"], loop[board]["lon"])) * 1.25
    if walk_to_stop_mi > sh["max_walk_to_stop_miles"]:
        return None

    off, back = near_stops[0][0], near_stops[-1][0]  # get off near the first store, get back on near the last
    store_walk_mi = near_stops[0][1] + near_stops[-1][1]
    if len(stops) == 2:
        store_walk_mi += miles_between((stops[0]["lat"], stops[0]["lon"]), (stops[1]["lat"], stops[1]["lon"])) * 1.25
    store_walk = store_walk_mi / walk_speed * 60
    shopping = RATES["shopping_minutes_per_store"] * len(stops)

    wait_out = sh["tracker_wait_minutes"]
    ride_out = _loop_minutes_between(board, off)
    # the shuttle we rode reaches the return stop `offset` minutes after dropping us off, then every loop
    away = store_walk + shopping
    offset = _loop_minutes_between(off, back) if back != off else 0.0
    loops = max(0, math.ceil((away - offset) / sh["loop_minutes"] - 1e-9))
    wait_back = offset + loops * sh["loop_minutes"] - away
    ride_back = _loop_minutes_between(back, board)
    walk_home = walk_to_stop_mi / walk_speed * 60

    travel = walk_home * 2 + wait_out + ride_out + store_walk + wait_back + ride_back
    breakdown = {"walk_to_stop": round(walk_home), "wait_out": round(wait_out), "ride_out": round(ride_out),
                 "walk_at_stores": round(store_walk), "shopping": shopping,
                 "wait_back": round(wait_back), "ride_back": round(ride_back),
                 "walk_home": round(walk_home)}
    return {
        "mode": "shuttle", "cost": 0.0, "minutes": round(travel), "kind": "free", "breakdown": breakdown,
        "detail": (f"board at {loop[board]['name']} ({walk_to_stop_mi:.1f} mi walk), get off at {loop[off]['name']}; "
                   f"wait ~{breakdown['wait_out']} min going (tracker app), ~{breakdown['wait_back']} min coming back"),
        "basis": sh["basis"], "shuttle_hours": (sh["start_hour"], sh["end_hour"]),
        "warning": "Heavy bags on the walk to the stop" if heavy and walk_to_stop_mi > 0.4 else None,
    }


def transport_options(stops: list[dict], routes: dict, heavy: bool, zipcar_hourly: float,
                      home: tuple[float, float] | None = None, day: date | None = None) -> list[dict]:
    """Every sensible way to reach these stores (one or two) from home and back."""
    opts = []
    if home and day:
        sh = shuttle_option(stops, home, day, heavy)
        if sh:
            opts.append(sh)
    if len(stops) == 1:
        r = routes[stops[0]["id"]]
        if r["walk_miles"] <= RATES["walk"]["max_one_way_miles"]:
            walk_min = r["walk_miles"] / RATES["walk"]["speed_mph"] * 60 * 2
            opts.append({
                "mode": "walk", "cost": 0.0, "minutes": round(walk_min), "kind": "free",
                "detail": f"{r['walk_miles']:.1f} mi each way on foot",
                "warning": "Heavy bags for a walk home" if heavy else None,
            })
        opts.append({
            "mode": "uber", "cost": uber_round_trip(r["drive_miles"]), "minutes": round(r["drive_minutes"] * 2 + 8),
            "kind": "estimated", "detail": f"{r['drive_miles']:.1f} mi each way, round trip",
            "basis": RATES["uber"]["basis"], "warning": None,
        })
        cost, hours = zipcar_cost(r["drive_minutes"] * 2, 1, zipcar_hourly)
        opts.append({
            "mode": "zipcar", "cost": cost, "minutes": round(r["drive_minutes"] * 2), "kind": "estimated",
            "detail": f"{hours:g} h booking at ${zipcar_hourly:.2f}/h", "basis": RATES["zipcar"]["basis"], "warning": None,
        })
    else:
        a, b = (routes[s["id"]] for s in stops)
        # home -> A -> B -> home; the A->B leg is the straight line between the stores x 1.3, at 20 mph
        leg_ab_mi = max(miles_between((stops[0]["lat"], stops[0]["lon"]), (stops[1]["lat"], stops[1]["lon"])) * 1.3, 0.3)
        leg_ab_min = max(leg_ab_mi / 20 * 60 + 3, 4.0)
        drive_min = a["drive_minutes"] + leg_ab_min + b["drive_minutes"]
        cost, hours = zipcar_cost(drive_min, 2, zipcar_hourly)
        opts.append({
            "mode": "zipcar", "cost": cost, "minutes": round(drive_min), "kind": "estimated",
            "detail": f"{hours:g} h booking, two stops, at ${zipcar_hourly:.2f}/h", "basis": RATES["zipcar"]["basis"], "warning": None,
        })
        uber = uber_round_trip(a["drive_miles"]) / 2 + RATES["uber"]["base_usd"] + RATES["uber"]["per_mile_usd"] * leg_ab_mi + uber_round_trip(b["drive_miles"]) / 2
        opts.append({
            "mode": "uber", "cost": round(uber, 2), "minutes": round(drive_min + 12), "kind": "estimated",
            "detail": "three rides: home -> first store -> second store -> home", "basis": RATES["uber"]["basis"], "warning": None,
        })
    return opts


# ----------------------------------------------------------------------------------- prices
def _in_window(offer: dict, day: date) -> bool:
    if offer.get("source") != "weekly_sale":
        return True
    try:
        start = date.fromisoformat(offer["valid_from"]) if offer.get("valid_from") else None
        end = date.fromisoformat(offer["valid_to"]) if offer.get("valid_to") else None
    except ValueError:
        return False
    return (start is None or start <= day) and (end is None or day <= end)


def price_item_at_brand(item: dict, brand: str, choices: dict, offers_by_id: dict, day: date,
                        overrides: dict | None = None, included: set | None = None, forced: dict | None = None) -> dict:
    """The cost of one shopping-list item at one store brand on one day, with where the price came from.

    `choices[(item, brand)]` is every offer the Price Scout judged to be the right product. Here the
    cheapest one valid on that day is chosen (ties broken by offer id), unless the user picked another.
    The other matches are returned as `options` so the user can switch.
    If the user chose one specific version for this item (`forced`), only that product counts, and a
    store that does not sell it has no price for the item.
    """
    matches = choices.get((item["key"], brand)) or []
    if isinstance(matches, dict):  # one match given directly
        matches = [matches]
    forced_id = (forced or {}).get(item["key"])
    if forced_id:
        matches = [m for m in matches if m["offer_id"] == forced_id]
    lines = []
    for m in matches:
        offer = offers_by_id.get(m["offer_id"])
        if not offer or not _in_window(offer, day):
            continue
        # a variant the Price Scout was unsure about is used only after the user approves it
        if not forced_id and m.get("fit", "exact") != "exact" and (item["key"], m["offer_id"]) not in (included or set()):
            continue
        # how many to buy: worked out by code from the offer's size; the Price Scout's estimate only as a fallback
        conv = units_needed(item.get("qty"), item.get("qty_unit"), item.get("search_keyword", ""),
                            offer["name"], offer.get("unit_text", ""))
        units, units_by = (conv[0], "code") if conv else (max(m["units"], 0.0), "ai")
        lines.append({
            "units_by": units_by, "units_note": conv[1] if conv else "amount estimated by the Price Scout",
            "key": item["key"], "item": item["display"], "store_brand": brand, "price": offer["price"], "units": units,
            "cost": round(offer["price"] * units, 2), "product": offer["name"], "unit_text": offer.get("unit_text", ""),
            "source": offer["source"], "basis": offer["basis"], "offer_id": offer["offer_id"],
            "valid_to": offer.get("valid_to") or None,
        })
    if lines:
        # a product whose size the code could read beats one whose amount the AI had to guess; then cheapest
        lines.sort(key=lambda l: (l["units_by"] == "ai", l["cost"], l["offer_id"]))
        wanted = (overrides or {}).get((item["key"], brand))
        chosen = next((l for l in lines if l["offer_id"] == wanted), lines[0])
        options = [{k: l[k] for k in ("offer_id", "product", "price", "units", "cost", "unit_text", "source", "valid_to", "units_by")}
                   for l in lines]
        return {**chosen, "options": options, "user_choice": chosen["offer_id"] == wanted or bool(forced_id)}
    if forced_id:
        product = offers_by_id.get(forced_id, {}).get("name", "your chosen product")
        return {"key": item["key"], "item": item["display"], "store_brand": brand, "price": None, "units": 0, "cost": None,
                "product": None, "source": "missing", "offer_id": None, "options": [], "forced_elsewhere": True,
                "basis": f"You chose {product}, which is not sold here (or not on this day)."}
    ref = reference_price(item["ref_id"]) if item.get("ref_id") else None
    if ref and item.get("ref_units"):
        return {
            "key": item["key"], "item": item["display"], "store_brand": brand, "price": ref["price"], "units": item["ref_units"],
            "cost": round(ref["price"] * item["ref_units"], 2), "product": ref["name"], "unit_text": ref["unit_text"],
            "source": "estimated", "basis": ref["basis"], "offer_id": ref["offer_id"], "valid_to": None, "options": [],
        }
    return {"key": item["key"], "item": item["display"], "store_brand": brand, "price": None, "units": 0, "cost": None,
            "product": None, "source": "missing", "basis": "No price found for this item at this store", "offer_id": None,
            "options": []}


def priced_at_stores(lines_by_brand: dict, idx: int) -> list[tuple[str, dict]]:
    return [(b, lines[idx]) for b, lines in lines_by_brand.items() if lines[idx]["source"] in ("weekly_sale", "website_listed")]


def fill_from_other_stores(lines_by_brand: dict, items: list[dict], week_seen: dict | None = None,
                           forced: dict | None = None) -> None:
    """Most stores sell apples even when we have no apple price for them. When a store has no price of
    its own for an item, use the median of what the other New Haven stores charge for the same amount
    (labeled as an estimate): that day's prices, or, if every sale has ended that day, this week's.
    Only if no store has a price all week, fall back to the U.S. average; if that does not exist either,
    the item is marked "not compared" for every store and every day, so it never tilts the ranking.
    """
    for idx, item in enumerate(items):
        if item["key"] in (forced or {}):
            continue  # the user wants one specific product: stores without it stay without a price
        seen = priced_at_stores(lines_by_brand, idx) or (week_seen or {}).get(idx, [])
        if seen:
            costs = sorted(l["cost"] for _, l in seen)
            mid = len(costs) // 2
            median = costs[mid] if len(costs) % 2 else round((costs[mid - 1] + costs[mid]) / 2, 2)
            stores = ", ".join(sorted({b for b, _ in seen}))
            for b, lines in lines_by_brand.items():
                if lines[idx]["source"] in ("weekly_sale", "website_listed"):
                    continue
                lines[idx] = {
                    "key": item["key"], "item": item["display"], "store_brand": b, "price": median, "units": 1,
                    "cost": median, "product": f"{item['display']} (no {b} price on file)", "unit_text": "for the amount asked",
                    "source": "estimated", "offer_id": f"local-{item['key']}", "valid_to": None, "options": [],
                    "basis": (f"Estimated: median of this week's prices for the same amount at {len(seen)} other "
                              f"New Haven store(s) ({stores}). Sale prices can make this a little low."),
                    "units_by": "code",
                }
        elif all(lines[idx]["cost"] is None for lines in lines_by_brand.values()):
            for lines in lines_by_brand.values():
                lines[idx] = {**lines[idx], "not_compared": True,
                              "basis": "No price at any store: buy it wherever you go; it is left out of every total alike."}


# ----------------------------------------------------------------------------------- timing
def _hours_open(store: dict, day: date) -> tuple[int, int] | None:
    """Very small parser for OSM opening_hours like 'Mo-Sa 07:00-22:00; Su 07:00-21:00'."""
    spec = store.get("hours")
    if not spec:
        return (9, 20)  # unknown hours: assume a normal grocery day, flagged in the plan
    dows = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
    today = dows[day.weekday()]
    for part in spec.split(";"):
        part = part.strip()
        if " " in part:
            days, times = part.split(" ", 1)
        else:
            days, times = "Mo-Su", part
        span = days.split("-")
        if len(span) == 2:
            i, j = dows.index(span[0]), dows.index(span[1])
            covered = dows[i:j + 1]
        else:
            covered = [days]
        if today in covered:
            start, end = times.split("-")
            return int(start[:2]), (24 if end.startswith("24") else int(end[:2]))
    return None


def best_time(day_weather: dict, open_hours: tuple[int, int], mode: str, is_weekend: bool, earliest: int = 0) -> dict | None:
    """Pick the best start hour: dry, mild, not at the usual rush, inside store hours. None if no hour is left."""
    start, end = open_hours
    best = None
    for h in day_weather["hours"]:
        hour = h["hour"]
        if hour < max(start, 8, earliest) or hour > min(end, 21) - 1:
            continue
        rain = h["rain_pct"] or 0
        penalty = rain * (2.0 if mode == "walk" else 0.6)
        temp = h["temp_f"]
        if mode == "walk" and (temp < 35 or temp > 88):
            penalty += 40
        crowd = 0
        if is_weekend and 11 <= hour <= 16:
            crowd = 25
        elif not is_weekend and 17 <= hour <= 18:
            crowd = 20
        penalty += crowd
        if best is None or penalty < best["penalty"]:
            best = {"hour": hour, "penalty": penalty, "rain_pct": rain, "temp_f": temp, "busy": crowd > 0}
    return best


# ---------------------------------------------------------------------------------- ranking
def build_plans(items, choices, offers_by_id, routes, weather, prefs) -> dict:
    """Score every (day, store or store pair, transport) option and return the best ones."""
    brands = sorted({s["brand"] for s in STORES})
    nearest = {}
    for s in STORES:
        if prefs.get("exclude_membership") and s["membership"]:
            continue
        if prefs.get("allowed_brands") and s["brand"] not in prefs["allowed_brands"]:
            continue
        r = routes[s["id"]]
        if s["brand"] not in nearest or r["drive_miles"] < routes[nearest[s["brand"]]["id"]]["drive_miles"]:
            nearest[s["brand"]] = s
    # candidate stores: the nearest branch of every chain, plus branches the Yale grocery shuttle reaches
    cands = list(nearest.values())
    for s in STORES:
        if s in cands or (prefs.get("allowed_brands") and s["brand"] not in prefs["allowed_brands"]):
            continue
        if prefs.get("exclude_membership") and s["membership"]:
            continue
        near_stop = min(miles_between((st["lat"], st["lon"]), (s["lat"], s["lon"])) for st in SHUTTLE["store_stops"])
        if near_stop * 1.25 <= SHUTTLE["max_walk_from_store_stop_miles"]:
            cands.append(s)
    home = prefs["home"]
    total_weight = sum(i.get("weight_lb", 1.0) for i in items)
    carry_limit = prefs.get("carry_limit_lb", 15)
    heavy = total_weight > carry_limit
    now = datetime.now()
    start_day = now.date()
    horizon = max(1, min(int(prefs.get("days_ahead", 7)), 7))
    wx_by_date = {d["date"]: d for d in weather["days"]}
    hourly = float(prefs.get("zipcar_hourly") or RATES["zipcar"]["hourly_usd"])

    candidates = []
    # pass 1: each store's own prices for every day; also what any store charged at any time this week
    day_lines, week_seen = {}, {}
    for offset in range(horizon):
        day = start_day + timedelta(days=offset)
        lines_by_brand = {b: [price_item_at_brand(it, b, choices, offers_by_id, day, prefs.get("overrides"), prefs.get("included"), prefs.get("forced")) for it in items]
                          for b in {s["brand"] for s in cands}}
        day_lines[offset] = lines_by_brand
        for idx in range(len(items)):
            for b, line in priced_at_stores(lines_by_brand, idx):
                week_seen.setdefault(idx, {})[b] = line  # one price per store (the latest day wins)
    week_seen = {idx: list(v.items()) for idx, v in week_seen.items()}
    # pass 2: fill the gaps the same way for every day, then score every option
    for offset in range(horizon):
        day = start_day + timedelta(days=offset)
        wx = wx_by_date.get(day.isoformat())
        if not wx:
            continue
        lines_by_brand = day_lines[offset]
        fill_from_other_stores(lines_by_brand, items, week_seen, prefs.get("forced"))
        groups = [[s] for s in cands]
        # two-store trips at two different chains (e.g. ALDI + Trader Joe's)
        groups += [[a, b] for i, a in enumerate(cands) for b in cands[i + 1:] if a["brand"] != b["brand"]]
        for stores in groups:
            group = [s["brand"] for s in stores]
            hours = [_hours_open(s, day) for s in stores]
            if any(h is None for h in hours):
                continue
            lines = []
            for idx in range(len(items)):
                options = [lines_by_brand[b][idx] for b in group if lines_by_brand[b][idx]["cost"] is not None]
                # a price seen at the store beats a national estimate; then the cheaper one wins
                options.sort(key=lambda l: (l["source"] == "estimated", l["cost"]))
                lines.append(options[0] if options else lines_by_brand[group[0]][idx])
            if any(l.get("forced_elsewhere") for l in lines):
                continue  # the user chose a product none of these stores sells (that day): not a valid trip
            if len(group) == 2 and len({l["store_brand"] for l in lines if l["cost"] is not None}) < 2:
                continue  # a "two-store" trip that buys everything at one store is just a one-store trip
            basket = round(sum(l["cost"] for l in lines if l["cost"] is not None), 2)
            # items priced at no store at all are left out of every plan alike, so they never tilt the ranking
            missing = [l["item"] for l in lines if l["cost"] is None and not l.get("not_compared")]
            not_compared = [l["item"] for l in lines if l.get("not_compared")]
            store_priced = sum(1 for l in lines if l["source"] in ("weekly_sale", "website_listed"))
            for t in transport_options(stores, routes, heavy, hourly, home, day):
                if prefs.get("modes") and t["mode"] not in prefs["modes"]:
                    continue
                if t["mode"] == "walk" and total_weight > carry_limit * 2:
                    continue  # nobody should carry twice their limit home on foot
                open_span = (max(h[0] for h in hours), min(h[1] for h in hours))
                if t["mode"] == "shuttle":  # start early enough to be home before the shuttle stops (about 5pm)
                    trip_hours = math.ceil((t["minutes"] + RATES["shopping_minutes_per_store"] * len(stores)) / 60)
                    open_span = (max(open_span[0], t["shuttle_hours"][0]),
                                 min(open_span[1], t["shuttle_hours"][1] - trip_hours + 1))
                earliest = now.hour + 1 if offset == 0 else 0
                timing = best_time(wx, open_span, t["mode"], day.weekday() >= 5, earliest)
                if timing is None:
                    continue  # no good hour left on this day
                travel_minutes = round(t["minutes"])
                shop_minutes = RATES["shopping_minutes_per_store"] * len(stores)
                minutes = travel_minutes + shop_minutes
                # the user's time, priced at what they said an hour is worth (travel + shopping)
                time_value = round(prefs.get("time_value_per_hour", 0) * minutes / 60, 2)
                estimated = sum(1 for l in lines if l["source"] == "estimated")
                # prefer plans built on prices seen at the store over plans built on national averages
                score = (basket + t["cost"] + time_value + timing["penalty"] * 0.05 + len(missing) * 25
                         + estimated * ESTIMATE_PENALTY)
                if t["mode"] == "walk" and heavy:
                    score += 10
                candidates.append({
                    "day": day.isoformat(),
                    "day_label": f"{DAY_NAMES[day.weekday()]} {day.month}/{day.day}",
                    "stores": [{"id": s["id"], "brand": s["brand"], "address": s["address"], "lat": s["lat"], "lon": s["lon"],
                                "membership": s["membership"], "hours": s["hours"],
                                "drive_miles": routes[s["id"]]["drive_miles"]} for s in stores],
                    "transport": t,
                    "start_hour": timing["hour"],
                    "weather": {"summary": wx["summary"], "rain_pct": timing["rain_pct"], "temp_f": timing["temp_f"],
                                "day_rain_pct": wx["rain_pct"], "high_f": wx["high_f"]},
                    "busy_at_start": timing["busy"],
                    "basket": basket,
                    "transport_cost": t["cost"],
                    "total": round(basket + t["cost"], 2),
                    "total_minutes": round(minutes),
                    "travel_minutes": travel_minutes,
                    "shop_minutes": shop_minutes,
                    "time_cost": time_value,
                    "total_with_time": round(basket + t["cost"] + time_value, 2),
                    "lines": lines,
                    "missing": missing,
                    "not_compared": not_compared,
                    "store_priced_items": store_priced,
                    "score": round(score, 3),
                })
    candidates.sort(key=lambda c: c["score"])
    # keep variety: the best plan per (stores, mode), then the top few
    seen, ranked = set(), []
    for c in candidates:
        key = (tuple(s["brand"] for s in c["stores"]), c["transport"]["mode"])
        if key in seen:
            continue
        seen.add(key)
        ranked.append(c)
    for i, c in enumerate(ranked):
        c["plan_id"] = f"P{i + 1}"
    best = ranked[0] if ranked else None
    nearest_store = min(nearest.values(), key=lambda s: routes[s["id"]]["drive_miles"]) if nearest else None
    baseline = None
    if nearest_store:
        baseline = next((c for c in ranked if [s["id"] for s in c["stores"]] == [nearest_store["id"]]
                         and c["transport"]["mode"] in ("walk", "uber")), None)
    sale_deadlines = sorted({(l["item"], l["store_brand"], l["valid_to"]) for c in ranked[:5] for l in c["lines"] if l.get("valid_to")},
                            key=lambda x: x[2])
    return {
        "best": best,
        "alternatives": ranked[1:8],
        "baseline": baseline,
        "heavy_bags": heavy,
        "sale_deadlines": [{"item": a, "store_brand": b, "valid_to": c} for a, b, c in sale_deadlines][:8],
        "busy_note": BUSY_NOTE,
        "generated_at": datetime.now().isoformat(timespec="minutes"),
        "brands_considered": sorted(nearest),
        "all_brands": brands,
    }
