"""Event log line format, secret filtering and size rotation. No database."""

import json
import re

from chat.event_log import LOG_PATH, EventLog, classify_outcome, event_source, get_log, parse_line, request_source
from rag.language import FAILED, NOT_FOUND


def test_line_has_timestamp_level_component_and_event(tmp_path):
    log = EventLog(tmp_path / "events.log")
    log.write("INFO", "chat", "question", language="en", outcome="found", question="Status of PO-1")
    parsed = parse_line(log.read_text())
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}", parsed["timestamp"])
    assert parsed["level"] == "INFO"
    assert parsed["component"] == "chat"
    assert parsed["event"] == "question"
    assert parsed["language"] == "en"
    assert parsed["outcome"] == "found"
    assert parsed["question"] == "Status of PO-1"


def test_quoted_values_and_tool_arguments_round_trip(tmp_path):
    log = EventLog(tmp_path / "events.log")
    tools = [{"name": "get_order_status", "arguments": {"order_id": "PO-1", "query": 'say "hello"'}}]
    log.write("INFO", "chat", "question", question='What is "late"?', tools=tools)
    parsed = parse_line(log.read_text())
    assert parsed["question"] == 'What is "late"?'
    assert json.loads(parsed["tools"]) == [
        {"name": "get_order_status", "arguments": {"order_id": "PO-1", "query": 'say "hello"'}}
    ]


def test_secrets_and_long_questions_are_not_stored(tmp_path):
    log = EventLog(tmp_path / "events.log")
    secret = "super-secret-value"
    header = "Basic YWRtaW46c3VwZXItc2VjcmV0"
    log.write(
        "WARNING",
        "admin",
        "login",
        password=secret,
        authorization=header,
        detail=f"password={secret} sent",
        question="Q" * 250,
    )
    text = log.read_text()
    assert secret not in text
    assert header not in text
    assert "Basic " not in text
    parsed = parse_line(text)
    assert parsed["password"] == "[redacted]"
    assert parsed["authorization"] == "[redacted]"
    assert "=[redacted]" in parsed["detail"]
    assert len(parsed["question"]) == 200
    assert parsed["question"].endswith("...")


def test_rotation_keeps_older_lines_first(tmp_path):
    log = EventLog(tmp_path / "events.log", max_bytes=220, backups=2)
    log.write("INFO", "server", "start", marker="first-event")
    for index in range(30):
        log.write("INFO", "server", "health", marker=f"m{index:02d}", detail="x" * 30)
    log.write("INFO", "server", "stop", marker="last-event")
    assert (tmp_path / "events.log.1").exists()
    text = log.read_text()
    assert "last-event" in text
    older = (tmp_path / "events.log.1").read_text().splitlines()[0]
    newer = (tmp_path / "events.log").read_text().splitlines()[0]
    assert text.index(older) < text.index(newer)
    assert log.path.stat().st_size <= 220 + 400


def test_filter_returns_newest_first(tmp_path):
    log = EventLog(tmp_path / "events.log")
    log.write("INFO", "server", "start")
    log.write("ERROR", "chat", "question", outcome="error")
    log.write("INFO", "chat", "question", outcome="found")
    errors = log.read_events(level="ERROR")
    assert len(errors) == 1
    assert errors[0]["outcome"] == "error"
    latest = log.read_events(component="chat", limit=1)
    assert latest[0]["outcome"] == "found"


def test_request_source_rejects_unknown_and_blank_is_user():
    assert request_source(None) == "user"
    assert request_source("") == "user"
    assert request_source("smoke") == "smoke"
    assert request_source("test") == "test"
    assert request_source("admin") is None


def test_a_line_without_source_is_legacy_and_the_file_is_not_rewritten(tmp_path):
    log = EventLog(tmp_path / "events.log")
    log.write("INFO", "chat", "question", outcome="found", question="old")
    original = log.read_text()
    parsed = parse_line(original)
    assert "source" not in parsed
    assert event_source(parsed) == "legacy"
    assert log.read_text() == original


def test_pytest_does_not_use_the_project_event_log():
    assert get_log().path != LOG_PATH


def test_outcome_classes():
    assert classify_outcome("The order is in QA.", False) == "found"
    assert classify_outcome(NOT_FOUND["en"], False) == "not_found"
    assert classify_outcome(NOT_FOUND["he"], False) == "not_found"
    assert classify_outcome(FAILED["ru"], False) == "error"
    assert classify_outcome("something", True) == "error"
