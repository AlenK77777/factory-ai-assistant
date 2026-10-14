"""Inbox loader: column detection, one-file load, and the PowerShell flow."""

import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from chat.event_log import get_log
from ingest.load_excel import classify_columns, load_single

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "process_inbox.ps1"


def test_columns_decide_orders_or_defects_not_the_file_name():
    orders = [
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
    defects = ["defect_id", "order_id", "detected_date", "station", "defect_type", "quantity"]
    assert classify_columns(orders) == "orders"
    assert classify_columns(defects) == "defects"
    # A defects workbook keeps that type even if the name says orders.
    assert classify_columns(["defect_id", "order_id", "defect_type"]) == "defects"
    assert classify_columns(["order_id", "quantity"]) is None


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def executemany(self, sql, rows):
        self.rows = list(rows)

    def execute(self, *args):
        return None


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return _Cursor()

    def execute(self, sql):
        return self

    def fetchall(self):
        return [("PO-1",)]


def test_file_load_reports_rejected_rows_without_a_second_workbook(tmp_path, monkeypatch):
    path = tmp_path / "defects.xlsx"
    pd.DataFrame(
        [
            {
                "order_id": "PO-1",
                "customer_sector": "Medical",
                "product_type": "HDI",
                "layers": 4,
                "quantity": 10,
                "status": "In QA",
                "production_line": "Line 1",
                "created_date": date(2026, 7, 1),
                "due_date": date(2026, 7, 20),
            },
            {
                "order_id": "",
                "customer_sector": "Medical",
                "product_type": "HDI",
                "layers": 4,
                "quantity": 10,
                "status": "In QA",
                "production_line": "Line 1",
                "created_date": date(2026, 7, 1),
                "due_date": date(2026, 7, 20),
            },
        ]
    ).to_excel(path, index=False)
    monkeypatch.setattr("ingest.load_excel.connect", lambda: _Conn())
    csv_path = tmp_path / "rejected.csv"
    loaded, rejected = load_single(path, csv_path)
    assert loaded == 1
    assert rejected == 1
    assert "order_id is missing" in csv_path.read_text(encoding="utf-8")


def _powershell() -> str:
    """pwsh on Linux CI; Windows PowerShell when pwsh is not installed."""
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    pytest.skip("PowerShell is not installed (neither pwsh nor powershell was found).")


def _run_script(root: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = str(ROOT)
    if env:
        child_env.update(env)
    return subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-ProjectRoot",
            str(root),
            "-Python",
            sys.executable,
            "-Loader",
            str(root / "loader_stub.py"),
        ],
        cwd=str(root),
        capture_output=True,
        text=True,
        env=child_env,
        check=False,
    )


STUB = '''
import os
import sys
from pathlib import Path

args = sys.argv[1:]
command = args[0] if args else ""
if command == "--check":
    if os.environ.get("INBOX_DB_DOWN") == "1":
        print("Database is not reachable. Nothing was moved.")
        sys.exit(2)
    sys.exit(0)
if command == "--detect":
    from ingest.load_excel import classify_workbook
    kind = classify_workbook(Path(args[1]))
    print(kind or "unknown")
    sys.exit(0 if kind else 2)
if command == "--file":
    path = Path(args[1])
    csv_path = Path(args[3])
    csv_path.write_text("source_file,reason\\n", encoding="utf-8")
    order_log = Path(os.environ["INBOX_ORDER_LOG"])
    with order_log.open("a", encoding="utf-8") as handle:
        handle.write(path.name + "\\n")
    print(f"{path.name}: 300 loaded, 6 rejected")
    sys.exit(1)
if command == "--inbox-log":
    from chat.event_log import log_event
    file_name, result, loaded, rejected = args[1:5]
    level = "ERROR" if result == "failed" else "INFO"
    log_event(
        level, "ingest", "inbox",
        source="automation", file=file_name, result=result,
        rows_loaded=int(loaded), rows_rejected=int(rejected),
    )
    sys.exit(0)
sys.exit(2)
'''


def test_inbox_script_loads_orders_before_defects_and_skips_a_locked_file(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    order_book = inbox / "nightly.xlsx"
    defect_book = inbox / "scrap.xlsx"
    locked_book = inbox / "open.xlsx"
    pd.DataFrame(
        [{"order_id": "PO-1", "customer_sector": "Medical", "due_date": "2026-07-03", "layers": 2, "product_type": "HDI"}]
    ).to_excel(order_book, index=False)
    pd.DataFrame(
        [{"defect_id": "D-1", "order_id": "PO-1", "defect_type": "Open"}]
    ).to_excel(defect_book, index=False)
    pd.DataFrame([{"order_id": "PO-2", "customer_sector": "Medical", "due_date": "2026-07-03"}]).to_excel(locked_book, index=False)
    (inbox / "~$open.xlsx").write_text("excel lock", encoding="utf-8")
    (tmp_path / "loader_stub.py").write_text(STUB, encoding="utf-8")
    order_log = tmp_path / "order.txt"
    completed = _run_script(tmp_path, {"INBOX_ORDER_LOG": str(order_log)})
    assert completed.returncode == 1
    assert completed.stdout.strip() == "nightly.xlsx: 300 loaded, 6 rejected; scrap.xlsx: 300 loaded, 6 rejected; open.xlsx: locked"
    assert order_log.read_text(encoding="utf-8").splitlines() == ["nightly.xlsx", "scrap.xlsx"]
    assert locked_book.exists()
    assert not order_book.exists()
    assert not defect_book.exists()
    processed = list((tmp_path / "processed").glob("*.xlsx"))
    assert len(processed) == 2
    assert list((tmp_path / "processed").glob("*_rejected.csv"))
    events = get_log().read_events()
    inbox_events = [event for event in events if event.get("event") == "inbox"]
    assert inbox_events
    assert all(event.get("source") == "automation" for event in inbox_events)
    assert any(event.get("result") == "locked" for event in inbox_events)
    text = get_log().path.read_text(encoding="utf-8")
    assert "POSTGRES" not in text
    assert "password" not in text.lower()


def test_inbox_script_moves_nothing_when_the_database_is_down(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    book = inbox / "nightly.xlsx"
    book.write_bytes(b"not really opened")
    (tmp_path / "loader_stub.py").write_text(STUB, encoding="utf-8")
    completed = _run_script(tmp_path, {"INBOX_DB_DOWN": "1", "INBOX_ORDER_LOG": str(tmp_path / "order.txt")})
    assert completed.returncode == 2
    assert completed.stdout.strip() == "Database is not reachable. Nothing was moved."
    assert book.exists()
    assert list((tmp_path / "processed").glob("*")) == []
    assert list((tmp_path / "failed").glob("*")) == []
