"""Tests for the chat tool-calling loop. The model and the tools are fakes."""

import json

from chat.loop import NUDGE, parse_tool_call, run_turn, system_prompt
from rag.language import NOT_FOUND

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_order_status",
            "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}},
        },
    }
]


def scripted_model(replies: list[dict]):
    """A model that returns the next scripted assistant message and records what it saw."""
    seen = []

    def model_fn(messages, tools):
        seen.append((list(messages), tools))
        return replies.pop(0)

    return model_fn, seen


def tool_call(name: str, arguments) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


def answer(text: str) -> dict:
    return {"role": "assistant", "content": text}


def test_calls_a_tool_then_answers_from_its_result():
    model_fn, seen = scripted_model(
        [
            tool_call("get_order_status", {"order_id": "PO-10049"}),
            answer("PO-10049 is In QA. Defects: Short, 2 records."),
        ]
    )
    calls = []

    def tool_fn(name, arguments):
        calls.append((name, arguments))
        return {"result": {"order_id": "PO-10049", "status": "In QA"}, "is_error": False}

    result = run_turn("What is the status of PO-10049?", [], TOOLS, "en", model_fn, tool_fn)

    assert calls == [("get_order_status", {"order_id": "PO-10049"})]
    assert result.answer.startswith("PO-10049 is In QA")
    assert result.traces[0].name == "get_order_status"
    assert result.traces[0].result["status"] == "In QA"
    tool_messages = [message for message in seen[1][0] if message["role"] == "tool"]
    assert json.loads(tool_messages[0]["content"])["status"] == "In QA"
    assert seen[0][1] == TOOLS


def test_string_arguments_are_parsed():
    name, arguments = parse_tool_call(
        {"function": {"name": "get_order_status", "arguments": '{"order_id": "PO-10049"}'}}
    )
    assert name == "get_order_status"
    assert arguments == {"order_id": "PO-10049"}


def test_a_reply_without_a_tool_is_nudged_once():
    model_fn, seen = scripted_model(
        [
            answer("I think it is shipped."),
            answer("Nothing relevant was found."),
        ]
    )
    result = run_turn("Tell me the status.", [], TOOLS, "en", model_fn, lambda name, arguments: None)

    assert result.answer == NOT_FOUND["en"]
    assert result.traces == []
    assert seen[1][0][-1]["content"] == NUDGE


def test_history_is_sent_with_the_question():
    history = [
        {"role": "user", "content": "What is the status of PO-10049?"},
        {"role": "assistant", "content": "PO-10049 is In QA, with 2 defects."},
    ]
    # The first reply uses no tool, so the loop asks once more. The history is on the first call.
    model_fn, seen = scripted_model([answer("Short, 2 records."), answer("Short, 2 records.")])
    result = run_turn("What type are its defects?", history, TOOLS, "en", model_fn, lambda n, a: None)

    sent = seen[0][0]
    assert sent[0]["role"] == "system"
    assert sent[1]["content"].startswith("What is the status of PO-10049")
    assert sent[-1]["content"] == "What type are its defects?"
    assert "In QA" in sent[2]["content"]
    assert result.answer == NOT_FOUND["en"]


def test_system_prompt_uses_the_question_language():
    assert "Russian" in system_prompt("ru")
    assert "Hebrew" in system_prompt("he")
    assert "English" in system_prompt("en")


def test_tool_error_is_recorded_and_shown_to_the_model():
    model_fn, seen = scripted_model(
        [
            tool_call("get_order_status", {"order_id": "PO-99999"}),
            answer("Order PO-99999 was not found."),
        ]
    )

    def tool_fn(name, arguments):
        return {"result": "Order PO-99999 was not found.", "is_error": True}

    result = run_turn("Status of PO-99999?", [], TOOLS, "en", model_fn, tool_fn)

    assert result.traces[0].is_error is True
    assert "not found" in seen[1][0][-1]["content"]
    assert result.answer == "Order PO-99999 was not found."


def test_stops_after_too_many_tool_rounds():
    model_fn, _seen = scripted_model([tool_call("get_order_status", {"order_id": "PO-1"})] * 6)
    result = run_turn("status", [], TOOLS, "en", model_fn, lambda n, a: {"result": {}, "is_error": False})
    assert result.answer.startswith("The local model did not return a valid answer")
    assert len(result.traces) == 4


def test_a_search_that_finds_nothing_cannot_invent_an_answer():
    model_fn, _seen = scripted_model(
        [
            tool_call("search_instructions", {"query": "capital of France"}),
            answer("Paris, and bribery is illegal."),
        ]
    )

    def tool_fn(name, arguments):
        return {"result": {"found": False, "results": []}, "is_error": False}

    result = run_turn("מהי בירת צרפת?", [], TOOLS, "he", model_fn, tool_fn)
    assert result.answer == NOT_FOUND["he"]
    assert result.traces[0].name == "search_instructions"


def test_page_sets_direction_per_message():
    page = ( __import__("pathlib").Path(__file__).resolve().parent.parent / "chat" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'dir="auto"' in page
    assert "How I got this" in page
    assert "Admin panel" in page
    assert "Settings version" in page
