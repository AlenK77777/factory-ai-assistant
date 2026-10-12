"""Tunable chat settings. Defaults live here; the saved copy is data/chat_settings.json.

Passwords are not settings. They stay in .env and are never written to this file.
"""

import json
import re
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "data" / "chat_settings.json"

# Measured on the EN/RU/HE question set after structure-aware chunking.
MEASURED_MIN_K = 6
CALIBRATED_THRESHOLD = {"qwen3-embedding:0.6b": 0.48}

CHUNKING_MODES = ("structure", "fixed")

DEFAULT_SYSTEM_PROMPT = """You are the assistant for employees of a PCB plant. The data and instructions are synthetic.

Write the answer in {language}.

Rules:
- Facts about orders, defects and work instructions come only from tool results in this conversation. Do not answer those facts from memory.
- For an order or for defects, call get_order_status, list_late_orders or defect_summary.
- For a question about what to do, a rule, a limit or a procedure, call search_instructions with the user's full question before you answer, even if the question seems unrelated to the plant.
- Keep a fact from a tool separate from your own reading. Give the facts first. If you add an interpretation, put it after the facts and label it as your reading, not as a plant rule.
- For a procedure, give only the 3 to 5 most important actions. If the excerpts list immediate actions, start with those. End with the source file name.
- Keep the answer short.
- If a tool says the order does not exist, or search_instructions has found=false, or the returned text does not answer the question, say so plainly. For a missing instruction use exactly: {not_found}
- A follow-up refers to the earlier turns. Call a tool again when the follow-up needs data.
"""

# key -> spec used by the admin panel and by validation.
FIELDS = (
    {
        "key": "chat_model",
        "group": "Chat",
        "label": "Chat model",
        "help": "Local Ollama model that answers and calls tools. Change this to compare qwen3:8b and qwen3:14b.",
        "kind": "model",
        "default": "qwen3:8b",
    },
    {
        "key": "temperature",
        "group": "Chat",
        "label": "Temperature",
        "help": "0 repeats the same answer. Higher values vary more. Thinking mode was tried at 0.6.",
        "kind": "number",
        "default": 0.0,
        "minimum": 0,
        "maximum": 2,
        "step": 0.1,
    },
    {
        "key": "think",
        "group": "Chat",
        "label": "Think",
        "help": "Let qwen3 reason before answering. Slower. Tool calls were tested with thinking off.",
        "kind": "bool",
        "default": False,
    },
    {
        "key": "num_ctx",
        "group": "Chat",
        "label": "Context size",
        "help": "How many tokens the model can read at once, including the tool results.",
        "kind": "number",
        "default": 8192,
        "minimum": 512,
        "maximum": 32768,
        "step": 256,
    },
    {
        "key": "keep_alive",
        "group": "Chat",
        "label": "Keep alive",
        "help": "How long Ollama keeps the model loaded after an answer. Examples: 5m, 30s, 0, -1 (stay loaded).",
        "kind": "text",
        "default": "5m",
    },
    {
        "key": "history_turns",
        "group": "Chat",
        "label": "History turns",
        "help": "Earlier question-and-answer pairs sent with the next question, so a follow-up can say “its defects”.",
        "kind": "number",
        "default": 4,
        "minimum": 0,
        "maximum": 20,
        "step": 1,
    },
    {
        "key": "system_prompt",
        "group": "Chat",
        "label": "System prompt",
        "help": "Rules for the assistant. {language} becomes English, Russian or Hebrew. {not_found} becomes the fixed “nothing relevant” sentence.",
        "kind": "textarea",
        "default": DEFAULT_SYSTEM_PROMPT.strip(),
    },
    {
        "key": "retrieval_k",
        "group": "Retrieval",
        "label": "k",
        "help": "How many instruction chunks are searched. The model cannot lower this. Measured minimum for the delamination steps is 6.",
        "kind": "number",
        "default": MEASURED_MIN_K,
        "minimum": 1,
        "maximum": 20,
        "step": 1,
    },
    {
        "key": "similarity_threshold",
        "group": "Retrieval",
        "label": "Similarity threshold",
        "help": "A chunk at or above this score is marked relevant. 0.48 was measured for qwen3-embedding:0.6b. It is a hint, not proof.",
        "kind": "number",
        "default": 0.48,
        "minimum": 0,
        "maximum": 1,
        "step": 0.01,
    },
    {
        "key": "embedding_model",
        "group": "Indexing",
        "label": "Embedding model",
        "help": "Turns questions and instructions into vectors. Changing it re-indexes, and migrates the table if the vector size changes.",
        "kind": "model",
        "default": "qwen3-embedding:0.6b",
    },
    {
        "key": "chunking_mode",
        "group": "Indexing",
        "label": "Chunking",
        "help": "structure keeps each Markdown section and never cuts a numbered list. fixed cuts every chunk to the size below.",
        "kind": "select",
        "default": "structure",
        "options": list(CHUNKING_MODES),
    },
    {
        "key": "chunk_size",
        "group": "Indexing",
        "label": "Chunk size",
        "help": "Character limit. In structure mode it only splits a long section that is not a numbered list.",
        "kind": "number",
        "default": 500,
        "minimum": 100,
        "maximum": 8000,
        "step": 50,
    },
    {
        "key": "chunk_overlap",
        "group": "Indexing",
        "label": "Overlap",
        "help": "Characters repeated on the next piece when the size fallback cuts a long paragraph. Must be smaller than the chunk size.",
        "kind": "number",
        "default": 50,
        "minimum": 0,
        "maximum": 2000,
        "step": 10,
    },
)

TUNABLE = tuple(field["key"] for field in FIELDS)
REINDEX_KEYS = ("embedding_model", "chunking_mode", "chunk_size", "chunk_overlap")
LABELS = {field["key"]: field["label"] for field in FIELDS}
DEFAULTS = {field["key"]: field["default"] for field in FIELDS}
DEFAULTS["version"] = 1

_KEEP_ALIVE = re.compile(r"^(-1|0|[1-9][0-9]*(?:\.\d+)?(?:ms|s|m|h)?)$")
_lock = threading.Lock()


def field_specs() -> list[dict]:
    """Descriptions for the admin panel. Defaults and ranges, no secrets."""
    specs = []
    for field in FIELDS:
        specs.append(
            {
                "key": field["key"],
                "group": field["group"],
                "label": field["label"],
                "help": field["help"],
                "kind": field["kind"],
                "default": field["default"],
                "minimum": field.get("minimum"),
                "maximum": field.get("maximum"),
                "step": field.get("step"),
                "options": field.get("options", []),
            }
        )
    return specs


def _blank(key: str) -> dict:
    return {item: DEFAULTS[item] for item in TUNABLE}


def load_settings() -> dict:
    """Saved values, or the defaults in code when no file exists or the file is unusable."""
    values = _blank("x")
    values["version"] = DEFAULTS["version"]
    path = SETTINGS_PATH
    if not path.exists():
        return values
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return values
    if not isinstance(stored, dict):
        return values
    for key in TUNABLE:
        if key in stored:
            values[key] = stored[key]
    cleaned, errors = validate(values, installed_models=None)
    if errors or cleaned is None:
        return {**_blank("x"), "version": DEFAULTS["version"]}
    cleaned["version"] = int(stored.get("version", DEFAULTS["version"]))
    return cleaned


def save_settings(values: dict, version: int) -> dict:
    """Write only the tunable keys and the version. Extra keys, including passwords, are dropped."""
    document = {key: values[key] for key in TUNABLE}
    document["version"] = int(version)
    path = SETTINGS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)
    return document


def _number(raw, key: str, integer: bool) -> tuple[object, str | None]:
    if isinstance(raw, bool) or raw is None:
        return raw, f"{LABELS[key]} must be a number."
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return raw, f"{LABELS[key]} must be a number."
    if integer and not number.is_integer():
        return raw, f"{LABELS[key]} must be a whole number."
    return (int(number) if integer else number), None


def validate(raw: dict, installed_models: list[str] | None) -> tuple[dict | None, list[str]]:
    """Return (cleaned values, errors). Errors block Apply. installed_models=None skips the model-list check."""
    if not isinstance(raw, dict):
        return None, ["Settings must be an object."]
    errors: list[str] = []
    cleaned: dict = {}

    def require(key: str) -> object:
        if key not in raw:
            errors.append(f"{LABELS[key]} is missing.")
            return None
        return raw[key]

    chat_model = require("chat_model")
    if chat_model is not None:
        chat_model = str(chat_model).strip()
        if not chat_model:
            errors.append("Chat model is empty.")
        elif installed_models is not None and chat_model not in installed_models:
            errors.append(f"Chat model {chat_model} is not installed in Ollama.")
        cleaned["chat_model"] = chat_model

    temperature, error = _number(require("temperature"), "temperature", integer=False)
    if error and "temperature" in raw:
        errors.append(error)
    elif "temperature" in raw:
        field = _field("temperature")
        if not field["minimum"] <= temperature <= field["maximum"]:
            errors.append(f"Temperature must be between {field['minimum']} and {field['maximum']}.")
        else:
            cleaned["temperature"] = float(temperature)

    think = require("think")
    if "think" in raw:
        if not isinstance(think, bool):
            errors.append("Think must be on or off.")
        else:
            cleaned["think"] = think

    num_ctx, error = _number(require("num_ctx"), "num_ctx", integer=True)
    if error and "num_ctx" in raw:
        errors.append(error)
    elif "num_ctx" in raw:
        field = _field("num_ctx")
        if not field["minimum"] <= num_ctx <= field["maximum"]:
            errors.append(f"Context size must be between {field['minimum']} and {field['maximum']}.")
        else:
            cleaned["num_ctx"] = num_ctx

    keep_alive = require("keep_alive")
    if keep_alive is not None:
        keep_alive = str(keep_alive).strip()
        if not _KEEP_ALIVE.match(keep_alive):
            errors.append("Keep alive must look like 5m, 30s, 0 or -1.")
        else:
            cleaned["keep_alive"] = keep_alive

    turns, error = _number(require("history_turns"), "history_turns", integer=True)
    if error and "history_turns" in raw:
        errors.append(error)
    elif "history_turns" in raw:
        field = _field("history_turns")
        if not field["minimum"] <= turns <= field["maximum"]:
            errors.append(f"History turns must be between {field['minimum']} and {field['maximum']}.")
        else:
            cleaned["history_turns"] = turns

    prompt = require("system_prompt")
    if prompt is not None:
        prompt = str(prompt).strip()
        if not prompt:
            errors.append("System prompt is empty.")
        elif len(prompt) > 20000:
            errors.append("System prompt is longer than 20000 characters.")
        else:
            cleaned["system_prompt"] = prompt

    retrieval_k, error = _number(require("retrieval_k"), "retrieval_k", integer=True)
    if error and "retrieval_k" in raw:
        errors.append(error)
    elif "retrieval_k" in raw:
        field = _field("retrieval_k")
        if not field["minimum"] <= retrieval_k <= field["maximum"]:
            errors.append(f"k must be between {field['minimum']} and {field['maximum']}.")
        else:
            cleaned["retrieval_k"] = retrieval_k

    threshold, error = _number(require("similarity_threshold"), "similarity_threshold", integer=False)
    if error and "similarity_threshold" in raw:
        errors.append(error)
    elif "similarity_threshold" in raw:
        field = _field("similarity_threshold")
        if not field["minimum"] <= threshold <= field["maximum"]:
            errors.append(f"Similarity threshold must be between {field['minimum']} and {field['maximum']}.")
        else:
            cleaned["similarity_threshold"] = float(threshold)

    embedding_model = require("embedding_model")
    if embedding_model is not None:
        embedding_model = str(embedding_model).strip()
        if not embedding_model:
            errors.append("Embedding model is empty.")
        elif installed_models is not None and embedding_model not in installed_models:
            errors.append(f"Embedding model {embedding_model} is not installed in Ollama.")
        cleaned["embedding_model"] = embedding_model

    mode = require("chunking_mode")
    if mode is not None:
        mode = str(mode).strip()
        if mode not in CHUNKING_MODES:
            errors.append("Chunking must be structure or fixed.")
        else:
            cleaned["chunking_mode"] = mode

    chunk_size, error = _number(require("chunk_size"), "chunk_size", integer=True)
    if error and "chunk_size" in raw:
        errors.append(error)
    elif "chunk_size" in raw:
        field = _field("chunk_size")
        if not field["minimum"] <= chunk_size <= field["maximum"]:
            errors.append(f"Chunk size must be between {field['minimum']} and {field['maximum']}.")
        else:
            cleaned["chunk_size"] = chunk_size

    overlap, error = _number(require("chunk_overlap"), "chunk_overlap", integer=True)
    if error and "chunk_overlap" in raw:
        errors.append(error)
    elif "chunk_overlap" in raw and "chunk_size" in cleaned:
        field = _field("chunk_overlap")
        if overlap < field["minimum"] or overlap > field["maximum"]:
            errors.append(f"Overlap must be between {field['minimum']} and {field['maximum']}.")
        elif overlap >= cleaned["chunk_size"]:
            errors.append("Overlap must be smaller than the chunk size.")
        else:
            cleaned["chunk_overlap"] = overlap

    if errors:
        return None, errors
    return cleaned, []


def warnings_for(values: dict) -> list[str]:
    """Measurements the value disagrees with. These do not block Apply."""
    notes = []
    if int(values["retrieval_k"]) < MEASURED_MIN_K:
        notes.append(
            f"k is {values['retrieval_k']}. The measured minimum that still retrieves "
            f"the delamination steps is {MEASURED_MIN_K}."
        )
    model = values["embedding_model"]
    measured = CALIBRATED_THRESHOLD.get(model)
    threshold = float(values["similarity_threshold"])
    if measured is None:
        known = "qwen3-embedding:0.6b"
        notes.append(
            f"No similarity threshold has been measured for {model}. "
            f"{CALIBRATED_THRESHOLD[known]} was measured for {known}."
        )
    elif abs(threshold - measured) > 0.001:
        notes.append(
            f"The measured similarity threshold for {model} is {measured}. "
            f"The panel value is {threshold}."
        )
    if values["think"] and float(values["temperature"]) == 0:
        notes.append("Thinking is on with temperature 0. qwen3 can repeat itself; 0.6 was used when thinking was tried.")
    elif values["think"]:
        notes.append("Thinking is on. Answers take longer, and tool calls were tested with thinking off.")
    return notes


def summarize_changes(old: dict, new: dict) -> list[dict]:
    """Only the fields that differ. Re-index fields are marked."""
    changes = []
    for key in TUNABLE:
        before, after = old.get(key), new.get(key)
        if before == after:
            continue
        shown_before, shown_after = before, after
        if key == "system_prompt":
            shown_before = f"{len(str(before))} characters"
            shown_after = f"{len(str(after))} characters"
        changes.append(
            {
                "key": key,
                "label": LABELS[key],
                "before": shown_before,
                "after": shown_after,
                "reindex": key in REINDEX_KEYS,
            }
        )
    return changes


def needs_reindex(old: dict, new: dict) -> bool:
    """True when the instruction index would no longer match the new settings."""
    return any(old.get(key) != new.get(key) for key in REINDEX_KEYS)


def embedding_model_changed(old: dict, new: dict) -> bool:
    """True when the vector size must be checked and the table migrated if it differs."""
    return old.get("embedding_model") != new.get("embedding_model")


def review(old: dict, new: dict, installed_models: list[str] | None) -> dict:
    """Validation, warnings and the change summary. Nothing is saved."""
    cleaned, errors = validate(new, installed_models)
    if errors or cleaned is None:
        return {
            "ok": False,
            "errors": errors,
            "warnings": [],
            "changes": [],
            "needs_reindex": False,
            "check_vector_size": False,
        }
    changes = summarize_changes(old, cleaned)
    return {
        "ok": True,
        "errors": [],
        "warnings": warnings_for(cleaned),
        "changes": changes,
        "needs_reindex": needs_reindex(old, cleaned),
        "check_vector_size": embedding_model_changed(old, cleaned),
        "values": cleaned,
    }


def reindex_confirmation_error(needs_reindex_flag: bool, confirmed: bool) -> str | None:
    """Block an unconfirmed re-index. Other applies do not need this flag."""
    if needs_reindex_flag and not confirmed:
        return "Re-indexing needs confirmation."
    return None


def _field(key: str) -> dict:
    for field in FIELDS:
        if field["key"] == key:
            return field
    raise KeyError(key)


def update_saved(values: dict) -> dict:
    """Increment the version and write the file. Caller has already validated."""
    with _lock:
        current = load_settings()
        return save_settings(values, version=int(current["version"]) + 1)
