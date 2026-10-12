"""Unit tests for answer validation and the retry. No database or Ollama needed."""

import json

import pytest

from rag import ask_local
from rag.ask_local import answer_model_for, ask_model, build_messages, parse_answer
from rag.language import NOT_FOUND

SOURCES = {"delamination-containment.md", "esd-rules.md"}
CHUNKS = [
    {"source": "delamination-containment.md", "chunk_index": 1, "content": "Stop processing the lot."},
    {"source": "esd-rules.md", "chunk_index": 2, "content": "Test your wrist strap every shift."},
]


def reply(
    answer="Stop the lot.",
    sources=("delamination-containment.md",),
    found=True,
    language="en",
    **extra,
) -> str:
    return json.dumps(
        {"language": language, "answer": answer, "sources": list(sources), "found": found, **extra}
    )


def fake_response(content: str) -> dict:
    return {"message": {"role": "assistant", "content": content}}


def test_schema_pins_language_and_requires_the_four_fields():
    schema = answer_model_for("ru").model_json_schema()
    assert schema["required"] == ["language", "answer", "sources", "found"]
    assert schema["additionalProperties"] is False
    language = schema["properties"]["language"]
    assert language.get("const") == "ru" or language.get("enum") == ["ru"]


def test_valid_english_reply_passes():
    parsed = parse_answer(reply(), SOURCES, "en")
    assert parsed.found is True
    assert parsed.language == "en"
    assert parsed.sources == ["delamination-containment.md"]


def test_duplicate_sources_are_removed():
    parsed = parse_answer(reply(sources=["esd-rules.md", "esd-rules.md "]), SOURCES, "en")
    assert parsed.sources == ["esd-rules.md"]


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        '{"answer": "Stop the lot.", "sources": ["esd-rules.md"]',  # cut off
        '{"answer": "x", "found": true}',  # sources and language missing
        reply(extra_field=1),
        reply(answer=""),
        reply(found="maybe"),
        reply(language="fr"),
    ],
)
def test_invalid_reply_is_rejected(content):
    with pytest.raises(ValueError):
        parse_answer(content, SOURCES, "en")


def test_wrong_language_code_is_rejected():
    with pytest.raises(ValueError):
        parse_answer(reply(language="en"), SOURCES, "ru")


def test_english_answer_for_a_russian_question_is_rejected():
    with pytest.raises(ValueError, match="Russian"):
        parse_answer(reply(language="ru", answer="Stop the lot."), SOURCES, "ru")


def test_source_that_was_not_retrieved_is_rejected():
    with pytest.raises(ValueError, match="not excerpt file names"):
        parse_answer(reply(sources=["general-knowledge.md"]), SOURCES, "en")


def test_found_without_sources_is_rejected():
    with pytest.raises(ValueError, match="sources\" is empty"):
        parse_answer(reply(sources=[]), SOURCES, "en")


def test_not_found_with_sources_is_rejected():
    with pytest.raises(ValueError, match="not empty"):
        parse_answer(reply(found=False), SOURCES, "en")


@pytest.mark.parametrize("language", ["en", "ru", "he"])
def test_not_found_answer_is_replaced_by_the_fixed_text(language):
    parsed = parse_answer(
        reply(language=language, answer="Paris is the capital of France.", sources=[], found=False),
        SOURCES,
        language,
    )
    assert parsed.answer == NOT_FOUND[language]
    assert parsed.sources == []
    assert parsed.language == language


def test_messages_contain_question_source_names_and_language():
    user_message = build_messages("Что делать?", CHUNKS, "ru")[1]["content"]
    assert "source: delamination-containment.md" in user_message
    assert "source: esd-rules.md" in user_message
    assert "Question: Что делать?" in user_message
    assert '"language": "ru"' in user_message
    assert NOT_FOUND["ru"] in user_message


def test_invalid_reply_is_retried_once_with_feedback(monkeypatch):
    replies = iter([fake_response("{broken"), fake_response(reply())])
    sent = []

    def fake_call_model(messages, think, schema):
        sent.append((messages, schema))
        return next(replies)

    monkeypatch.setattr(ask_local, "call_model", fake_call_model)
    parsed, responses, language = ask_model("What now?", CHUNKS, think=False)

    assert parsed is not None and parsed.found
    assert language == "en"
    assert len(responses) == 2
    assert sent[0][1]["properties"]["language"].get("const", sent[0][1]["properties"]["language"].get("enum")) in (
        "en",
        ["en"],
    )
    assert sent[1][0][-2] == {"role": "assistant", "content": "{broken"}
    assert sent[1][0][-1]["content"].startswith("Your reply was invalid")


def test_gives_up_after_the_retry(monkeypatch):
    calls = []

    def fake_call_model(messages, think, schema):
        calls.append(1)
        return fake_response("still not json")

    monkeypatch.setattr(ask_local, "call_model", fake_call_model)
    parsed, responses, language = ask_model("Что делать?", CHUNKS, think=False)

    assert parsed is None
    assert language == "ru"
    assert len(calls) == 2
