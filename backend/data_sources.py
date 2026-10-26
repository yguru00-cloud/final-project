"""Live data with a saved fallback: weekly sales, base prices, address lookup, routes, and weather.

Every price returned here carries a `source` ("weekly_sale" or "website_listed") and a `basis`
(where it came from and when), so nothing downstream ever has to guess where a number came from.
When a live service fails, the last saved copy in data/cache/ is used and marked `stale`.
"""

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from backend.config import CACHE_DIR, DATA_DIR, HTTP_TIMEOUT, USE_LIVE_DATA

UA = {"User-Agent": "NewHavenGroceryPlanner/1.0 (Yale student project)"}
NEW_HAVEN = (41.3083, -72.9279)
SERVICE_RADIUS_MILES = 20

FLIPP_SEARCH = "https://backflipp.wishabi.com/flipp/items/search"
POSTAL_CODE = "06511"


def _load_json(name: str) -> Any:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


STORES: list[dict] = _load_json("stores.json")["stores"]
RATES: dict = _load_json("transport_rates.json")
SHUTTLE: dict = _load_json("shuttle.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cache_path(kind: str, key: str):
    safe = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:40] or "x"
    digest = hashlib.sha1(key.encode()).hexdigest()[:8]
    folder = CACHE_DIR / kind
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{safe}-{digest}.json"


def _cached_get(kind: str, key: str, fetch) -> tuple[Any, dict]:
    """Try the live service; save what it returns; fall back to the saved copy if it fails."""
    path = _cache_path(kind, key)
    if USE_LIVE_DATA:
        try:
            data = fetch()
            path.write_text(json.dumps({"fetched_at": _now(), "data": data}, ensure_ascii=False), encoding="utf-8")
            return data, {"live": True, "fetched_at": _now()}
        except Exception as exc:  # network down, service changed, rate limited...
            error = f"{type(exc).__name__}: {exc}"[:200]
    else:
        error = "live data switched off (USE_LIVE_DATA=0)"
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        return saved["data"], {"live": False, "stale": True, "fetched_at": saved["fetched_at"], "error": error}
    raise RuntimeError(f"{kind} is unavailable and there is no saved copy ({error})")


def miles_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


# ----------------------------------------------------------------------------- weekly sales
def _merchant_key(name: str | None) -> str:
    return (name or "").strip().lower()


FLIPP_MERCHANTS = {_merchant_key(s["flipp_merchant"]): s["brand"] for s in STORES if s["flipp_merchant"]}


def search_weekly_sales(keyword: str) -> dict:
    """This week's flyer deals for one keyword at the stores this app covers (New Haven, 06511)."""
    keyword = keyword.strip()[:40]

    def fetch():
        r = httpx.get(
            FLIPP_SEARCH,
            params={"locale": "en-us", "postal_code": POSTAL_CODE, "q": keyword},
            headers=UA,
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("items", [])

    items, meta = _cached_get("sales", keyword, fetch)
    offers = []
    for it in items:
        brand = FLIPP_MERCHANTS.get(_merchant_key(it.get("merchant_name")))
        price = it.get("current_price")
        if not brand or price in (None, "", 0):
            continue
        unit = " ".join(x for x in [it.get("pre_price_text"), it.get("post_price_text")] if x).strip()
        price = float(price)
        multi = re.fullmatch(r"\s*(\d+)\s*(?:for|/)\s*", it.get("pre_price_text") or "")
        if multi and int(multi.group(1)) > 1:  # "2 for $5" is $2.50 each
            n = int(multi.group(1))
            unit = f"each ({n} for ${price:g})"
            price = round(price / n, 2)
        offers.append({
            "offer_id": f"flipp-{it.get('id')}",
            "store_brand": brand,
            "name": (it.get("name") or "")[:120],
            "price": price,
            "unit_text": unit[:60],
            "sale_story": (it.get("sale_story") or "")[:80],
            "valid_from": (it.get("valid_from") or "")[:10],
            "valid_to": (it.get("valid_to") or "")[:10],
            "source": "weekly_sale",
            "basis": f"{brand} weekly flyer (via Flipp), valid {(it.get('valid_from') or '')[:10]} to {(it.get('valid_to') or '')[:10]}",
        })
    return {"keyword": keyword, "offers": offers[:30], "data": meta}


# ------------------------------------------------------------------------------ base prices
def _base_prices() -> dict:
    path = DATA_DIR / "base_prices.json"
    if not path.exists():
        return {"items": []}
    return json.loads(path.read_text(encoding="utf-8"))


def search_base_prices(keyword: str) -> dict:
    """Everyday prices read from store websites (Trader Joe's, Costco, ALDI, Stop & Shop), saved with the date."""
    def singular(w: str) -> str:
        return w[:-3] + "o" if w.endswith("oes") else w[:-1] if w.endswith("s") and not w.endswith("ss") else w

    words = [singular(w) for w in re.findall(r"[a-z]+", keyword.lower()) if len(w) > 2]
    data = _base_prices()
    hits, per_store = [], {}
    for it in data.get("items", []):
        tags = {singular(t) for t in re.findall(r"[a-z]+", " ".join(it.get("tags", [])).lower())}
        tokens = tags | {singular(t) for t in re.findall(r"[a-z]+", it["name"].lower())}
        # every word must appear, and at least one must be what the product IS (its tag), so "apples"
        # does not find "Apple Smoked Bacon" while "whole milk" still finds "Whole Milk"
        if words and all(w in tokens for w in words) and any(w in tags for w in words):
            if per_store.get(it["store_brand"], 0) >= 4:
                continue
            per_store[it["store_brand"]] = per_store.get(it["store_brand"], 0) + 1
            hits.append({
                "offer_id": it["id"],
                "store_brand": it["store_brand"],
                "name": it["name"],
                "price": it["price"],
                "unit_text": it.get("unit_text", ""),
                "valid_from": "",
                "valid_to": "",
                "source": "website_listed",
                "basis": it.get("basis") or f"{it['store_brand']} website price, checked {it['checked']}. In-store prices may differ.",
                "url": it.get("url", ""),
            })
    return {"keyword": keyword, "offers": hits[:20]}


def coverage() -> list[dict]:
    """Staples with a website price at several stores, i.e. the items the planner compares best."""
    by_item: dict[str, set] = {}
    for it in _base_prices().get("items", []):
        key = it["id"].split("-")[2] if it["id"].count("-") >= 3 else it["tags"][0]
        by_item.setdefault(key, set()).add(it["store_brand"])
    stores = len({b for s in by_item.values() for b in s})
    return sorted(({"item": k.replace("_", " "), "stores": len(v), "of": stores} for k, v in by_item.items()),
                  key=lambda x: (-x["stores"], x["item"]))


REFERENCE: dict = _load_json("reference_prices.json")


def reference_price(ref_id: str) -> dict | None:
    """The national average price for one staple (BLS). Never store-specific: always 'estimated'."""
    for it in REFERENCE["items"]:
        if it["id"] == ref_id:
            return {
                "offer_id": it["id"],
                "name": it["name"],
                "price": it["price"],
                "unit_text": f"per {it['unit']}",
                "source": "estimated",
                "basis": f"Estimated: U.S. average price of {it['bls_name'].lower()} ({it['period']}, Bureau of Labor Statistics), not this store's price",
            }
    return None


def reference_catalog() -> list[dict]:
    return [{"ref_id": it["id"], "name": it["name"], "unit": it["unit"]} for it in REFERENCE["items"]]


# ------------------------------------------------------------------------------ geography
def geocode(address: str) -> dict:
    """Turn a street address into coordinates (US Census first, OpenStreetMap as a backup)."""
    address = address.strip()[:200]
    # A bare street address is assumed to be in New Haven; anything with a town or state is used as typed.
    query = address if "," in address or re.search(r"\b(ct|connecticut)\b", address, re.I) else f"{address}, New Haven, CT"

    def fetch():
        try:
            r = httpx.get(
                "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
                params={"address": query, "benchmark": "Public_AR_Current", "format": "json"},
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            matches = r.json()["result"]["addressMatches"]
            if matches:
                m = matches[0]
                return {"lat": m["coordinates"]["y"], "lon": m["coordinates"]["x"], "label": m["matchedAddress"]}
        except Exception:
            pass
        r = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "us"},
            headers=UA,
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        hits = r.json()
        if not hits:
            return None
        return {"lat": float(hits[0]["lat"]), "lon": float(hits[0]["lon"]), "label": hits[0]["display_name"]}

    place, meta = _cached_get("geocode", query, fetch)
    if not place:
        return {"ok": False, "error": "We could not find that address. Try a street address like '165 Whitney Ave'."}
    miles = miles_between(NEW_HAVEN, (place["lat"], place["lon"]))
    if miles > SERVICE_RADIUS_MILES:
        return {"ok": False, "error": f"That address is {miles:.0f} miles from New Haven. This planner only covers the New Haven area."}
    return {"ok": True, **place, "data": meta}


def routes_from(home: tuple[float, float]) -> dict:
    """Driving distance and time from home to every store (OSRM), with straight-line backup."""
    coords = ";".join([f"{home[1]},{home[0]}"] + [f"{s['lon']},{s['lat']}" for s in STORES])
    key = f"{home[0]:.4f},{home[1]:.4f}"

    def fetch():
        r = httpx.get(
            f"https://router.project-osrm.org/table/v1/driving/{coords}",
            params={"sources": "0", "annotations": "distance,duration"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("code") != "Ok":
            raise RuntimeError(d.get("code"))
        return {"distance_m": d["distances"][0][1:], "duration_s": d["durations"][0][1:]}

    try:
        table, meta = _cached_get("routes", key, fetch)
        basis = "Driving distance from OSRM (OpenStreetMap routing); time = OSRM time x 1.4 + 3 min for traffic and parking"
    except RuntimeError:
        table, meta = None, {"live": False, "error": "routing unavailable"}
        basis = "Estimated: straight-line distance x 1.3, at 22 mph"
    out = {}
    for i, s in enumerate(STORES):
        crow = miles_between(home, (s["lat"], s["lon"]))
        if table and table["distance_m"][i] is not None:
            drive_mi = table["distance_m"][i] / 1609.34
            # OSRM assumes empty roads; city traffic, lights and parking add roughly 40%
            drive_min = table["duration_s"][i] / 60 * 1.4 + 3
        else:
            drive_mi = crow * 1.3
            drive_min = drive_mi / 22 * 60
        out[s["id"]] = {
            "drive_miles": round(drive_mi, 2),
            "drive_minutes": round(drive_min, 1),
            # walking is shorter than driving on one-way streets; straight line x 1.25 is a fair guess
            "walk_miles": round(crow * 1.25, 2),
        }
    return {"routes": out, "basis": basis, "data": meta}


# ---------------------------------------------------------------------------------- weather
def weather_week(lat: float = NEW_HAVEN[0], lon: float = NEW_HAVEN[1]) -> dict:
    """Seven-day forecast for New Haven: daily summary plus hourly rain chance and temperature."""

    def fetch():
        r = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": round(lat, 3),
                "longitude": round(lon, 3),
                "hourly": "temperature_2m,precipitation_probability,precipitation,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "precipitation_unit": "inch",
                "timezone": "America/New_York",
                "forecast_days": 7,
            },
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()

    d, meta = _cached_get("weather", f"{lat:.2f},{lon:.2f}", fetch)
    days = []
    for i, day in enumerate(d["daily"]["time"]):
        hours = []
        for j, t in enumerate(d["hourly"]["time"]):
            if t.startswith(day):
                hours.append({
                    "hour": int(t[11:13]),
                    "temp_f": d["hourly"]["temperature_2m"][j],
                    "rain_pct": d["hourly"]["precipitation_probability"][j],
                    "rain_in": d["hourly"]["precipitation"][j],
                    "wind_mph": d["hourly"]["wind_speed_10m"][j],
                })
        days.append({
            "date": day,
            "code": d["daily"]["weather_code"][i],
            "summary": WEATHER_CODES.get(d["daily"]["weather_code"][i], "Mixed"),
            "high_f": d["daily"]["temperature_2m_max"][i],
            "low_f": d["daily"]["temperature_2m_min"][i],
            "rain_pct": d["daily"]["precipitation_probability_max"][i],
            "rain_in": d["daily"]["precipitation_sum"][i],
            "hours": hours,
        })
    return {"days": days, "basis": "Open-Meteo forecast for New Haven", "data": meta}


WEATHER_CODES = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog", 48: "Fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Rain showers", 81: "Rain showers", 82: "Heavy showers", 85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm",
}
