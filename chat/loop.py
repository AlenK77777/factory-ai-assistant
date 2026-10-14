"""One chat turn: the local model may call MCP tools, then it answers.

The model function and the tool function are passed in, so tests can use fakes.
No database and no Ollama here.
"""

import json
import re
from dataclasses import dataclass, field

from rag.language import FAILED, LANGUAGE_NAMES, NOT_FOUND, Language

# A question can need two tools (the order, then the instruction).
MAX_TOOL_ROUNDS = 4

NUDGE = (
    "Answer only the latest user message. Do not answer an earlier question again. "
    "Call a tool only if that latest message asks about an order, defects, or a procedure. "
    "If it does not, answer that message with no tool. "
    "Do not invent numbers, statuses or procedure steps."
)

# Small talk is not a plant question. Answer it without tools.
_CHITCHAT = re.compile(
    r"^\s*(?:"
    r"hi|hello|hey|thanks|thank you|thx|"
    r"who\s*(?:are|r|'re)?\s*(?:you|u)|"
    r"who\s*r\s*u|"
    r"what(?:'s| is)\s+your\s+name|"
    r"what\s+are\s+you|"
    r"how\s+are\s+you|"
    r"how\s+r\s+u"
    r")\s*[?.!]*\s*$",
    re.IGNORECASE,
)

# A follow-up can say "its defects" without repeating plant words.
_FOLLOWUP = re.compile(r"\b(it|its|it's|that|this|those|them|their|the same)\b", re.IGNORECASE)
# Orders, defects and procedures stay on the tool path. Other questions do not.
_PLANT = re.compile(
    r"\b(order|orders|defect|defects|station|status|late|hold|procedure|instruction|"
    r"rule|limit|delamination|esd|aoi|plating|solder|drill|etch|lamination|qa|po-\d+)\b",
    re.IGNORECASE,
)

MODE_SWITCH = {
    "en": (
        "I am switching to ordinary chat mode and answering as a regular language model, "
        "not as the factory assistant. This is general knowledge, not retrieved from the plant documents."
    ),
    "ru": (
        "Я перехожу в обычный режим и отвечаю как языковая модель, а не как заводской помощник. "
        "Это общие сведения, а не ответ из заводских документов."
    ),
    "he": (
        "אני עובר למצב רגיל ועונה כמודל שפה, לא כעוזר המפעל. "
        "זה ידע כללי, לא תשובה שנשלפה ממסמכי המפעל."
    ),
}

GENERAL_PROMPT = (
    "You are a regular language model, not the factory assistant. "
    "Answer the latest user message from general knowledge in {language}. "
    "Do not mention factory orders, defect counts, or work-instruction steps."
)

_READING_LABEL = re.compile(r"(?i)\b(?:your|my)\s+reading\s*:\s*")
_READING_BULLET = re.compile(
    r"- Keep a fact from a tool separate from your own reading\.[^\n]*\n?",
    re.IGNORECASE,
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
    # Set for answers that are not classified from the text, such as chit-chat.
    outcome: str | None = None


def is_chitchat(question: str) -> bool:
    """True for greetings and 'who are you' style messages, not plant questions."""
    return _CHITCHAT.match(question.strip()) is not None


def wants_general_knowledge(question: str, history: list[dict]) -> bool:
    """Small talk and questions that are not about the plant.

    A short follow-up such as "what about its defects?" stays on the tool path.
    """
    if is_chitchat(question):
        return True
    if _PLANT.search(question):
        return False
    if history and _FOLLOWUP.search(question):
        return False
    return True


def general_knowledge_answer(model_text: str, language: Language) -> str:
    """Not-found line, then a normal answer that is clearly not from the plant documents."""
    body = hide_reading_label(model_text).strip() or FAILED[language]
    return f"{NOT_FOUND[language]}\n{MODE_SWITCH[language]}\n{body}"


def hide_reading_label(text: str) -> str:
    """Drop the 'Your reading:' label so it never appears in an answer."""
    return _READING_LABEL.sub("", text).strip()


def system_prompt(language: Language, template: str | None = None) -> str:
    """Rules for the plant chat. {language} and {not_found} come from the question."""
    if template is None:
        from chat.settings import load_settings

        template = load_settings()["system_prompt"]
    text = template.replace("{language}", LANGUAGE_NAMES[language]).replace("{not_found}", NOT_FOUND[language])
    text = _READING_BULLET.sub("", text)
    if "defective units" not in text:
        text += (
            "\n- When you rank stations, defect types, or customer sectors, rank by defective units "
            "(the sum of quantities), not by the number of records, and say so in the answer.\n"
        )
    if "latest user message" not in text:
        text += "\n- Answer only the latest user message. Do not answer an earlier question again.\n"
    return text


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
    return hide_reading_label(content) or FAILED[language]


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
    if wants_general_knowledge(question, history):
        # No tools and no earlier plant turns, so this cannot repeat the previous answer.
        prompt = GENERAL_PROMPT.replace("{language}", LANGUAGE_NAMES[language])
        message = model_fn(
            [{"role": "system", "content": prompt}, {"role": "user", "content": question}],
            [],
        )
        content = (message.get("content") or "").strip()
        return TurnResult(
            answer=general_knowledge_answer(content, language),
            language=language,
            traces=[],
            outcome="not_found",
        )

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
