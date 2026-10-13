"""One append-only event log for the factory assistant.

Each line is a single event, oldest first in the file:

    2026-10-13T15:04:01.250+03:00 INFO chat question language=en outcome=found question="..."

The timestamp is ISO 8601 with a timezone. Then level, component, event name,
and key=value details. Values with spaces or punctuation are quoted.
Passwords, secrets and Basic auth headers are never written.
The file rotates by size so it cannot grow without a limit.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path

from rag.language import FAILED, NOT_FOUND

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "logs" / "events.log"

LEVELS = ("INFO", "WARNING", "ERROR")
# Current file plus this many older files (events.log.1 is the newest backup).
BACKUPS = 4
MAX_BYTES = 2_000_000
QUESTION_LIMIT = 200

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,40}$")
_SECRET_KEY = re.compile(r"password|passwd|secret|token|authorization|api_key|credential", re.I)
_SECRET_TEXT = re.compile(r"(?i)(password|passwd|secret|token|api_key|credential)\s*[=:]\s*\S+")
_EVENT_LINE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\S+) "
    r"(?P<level>INFO|WARNING|ERROR) "
    r"(?P<component>[A-Za-z][A-Za-z0-9_.-]*) "
    r"(?P<event>[A-Za-z][A-Za-z0-9_.-]*)"
    r"(?: (?P<rest>.*))?$"
)
_BARE = re.compile(r"^[A-Za-z0-9_.:/+-]+$")

_lock = threading.Lock()
_default: EventLog | None = None


def now_stamp() -> str:
    """Local time with a numeric timezone offset, to the millisecond."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def classify_outcome(answer: str, tool_error: bool) -> str:
    """found, not_found, or error. Used for chat question events."""
    if tool_error or answer in FAILED.values():
        return "error"
    if answer in NOT_FOUND.values():
        return "not_found"
    return "found"


def _limit_for(key: str) -> int:
    lowered = key.lower()
    if lowered in {"question", "message", "query", "text"}:
        return QUESTION_LIMIT
    if lowered == "changes":
        return 2000
    return 500


def _scrub(text: str) -> str:
    """Remove secret assignments that slipped into a free-text value."""
    if text.lower().startswith("basic "):
        return "[redacted]"
    return _SECRET_TEXT.sub(lambda match: match.group(1) + "=[redacted]", text)


def clean(key: str, value):
    """A loggable value. Secret keys become [redacted]. Long questions are cut to 200."""
    if _SECRET_KEY.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(item_key): clean(str(item_key), item) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        cleaned = []
        for item in value:
            if isinstance(item, dict):
                cleaned.append({str(item_key): clean(str(item_key), item) for item_key, item in item.items()})
            else:
                cleaned.append(clean(key, item))
        return cleaned
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(round(value, 2))
    text = _scrub(str(value).replace("\r", " ").replace("\n", " ").strip())
    limit = _limit_for(key)
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def _quote(value: str) -> str:
    if _BARE.match(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def format_line(timestamp: str, level: str, component: str, event: str, details: dict) -> str:
    """One event line. details values are already cleaned strings or JSON-ready objects."""
    if level not in LEVELS:
        raise ValueError(f"Unknown level {level}")
    if not _NAME.match(component) or not _NAME.match(event):
        raise ValueError("Component and event names must be short words.")
    parts = [timestamp, level, component, event]
    for key, value in details.items():
        if value is None or not _NAME.match(str(key)):
            continue
        text = value
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        parts.append(f"{key}={_quote(str(text))}")
    return " ".join(parts)


def parse_details(text: str) -> dict[str, str]:
    """Read key=value pairs. Quoted values may contain spaces."""
    details: dict[str, str] = {}
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index] == " ":
            index += 1
        if index >= length:
            break
        equals = text.find("=", index)
        if equals < 0:
            break
        key = text[index:equals]
        index = equals + 1
        if index < length and text[index] == '"':
            index += 1
            chars: list[str] = []
            while index < length:
                character = text[index]
                if character == "\\" and index + 1 < length:
                    chars.append(text[index + 1])
                    index += 2
                    continue
                if character == '"':
                    index += 1
                    break
                chars.append(character)
                index += 1
            details[key] = "".join(chars)
        else:
            end = text.find(" ", index)
            if end < 0:
                details[key] = text[index:]
                break
            details[key] = text[index:end]
            index = end
    return details


def parse_line(line: str) -> dict | None:
    """One event dict, or None when the line is blank or not an event."""
    stripped = line.strip()
    if not stripped:
        return None
    match = _EVENT_LINE.match(stripped)
    if match is None:
        return None
    parsed = {
        "timestamp": match.group("timestamp"),
        "level": match.group("level"),
        "component": match.group("component"),
        "event": match.group("event"),
    }
    parsed.update(parse_details(match.group("rest") or ""))
    return parsed


class EventLog:
    """A size-capped log directory. Writes are serialised."""

    def __init__(self, path: Path = LOG_PATH, max_bytes: int = MAX_BYTES, backups: int = BACKUPS) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()

    def write(self, level: str, component: str, event: str, **details) -> None:
        cleaned = {}
        for key, value in details.items():
            if value is None:
                continue
            cleaned_value = clean(str(key), value)
            if isinstance(cleaned_value, (dict, list)):
                cleaned[str(key)] = cleaned_value
            else:
                cleaned[str(key)] = cleaned_value
        line = format_line(now_stamp(), level, component, event, cleaned)
        data = (line + "\n").encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size + len(data) > self.max_bytes:
                self._rotate()
            with self.path.open("ab") as handle:
                handle.write(data)

    def _rotate(self) -> None:
        oldest = self.path.with_name(f"{self.path.name}.{self.backups}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self.backups - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            target = self.path.with_name(f"{self.path.name}.{index + 1}")
            if source.exists():
                if target.exists():
                    target.unlink()
                source.replace(target)
        if self.path.exists():
            backup = self.path.with_name(f"{self.path.name}.1")
            if backup.exists():
                backup.unlink()
            self.path.replace(backup)

    def paths_oldest_first(self) -> list[Path]:
        names = [self.path.with_name(f"{self.path.name}.{index}") for index in range(self.backups, 0, -1)]
        names.append(self.path)
        return [path for path in names if path.exists()]

    def read_text(self) -> str:
        """The whole log, oldest event first."""
        with self._lock:
            parts = []
            for path in self.paths_oldest_first():
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
            return "".join(parts)

    def read_events(self, level: str | None = None, component: str | None = None, limit: int | None = None) -> list[dict]:
        """Matching events, newest first when limit is set, otherwise oldest first."""
        matched = []
        for line in self.read_text().splitlines():
            parsed = parse_line(line)
            if parsed is None:
                continue
            if level and parsed["level"] != level:
                continue
            if component and parsed["component"] != component:
                continue
            matched.append(parsed)
        if limit is not None:
            return list(reversed(matched[-limit:]))
        return matched


def get_log() -> EventLog:
    global _default
    with _lock:
        if _default is None:
            _default = EventLog()
        return _default


def log_event(level: str, component: str, event: str, **details) -> None:
    """Append one event to logs/events.log. Safe to call from any thread."""
    try:
        get_log().write(level, component, event, **details)
    except OSError as error:
        # A full disk must not take down the chat.
        print(f"[event-log] {error}")
