"""Numbers for the dashboard.

Production numbers come from the read-only database role.
Assistant numbers come from logs/events.log (chat question events).
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import median

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

from chat.event_log import event_source, get_log, log_event

ROOT = Path(__file__).resolve().parent.parent

STATUS_ORDER = ("Planned", "In Production", "In QA", "On Hold", "Shipped")

ORDERS_SQL = """
SELECT order_id, customer_sector, status, due_date, quantity
FROM orders
"""

DEFECTS_SQL = """
SELECT d.detected_date, d.station, d.defect_type, d.quantity, o.customer_sector
FROM defects d
JOIN orders o ON o.order_id = d.order_id
"""

RUNS_SQL = """
SELECT file_name, started_at, rows_loaded, rows_rejected
FROM ingest_runs
ORDER BY started_at DESC
LIMIT 10
"""


def as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _count_by(rows: list[dict], key: str, preferred: tuple[str, ...] = ()) -> list[dict]:
    totals: dict[str, int] = defaultdict(int)
    for row in rows:
        totals[str(row[key])] += 1
    names = [name for name in preferred if name in totals]
    names += sorted((name for name in totals if name not in names), key=lambda name: (-totals[name], name))
    return [{"name": name, "count": totals[name]} for name in names]


def _units_by(rows: list[dict], key: str) -> list[dict]:
    totals: dict[str, dict] = {}
    for row in rows:
        name = str(row[key])
        bucket = totals.setdefault(name, {"name": name, "count": 0, "units": 0})
        bucket["count"] += 1
        bucket["units"] += int(row.get("quantity") or 0)
    return sorted(totals.values(), key=lambda item: (-item["units"], item["name"]))


def production_report(orders: list[dict], defects: list[dict], runs: list[dict], today: date) -> dict:
    """KPI tiles and tables. Late means due before today and not Shipped."""
    ordered_units = sum(int(row.get("quantity") or 0) for row in orders)
    defective_units = sum(int(row.get("quantity") or 0) for row in defects)
    late_rows = []
    on_hold = 0
    for row in orders:
        if row.get("status") == "On Hold":
            on_hold += 1
        due = as_date(row["due_date"])
        if due < today and row.get("status") != "Shipped":
            late_rows.append(
                {
                    "order_id": row["order_id"],
                    "customer_sector": row["customer_sector"],
                    "status": row["status"],
                    "due_date": due.isoformat(),
                    "days_late": (today - due).days,
                }
            )
    late_rows.sort(key=lambda item: (-item["days_late"], item["order_id"]))
    weeks: dict[str, dict] = {}
    for row in defects:
        detected = as_date(row["detected_date"])
        year, week, _day = detected.isocalendar()
        label = f"{year}-W{week:02d}"
        bucket = weeks.setdefault(label, {"week": label, "count": 0, "units": 0})
        bucket["count"] += 1
        bucket["units"] += int(row.get("quantity") or 0)
    weekly = [weeks[key] for key in sorted(weeks)]
    # The first week in the data starts mid-week. The week that contains today is not finished.
    current_year, current_week, _day = today.isocalendar()
    current_label = f"{current_year}-W{current_week:02d}"
    partial = set()
    if weekly:
        partial.add(weekly[0]["week"])
    if current_label in weeks:
        partial.add(current_label)
    for bucket in weekly:
        bucket["partial"] = bucket["week"] in partial
        bucket["label"] = bucket["week"] + (" partial" if bucket["partial"] else "")
    rate = round(100.0 * defective_units / ordered_units, 1) if ordered_units else 0.0
    ingest = []
    for row in runs:
        started = row.get("started_at")
        if isinstance(started, datetime):
            started = started.astimezone().isoformat(timespec="seconds")
        ingest.append(
            {
                "file_name": row.get("file_name") or "",
                "started_at": str(started or ""),
                "rows_loaded": int(row.get("rows_loaded") or 0),
                "rows_rejected": int(row.get("rows_rejected") or 0),
            }
        )
    return {
        "error": None,
        "kpis": {
            "total_orders": len(orders),
            "late": len(late_rows),
            "on_hold": on_hold,
            "defective_units": defective_units,
            "ordered_units": ordered_units,
            "defect_rate": rate,
        },
        "orders_by_status": _count_by(orders, "status", STATUS_ORDER),
        "defects_by_station": _units_by(defects, "station"),
        "defects_by_type": _units_by(defects, "defect_type"),
        "defects_by_sector": _units_by(defects, "customer_sector"),
        "weekly_defects": weekly,
        "late_orders": late_rows[:10],
        "ingest_runs": ingest,
    }


def empty_production(message: str) -> dict:
    report = production_report([], [], [], date.today())
    report["error"] = message
    return report


def fetch_production(today: date | None = None) -> dict:
    """Read orders, defects and the last ingestion runs. Never writes."""
    load_dotenv(ROOT / ".env")
    today = today or date.today()
    try:
        with psycopg.connect(
            host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
            port=os.getenv("POSTGRES_PORT", "5432"),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["MCP_DB_USER"],
            password=os.environ["MCP_DB_PASSWORD"],
            connect_timeout=5,
            options="-c statement_timeout=5000",
            row_factory=dict_row,
        ) as conn:
            conn.read_only = True
            orders = conn.execute(ORDERS_SQL).fetchall()
            defects = conn.execute(DEFECTS_SQL).fetchall()
            runs = conn.execute(RUNS_SQL).fetchall()
    except KeyError:
        log_event("ERROR", "dashboard", "production", error="Database settings are missing from .env.")
        return empty_production("Database settings are missing from .env.")
    except psycopg.Error as error:
        log_event("ERROR", "dashboard", "production", error=f"{type(error).__name__}: {error}")
        return empty_production("The database is not reachable. Check that the db container is running.")
    return production_report(list(orders), list(defects), list(runs), today)


def _tools_of(event: dict) -> list[dict]:
    raw = event.get("tools") or "[]"
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def ai_report(events: list[dict], include_test: bool = False) -> dict:
    """Assistant section. Only chat question events count.

    By default only source=user is counted. Smoke, test, and legacy lines
    (events written before source existed) are left out unless include_test is set.
    source is a label for statistics, not a security control.
    """
    questions = [event for event in events if event.get("component") == "chat" and event.get("event") == "question"]
    if not include_test:
        questions = [event for event in questions if event_source(event) == "user"]
    per_day: dict[str, int] = defaultdict(int)
    per_language: dict[str, int] = defaultdict(int)
    outcomes = {"found": 0, "not_found": 0, "error": 0}
    tool_counts: dict[str, int] = defaultdict(int)
    latency: dict[str, list[float]] = defaultdict(list)
    unanswered = []
    for event in questions:
        stamp = str(event.get("timestamp") or "")
        day = stamp[:10]
        if day:
            per_day[day] += 1
        language = str(event.get("language") or "unknown")
        per_language[language] += 1
        outcome = str(event.get("outcome") or "")
        if outcome not in outcomes:
            outcome = "error"
        outcomes[outcome] += 1
        model = str(event.get("model") or "unknown")
        if event.get("latency_s") not in (None, ""):
            try:
                latency[model].append(float(event["latency_s"]))
            except (TypeError, ValueError):
                pass
        for tool in _tools_of(event):
            name = str(tool.get("name") or "").strip()
            if name:
                tool_counts[name] += 1
        if outcome == "not_found":
            unanswered.append(
                {
                    "timestamp": stamp,
                    "language": language,
                    "model": model,
                    "question": str(event.get("question") or ""),
                }
            )
    unanswered.sort(key=lambda item: item["timestamp"], reverse=True)
    latency_rows = []
    for model, samples in latency.items():
        latency_rows.append(
            {
                "model": model,
                "count": len(samples),
                "median_s": round(float(median(samples)), 2),
                "slowest_s": round(max(samples), 2),
            }
        )
    latency_rows.sort(key=lambda item: item["model"])
    return {
        "questions_per_day": [{"day": day, "count": per_day[day]} for day in sorted(per_day)],
        "by_language": [{"name": name, "count": per_language[name]} for name in sorted(per_language, key=lambda name: (-per_language[name], name))],
        "outcomes": outcomes,
        "tools": [{"name": name, "count": tool_counts[name]} for name in sorted(tool_counts, key=lambda name: (-tool_counts[name], name))],
        "latency_by_model": latency_rows,
        "unanswered": unanswered,
    }


def load_ai_report(include_test: bool = False) -> dict:
    return ai_report(get_log().read_events(), include_test=include_test)
