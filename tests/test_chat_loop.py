"""Tests for the chat tool-calling loop. The model and the tools are fakes."""

import json

from chat.loop import (
    NUDGE,
    SELF_DESCRIPTION,
    ToolTrace,
    hide_reading_label,
    parse_tool_call,
    run_turn,
    system_prompt,
)
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
    assert not any(name == "search_instructions" for name, _arguments in calls)
    assert result.answer.startswith("PO-10049 is In QA")
    order_trace = next(trace for trace in result.traces if trace.name == "get_order_status")
    assert order_trace.result["status"] == "In QA"
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
    calls = []

    def model_fn(messages, tools):
        calls.append(messages)
        return {
            "role": "assistant",
            "content": "Received. I will follow your instructions to answer only the latest message.",
        }

    def tool_fn(name, arguments):
        calls.append((name, arguments))
        assert name == "get_order_status"
        assert arguments == {"order_id": "PO-10049"}
        return {
            "result": {
                "order_id": "PO-10049",
                "status": "In QA",
                "defect_types": [
                    {"defect_type": "Drill Breakout", "count": 1, "units": 25},
                    {"defect_type": "Open", "count": 1, "units": 14},
                ],
            },
            "is_error": False,
        }

    result = run_turn("What type are its defects?", history, TOOLS, "en", model_fn, tool_fn)
    sent = calls[1]
    assert sent[0]["role"] == "system"
    assert sent[1]["content"].startswith("What is the status of PO-10049")
    assert sent[-1]["content"] == "What type are its defects?"
    assert calls[0] == ("get_order_status", {"order_id": "PO-10049"})
    assert "Drill Breakout" in result.answer
    assert "Open" in result.answer
    assert "follow your instructions" not in result.answer.lower()
    assert "latest message" not in result.answer.lower()
    assert not any(trace.name == "search_instructions" for trace in result.traces)


def test_system_prompt_uses_the_question_language():
    assert "Russian" in system_prompt("ru")
    assert "Hebrew" in system_prompt("he")
    assert "English" in system_prompt("en")
    assert "PCB" not in system_prompt("en")
    assert "Factory" in system_prompt("en", template="You help a PCB plant.")
    assert "PCD" not in system_prompt("en", template="You help a PCD line.")
    today = __import__("datetime").date.today().isoformat()
    prompt = system_prompt("en")
    assert f"Today's date is {today}" in prompt
    assert "days_late" in prompt
    assert "Never calculate a date" in prompt
    assert "resolve words such as it" in prompt


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


def test_who_are_you_is_a_fixed_description():
    history = [
        {"role": "user", "content": "Which station had the most defects in the last 30 days?"},
        {"role": "assistant", "content": "AOI had the most defects."},
    ]

    def model_fn(messages, tools):
        raise AssertionError("Who-are-you does not call the model.")

    result = run_turn("who are you", history, TOOLS, "en", model_fn, lambda name, arguments: None)
    assert result.answer == (
        "I am the Factory Assistant. I answer questions about orders, defects, and work instructions."
    )
    assert "AOI" not in result.answer
    assert "Steps below are copied" not in result.answer


def test_a_general_question_is_answered_outside_the_plant_documents():
    def model_fn(messages, tools):
        assert tools == []
        return {"role": "assistant", "content": "Paris is the capital of France."}

    def tool_fn(name, arguments):
        assert name == "search_instructions"
        assert arguments["query"] == "What is the capital of France?"
        return {"result": {"found": False, "results": []}, "is_error": False}

    result = run_turn(
        "What is the capital of France?", [], TOOLS, "en", model_fn, tool_fn, general_knowledge=True
    )
    assert result.traces[0].name == "search_instructions"
    assert result.traces[0].result["found"] is False
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


def test_delamination_questions_return_the_instruction_in_every_language():
    """Russian used to miss the English plant-word list and skip the tool."""
    instruction = """# WI-QA-031 Delamination Containment

## Immediate actions (first 30 minutes)

1. Stop processing the affected lot. Do not rework, re-press or bake the panels.

## Containment scope

- all panels from the same lamination press load (check the press load record);
"""
    questions = (
        ("en", "What should I do if delamination is found?"),
        ("ru", "Что делать, если обнаружена деламинация?"),
        ("he", "מה לעשות אם התגלתה דלמינציה?"),
    )
    for language, question in questions:
        def model_fn(messages, tools, language=language):
            assert language != "en"
            assert tools == []
            assert "Translate" in messages[0]["content"]
            assert "regular language model" not in messages[0]["content"]
            return {"role": "assistant", "content": "Translated steps."}

        def tool_fn(name, arguments, question=question):
            assert name == "search_instructions"
            assert arguments["query"] == question
            return {
                "result": {
                    "found": True,
                    "results": [
                        {
                            "source": "plating-void-investigation.md",
                            "chunk_index": 2,
                            "content": "## Immediate actions\n\n1. Identify all lots that went through the same electroless copper run.",
                            "relevant": True,
                            "similarity": 0.70,
                        },
                        {
                            "source": "delamination-containment.md",
                            "chunk_index": 2,
                            "content": instruction,
                            "relevant": True,
                            "similarity": 0.76,
                        },
                        {
                            "source": "delamination-containment.md",
                            "chunk_index": 3,
                            "content": "## Containment scope\n\n- all panels from the same lamination press load (check the press load record);",
                            "relevant": True,
                            "similarity": 0.68,
                        },
                    ],
                },
                "is_error": False,
            }

        result = run_turn(question, [], TOOLS, language, model_fn, tool_fn, general_knowledge=True)
        assert "delamination-containment.md" in result.answer
        assert "Do not rework, re-press or bake" in result.answer
        assert "Containment scope" in result.answer
        assert "epoxy" not in result.answer.lower()
        assert "electroless" not in result.answer.lower()
        assert "regular language model" not in result.answer
        if language == "en":
            assert "Machine translation" not in result.answer
        else:
            original, marker, translation = result.answer.partition(
                "Machine translation, the original above is binding."
            )
            assert "Do not rework, re-press or bake" in original
            assert marker
            assert "Translated steps." in translation


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
                    {
                        "source": "delamination-containment.md",
                        "chunk_index": 1,
                        "content": instruction,
                        "relevant": True,
                        "similarity": 0.76,
                    },
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


def test_every_search_requests_the_saved_k(monkeypatch):
    question = "What should I do if delamination is found?"
    import chat.settings as settings_module

    original = settings_module.load_settings

    def check(saved_k: int) -> None:
        def fake_settings(saved_k=saved_k):
            values = dict(original())
            values["retrieval_k"] = saved_k
            return values

        monkeypatch.setattr(settings_module, "load_settings", fake_settings)
        seen = []

        def tool_fn(name, arguments):
            seen.append((name, arguments.get("k")))
            if name == "get_order_status":
                return {"result": {"order_id": "PO-10049", "status": "In QA"}, "is_error": False}
            return {"result": {"found": False, "results": []}, "is_error": False}

        model_fn, _seen = scripted_model(
            [
                tool_call("get_order_status", {"order_id": "PO-10049"}),
                answer("PO-10049 is In QA."),
            ]
        )
        run_turn(question, [], TOOLS, "en", model_fn, tool_fn)
        assert ("search_instructions", saved_k) in seen
        assert all(item == ("search_instructions", saved_k) or item[0] == "get_order_status" for item in seen)

    check(6)
    check(4)


def test_answer_routing_regression():
    """Status uses the database. How-to uses the instruction. Chat and off-topic do not."""
    status = "What is the status of order PO-10049?"
    model_fn, _seen = scripted_model(
        [
            tool_call("get_order_status", {"order_id": "PO-10049"}),
            answer("PO-10049 is In QA."),
        ]
    )

    def status_tool(name, arguments):
        if name == "get_order_status":
            return {"result": {"order_id": "PO-10049", "status": "In QA"}, "is_error": False}
        return {
            "result": {
                "found": True,
                "results": [
                    {
                        "source": "order-hold-and-release.md",
                        "chunk_index": 6,
                        "content": "tag, and record who released the order and why.",
                        "relevant": True,
                        "similarity": 0.558,
                    }
                ],
            },
            "is_error": False,
        }

    result = run_turn(status, [], TOOLS, "en", model_fn, status_tool)
    assert result.answer.startswith("PO-10049 is In QA")
    assert "Steps below are copied" not in result.answer
    assert "tag, and record who" not in result.answer
    assert any(trace.name == "get_order_status" for trace in result.traces)
    assert not any(trace.name == "search_instructions" for trace in result.traces)

    how_to = (
        ("en", "What should I do if delamination is found?"),
        ("ru", "Что делать, если обнаружена деламинация?"),
        ("he", "מה לעשות אם התגלתה דלמינציה?"),
    )

    def how_model(messages, tools):
        assert tools == []
        return {"role": "assistant", "content": "Translated steps."}

    def how_tool(name, arguments):
        assert name == "search_instructions"
        return {
            "result": {
                "found": True,
                "results": [
                    {
                        "source": "delamination-containment.md",
                        "chunk_index": 2,
                        "content": "1. Do not rework, re-press or bake the panels.",
                        "relevant": True,
                        "similarity": 0.76,
                    }
                ],
            },
            "is_error": False,
        }

    for language, question in how_to:
        result = run_turn(question, [], TOOLS, language, how_model, how_tool)
        assert "delamination-containment.md" in result.answer
        assert "Do not rework, re-press or bake" in result.answer
        assert "order-hold-and-release" not in result.answer

    def no_model(messages, tools):
        raise AssertionError("This question does not call the model.")

    who = run_turn("who are you", [], TOOLS, "en", no_model, lambda name, arguments: None)
    assert who.answer == SELF_DESCRIPTION["en"]

    def off_tool(name, arguments):
        return {"result": {"found": False, "results": []}, "is_error": False}

    off = run_turn("What is the capital of France?", [], TOOLS, "en", no_model, off_tool)
    assert off.answer == NOT_FOUND["en"]


def test_a_wrong_days_late_calculation_is_replaced_with_the_tool_value():
    assert (__import__("datetime").date(2026, 10, 14) - __import__("datetime").date(2026, 7, 3)).days == 103
    model_fn, _seen = scripted_model(
        [
            tool_call("get_order_status", {"order_id": "PO-10049"}),
            answer("PO-10049 is In QA, 16 days late (due July 3)."),
        ]
    )

    def tool_fn(name, arguments):
        assert name == "get_order_status"
        return {
            "result": {
                "order_id": "PO-10049",
                "status": "In QA",
                "due_date": "2026-07-03",
                "is_late": True,
                "days_late": 103,
            },
            "is_error": False,
        }

    result = run_turn("What is the status of order PO-10049?", [], TOOLS, "en", model_fn, tool_fn)
    assert "103 days late" in result.answer
    assert "16 days" not in result.answer
    assert not any(trace.name == "search_instructions" for trace in result.traces)


def test_bold_markers_are_removed_from_answers():
    from chat.loop import answer_from_tools, plain_answer

    assert plain_answer("The **AOI** station is *first*.") == "The AOI station is first."
    assert "PCB" not in plain_answer("This is a PCB line.")
    assert "Factory" in plain_answer("This is a PCB line.")
    trace = ToolTrace(name="defect_summary", arguments={}, result={"groups": []}, is_error=False)
    answer = answer_from_tools("**AOI** is first.", [trace], "en")
    assert "*" not in answer
    assert "AOI" in answer


def test_page_sets_direction_per_message():
    page = ( __import__("pathlib").Path(__file__).resolve().parent.parent / "chat" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'dir="auto"' in page
    assert "How I got this" in page
    assert "Admin panel" in page
    assert "Settings version" in page
    assert "Ask about an order, defects, or a work instruction. Synthetic Data." in page
    assert "English, Russian or Hebrew" not in page
    assert ">Back</button>" in page
    assert "Back to chat" not in page
    assert "document.querySelector(\".side-bottom\").hidden = focusPage;" in page
    assert "#side .side-bottom[hidden] { display: none; }" in page
    assert 'id="clear-chat"' in page
    assert 'id="export-chat"' in page
    assert 'id="knowledge-base"' in page
    assert 'aria-pressed="false"' in page
    assert "Thinking" in page
    assert "Settings used: k=" in page
    assert 'setAttribute("aria-label", "Copy")' in page
    assert "#question { resize: none; }" in page
    assert "overflow: hidden;" in page
    assert ".chat { flex: 1; min-width: 0; height: 100vh; overflow: auto; }" in page
    assert "height: 100vh;\n      overflow: auto;" in page
