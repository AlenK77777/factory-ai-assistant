"""Tests for the chat tool-calling loop. The model and the tools are fakes."""

import json

from chat.loop import NUDGE, ToolTrace, hide_reading_label, parse_tool_call, run_turn, system_prompt
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
            tool_call("search_instructions", {"query": "delamination"}),
            answer("Invented step: ignore it."),
        ]
    )

    def tool_fn(name, arguments):
        return {"result": {"found": False, "results": []}, "is_error": False}

    result = run_turn("What should I do if delamination is found?", [], TOOLS, "en", model_fn, tool_fn)
    assert result.answer == NOT_FOUND["en"]
    assert result.traces[0].name == "search_instructions"


def test_who_are_you_does_not_repeat_the_previous_tool_answer():
    history = [
        {"role": "user", "content": "Which station had the most defects in the last 30 days?"},
        {"role": "assistant", "content": "AOI had the most defects."},
    ]
    seen = {}

    def model_fn(messages, tools):
        seen["tools"] = tools
        seen["history"] = [item["content"] for item in messages if item["role"] != "system"]
        return {"role": "assistant", "content": "I am a regular language model."}

    def tool_fn(name, arguments):
        raise AssertionError("Chit-chat must not call a tool.")

    result = run_turn("who r u", history, TOOLS, "en", model_fn, tool_fn)
    assert result.traces == []
    assert result.outcome == "not_found"
    assert seen["tools"] == []
    assert seen["history"] == ["who r u"]
    assert result.answer.startswith(NOT_FOUND["en"])
    assert "regular language model" in result.answer
    assert "not as the factory assistant" in result.answer
    assert "I am a regular language model." in result.answer
    assert "AOI" not in result.answer


def test_a_general_question_is_answered_outside_the_plant_documents():
    def model_fn(messages, tools):
        assert tools == []
        return {"role": "assistant", "content": "Paris is the capital of France."}

    def tool_fn(name, arguments):
        raise AssertionError("A general question must not call a tool.")

    result = run_turn("What is the capital of France?", [], TOOLS, "en", model_fn, tool_fn)
    assert result.traces == []
    assert result.outcome == "not_found"
    assert result.answer.startswith(NOT_FOUND["en"])
    assert "not retrieved from the plant documents" in result.answer
    assert "Paris is the capital of France." in result.answer


def test_your_reading_label_is_removed_from_the_answer():
    assert hide_reading_label("AOI is first.\nYour reading: this is only a guess.") == (
        "AOI is first.\nthis is only a guess."
    )
    trace = ToolTrace(name="defect_summary", arguments={}, result={"groups": []}, is_error=False)
    from chat.loop import answer_from_tools

    assert "Your reading:" not in answer_from_tools("Facts.\nYour reading: extra.", [trace], "en")
    assert "Your reading" not in system_prompt("en", template="Rules.\n- Keep a fact from a tool separate from your own reading. Give the facts first.\nDone.")


def test_a_false_defect_claim_is_corrected_before_the_procedure():
    question = "PO-10049 has delamination, what should I do?"
    instruction = """# WI-QA-031 Delamination Containment

## Immediate actions (first 30 minutes)

1. Stop processing the affected lot. Do not rework, re-press or bake the panels.
2. Attach a red HOLD tag to every affected panel and move the lot to quarantine rack Q2.

## Containment scope

Delamination usually affects more than one panel. Also put on hold:

- all panels from the same lamination press load (check the press load record);

## Disposition

- The Material Review Board (MRB) decides within 1 working day.
- For Defense, Aerospace and Medical orders, delaminated boards are always scrapped. "Use as is" is not allowed.
"""
    model_fn, _seen = scripted_model(
        [
            tool_call("get_order_status", {"order_id": "PO-10049"}),
            tool_call("search_instructions", {"query": question}),
            answer("Scrap the boards now. Skip containment."),
        ]
    )

    def tool_fn(name, arguments):
        if name == "get_order_status":
            return {
                "result": {
                    "order_id": "PO-10049",
                    "defect_types": [
                        {"defect_type": "Drill Breakout", "count": 1, "units": 25},
                        {"defect_type": "Open", "count": 1, "units": 10},
                    ],
                },
                "is_error": False,
            }
        return {
            "result": {
                "found": True,
                "results": [
                    {"source": "delamination-containment.md", "chunk_index": 1, "content": instruction, "relevant": True},
                ],
            },
            "is_error": False,
        }

    result = run_turn(question, [], TOOLS, "en", model_fn, tool_fn)
    first, _, rest = result.answer.partition("\n")
    assert first.startswith("The order record does not show delamination for PO-10049.")
    assert "Drill Breakout and Open" in first
    assert "Do not rework, re-press or bake" in result.answer
    assert "Containment scope" in result.answer
    assert "same lamination press load" in result.answer
    assert "Scrap the boards now" not in result.answer
    mrb = result.answer.lower().find("material review board")
    scrap = result.answer.lower().find("scrapped")
    assert mrb >= 0 and scrap > mrb


def test_page_sets_direction_per_message():
    page = ( __import__("pathlib").Path(__file__).resolve().parent.parent / "chat" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'dir="auto"' in page
    assert "How I got this" in page
    assert "Admin panel" in page
    assert "Settings version" in page
