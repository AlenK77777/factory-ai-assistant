"""One chat turn: the local model may call MCP tools, then it answers.

The model function and the tool function are passed in, so tests can use fakes.
No database and no Ollama here.
"""

import json
from dataclasses import dataclass, field

from rag.language import FAILED, LANGUAGE_NAMES, NOT_FOUND, Language

# A question can need two tools (the order, then the instruction).
MAX_TOOL_ROUNDS = 4

NUDGE = (
    "You replied without using a tool. If the question is about an order or defects, call "
    "get_order_status, list_late_orders or defect_summary. If it asks what to do, or about a "
    "rule, a limit or a procedure, call search_instructions with the question, even if it seems "
    "unrelated. If a tool then shows nothing relevant, say so plainly in the required language. "
    "Do not invent numbers, statuses or procedure steps."
)


@dataclass
class ToolTrace:
    name: str
    arguments: dict
    result: object
    is_error: bool


@dataclass
class TurnResult:
    answer: str
    language: str
    traces: list[ToolTrace] = field(default_factory=list)


def system_prompt(language: Language, template: str | None = None) -> str:
    """Rules for the plant chat. {language} and {not_found} come from the question."""
    if template is None:
        from chat.settings import load_settings

        template = load_settings()["system_prompt"]
    return template.replace("{language}", LANGUAGE_NAMES[language]).replace("{not_found}", NOT_FOUND[language])


def parse_tool_call(call: dict) -> tuple[str, dict]:
    """Read one Ollama tool call. Arguments may be a dict or a JSON string."""
    function = call.get("function") or {}
    name = str(function.get("name") or "").strip()
    arguments = function.get("arguments") or {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as error:
            raise ValueError(f"tool arguments are not valid JSON: {error}") from error
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    if not name:
        raise ValueError("tool call has no name")
    return name, arguments


def answer_from_tools(content: str, traces: list[ToolTrace], language: Language) -> str:
    """Keep the model's words only when a tool returned something it can use.

    No tool call, or a search that found nothing, is replaced by the fixed
    "nothing relevant" sentence. That blocks an answer invented from memory.
    """
    if not traces:
        return NOT_FOUND[language]
    searches = [
        trace
        for trace in traces
        if trace.name == "search_instructions" and not trace.is_error and isinstance(trace.result, dict)
    ]
    other_facts = [trace for trace in traces if trace.name != "search_instructions" and not trace.is_error]
    if searches and not other_facts and all(trace.result.get("found") is False for trace in searches):
        return NOT_FOUND[language]
    return content or FAILED[language]


def run_turn(
    question: str,
    history: list[dict],
    tools: list[dict],
    language: Language,
    model_fn,
    tool_fn,
) -> TurnResult:
    """Ask the model, run any tool calls, and return the answer plus a trace.

    model_fn(messages, tools) returns the assistant message dict.
    tool_fn(name, arguments) returns {"result": ..., "is_error": bool}.
    history is earlier user and assistant texts only, oldest first.
    """
    messages: list[dict] = [{"role": "system", "content": system_prompt(language)}]
    messages.extend(history)
    messages.append({"role": "user", "content": question})
    traces: list[ToolTrace] = []
    nudged = False

    for _ in range(MAX_TOOL_ROUNDS):
        message = model_fn(messages, tools)
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            messages.append(message)
            for call in tool_calls:
                try:
                    name, arguments = parse_tool_call(call)
                except ValueError as error:
                    name = str((call.get("function") or {}).get("name") or "")
                    arguments = {}
                    outcome = {"result": str(error), "is_error": True}
                else:
                    try:
                        outcome = tool_fn(name, arguments)
                    except Exception as error:  # noqa: BLE001 - a tool failure is shown, not a crash
                        outcome = {"result": str(error), "is_error": True}
                traces.append(
                    ToolTrace(
                        name=name,
                        arguments=arguments,
                        result=outcome.get("result"),
                        is_error=bool(outcome.get("is_error")),
                    )
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_name": name,
                        "content": json.dumps(outcome.get("result"), ensure_ascii=False, default=str),
                    }
                )
            continue

        content = (message.get("content") or "").strip()
        # One chance to call a tool before an answer that used none.
        if not traces and not nudged:
            nudged = True
            if content:
                messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": NUDGE})
            continue
        return TurnResult(
            answer=answer_from_tools(content, traces, language),
            language=language,
            traces=traces,
        )

    return TurnResult(answer=FAILED[language], language=language, traces=traces)
