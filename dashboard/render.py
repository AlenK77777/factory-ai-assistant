"""Self-contained dashboard HTML (inline SVG) and an Excel workbook with real charts."""

from __future__ import annotations

import html
from datetime import date, datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.chart.data_source import AxDataSource, StrRef
from openpyxl.chart.label import DataLabelList

from dashboard.data import fetch_production, load_ai_report

LANGUAGE_NAMES = {"en": "English", "ru": "Russian", "he": "Hebrew"}


def generated_stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def snapshot(today=None, include_test: bool = False) -> dict:
    """Current production and assistant numbers, plus the time they were read."""
    return {
        "generated_at": generated_stamp(),
        "include_test": include_test,
        "production": fetch_production(today),
        "ai": load_ai_report(include_test=include_test),
    }


# The model returned something. Nobody has checked that it is right.
FOUND_LABEL = "found an answer (correctness not checked)"


def _num(value) -> str:
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.1f}"
    return str(int(value) if isinstance(value, float) else value)


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _y_ticks(peak: float) -> list[float]:
    """Four or five labelled ticks from 0 up to the largest value."""
    if peak <= 0:
        return [0.0]
    step = peak / 4
    return [round(step * index, 2) for index in range(5)]


def svg_bars(title: str, rows: list[tuple[str, float]], category: str, value: str) -> str:
    """Horizontal bar chart with a category axis and a value axis."""
    if not rows:
        return f"<h3>{_esc(title)}</h3><p>No data.</p>"
    row_h = 28
    longest = max(len(str(label)) for label, _value in rows)
    label_w = max(168, min(460, longest * 8))
    chart_w = 380
    top = 22
    width = label_w + chart_w + 72
    height = top + len(rows) * row_h + 22
    peak = max(value_item for _, value_item in rows) or 1
    parts = [
        f"<h3>{_esc(title)}</h3>",
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">',
        f"<title>{_esc(title)}</title>",
        f'<text x="0" y="14" font-size="12" fill="#5c6b7a">{_esc(category)}</text>',
        f'<text x="{label_w + chart_w / 2:.0f}" y="{height - 4}" font-size="12" text-anchor="middle" fill="#5c6b7a">{_esc(value)}</text>',
    ]
    for index, (label, amount) in enumerate(rows):
        y = top + index * row_h
        bar = chart_w * (float(amount) / peak)
        shown = label if len(str(label)) * 8 <= label_w else str(label)[: max(1, label_w // 8 - 1)] + "..."
        parts.append(f'<text x="0" y="{y + 14}" font-size="13" fill="#1c2430">{_esc(shown)}</text>')
        parts.append(f'<rect x="{label_w}" y="{y}" width="{bar:.1f}" height="18" fill="#1f4e79"></rect>')
        parts.append(
            f'<text x="{label_w + bar + 6:.1f}" y="{y + 14}" font-size="12" fill="#1c2430">{_esc(_num(amount))}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


def svg_line(title: str, rows: list[tuple[str, float]], category: str, value: str) -> str:
    """Line chart with a labelled Y scale and rotated X labels."""
    if not rows:
        return f"<h3>{_esc(title)}</h3><p>No data.</p>"
    width, height = 720, 280
    left, right, top, bottom = 78, 16, 28, 92
    peak = max(amount for _, amount in rows) or 1
    count = len(rows)
    ticks = _y_ticks(float(peak))

    def point(index: int, amount: float) -> tuple[float, float]:
        span = width - left - right
        x = left if count == 1 else left + span * index / (count - 1)
        y = top + (height - top - bottom) * (1 - float(amount) / float(ticks[-1] or peak))
        return x, y

    coords = [point(index, amount) for index, (_, amount) in enumerate(rows)]
    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    parts = [
        f"<h3>{_esc(title)}</h3>",
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">',
        f"<title>{_esc(title)}</title>",
        f'<text transform="rotate(-90 16 {height / 2:.0f})" x="16" y="{height / 2:.0f}" font-size="12" text-anchor="middle" fill="#5c6b7a">{_esc(value)}</text>',
        f'<text x="{(left + width - right) / 2:.0f}" y="{height - 6}" font-size="12" text-anchor="middle" fill="#5c6b7a">{_esc(category)}</text>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" stroke="#d5dbe3"/>',
        f'<line x1="{left}" y1="{height - bottom}" x2="{width - right}" y2="{height - bottom}" stroke="#d5dbe3"/>',
    ]
    scale = float(ticks[-1] or peak)
    for tick in ticks:
        y = top + (height - top - bottom) * (1 - tick / scale)
        parts.append(f'<line x1="{left - 4}" y1="{y:.1f}" x2="{left}" y2="{y:.1f}" stroke="#5c6b7a"/>')
        parts.append(
            f'<text x="{left - 8}" y="{y + 4:.1f}" font-size="11" text-anchor="end" fill="#3d4b5c">{_esc(_num(tick))}</text>'
        )
    parts.append(f'<polyline fill="none" stroke="#1f4e79" stroke-width="2" points="{polyline}"/>')
    step = 1 if count <= 10 else max(1, count // 8)
    for index, ((label, _amount), (x, y)) in enumerate(zip(rows, coords)):
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#1f4e79"/>')
        if index % step == 0 or index == count - 1:
            parts.append(
                f'<text x="{x:.1f}" y="{height - bottom + 14}" font-size="11" text-anchor="end" fill="#3d4b5c" transform="rotate(-40 {x:.1f} {height - bottom + 14})">{_esc(label)}</text>'
            )
    parts.append("</svg>")
    return "\n".join(parts)


def svg_grouped(title: str, categories: list[str], series: list[tuple[str, list[float]]], category: str, value: str) -> str:
    """Grouped bars with a value on each bar and a colour legend."""
    if not categories or not series:
        return f"<h3>{_esc(title)}</h3><p>No data.</p>"
    row_h = 36
    label_w = 180
    chart_w = 320
    top = 36
    width = label_w + chart_w + 56
    height = top + len(categories) * row_h + 22
    peak = max((amount for _, amounts in series for amount in amounts), default=1) or 1
    colors = ("#1f4e79", "#a15c07")
    parts = [
        f"<h3>{_esc(title)}</h3>",
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">',
        f"<title>{_esc(title)}</title>",
        f'<text x="0" y="14" font-size="12" fill="#5c6b7a">{_esc(category)}</text>',
        f'<text x="{label_w + chart_w / 2:.0f}" y="{height - 4}" font-size="12" text-anchor="middle" fill="#5c6b7a">{_esc(value)}</text>',
    ]
    for series_index, (name, _amounts) in enumerate(series):
        legend_x = label_w + series_index * 130
        color = colors[series_index % len(colors)]
        parts.append(f'<rect x="{legend_x}" y="4" width="12" height="12" fill="{color}"></rect>')
        parts.append(f'<text x="{legend_x + 16}" y="14" font-size="12" fill="#1c2430">{_esc(name)}</text>')
    for index, label in enumerate(categories):
        y = top + index * row_h
        shown = label if len(label) <= 26 else label[:25] + "..."
        parts.append(f'<text x="0" y="{y + 16}" font-size="13" fill="#1c2430">{_esc(shown)}</text>')
        for series_index, (_name, amounts) in enumerate(series):
            amount = amounts[index] if index < len(amounts) else 0
            bar = chart_w * (float(amount) / peak)
            bar_y = y + series_index * 14
            color = colors[series_index % len(colors)]
            parts.append(f'<rect x="{label_w}" y="{bar_y}" width="{bar:.1f}" height="12" fill="{color}"></rect>')
            parts.append(
                f'<text x="{label_w + bar + 4:.1f}" y="{bar_y + 10}" font-size="11" fill="#1c2430">{_esc(_num(amount))}</text>'
            )
    parts.append("</svg>")
    return "\n".join(parts)


def html_table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "<p>No data.</p>"
    head = "".join(f"<th>{_esc(header)}</th>" for header in headers)
    body = []
    for row in rows:
        cells = "".join(f"<td>{_esc(cell)}</td>" for cell in row)
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _tiles(kpis: dict) -> str:
    items = [
        ("Total orders", kpis["total_orders"]),
        ("Late", kpis["late"]),
        ("On hold", kpis["on_hold"]),
        ("Defective units", kpis["defective_units"]),
        ("Defect rate", f"{kpis['defect_rate']:.1f}%"),
    ]
    boxes = []
    for label, value in items:
        boxes.append(f'<div class="tile"><span>{_esc(label)}</span><strong>{_esc(value)}</strong></div>')
    return '<div class="tiles">' + "".join(boxes) + "</div>"


def render_html(report: dict) -> str:
    """A full HTML document. Charts are inline SVG. No external files or CDN."""
    production = report["production"]
    ai = report["ai"]
    kpis = production["kpis"]
    outcomes = ai["outcomes"]
    total_questions = sum(outcomes.values())
    share_rows = []
    for key, label in (("found", FOUND_LABEL), ("not_found", "Not found"), ("error", "Errors")):
        count = outcomes[key]
        percent = round(100.0 * count / total_questions, 1) if total_questions else 0.0
        share_rows.append((f"{label} ({percent:.1f}%)", count))
    language_rows = [(LANGUAGE_NAMES.get(row["name"], row["name"]), row["count"]) for row in ai["by_language"]]
    sections = [
        "<h2>Production</h2>",
        f"<p class='note'>Defect rate is defective units divided by boards ordered ({kpis['ordered_units']}). Late means the due date is before today and the order is not Shipped.</p>",
        _tiles(kpis),
        svg_bars("Orders by status", [(row["name"], row["count"]) for row in production["orders_by_status"]], "Status", "Orders"),
        svg_bars("Defects by station", [(row["name"], row["units"]) for row in production["defects_by_station"]], "Station", "Defective units"),
        svg_bars("Defects by type", [(row["name"], row["units"]) for row in production["defects_by_type"]], "Defect type", "Defective units"),
        svg_bars("Defects by customer sector", [(row["name"], row["units"]) for row in production["defects_by_sector"]], "Customer sector", "Defective units"),
        svg_line("Weekly defect trend", [(row["label"], row["units"]) for row in production["weekly_defects"]], "Week", "Defective units"),
        "<h3>Top 10 late orders</h3>",
        html_table(
            ["Order", "Sector", "Status", "Due", "Days late"],
            [[row["order_id"], row["customer_sector"], row["status"], row["due_date"], row["days_late"]] for row in production["late_orders"]],
        ),
        "<h3>Last Excel ingestion runs</h3>",
        html_table(
            ["File", "Started", "Loaded", "Rejected"],
            [[row["file_name"], row["started_at"], row["rows_loaded"], row["rows_rejected"]] for row in production["ingest_runs"]],
        ),
        "<h2>AI assistant</h2>",
        "<p class='note'>Counted from the event log. A question is Not found when the assistant had nothing relevant to answer with.</p>",
        svg_line("Questions per day", [(row["day"], row["count"]) for row in ai["questions_per_day"]], "Day", "Questions"),
        svg_bars("Questions by language", language_rows, "Language", "Questions"),
        svg_bars("Question outcomes", share_rows, "Outcome", "Questions"),
        svg_bars("Tool usage", [(row["name"], row["count"]) for row in ai["tools"]], "Tool", "Calls"),
        svg_grouped(
            "Latency by model",
            [row["model"] for row in ai["latency_by_model"]],
            [
                ("Median", [row["median_s"] for row in ai["latency_by_model"]]),
                ("Slowest", [row["slowest_s"] for row in ai["latency_by_model"]]),
            ],
            "Model",
            "Seconds",
        ),
        html_table(
            ["Model", "Questions", "Median seconds", "Slowest seconds"],
            [[row["model"], row["count"], row["median_s"], row["slowest_s"]] for row in ai["latency_by_model"]],
        ),
        "<h3>Questions with no answer found</h3>",
        html_table(
            ["Time", "Language", "Model", "Question"],
            [[row["timestamp"], row["language"], row["model"], row["question"]] for row in ai["unanswered"]],
        ),
    ]
    error = ""
    if production.get("error"):
        error = f"<p class='error'>{_esc(production['error'])}</p>"
    style = """
    body { margin: 0; font: 15px/1.4 "Segoe UI", sans-serif; color: #1c2430; background: #fff; }
    main { padding: 16px 18px 32px; }
    h1 { font-size: 22px; margin: 0 0 4px; }
    h2 { font-size: 18px; margin: 22px 0 8px; border-top: 1px solid #d5dbe3; padding-top: 14px; }
    h3 { font-size: 15px; margin: 16px 0 6px; }
    .when, .note { color: #5c6b7a; margin: 0 0 8px; }
    .error { background: #fde8e6; border: 1px solid #e3b2ac; color: #6d2018; padding: 8px 10px; }
    .tiles { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0 12px; }
    .tile { background: #f4f7fb; border: 1px solid #d5dbe3; border-radius: 8px; padding: 8px 10px; min-width: 110px; }
    .tile span { display: block; color: #5c6b7a; font-size: 12px; }
    .tile strong { font-size: 22px; }
    table { border-collapse: collapse; width: 100%; margin: 0 0 8px; }
    th, td { border: 1px solid #d5dbe3; padding: 4px 6px; text-align: left; vertical-align: top; }
    th { background: #f4f7fb; }
    svg { display: block; margin: 0 0 8px; }
    """
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<title>Factory dashboard</title><style>"
        + style
        + "</style></head><body><main>"
        + "<h1>Factory dashboard</h1>"
        + f"<p class='when'>Generated at {_esc(report['generated_at'])}</p>"
        + f"<p class='when'>Test traffic included: {'yes' if report.get('include_test') else 'no'}</p>"
        + "<p class='note'>Synthetic data. This file has no external images, scripts or fonts.</p>"
        + error
        + "\n".join(sections)
        + "</main></body></html>"
    )


# These columns are dates. Excel must store them as dates, not text.
_DATE_COLUMNS = {"Due": False, "Day": False}
_DATETIME_COLUMNS = {"Started": True, "Time": True}


def _excel_value(header: str, value):
    """Turn an ISO date string into a real Excel date. Other values stay as they are."""
    if header in _DATE_COLUMNS or header in _DATETIME_COLUMNS:
        with_time = header in _DATETIME_COLUMNS
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date) and not with_time:
            return value
        else:
            text = str(value)
            if with_time and "T" in text:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            elif len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
                return date.fromisoformat(text[:10])
            else:
                return value
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return parsed
    return value


def _use_text_categories(chart) -> None:
    """openpyxl writes category labels as numbers. Excel then hides the words."""
    for series in chart.ser:
        category = series.cat
        if category is not None and category.numRef is not None:
            series.cat = AxDataSource(strRef=StrRef(f=category.numRef.f))


def _add_sheet(
    workbook: Workbook,
    title: str,
    headers: list[str],
    rows: list[list],
    chart: str | None = None,
    chart_columns: int | None = None,
    category_axis: str | None = None,
    value_axis: str | None = None,
) -> None:
    """One sheet: a table, and a native Excel chart bound to those cells."""
    sheet = workbook.create_sheet(title)
    sheet.append(headers)
    for row_index, row in enumerate(rows, start=2):
        for column_index, (header, value) in enumerate(zip(headers, row), start=1):
            cell = sheet.cell(row_index, column_index, _excel_value(header, value))
            if isinstance(cell.value, datetime):
                cell.number_format = "YYYY-MM-DD HH:MM"
            elif isinstance(cell.value, date):
                cell.number_format = "YYYY-MM-DD"
    sheet.freeze_panes = "A2"
    for column in sheet.columns:
        letter = column[0].column_letter
        width = max(12, min(62, max(len(str(cell.value or "")) for cell in column) + 2))
        sheet.column_dimensions[letter].width = width
    if not chart or not rows:
        return
    drawing = BarChart() if chart == "bar" else LineChart()
    if chart == "bar":
        drawing.type = "col"
    drawing.title = title
    drawing.x_axis.title = category_axis or headers[0]
    drawing.y_axis.title = value_axis or headers[1]
    # Setting a title used to leave the axis deleted, which hides the scale.
    drawing.x_axis.delete = False
    drawing.y_axis.delete = False
    drawing.style = 10
    drawing.x_axis.delete = False
    drawing.y_axis.delete = False
    last_col = len(headers) if chart_columns is None else 1 + chart_columns
    data = Reference(sheet, min_col=2, max_col=last_col, min_row=1, max_row=1 + len(rows))
    categories = Reference(sheet, min_col=1, min_row=2, max_row=1 + len(rows))
    drawing.add_data(data, titles_from_data=True)
    drawing.set_categories(categories)
    _use_text_categories(drawing)
    labels = DataLabelList()
    labels.showVal = True
    labels.showCatName = False
    labels.showSerName = False
    drawing.dataLabels = labels
    if chart == "bar":
        drawing.shape = 4
    drawing.width = 18
    drawing.height = 8
    if len(drawing.ser) < 2:
        drawing.legend = None
    sheet.add_chart(drawing, "A" + str(len(rows) + 3))


def render_excel(report: dict) -> bytes:
    """Workbook with one sheet per section. Charts point at the cells, so the numbers stay editable."""
    production = report["production"]
    ai = report["ai"]
    kpis = production["kpis"]
    workbook = Workbook()
    overview = workbook.active
    overview.title = "Overview"
    generated = _excel_value("Time", report["generated_at"])
    overview["A1"] = "Generated at"
    overview["B1"] = generated
    if isinstance(generated, datetime):
        overview["B1"].number_format = "YYYY-MM-DD HH:MM"
    overview["A2"] = "Test traffic included"
    overview["B2"] = "yes" if report.get("include_test") else "no"
    overview["A3"] = "Note"
    overview["B3"] = "Synthetic data. Defect rate is defective units divided by boards ordered. Late means due before today and not Shipped."
    if production.get("error"):
        overview["A4"] = "Production error"
        overview["B4"] = production["error"]
    # Rows 7-12 are fixed so the defect-rate formula can point at the two counts.
    overview["A6"] = "KPI"
    overview["B6"] = "Value"
    for row_number, (label, value) in enumerate(
        (
            ("Total orders", kpis["total_orders"]),
            ("Late", kpis["late"]),
            ("On hold", kpis["on_hold"]),
            ("Defective units", kpis["defective_units"]),
            ("Boards ordered", kpis["ordered_units"]),
        ),
        start=7,
    ):
        overview.cell(row_number, 1, label)
        overview.cell(row_number, 2, value)
    overview["A12"] = "Defect rate"
    overview["B12"] = "=IF(B11=0,0,B10/B11)"
    overview["B12"].number_format = "0.0%"
    overview.column_dimensions["A"].width = 28
    overview.column_dimensions["B"].width = 88

    _add_sheet(
        workbook,
        "Orders by status",
        ["Status", "Orders"],
        [[row["name"], row["count"]] for row in production["orders_by_status"]],
        "bar",
        category_axis="Status",
        value_axis="Orders",
    )
    _add_sheet(
        workbook,
        "Defects by station",
        ["Station", "Units"],
        [[row["name"], row["units"]] for row in production["defects_by_station"]],
        "bar",
        category_axis="Station",
        value_axis="Defective units",
    )
    _add_sheet(
        workbook,
        "Defects by type",
        ["Defect type", "Units"],
        [[row["name"], row["units"]] for row in production["defects_by_type"]],
        "bar",
        category_axis="Defect type",
        value_axis="Defective units",
    )
    _add_sheet(
        workbook,
        "Defects by sector",
        ["Customer sector", "Units"],
        [[row["name"], row["units"]] for row in production["defects_by_sector"]],
        "bar",
        category_axis="Customer sector",
        value_axis="Defective units",
    )
    _add_sheet(
        workbook,
        "Weekly defects",
        ["Week", "Units"],
        [[row["label"], row["units"]] for row in production["weekly_defects"]],
        "line",
        category_axis="Week",
        value_axis="Defective units",
    )
    _add_sheet(
        workbook,
        "Late orders",
        ["Order", "Sector", "Status", "Due", "Days late"],
        [[row["order_id"], row["customer_sector"], row["status"], row["due_date"], row["days_late"]] for row in production["late_orders"]],
        None,
    )
    _add_sheet(
        workbook,
        "Ingest runs",
        ["File", "Started", "Loaded", "Rejected"],
        [[row["file_name"], row["started_at"], row["rows_loaded"], row["rows_rejected"]] for row in production["ingest_runs"]],
        None,
    )
    total = sum(ai["outcomes"].values())
    outcome_rows = []
    for key, label in (("found", FOUND_LABEL), ("not_found", "Not found"), ("error", "Errors")):
        count = ai["outcomes"][key]
        percent = round(100.0 * count / total, 1) if total else 0.0
        outcome_rows.append([label, count, percent])
    _add_sheet(
        workbook,
        "Questions per day",
        ["Day", "Questions"],
        [[row["day"], row["count"]] for row in ai["questions_per_day"]],
        "line",
        category_axis="Day",
        value_axis="Questions",
    )
    _add_sheet(
        workbook,
        "Questions by language",
        ["Language", "Questions"],
        [[LANGUAGE_NAMES.get(row["name"], row["name"]), row["count"]] for row in ai["by_language"]],
        "bar",
        category_axis="Language",
        value_axis="Questions",
    )
    _add_sheet(
        workbook,
        "Outcomes",
        ["Outcome", "Questions", "Percent"],
        outcome_rows,
        "bar",
        chart_columns=1,
        category_axis="Outcome",
        value_axis="Questions",
    )
    _add_sheet(
        workbook,
        "Tool usage",
        ["Tool", "Calls"],
        [[row["name"], row["count"]] for row in ai["tools"]],
        "bar",
        category_axis="Tool",
        value_axis="Calls",
    )
    _add_sheet(
        workbook,
        "Latency by model",
        ["Model", "Median seconds", "Slowest seconds"],
        [[row["model"], row["median_s"], row["slowest_s"]] for row in ai["latency_by_model"]],
        "bar",
        category_axis="Model",
        value_axis="Seconds",
    )
    _add_sheet(
        workbook,
        "Unanswered",
        ["Time", "Language", "Model", "Question"],
        [[row["timestamp"], row["language"], row["model"], row["question"]] for row in ai["unanswered"]],
        None,
    )
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def build_html(today=None, include_test: bool = False) -> str:
    return render_html(snapshot(today, include_test=include_test))


def build_excel(today=None, include_test: bool = False) -> tuple[str, bytes]:
    report = snapshot(today, include_test=include_test)
    return report["generated_at"], render_excel(report)
