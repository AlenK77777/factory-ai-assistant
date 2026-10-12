"""Unit tests for answer validation and the retry. No database or Ollama needed."""

import json

import pytest

from rag import ask_local
from rag.ask_local import ANSWER_SCHEMA, NOT_FOUND_ANSWER, ask_model, build_messages, parse_answer

SOURCES = {"delamination-containment.md", "esd-rules.md"}
CHUNKS = [
    {"source": "delamination-containment.md", "chunk_index": 1, "content": "Stop processing the lot."},
    {"source": "esd-rules.md", "chunk_index": 2, "content": "Test your wrist strap every shift."},
]


def reply(answer="Stop the lot.", sources=("delamination-containment.md",), found=True, **extra) -> str:
    return json.dumps({"answer": answer, "sources": list(sources), "found": found, **extra})


def fake_response(content: str) -> dict:
    return {"message": {"role": "assistant", "content": content}}


def test_schema_requires_exactly_the_three_fields():
    assert set(ANSWER_SCHEMA["required"]) == {"answer", "sources", "found"}
    assert ANSWER_SCHEMA["additionalProperties"] is False


def test_valid_reply_passes():
    answer = parse_answer(reply(), SOURCES)
    assert answer.found is True
    assert answer.sources == ["delamination-containment.md"]


def test_duplicate_sources_are_removed():
    answer = parse_answer(reply(sources=["esd-rules.md", "esd-rules.md "]), SOURCES)
    assert answer.sources == ["esd-rules.md"]


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        '{"answer": "Stop the lot.", "sources": ["esd-rules.md"]',  # cut off
        '{"answer": "x", "found": true}',  # sources missing
        reply(extra_field=1),
        reply(answer=""),
        reply(found="maybe"),
    ],
)
def test_invalid_reply_is_rejected(content):
    with pytest.raises(ValueError):
        parse_answer(content, SOURCES)


def test_source_that_was_not_retrieved_is_rejected():
    with pytest.raises(ValueError, match="not excerpt file names"):
        parse_answer(reply(sources=["general-knowledge.md"]), SOURCES)


def test_found_without_sources_is_rejected():
    with pytest.raises(ValueError, match="sources\" is empty"):
        parse_answer(reply(sources=[]), SOURCES)


def test_not_found_with_sources_is_rejected():
    with pytest.raises(ValueError, match="not empty"):
        parse_answer(reply(found=False), SOURCES)


def test_not_found_answer_is_replaced_by_the_fixed_text():
    answer = parse_answer(reply(answer="Paris is the capital of France.", sources=[], found=False), SOURCES)
    assert answer.answer == NOT_FOUND_ANSWER
    assert answer.sources == []


def test_messages_contain_question_and_source_names():
    user_message = build_messages("What now?", CHUNKS)[1]["content"]
    assert "source: delamination-containment.md" in user_message
    assert "source: esd-rules.md" in user_message
    assert user_message.endswith("Question: What now?")


def test_invalid_reply_is_retried_once_with_feedback(monkeypatch):
    replies = iter([fake_response("{broken"), fake_response(reply())])
    sent = []

    def fake_call_model(messages, think):
        sent.append(messages)
        return next(replies)

    monkeypatch.setattr(ask_local, "call_model", fake_call_model)
    answer, responses = ask_model("What now?", CHUNKS, think=False)

    assert answer is not None and answer.found
    assert len(responses) == 2
    assert sent[1][-2] == {"role": "assistant", "content": "{broken"}
    assert sent[1][-1]["content"].startswith("Your reply was invalid")


def test_gives_up_after_the_retry(monkeypatch):
    calls = []

    def fake_call_model(messages, think):
        calls.append(1)
        return fake_response("still not json")

    monkeypatch.setattr(ask_local, "call_model", fake_call_model)
    answer, responses = ask_model("What now?", CHUNKS, think=False)

    assert answer is None
    assert len(calls) == 2
