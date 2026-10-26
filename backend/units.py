"""Unit conversion done by code, so every store is compared on the same amount, the same way, every time.

The List Reader turns "2 dozen eggs" into qty=2, unit="dozen". Here we read each offer's size
("12 ct", "1 gal", "20 oz, 2 count", "per lb", "3 x 1.33 lb") and work out how many of that offer
a shopper must buy: whole packages are rounded up, things sold by the pound can be fractional.
If a size cannot be read, the caller falls back to the Price Scout's own estimate and labels it.
"""

import math
import re

# request units -> (dimension, factor to the base unit). Base units: lb, fl oz, count, package.
REQUEST_UNITS = {
    "lb": ("weight", 1.0), "oz": ("weight", 1 / 16), "kg": ("weight", 2.20462), "g": ("weight", 0.00220462),
    "gal": ("volume", 128.0), "half_gal": ("volume", 64.0), "qt": ("volume", 32.0), "fl_oz": ("volume", 1.0),
    "l": ("volume", 33.814), "ml": ("volume", 0.033814),
    "ct": ("count", 1.0), "dozen": ("count", 12.0), "bunch": ("count", 5.0),
    "pack": ("package", 1.0),
}
UNIT_LABELS = {"lb": "lb", "oz": "oz", "kg": "kg", "g": "g", "gal": "gal", "half_gal": "half gal", "qt": "qt",
               "fl_oz": "fl oz", "l": "L", "ml": "mL", "ct": "", "dozen": "dozen", "bunch": "bunch", "pack": "pack"}
STEP = {"lb": 0.5, "oz": 4, "kg": 0.5, "g": 100, "gal": 0.5, "half_gal": 1, "qt": 1, "fl_oz": 8, "l": 0.5, "ml": 250,
        "ct": 1, "dozen": 1, "bunch": 1, "pack": 1}

# Typical weight of one piece of produce, used to compare "6 bananas" with "per lb" prices.
# Banana: ALDI's own listing says "About 0.33 lb each". Potato and tomato are typical medium sizes.
EACH_LB = {"banana": 0.33, "potato": 0.5, "tomato": 0.33, "apple": 0.4, "onion": 0.5, "orange": 0.5}
EACH_LB_BASIS = "piece weights: banana 0.33 lb (ALDI listing), medium potato 0.5 lb, tomato 0.33 lb (typical sizes)"
BUNCH_COUNT = 5  # Stop & Shop lists a banana bunch as "apx 4-6 ct"

_NUM = r"(\d+(?:\.\d+)?|\d+/\d+)"


def _num(s: str) -> float:
    if "/" in s:
        a, b = s.split("/")
        return float(a) / float(b)
    return float(s)


def base_amount(qty: float, unit: str) -> tuple[str, float] | None:
    if unit not in REQUEST_UNITS or qty is None or qty <= 0:
        return None
    dim, f = REQUEST_UNITS[unit]
    return dim, qty * f


def parse_offer(name: str, unit_text: str) -> dict | None:
    """Read an offer's selling unit. Returns {"per_lb": True} or {"weight"/"volume"/"count": total, "packs": n}."""
    text = f"{unit_text or ''} || {name or ''}".lower().replace("lbs", "lb").replace("pounds", "lb").replace("pound", "lb")
    text = text.replace("half gallon", "0.5 gal").replace("half-gallon", "0.5 gal").replace("1/2 gal", "0.5 gal")
    ut = (unit_text or "").lower().strip()
    # Sold by weight: our own "per lb (...)" or flyer texts like "Per Lb.", "/LB.", "lb". A package price that
    # merely shows its unit price ("each (8 oz pkg), $4.38 /lb") is NOT sold by weight.
    if not ut.startswith("each") and (ut.startswith("per lb") or re.search(r"(^|per\s*|/\s*|\s)lb\.?$", ut)
                                      or re.search(r"(per\s*|/\s*)lb\b", ut)):
        return {"per_lb": True}
    out: dict = {}
    # "8 x 1 lb", "3 x 1.33 lb", "2 x 1 gal"
    m = re.search(_NUM + r"\s*x\s*" + _NUM + r"\s*(lb|oz|fl oz|gal)", text)
    if m:
        n, size, u = _num(m.group(1)), _num(m.group(2)), m.group(3)
        out["packs"] = n
        _add_size(out, size * n, u)
    if not out:
        m = re.search(_NUM + r"\s*(fl\.? ?oz|oz|lb|gal(?:lon)?|qt|ml|l\b|kg|g\b)", text)
        if m:
            _add_size(out, _num(m.group(1)), m.group(2))
        mult = re.search(r"(\d+)[ -]?(?:count|pack|pk)\b", text)
        if out and mult and not re.search(r"dozen", text):
            n = int(mult.group(1))
            if n > 1 and "count" not in out:
                for k in ("weight", "volume"):
                    if k in out:
                        out[k] *= n
                out["packs"] = n
    # counts: "5 dozen-count", "1 doz", "12 ct", "24-count", "1 bunch", "1 ea"
    m = re.search(_NUM + r"\s*(?:dozen|doz)\b", text)
    if m:
        out = {"count": _num(m.group(1)) * 12}
    elif re.search(r"\b1 bunch\b", text):
        out = {"count": BUNCH_COUNT}
    elif re.search(r"\b1 ea\b", text) or ut.strip() in ("each", "each (1 each)"):
        out = out or {"count": 1}
    else:
        m = re.search(r"(\d+)[\s-]*(?:ct|count)\b", text)
        if m and "weight" not in out and "volume" not in out:
            out = {"count": float(m.group(1))}
    return out or None


def parse_count(name: str, unit_text: str) -> float | None:
    """A piece count anywhere in the offer text ("12 count", "24-count", "1 dozen"), for things bought by count."""
    text = f"{unit_text or ''} {name or ''}".lower()
    m = re.search(_NUM + r"\s*(?:dozen|doz)\b", text)
    if m:
        return _num(m.group(1)) * 12
    m = re.search(r"(\d+)[\s-]*(?:ct|count)\b", text)
    return float(m.group(1)) if m else None


def _add_size(out: dict, size: float, u: str) -> None:
    u = u.replace(".", "").strip()
    if u in ("lb",):
        out["weight"] = size
    elif u == "oz":
        out["weight"] = size / 16
        out["oz_maybe_fluid"] = size  # "64 oz" milk is fluid ounces
    elif u in ("fl oz", "floz"):
        out["volume"] = size
    elif u.startswith("gal"):
        out["volume"] = size * 128
    elif u == "qt":
        out["volume"] = size * 32
    elif u == "ml":
        out["volume"] = size * 0.033814
    elif u == "l":
        out["volume"] = size * 33.814
    elif u == "kg":
        out["weight"] = size * 2.20462
    elif u == "g":
        out["weight"] = size * 0.00220462


def _piece_lb(item_name: str) -> float | None:
    words = re.findall(r"[a-z]+", (item_name or "").lower())
    for w in words:
        w = w[:-2] if w.endswith("es") and w[:-2] in EACH_LB else w[:-1] if w.endswith("s") else w
        if w in EACH_LB:
            return EACH_LB[w]
    return None


def units_needed(qty: float, unit: str, item_name: str, offer_name: str, offer_unit_text: str) -> tuple[float, str] | None:
    """How many of this offer to buy for the requested amount, and a short note on how it was worked out."""
    want = base_amount(qty, unit)
    size = parse_offer(offer_name, offer_unit_text)
    if not want or not size:
        return None
    dim, amount = want
    piece = _piece_lb(item_name)

    def packs(need: float, per: float) -> float:
        return float(max(1, math.ceil(need / per - 1e-9)))

    if size.get("per_lb"):
        if dim == "weight":
            return round(amount, 2), f"{amount:g} lb at the per-lb price"
        if dim == "count" and piece:
            lb = amount * piece
            return round(lb, 2), f"{amount:g} pieces ≈ {lb:.2f} lb ({EACH_LB_BASIS})"
        return None
    if dim == "weight":
        if "weight" in size:
            return packs(amount, size["weight"]), f"need {amount:g} lb, pack holds {size['weight']:g} lb"
        if "count" in size and piece:
            pieces = amount / piece
            return packs(pieces, size["count"]), f"{amount:g} lb ≈ {pieces:.0f} pieces ({EACH_LB_BASIS})"
    if dim == "volume":
        vol = size.get("volume") or size.get("oz_maybe_fluid")
        if vol:
            return packs(amount, vol), f"need {amount:g} fl oz, pack holds {vol:g} fl oz"
    if dim == "count":
        count = size.get("count") or (None if piece else parse_count(offer_name, offer_unit_text))
        if count:
            return packs(amount, count), f"need {amount:g}, pack has {count:g}"
        if "weight" in size and piece:
            lb = amount * piece
            return packs(lb, size["weight"]), f"{amount:g} pieces ≈ {lb:.2f} lb, pack holds {size['weight']:g} lb"
    if dim == "package":
        return packs(amount, size.get("packs", 1)), f"{amount:g} package(s)"
    return None


REF_UNITS = {"dozen": ("count", 12.0), "gallon": ("volume", 128.0), "half gallon": ("volume", 64.0),
             "lb": ("weight", 1.0), "2 liter bottle": ("volume", 67.628)}


def ref_units_for(qty: float, unit: str, ref_unit: str, keyword: str) -> float | None:
    """The requested amount expressed in a reference price's unit (e.g. 18 ct eggs = 1.5 dozen)."""
    want, ref = base_amount(qty, unit), REF_UNITS.get(ref_unit)
    if not want or not ref:
        return None
    (dim, amount), (rdim, factor) = want, ref
    if dim == rdim:
        return round(amount / factor, 3)
    piece = _piece_lb(keyword)
    if dim == "count" and rdim == "weight" and piece:
        return round(amount * piece, 3)
    if dim == "weight" and rdim == "count" and piece:
        return round(amount / piece / factor, 3)
    return None


def describe(qty: float, unit: str, name: str) -> str:
    """'2 dozen eggs', '3 lb chicken breast', '6 bananas'."""
    label = UNIT_LABELS.get(unit, unit)
    return f"{qty:g} {label} {name}".replace("  ", " ").strip()


# Variants a shopper may not want unless they asked for them. Checked by code, so the same product is
# always treated the same way. Brand, organic, cage-free, pasture-raised, pack size and cut thickness
# are NOT variants: the cheapest ordinary match wins and the user can still switch products.
VARIANT_TERMS = {
    "lactose-free": ["lactose free", "lactose-free", "lactaid"],
    "A2 milk": ["a2/a2", "a2 milk"],
    "plant-based": ["oat milk", "almond", "soy milk", "coconut milk", "plant based", "plant-based"],
    "flavored": ["chocolate", "vanilla", "strawberry", "flavored", "honey wheat", "cinnamon", "maple", "pumpkin spice",
                 "hazelnut", "mocha"],
    "instant / precooked": ["instant", "minute ", "microwav", "ready to", "fully cooked", "precooked", "pre-cooked"],
    "single-serve cups": [" cups", " cup "],
    "seasoned / prepared": ["seasoned", "marinated", "breaded", "rotisserie", "grilled", "pesto", "teriyaki"],
    "bone-in / skin-on": ["bone-in", "bone in", "split", "skin-on", "with rib meat", "w/rib"],
    "decaf": ["decaf"],
    "turkey / other meat": ["turkey bacon", "turkey"],
}


def variant_reason(product_name: str, request: str) -> str | None:
    """Why this product may not be what was asked for, or None if it is the ordinary product."""
    name, req = f" {product_name.lower()} ", f" {request.lower()} "
    for reason, terms in VARIANT_TERMS.items():
        if any(t.strip() in req for t in terms):
            continue  # the user asked for this kind (e.g. "lactose free milk")
        if any(t in name for t in terms):
            return reason
    return None
