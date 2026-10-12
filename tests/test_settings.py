"""Validation, change summary and the re-index decision. No Ollama or database."""

import json

from chat import settings as settings_module
from chat.settings import (
    DEFAULTS,
    MEASURED_MIN_K,
    embedding_model_changed,
    needs_reindex,
    reindex_confirmation_error,
    review,
    save_settings,
    summarize_changes,
    warnings_for,
)

INSTALLED = ["qwen3:8b", "qwen3:14b", "qwen3-embedding:0.6b", "nomic-embed-text:latest"]


def values(**changes) -> dict:
    base = {key: DEFAULTS[key] for key in DEFAULTS if key != "version"}
    base.update(changes)
    return base


def test_rejects_values_outside_their_range():
    result = review(values(), values(temperature=3, retrieval_k=0, chunk_overlap=500), INSTALLED)
    assert result["ok"] is False
    text = " ".join(result["errors"])
    assert "Temperature" in text
    assert "k must be between" in text
    assert "Overlap must be smaller" in text
    assert result["changes"] == []


def test_unknown_model_is_rejected():
    result = review(values(), values(chat_model="missing:1b"), INSTALLED)
    assert result["ok"] is False
    assert any("not installed" in error for error in result["errors"])


def test_k_below_the_measured_minimum_warns_and_still_applies():
    proposed = values(retrieval_k=4)
    result = review(values(), proposed, INSTALLED)
    assert result["ok"] is True
    assert result["errors"] == []
    assert any(str(MEASURED_MIN_K) in note for note in result["warnings"])
    assert result["needs_reindex"] is False
    assert [change["key"] for change in result["changes"]] == ["retrieval_k"]


def test_threshold_for_an_uncalibrated_model_warns():
    notes = warnings_for(values(embedding_model="nomic-embed-text:latest", similarity_threshold=0.48))
    assert any("No similarity threshold has been measured" in note for note in notes)
    assert any("0.48" in note and "qwen3-embedding:0.6b" in note for note in notes)


def test_summary_lists_only_what_changed_and_marks_reindex():
    changes = summarize_changes(values(), values(temperature=0.2, chunk_size=800, system_prompt="New rules"))
    by_key = {change["key"]: change for change in changes}
    assert set(by_key) == {"temperature", "chunk_size", "system_prompt"}
    assert by_key["temperature"]["before"] == 0.0
    assert by_key["temperature"]["after"] == 0.2
    assert by_key["temperature"]["reindex"] is False
    assert by_key["chunk_size"]["reindex"] is True
    assert "characters" in by_key["system_prompt"]["after"]


def test_reindex_decision():
    assert needs_reindex(values(), values(temperature=0.4)) is False
    assert needs_reindex(values(), values(chunking_mode="fixed")) is True
    assert needs_reindex(values(), values(chunk_overlap=80)) is True
    assert embedding_model_changed(values(), values(embedding_model="nomic-embed-text:latest")) is True
    assert embedding_model_changed(values(), values(chunk_size=700)) is False


def test_reindex_requires_confirmation():
    assert reindex_confirmation_error(True, False) == "Re-indexing needs confirmation."
    assert reindex_confirmation_error(True, True) is None
    assert reindex_confirmation_error(False, False) is None


def test_review_of_an_embedding_change_asks_for_a_vector_check():
    result = review(values(), values(embedding_model="nomic-embed-text:latest"), INSTALLED)
    assert result["ok"] is True
    assert result["needs_reindex"] is True
    assert result["check_vector_size"] is True


def test_saved_file_drops_secrets(tmp_path, monkeypatch):
    path = tmp_path / "chat_settings.json"
    monkeypatch.setattr(settings_module, "SETTINGS_PATH", path)
    dirty = values()
    dirty["postgres_password"] = "super-secret"
    dirty["MCP_DB_PASSWORD"] = "also-secret"
    save_settings(dirty, version=2)
    text = path.read_text(encoding="utf-8")
    assert "super-secret" not in text
    assert "also-secret" not in text
    stored = json.loads(text)
    assert stored["version"] == 2
    assert "postgres_password" not in stored
    assert stored["chat_model"] == DEFAULTS["chat_model"]


def test_identical_settings_have_an_empty_summary():
    assert summarize_changes(values(), values()) == []
