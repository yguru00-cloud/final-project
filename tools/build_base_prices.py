"""Build data/base_prices.json from the prices read off store-website screenshots.

The screenshots (price_pages/*.png, not in the repo) were taken by hand on 2026-10-25 from
traderjoes.com, costco.com / sameday.costco.com, aldi.us (store: 2300 Dixwell Ave, Hamden) and
stopandshop.com (store: 150 Whalley Ave, New Haven). Each screenshot was read into
price_pages/extracted_<store>.json; this script only reshapes those records. It never changes a price.

Run:  python tools/build_base_prices.py
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGES = ROOT / "price_pages"
CHECKED_ON = {"ShopRite": "2026-10-26", "Walmart": "2026-10-26", "Target": "2026-10-26", "BJ's Wholesale Club": "2026-10-26"}
DEFAULT_CHECKED = "2026-10-25"

SITES = {
    "Trader Joe's": ("traderjoes.com", "Trader Joe's website price (national list; may vary by store)"),
    "ALDI": ("aldi.us", "ALDI website price for the 2300 Dixwell Ave, Hamden store"),
    "Stop & Shop": ("stopandshop.com", "Stop & Shop website price for the 150 Whalley Ave, New Haven store"),
    "ShopRite": ("shoprite.com", "ShopRite website price for the 2100 Dixwell Ave, Hamden store"),
    "Walmart": ("walmart.com", "Walmart website price for the 2300 Dixwell Ave, Hamden store"),
    "Target": ("target.com", "Target website price for the North Haven store"),
    "BJ's Wholesale Club": ("bjs.com", "BJ's website price for the North Haven club (membership required)"),
}
TAGS = {
    "eggs": ["eggs", "egg"], "milk": ["milk"], "chicken_breast": ["chicken", "breast", "chicken breast"],
    "ground_beef": ["ground", "beef", "ground beef"], "bacon": ["bacon"], "bread": ["bread"], "butter": ["butter"],
    "cheddar_cheese": ["cheddar", "cheese"], "rice": ["rice"], "pasta": ["pasta", "spaghetti", "penne"],
    "bananas": ["bananas", "banana"], "potatoes": ["potatoes", "potato"], "tomatoes": ["tomatoes", "tomato"],
    "coffee": ["coffee"], "yogurt": ["yogurt"],
}


def unit_text(rec: dict) -> str:
    size = (rec.get("size_text") or "").strip()
    if rec.get("price_basis") == "per_lb":
        return "per lb" + (f" ({size})" if size else "")
    shown = rec.get("unit_price_shown") or ""
    return "each" + (f" ({size})" if size else "") + (f", {shown}" if shown else "")


def main() -> None:
    items, skipped = [], []
    for path in sorted(PAGES.glob("extracted_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        brand = data["store_brand"]
        CHECKED = CHECKED_ON.get(brand, DEFAULT_CHECKED)
        skipped += [{"store": brand, **s} for s in data.get("skipped", [])]
        for n, rec in enumerate(data["items"], 1):
            if rec.get("price") in (None, "") or float(rec["price"]) <= 0:
                continue
            key = re.sub(r"[^a-z_]", "", rec["item_key"].lower().replace(" ", "_"))
            if key == "chicken_breast" and "split" in rec["name"].lower():
                continue  # bone-in split breasts are not the boneless staple
            # Variable-weight packs ("apx 1.5 lb", "About 8.4 lb / package") show an estimated package
            # price plus a per-lb price. Keep the per-lb price exactly as shown on the screen.
            size = (rec.get("size_text") or "").lower()
            per_lb = re.search(r"\$\s?(\d+(?:\.\d{1,2})?)\s*/\s*lb", rec.get("unit_price_shown") or "", re.I)
            if per_lb and (size.startswith("apx") or size.startswith("about")):
                rec = {**rec, "price": float(per_lb.group(1)), "price_basis": "per_lb",
                       "notes": f"{rec.get('notes') or ''} Screen showed est. package ${rec['price']} for {rec['size_text']}.".strip()}
            if brand == "Costco":
                same_day = rec.get("channel") == "sameday"
                site = "sameday.costco.com" if same_day else "costco.com"
                basis = (f"Costco Same-Day (Instacart) price, checked {CHECKED}. Warehouse prices are usually lower."
                         if same_day else f"Costco website price, checked {CHECKED}. Warehouse prices may differ.")
            else:
                site, label = SITES[brand]
                basis = f"{label}, checked {CHECKED}."
            items.append({
                "id": f"web-{re.sub(r'[^a-z]', '', brand.lower())}-{key}-{n}",
                "store_brand": brand,
                "name": rec["name"],
                "price": float(rec["price"]),
                "unit_text": unit_text(rec),
                "tags": TAGS.get(key, key.split("_")),
                "checked": CHECKED,
                "site": site,
                "basis": basis,
                "screenshot": rec.get("file"),
                "notes": rec.get("notes") or "",
                "regular_price": rec.get("regular_price"),
            })
    out = {
        "source": "Prices read by hand from store-website screenshots on 2026-10-25 and 2026-10-26 (screenshots kept outside the repo).",
        "items": items,
        "skipped": skipped,
    }
    (ROOT / "data" / "base_prices.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    by_store = {}
    for it in items:
        by_store[it["store_brand"]] = by_store.get(it["store_brand"], 0) + 1
    print("prices per store:", by_store, "| skipped screenshots:", len(skipped))


if __name__ == "__main__":
    main()
