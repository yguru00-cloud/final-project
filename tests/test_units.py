"""Offline tests for the unit conversion (no key, no internet).  Run:  python tests/test_units.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.units import ref_units_for, units_needed  # noqa: E402

CASES = [
    # (wanted qty, unit, item, offer name, offer unit text, expected units to buy)
    (2, "dozen", "eggs", "Goldhen Large Grade A Eggs - 1 Dozen", "each (12 ct)", 2),
    (2, "dozen", "eggs", "Kirkland Signature Eggs, Large, 24-count", "each (24-count)", 1),
    (60, "ct", "eggs", "Kirkland Signature Eggs, Large, 5 dozen-count", "each (5 dozen-count)", 1),
    (1, "gal", "milk", "Stop & Shop Vitamin D Whole Milk", "each (1/2 gallon), $0.05 /oz", 2),
    (1, "gal", "milk", "Organic Milk A2/A2", "each (64 Fl Oz)", 2),
    (3, "lb", "chicken breast", "Boneless Skinless Chicken Breasts Family Pack", "per lb (About 5.0 lb / package)", 3),
    (3, "lb", "chicken breast", "Fresh Family Pack Chicken Breasts", "PRICE DROPS Per Lb.", 3),
    (8, "oz", "cheddar cheese", "Stop & Shop Extra Sharp Cheddar", "each (8 oz pkg), $4.38 /lb", 1),
    (5, "lb", "rice", "Earthly Grains Long Grain White Rice", "each (48 oz)", 2),
    (5, "lb", "rice", "Eastside Rice, Long Grain, 25 lbs", "each (25 lb)", 1),
    (1, "lb", "pasta", "Barilla Penne Pasta, 1 lb, 8-count", "each (8 x 1 lb)", 1),
    (6, "ct", "bananas", "Bananas, per lb", "per lb (About 0.33 lb each)", 1.98),
    (6, "ct", "bananas", "Bananas", "each", 6),
    (2, "lb", "bananas", "Bananas, 3 lbs", "each (3 lbs)", 1),
    (1, "pack", "bread", "Wonder Classic White Bread, 20 oz, 2 count", "each (20 oz, 2 count (2 ct))", 1),
]

ok = 0
for qty, unit, item, name, ut, expected in CASES:
    got = units_needed(qty, unit, item, name, ut)
    good = got is not None and abs(got[0] - expected) < 0.01
    ok += good
    print(f"{'PASS' if good else 'FAIL'}  {qty:g} {unit} {item:<15} <- {name[:38]:<38} [{ut}]  buy {got[0] if got else None:g}" if got
          else f"FAIL  {qty:g} {unit} {item} <- {name} [{ut}]  could not convert")

ref = ref_units_for(18, "ct", "dozen", "eggs")
ok += ref == 1.5
print(f"{'PASS' if ref == 1.5 else 'FAIL'}  18 eggs = {ref} dozen (for the U.S.-average estimate)")
total = len(CASES) + 1
print(f"\n{ok}/{total} passed")
sys.exit(0 if ok == total else 1)
