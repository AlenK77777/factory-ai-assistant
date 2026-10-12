"""Generate synthetic Excel files for the ingestion demo.

Creates:
    data/production_orders.xlsx  (~300 orders)
    data/defects.xlsx            (~800 defects)

A fixed random seed makes every run produce the same data. A few invalid rows
are planted on purpose so the loader's validation can be checked; they are
printed at the end. All data is invented.

Usage (from the project root, with the venv active):
    python scripts/generate_data.py
"""

import random
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ORDERS_FILE = DATA_DIR / "production_orders.xlsx"
DEFECTS_FILE = DATA_DIR / "defects.xlsx"

SEED = 42
N_ORDERS = 300
N_DEFECTS = 800
FIRST_ORDER_NUMBER = 10001
FIRST_DATE = date(2026, 6, 1)
LAST_DATE = date(2026, 10, 9)

# Domain values. The loader keeps its own copy of the allowed values,
# just like a real loader would not trust the system that produced the file.
SECTORS = ["Defense", "Aerospace", "Medical", "Industrial"]
LAYER_OPTIONS = {
    "Rigid": [2, 4, 6, 8, 10, 12],
    "Rigid-Flex": [4, 6, 8, 10, 12],
    "Flex": [2, 4, 6],
    "HDI": [8, 10, 12, 14, 16],
}
STATUS_WEIGHTS = {
    "Planned": 10,
    "In Production": 25,
    "In QA": 10,
    "On Hold": 8,
    "Shipped": 47,
}
PRODUCTION_LINES = ["Line 1", "Line 2", "Line 3"]

# AOI and Electrical Test find most defects, as in a real PCB line.
STATION_WEIGHTS = {
    "Imaging": 8,
    "Etching": 8,
    "Lamination": 8,
    "Drilling": 10,
    "Plating": 10,
    "Solder Mask": 10,
    "AOI": 25,
    "Electrical Test": 15,
    "Final QA": 6,
}
DEFECT_TYPES = [
    "Open",
    "Short",
    "Misregistration",
    "Delamination",
    "Plating Void",
    "Solder Mask Defect",
    "Drill Breakout",
]
# Defect types most often found at each station (used 70% of the time).
TYPICAL_DEFECTS = {
    "Imaging": ["Open", "Short", "Misregistration"],
    "Etching": ["Open", "Short"],
    "Lamination": ["Delamination", "Misregistration"],
    "Drilling": ["Drill Breakout", "Misregistration"],
    "Plating": ["Plating Void", "Open"],
    "Solder Mask": ["Solder Mask Defect"],
    "AOI": ["Open", "Short"],
    "Electrical Test": ["Open", "Short"],
    "Final QA": ["Solder Mask Defect", "Delamination", "Plating Void"],
}


def random_date(rng: random.Random, start: date, end: date) -> date:
    """Return a random date between start and end (both included)."""
    return start + timedelta(days=rng.randint(0, (end - start).days))


def pick_weighted(rng: random.Random, weights: dict) -> str:
    """Pick one key of the dict, using its value as the weight."""
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def make_orders(rng: random.Random) -> list[dict]:
    """Create valid production orders."""
    orders = []
    for i in range(N_ORDERS):
        product_type = rng.choice(list(LAYER_OPTIONS))
        status = pick_weighted(rng, STATUS_WEIGHTS)
        # Planned orders are recent; all others started at least a week ago.
        if status == "Planned":
            created = random_date(rng, LAST_DATE - timedelta(days=21), LAST_DATE)
        else:
            created = random_date(rng, FIRST_DATE, LAST_DATE - timedelta(days=7))

        orders.append(
            {
                "order_id": f"PO-{FIRST_ORDER_NUMBER + i}",
                "customer_sector": rng.choice(SECTORS),
                "product_type": product_type,
                "layers": rng.choice(LAYER_OPTIONS[product_type]),
                "quantity": rng.randrange(10, 2001, 10),
                "status": status,
                "production_line": rng.choice(PRODUCTION_LINES),
                "created_date": created,
                "due_date": created + timedelta(days=rng.randint(14, 60)),
            }
        )
    return orders


def make_defects(rng: random.Random, orders: list[dict]) -> list[dict]:
    """Create valid defects for orders that have already started production."""
    started_orders = [o for o in orders if o["status"] != "Planned"]
    defects = []
    for _ in range(N_DEFECTS):
        order = rng.choice(started_orders)
        station = pick_weighted(rng, STATION_WEIGHTS)
        if rng.random() < 0.7:
            defect_type = rng.choice(TYPICAL_DEFECTS[station])
        else:
            defect_type = rng.choice(DEFECT_TYPES)
        latest = min(order["created_date"] + timedelta(days=30), LAST_DATE)

        defects.append(
            {
                "order_id": order["order_id"],
                "detected_date": random_date(rng, order["created_date"], latest),
                "station": station,
                "defect_type": defect_type,
                "quantity": rng.randint(1, 25),
            }
        )

    # Number the defects in date order, like a real defect log.
    defects.sort(key=lambda d: (d["detected_date"], d["order_id"]))
    return [{"defect_id": f"D-{i + 1:05d}", **d} for i, d in enumerate(defects)]


def plant_bad_orders(rng: random.Random, orders: list[dict]) -> tuple[list[dict], list[str]]:
    """Insert invalid order rows at random places. Returns (rows, descriptions)."""
    next_number = FIRST_ORDER_NUMBER + N_ORDERS
    template = rng.choice(orders)
    bad_rows = [
        ({**template, "order_id": None}, "order with a missing order_id"),
        (
            {**template, "order_id": f"PO-{next_number}", "quantity": -50},
            f"PO-{next_number}: negative quantity (-50)",
        ),
        (
            {**template, "order_id": f"PO-{next_number + 1}", "status": "Done"},
            f"PO-{next_number + 1}: unknown status 'Done'",
        ),
        (
            {
                **template,
                "order_id": f"PO-{next_number + 2}",
                "due_date": template["created_date"] - timedelta(days=5),
            },
            f"PO-{next_number + 2}: due_date before created_date",
        ),
        (
            {**template, "order_id": f"PO-{next_number + 3}", "layers": 18},
            f"PO-{next_number + 3}: 18 layers (allowed 2-16)",
        ),
    ]

    rows = list(orders)
    for row, _ in bad_rows:
        rows.insert(rng.randint(1, len(rows)), row)

    # The duplicate goes after the original, so the original is the one loaded.
    original = orders[20]
    duplicate = {**original, "quantity": original["quantity"] + 100}
    rows.insert(rng.randint(rows.index(original) + 1, len(rows)), duplicate)

    descriptions = [text for _, text in bad_rows]
    descriptions.append(f"{original['order_id']}: duplicate order_id (second copy)")
    return rows, descriptions


def plant_bad_defects(rng: random.Random, defects: list[dict]) -> tuple[list[dict], list[str]]:
    """Insert invalid defect rows at random places. Returns (rows, descriptions)."""
    n = len(defects)
    template = rng.choice(defects)
    ids = [f"D-{n + i:05d}" for i in range(1, 5)]
    bad_rows = [
        (
            {**template, "defect_id": ids[0], "order_id": "PO-99999"},
            f"{ids[0]}: references non-existent order PO-99999",
        ),
        (
            {**template, "defect_id": ids[1], "quantity": -3},
            f"{ids[1]}: negative quantity (-3)",
        ),
        (
            {**template, "defect_id": ids[2], "station": "Packing"},
            f"{ids[2]}: unknown station 'Packing'",
        ),
        (
            {**template, "defect_id": ids[3], "detected_date": None},
            f"{ids[3]}: missing detected_date",
        ),
    ]

    rows = list(defects)
    for row, _ in bad_rows:
        rows.insert(rng.randint(1, len(rows)), row)
    return rows, [text for _, text in bad_rows]


def write_excel(rows: list[dict], path: Path, sheet_name: str) -> None:
    """Write rows to an Excel file, with a friendly error if the file is open."""
    try:
        pd.DataFrame(rows).to_excel(path, index=False, sheet_name=sheet_name)
    except PermissionError:
        sys.exit(f"Cannot write {path}. Is it open in Excel? Close it and try again.")


def main() -> None:
    rng = random.Random(SEED)
    DATA_DIR.mkdir(exist_ok=True)

    orders = make_orders(rng)
    defects = make_defects(rng, orders)
    order_rows, bad_order_notes = plant_bad_orders(rng, orders)
    defect_rows, bad_defect_notes = plant_bad_defects(rng, defects)

    write_excel(order_rows, ORDERS_FILE, "Orders")
    write_excel(defect_rows, DEFECTS_FILE, "Defects")

    print(f"Wrote {ORDERS_FILE.relative_to(ROOT)}: {len(order_rows)} rows")
    print(f"Wrote {DEFECTS_FILE.relative_to(ROOT)}: {len(defect_rows)} rows")
    print(f"Planted {len(bad_order_notes) + len(bad_defect_notes)} invalid rows:")
    for note in bad_order_notes:
        print(f"  [orders]  {note}")
    for note in bad_defect_notes:
        print(f"  [defects] {note}")


if __name__ == "__main__":
    main()
