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


def system_prompt(language: Language) -> str:
    """Rules for the plant chat. The language is fixed for this question."""
    name = LANGUAGE_NAMES[language]
    return f"""You are the assistant for employees of a PCB plant. The data and instructions are synthetic.

Write the answer in {name}.

Rules:
- Facts about orders, defects and work instructions come only from tool results in this conversation. Do not answer those facts from memory.
- For an order or for defects, call get_order_status, list_late_orders or defect_summary.
- For a question about what to do, a rule, a limit or a procedure, call search_instructions with the user's full question before you answer, even if the question seems unrelated to the plant.
- Keep a fact from a tool separate from your own reading. Give the facts first. If you add an interpretation, put it after the facts and label it as your reading, not as a plant rule.
- For a procedure, give only the 3 to 5 most important actions. If the excerpts list immediate actions, start with those. End with the source file name.
- Keep the answer short.
- If a tool says the order does not exist, or search_instructions has found=false, or the returned text does not answer the question, say so plainly. For a missing instruction use exactly: {NOT_FOUND[language]}
- A follow-up refers to the earlier turns. Call a tool again when the follow-up needs data.
"""


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
