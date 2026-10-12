"""Load production orders and defects from Excel into PostgreSQL.

Steps:
1. Read both Excel files.
2. Validate every row. A row with problems is rejected with a reason.
3. Upsert the valid rows (running the loader again creates no duplicates).
4. Write the rejected rows to data/rejected_rows.csv.
5. Record each file in the ingest_runs table and print a summary.

All database work happens in one transaction: either everything is saved or nothing.

Usage (from the project root, with the venv active):
    python ingest/load_excel.py
    python ingest/load_excel.py --orders data/production_orders.xlsx --defects data/defects.xlsx
"""

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REJECTED_CSV = DATA_DIR / "rejected_rows.csv"

SECTORS = {"Defense", "Aerospace", "Medical", "Industrial"}
PRODUCT_TYPES = {"Rigid", "Rigid-Flex", "Flex", "HDI"}
STATUSES = {"Planned", "In Production", "In QA", "On Hold", "Shipped"}
STATIONS = {
    "Imaging",
    "Etching",
    "Lamination",
    "Drilling",
    "Plating",
    "Solder Mask",
    "AOI",
    "Electrical Test",
    "Final QA",
}
DEFECT_TYPES = {
    "Open",
    "Short",
    "Misregistration",
    "Delamination",
    "Plating Void",
    "Solder Mask Defect",
    "Drill Breakout",
}
MIN_LAYERS, MAX_LAYERS = 2, 16

ORDER_COLUMNS = [
    "order_id",
    "customer_sector",
    "product_type",
    "layers",
    "quantity",
    "status",
    "production_line",
    "created_date",
    "due_date",
]
DEFECT_COLUMNS = ["defect_id", "order_id", "detected_date", "station", "defect_type", "quantity"]


# ---------------------------------------------------------------------------
# Validation (pure functions, no database needed, covered by tests)
# ---------------------------------------------------------------------------


def is_blank(value) -> bool:
    """True for None, NaN/NaT (empty Excel cell) and empty or whitespace-only text."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def check_text(row: dict, field: str, errors: list[str]) -> str | None:
    """Return the field as trimmed text, or record an error if it is empty."""
    value = row.get(field)
    if is_blank(value):
        errors.append(f"{field} is missing")
        return None
    return str(value).strip()


def check_choice(row: dict, field: str, allowed: set[str], errors: list[str]) -> str | None:
    """Return the field if it is one of the allowed values, otherwise record an error."""
    value = check_text(row, field, errors)
    if value is not None and value not in allowed:
        errors.append(f"unknown {field} '{value}' (allowed: {', '.join(sorted(allowed))})")
        return None
    return value


def check_whole_number(
    row: dict, field: str, errors: list[str], minimum: int, maximum: int | None = None
) -> int | None:
    """Return the field as an int within [minimum, maximum], otherwise record an error."""
    value = row.get(field)
    if is_blank(value):
        errors.append(f"{field} is missing")
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = None
    if number is None or not number.is_integer():
        errors.append(f"{field} '{value}' is not a whole number")
        return None

    number = int(number)
    if number < minimum or (maximum is not None and number > maximum):
        limit = f"between {minimum} and {maximum}" if maximum is not None else f"at least {minimum}"
        errors.append(f"{field} must be {limit}, got {number}")
        return None
    return number


def check_date(row: dict, field: str, errors: list[str]) -> date | None:
    """Return the field as a date (Excel date cell or YYYY-MM-DD text), otherwise record an error."""
    value = row.get(field)
    if is_blank(value):
        errors.append(f"{field} is missing")
        return None
    # datetime (and pandas Timestamp) must be checked before date: they are subclasses of date.
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        errors.append(f"{field} '{value}' is not a valid date (expected YYYY-MM-DD)")
        return None


def validate_order(row: dict) -> tuple[dict | None, list[str]]:
    """Check one order row. Returns (clean_row, []) if valid, or (None, errors)."""
    errors: list[str] = []
    clean = {
        "order_id": check_text(row, "order_id", errors),
        "customer_sector": check_choice(row, "customer_sector", SECTORS, errors),
        "product_type": check_choice(row, "product_type", PRODUCT_TYPES, errors),
        "layers": check_whole_number(row, "layers", errors, MIN_LAYERS, MAX_LAYERS),
        "quantity": check_whole_number(row, "quantity", errors, minimum=1),
        "status": check_choice(row, "status", STATUSES, errors),
        "production_line": check_text(row, "production_line", errors),
        "created_date": check_date(row, "created_date", errors),
        "due_date": check_date(row, "due_date", errors),
    }
    created, due = clean["created_date"], clean["due_date"]
    if created is not None and due is not None and due < created:
        errors.append(f"due_date {due} is before created_date {created}")

    return (None, errors) if errors else (clean, [])


def validate_defect(row: dict, known_order_ids: set[str]) -> tuple[dict | None, list[str]]:
    """Check one defect row. known_order_ids are the orders it may refer to."""
    errors: list[str] = []
    clean = {
        "defect_id": check_text(row, "defect_id", errors),
        "order_id": check_text(row, "order_id", errors),
        "detected_date": check_date(row, "detected_date", errors),
        "station": check_choice(row, "station", STATIONS, errors),
        "defect_type": check_choice(row, "defect_type", DEFECT_TYPES, errors),
        "quantity": check_whole_number(row, "quantity", errors, minimum=1),
    }
    order_id = clean["order_id"]
    if order_id is not None and order_id not in known_order_ids:
        errors.append(f"order_id {order_id} does not exist in orders")

    return (None, errors) if errors else (clean, [])


def excel_rows(df: pd.DataFrame):
    """Yield (excel_row_number, row_dict). Row 1 in Excel is the header, so data starts at 2."""
    for index, row in enumerate(df.to_dict("records")):
        yield index + 2, row


def split_rows(df: pd.DataFrame, id_field: str, validate) -> tuple[list[dict], list[dict]]:
    """Validate every row and split into (valid, rejected).

    validate(row) returns (clean_row, errors). An id already used by an earlier
    valid row is a duplicate: the first valid row wins, later ones are rejected.
    """
    valid: list[dict] = []
    rejected: list[dict] = []
    first_seen: dict[str, int] = {}

    for excel_row, row in excel_rows(df):
        clean, errors = validate(row)
        row_id = None if is_blank(row.get(id_field)) else str(row[id_field]).strip()
        if row_id in first_seen:
            errors.append(f"duplicate {id_field} {row_id} (first seen on Excel row {first_seen[row_id]})")

        if errors:
            rejected.append({"excel_row": excel_row, "reason": "; ".join(errors), **row})
        else:
            first_seen[row_id] = excel_row
            valid.append(clean)
    return valid, rejected


def validate_orders(df: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """Validate all order rows. Returns (valid_rows, rejected_rows)."""
    return split_rows(df, "order_id", validate_order)


def validate_defects(df: pd.DataFrame, known_order_ids: set[str]) -> tuple[list[dict], list[dict]]:
    """Validate all defect rows against the known order ids. Returns (valid_rows, rejected_rows)."""
    return split_rows(df, "defect_id", lambda row: validate_defect(row, known_order_ids))


# ---------------------------------------------------------------------------
# Files and database
# ---------------------------------------------------------------------------

UPSERT_ORDER = """
    INSERT INTO orders (order_id, customer_sector, product_type, layers, quantity,
                        status, production_line, created_date, due_date)
    VALUES (%(order_id)s, %(customer_sector)s, %(product_type)s, %(layers)s, %(quantity)s,
            %(status)s, %(production_line)s, %(created_date)s, %(due_date)s)
    ON CONFLICT (order_id) DO UPDATE SET
        customer_sector = EXCLUDED.customer_sector,
        product_type    = EXCLUDED.product_type,
        layers          = EXCLUDED.layers,
        quantity        = EXCLUDED.quantity,
        status          = EXCLUDED.status,
        production_line = EXCLUDED.production_line,
        created_date    = EXCLUDED.created_date,
        due_date        = EXCLUDED.due_date
"""

UPSERT_DEFECT = """
    INSERT INTO defects (defect_id, order_id, detected_date, station, defect_type, quantity)
    VALUES (%(defect_id)s, %(order_id)s, %(detected_date)s, %(station)s, %(defect_type)s, %(quantity)s)
    ON CONFLICT (defect_id) DO UPDATE SET
        order_id      = EXCLUDED.order_id,
        detected_date = EXCLUDED.detected_date,
        station       = EXCLUDED.station,
        defect_type   = EXCLUDED.defect_type,
        quantity      = EXCLUDED.quantity
"""

INSERT_RUN = """
    INSERT INTO ingest_runs (file_name, started_at, rows_loaded, rows_rejected)
    VALUES (%s, %s, %s, %s)
"""


def read_excel(path: Path, required_columns: list[str]) -> pd.DataFrame:
    """Read an Excel file as raw values (validation does the type checks)."""
    if not path.exists():
        sys.exit(f"File not found: {path}\nCreate the sample files with: python scripts/generate_data.py")
    try:
        df = pd.read_excel(path, dtype=object, engine="openpyxl")
    except PermissionError:
        sys.exit(f"Cannot read {path}. Is it open in Excel? Close it and try again.")

    df.columns = [str(column).strip() for column in df.columns]
    missing = [column for column in required_columns if column not in df.columns]
    if missing:
        sys.exit(f"{path.name} is missing columns: {', '.join(missing)}")
    return df


def connect() -> psycopg.Connection:
    """Open a database connection using the settings in .env."""
    load_dotenv(ROOT / ".env")
    missing = [name for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB") if not os.getenv(name)]
    if missing:
        sys.exit(f"Missing settings: {', '.join(missing)}. Copy .env.example to .env and fill it in.")

    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = os.getenv("POSTGRES_PORT", "5432")
    try:
        return psycopg.connect(
            host=host,
            port=port,
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            connect_timeout=5,
        )
    except psycopg.OperationalError as error:
        sys.exit(f"Cannot connect to PostgreSQL at {host}:{port}.\n{error}\nIs the database running? Check: docker compose ps")


def load_file(conn, path: Path, valid: list[dict], rejected: list[dict], upsert_sql: str, started_at) -> None:
    """Upsert the valid rows of one file and record the run in ingest_runs."""
    with conn.cursor() as cur:
        cur.executemany(upsert_sql, valid)
        cur.execute(INSERT_RUN, (path.name, started_at, len(valid), len(rejected)))


def write_rejected(rejected: list[dict]) -> None:
    """Write all rejected rows (from both files) to data/rejected_rows.csv."""
    columns = ["source_file", "excel_row", "reason"] + ORDER_COLUMNS
    columns += [column for column in DEFECT_COLUMNS if column not in columns]
    try:
        pd.DataFrame(rejected, columns=columns).to_csv(REJECTED_CSV, index=False)
    except PermissionError:
        sys.exit(f"Cannot write {REJECTED_CSV}. Is it open in Excel? Close it and run the loader again.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load orders and defects from Excel into PostgreSQL.")
    parser.add_argument("--orders", type=Path, default=DATA_DIR / "production_orders.xlsx")
    parser.add_argument("--defects", type=Path, default=DATA_DIR / "defects.xlsx")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    orders_df = read_excel(args.orders, ORDER_COLUMNS)
    defects_df = read_excel(args.defects, DEFECT_COLUMNS)

    # The "with" block commits at the end, or rolls everything back on an error.
    with connect() as conn:
        started_at = datetime.now(timezone.utc)
        valid_orders, rejected_orders = validate_orders(orders_df)
        load_file(conn, args.orders, valid_orders, rejected_orders, UPSERT_ORDER, started_at)

        # Defects may refer to orders from this file or from earlier loads.
        known_order_ids = {row[0] for row in conn.execute("SELECT order_id FROM orders")}
        started_at = datetime.now(timezone.utc)
        valid_defects, rejected_defects = validate_defects(defects_df, known_order_ids)
        load_file(conn, args.defects, valid_defects, rejected_defects, UPSERT_DEFECT, started_at)

        total_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        total_defects = conn.execute("SELECT COUNT(*) FROM defects").fetchone()[0]

    rejected = [{"source_file": args.orders.name, **row} for row in rejected_orders]
    rejected += [{"source_file": args.defects.name, **row} for row in rejected_defects]
    write_rejected(rejected)

    print("Ingestion summary")
    print(f"  {args.orders.name}: {len(valid_orders)} loaded, {len(rejected_orders)} rejected")
    print(f"  {args.defects.name}: {len(valid_defects)} loaded, {len(rejected_defects)} rejected")
    print(f"  Rejected rows written to {REJECTED_CSV.relative_to(ROOT)} ({len(rejected)} rows)")
    print(f"  Database now holds {total_orders} orders and {total_defects} defects")


if __name__ == "__main__":
    main()
