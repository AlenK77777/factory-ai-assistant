"""One chat turn: the local model may call MCP tools, then it answers.

The model function and the tool function are passed in, so tests can use fakes.
No database and no Ollama here.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date

from rag.language import FAILED, LANGUAGE_NAMES, NOT_FOUND, Language

# A question can need two tools (the order, then the instruction).
MAX_TOOL_ROUNDS = 4

NUDGE = (
    "Call a tool for the latest message before you answer it. "
    "Earlier messages only show which order a word like its refers to."
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


# Shown above a Russian or Hebrew rendering of an English work instruction.
TRANSLATION_MARKER = "Machine translation, the original above is binding."

_TRANSLATE = (
    "Translate the work instruction below into {language}. "
    "Keep every section and every numbered step. Do not add, remove, or change a step. "
    "Do not add advice. Output only the translation."
)

# An order question still needs get_order_status even after the instruction search.
_ORDER_ID = re.compile(r"\bPO-\d+\b", re.IGNORECASE)
# Verbatim steps are only for "what should I do" / "how do I". Status questions are not.
_HOW_TO = re.compile(
    r"("
    r"\bwhat\s+should\s+(?:i|we)\s+do\b"
    r"|\bwhat\s+(?:do\s+i|to)\s+do\b"
    r"|\bhow\s+(?:do|should|can)\s+i\b"
    r"|\bhow\s+to\b"
    r"|что\s+делать"
    r"|как\s+(?:мне\s+)?(?:делать|сделать|поступить)"
    r"|מה\s+לעשות"
    r"|איך(?:\s+|$)"
    r")",
    re.IGNORECASE,
)
_SELF_QUESTION = re.compile(
    r"who\s*(?:are|r|'re)?\s*(?:you|u)|who\s*r\s*u|what\s+are\s+you",
    re.IGNORECASE,
)
_DATA = re.compile(r"\b(status|defect|defects|late|how many|which station)\b", re.IGNORECASE)
_INTERNAL = re.compile(
    r"follow your instructions|only the latest|latest user message|latest message|"
    r"do not answer an earlier|do not invent numbers|call a tool for the latest",
    re.IGNORECASE,
)
_DAYS_LATE = re.compile(r"\b\d+\s+days?\s+late\b", re.IGNORECASE)
_OLD_LATEST = re.compile(r"- Answer only the latest user message\.[^\n]*\n?", re.IGNORECASE)
SELF_DESCRIPTION = {
    "en": "I am the Factory Assistant. I answer questions about orders, defects, and work instructions.",
    "ru": "Я Factory Assistant. Я отвечаю на вопросы о заказах, дефектах и рабочих инструкциях.",
    "he": "אני Factory Assistant. אני עונה על שאלות על הזמנות, פגמים והוראות עבודה.",
}


def asks_for_procedure(question: str) -> bool:
    """True when the user asks what to do or how to do something."""
    return _HOW_TO.search(question) is not None


def is_data_question(question: str) -> bool:
    """Status, defects, and similar questions. They are not answered from instructions."""
    return not asks_for_procedure(question) and _DATA.search(question) is not None


def referenced_order_id(question: str, history: list[dict]) -> str:
    """Order id in the question, or the latest one in history when the question says its/that."""
    found = _ORDER_ID.search(question)
    if found:
        return found.group(0).upper()
    if not history or not _FOLLOWUP.search(question):
        return ""
    for message in reversed(history):
        earlier = _ORDER_ID.findall(str(message.get("content") or ""))
        if earlier:
            return earlier[-1].upper()
    return ""


def wants_general_knowledge(question: str, history: list[dict]) -> bool:
    """Small talk and questions that are not about the plant.

    A short follow-up such as "what about its defects?" stays on the tool path.
    The plant-word list is English only. A Russian or Hebrew procedure question
    does not match it. That used to skip tools and answer from general knowledge.
    The server searches the original question before this choice is trusted.
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
    return plain_answer(f"{NOT_FOUND[language]}\n{MODE_SWITCH[language]}\n{body}")


def hide_reading_label(text: str) -> str:
    """Drop the 'Your reading:' label so it never appears in an answer."""
    return _READING_LABEL.sub("", text).strip()


def plain_answer(text: str) -> str:
    """Show answers as plain text.

    The page does not render markdown, so bold markers show up as asterisks.
    PCD and PCB are written as Factory.
    """
    return text.replace("**", "").replace("__", "").replace("*", "").replace("PCD", "Factory").replace("PCB", "Factory")


def system_prompt(language: Language, template: str | None = None) -> str:
    """Rules for the plant chat. {language} and {not_found} come from the question."""
    if template is None:
        from chat.settings import load_settings

        template = load_settings()["system_prompt"]
    text = template.replace("{language}", LANGUAGE_NAMES[language]).replace("{not_found}", NOT_FOUND[language])
    text = text.replace("PCD", "Factory").replace("PCB", "Factory")
    text = _READING_BULLET.sub("", text)
    if "defective units" not in text:
        text += (
            "\n- When you rank stations, defect types, or customer sectors, rank by defective units "
            "(the sum of quantities), not by the number of records, and say so in the answer.\n"
        )
    latest = (
        "- Answer only the latest user message. Use earlier messages only to resolve words such as it, its, or that. "
        "Do not answer an earlier question again.\n"
    )
    if _OLD_LATEST.search(text):
        text = _OLD_LATEST.sub(latest, text)
    elif "resolve words such as it" not in text:
        text += "\n" + latest
    if "days_late" not in text:
        text += (
            f"- Today's date is {date.today().isoformat()}. Never calculate a date or a number of days. "
            "If a tool returns days_late, use that number.\n"
        )
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


# Defect names the order record can contradict. Matched only when the user asserts one.
_CLAIM = re.compile(
    r"\b(?:has|have|with|shows|showing)\b(?:\s+\w+){0,5}\s+"
    r"(drill breakout|solder mask defect|plating void|misregistration|delamination|short|open)\b",
    re.IGNORECASE,
)


def _recorded_defect_types(traces: list[ToolTrace]) -> tuple[str, list[str]]:
    """Order id and defect type names from get_order_status, if that tool ran."""
    for trace in traces:
        if trace.name != "get_order_status" or trace.is_error or not isinstance(trace.result, dict):
            continue
        order_id = str(trace.result.get("order_id") or "")
        names = []
        for item in trace.result.get("defect_types") or []:
            if isinstance(item, dict) and item.get("defect_type"):
                names.append(str(item["defect_type"]))
        return order_id, names
    return "", []


def contradiction_line(question: str, traces: list[ToolTrace]) -> str:
    """First line when the user asserts a defect the order record does not list."""
    claimed = []
    for match in _CLAIM.finditer(question):
        kind = match.group(1).casefold()
        if kind not in claimed:
            claimed.append(kind)
    if not claimed:
        return ""
    order_id, recorded = _recorded_defect_types(traces)
    if not order_id and not recorded:
        return ""
    recorded_fold = {name.casefold() for name in recorded}
    missing = [kind for kind in claimed if kind not in recorded_fold]
    if not missing:
        return ""
    if not recorded:
        shown = "no defect types"
    elif len(recorded) == 1:
        shown = recorded[0]
    else:
        shown = ", ".join(recorded[:-1]) + " and " + recorded[-1]
    which = " or ".join(missing)
    return f"The order record does not show {which} for {order_id}. It shows {shown}."


def _instruction_chunks(traces: list[ToolTrace]) -> list[dict]:
    """Chunks for one instruction file whose search marked them relevant.

    Relevance uses the saved similarity threshold. There is no second cutoff.
    A Russian question can rank another procedure first. The file with the most
    relevant chunks wins, and its text on disk keeps the later sections.
    """
    rows: list[dict] = []
    for trace in traces:
        if trace.name != "search_instructions" or trace.is_error or not isinstance(trace.result, dict):
            continue
        if trace.result.get("found") is False:
            continue
        for row in trace.result.get("results") or []:
            if isinstance(row, dict) and row.get("content"):
                rows.append(row)
    # `relevant` was set with the saved similarity threshold. There is no second cutoff.
    relevant = [row for row in rows if row.get("relevant") is True]
    if not relevant:
        return []
    pool = relevant
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for index, row in enumerate(pool):
        source = str(row.get("source") or "")
        counts[source] = counts.get(source, 0) + 1
        first_seen.setdefault(source, index)
    source = max(counts, key=lambda name: (counts[name], -first_seen[name]))
    full_text = _instruction_file(source)
    if full_text:
        return [{"source": source, "chunk_index": 0, "content": full_text, "relevant": True}]
    chosen = [row for row in rows if str(row.get("source") or "") == source]
    chosen.sort(key=lambda row: int(row.get("chunk_index") or 0))
    return chosen


def _instruction_file(source: str) -> str:
    """The instruction file named by retrieval, when it is a plain file name in docs/instructions."""
    from pathlib import Path

    name = Path(source).name
    if not name or name != source or name in {".", ".."}:
        return ""
    root = Path(__file__).resolve().parent.parent
    folder = (root / "docs" / "instructions").resolve()
    path = (folder / name).resolve()
    if path.parent != folder or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _sections_from_chunks(chunks: list[dict]) -> tuple[str, str, list[tuple[str, list[str]]]]:
    """Document title, source file, and sections in instruction order. Lines stay verbatim."""
    title = ""
    source = str(chunks[0].get("source") or "") if chunks else ""
    sections: list[tuple[str, list[str]]] = []
    by_title: dict[str, list[str]] = {}
    current: list[str] | None = None
    for chunk in chunks:
        if not source:
            source = str(chunk.get("source") or "")
        for raw in str(chunk.get("content") or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                heading = line.lstrip("#").strip()
                if raw.strip().startswith("# ") and not raw.strip().startswith("##") and not title:
                    title = heading
                    current = None
                    continue
                current = by_title.get(heading)
                if current is None:
                    current = []
                    by_title[heading] = current
                    sections.append((heading, current))
                continue
            if current is None:
                continue
            if line not in current:
                current.append(line)
    return title, source, sections


def format_procedure(traces: list[ToolTrace]) -> str:
    """Numbered steps copied from the retrieved instruction, grouped by section.

    The summary does not state a disposition. Scrap or use-as-is text stays in the
    section that contains the MRB step, which the instruction places first there.
    """
    chunks = _instruction_chunks(traces)
    title, source, sections = _sections_from_chunks(chunks)
    body_sections = [(heading, lines) for heading, lines in sections if lines]
    if not body_sections:
        return ""
    summary = "Steps below are copied from the work instruction"
    if source:
        summary += f" {source}"
    if title:
        summary += f" ({title})"
    summary += "."
    parts = [summary]
    for heading, lines in body_sections:
        parts.append(heading + "\n" + "\n".join(lines))
    return "\n\n".join(parts)


def without_internal(text: str) -> str:
    """Drop a reply that restates a hidden instruction instead of answering."""
    if _INTERNAL.search(text):
        return ""
    return text


def _order_result(traces: list[ToolTrace]) -> dict | None:
    for trace in traces:
        if trace.name == "get_order_status" and not trace.is_error and isinstance(trace.result, dict):
            return trace.result
    return None


def apply_days_late(text: str, traces: list[ToolTrace]) -> str:
    """Replace a model-computed lateness with days_late from the order tool."""
    order = _order_result(traces)
    if not order or not order.get("is_late") or order.get("days_late") is None:
        return text
    days = int(order["days_late"])
    if _DAYS_LATE.search(text):
        return _DAYS_LATE.sub(f"{days} days late", text, count=1)
    return text


def _defect_text(order: dict) -> str:
    parts = []
    for item in order.get("defect_types") or []:
        if isinstance(item, dict) and item.get("defect_type"):
            parts.append(f"{item['defect_type']}: {item.get('count')} records, {item.get('units')} defective units")
    order_id = order.get("order_id") or "The order"
    if not parts:
        return f"{order_id} has no defect types in the order record."
    return f"{order_id} defect types: " + "; ".join(parts) + "."


def _status_text(order: dict) -> str:
    order_id = order.get("order_id") or "The order"
    status = order.get("status") or "unknown"
    if order.get("is_late") and order.get("days_late") is not None:
        return f"{order_id} is {status}. {int(order['days_late'])} days late (due {order.get('due_date')})."
    return f"{order_id} is {status}."


def data_answer(question: str, traces: list[ToolTrace], content: str) -> str:
    """Use the order tool for a data question. Do not keep an internal instruction."""
    cleaned = apply_days_late(without_internal(content), traces)
    if cleaned:
        return cleaned
    order = _order_result(traces)
    if not order:
        return ""
    if re.search(r"\bdefect", question, re.IGNORECASE):
        return _defect_text(order)
    return _status_text(order)


def answer_from_tools(content: str, traces: list[ToolTrace], language: Language, question: str = "") -> str:
    """Keep the model's words only when a tool returned something it can use.

    No tool call, or a search that found nothing, is replaced by the fixed
    "nothing relevant" sentence. That blocks an answer invented from memory.
    A work instruction is copied from the chunks. A contradicted defect claim
    is stated before that text.
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
    # The search trace is context. Steps are the answer only for a how-to question.
    procedure = format_procedure(traces) if asks_for_procedure(question) else ""
    note = contradiction_line(question, traces)
    if procedure:
        body = procedure
    elif is_data_question(question):
        body = data_answer(question, traces, hide_reading_label(content)) or NOT_FOUND[language]
    else:
        body = without_internal(hide_reading_label(content)) or FAILED[language]
    if note:
        body = note + "\n\n" + body
    return plain_answer(body)


def _call_named(name: str, arguments: dict, tool_fn) -> ToolTrace:
    try:
        outcome = tool_fn(name, arguments)
    except Exception as error:  # noqa: BLE001 - a tool failure is recorded, not a crash
        return ToolTrace(name, arguments, str(error), True)
    if not isinstance(outcome, dict):
        return ToolTrace(name, arguments, outcome, True)
    return ToolTrace(name, arguments, outcome.get("result"), bool(outcome.get("is_error")))


def _server_search(question: str, tool_fn) -> ToolTrace:
    """Search the user's original question with the saved panel k. The model is not asked to do this."""
    from chat.settings import load_settings

    arguments = {"query": question, "k": int(load_settings()["retrieval_k"])}
    try:
        outcome = tool_fn("search_instructions", arguments)
    except Exception as error:  # noqa: BLE001 - a search failure is recorded, not a crash
        return ToolTrace("search_instructions", arguments, str(error), True)
    if not isinstance(outcome, dict):
        return ToolTrace("search_instructions", arguments, {"found": False, "results": []}, False)
    return ToolTrace(
        "search_instructions",
        arguments,
        outcome.get("result"),
        bool(outcome.get("is_error")),
    )


def _append_translation(answer: str, procedure: str, language: Language, model_fn) -> str:
    """Keep the English steps, then a marked translation for Russian and Hebrew."""
    if language == "en" or not procedure:
        return answer
    prompt = _TRANSLATE.replace("{language}", LANGUAGE_NAMES[language])
    try:
        message = model_fn(
            [{"role": "system", "content": prompt}, {"role": "user", "content": procedure}],
            [],
        )
    except Exception:  # noqa: BLE001 - the original steps still stand if translation fails
        return answer
    translated = plain_answer((message.get("content") or "").strip())
    if not translated:
        return answer
    return f"{answer}\n\n{TRANSLATION_MARKER}\n{translated}"


def run_turn(
    question: str,
    history: list[dict],
    tools: list[dict],
    language: Language,
    model_fn,
    tool_fn,
    general_knowledge: bool = False,
) -> TurnResult:
    """Ask the model, run any tool calls, and return the answer plus a trace.

    model_fn(messages, tools) returns the assistant message dict.
    tool_fn(name, arguments) returns {"result": ..., "is_error": bool}.
    history is earlier user and assistant texts only, oldest first.

    search_instructions runs for how-to and other non-data questions. A status
    or defect question is answered from the database tool, including a follow-up
    that says its and points at an earlier order id.
    """
    traces: list[ToolTrace] = []
    if is_data_question(question):
        order_id = referenced_order_id(question, history)
        if order_id and not _ORDER_ID.search(question):
            traces.append(_call_named("get_order_status", {"order_id": order_id}, tool_fn))
    else:
        traces.append(_server_search(question, tool_fn))
    if _SELF_QUESTION.search(question):
        return TurnResult(
            answer=SELF_DESCRIPTION[language],
            language=language,
            traces=traces,
            outcome="not_found",
        )
    procedure = format_procedure(traces) if asks_for_procedure(question) else ""
    # A how-to question with no order id is answered from the instruction alone.
    if procedure and not _ORDER_ID.search(question):
        answer = _append_translation(
            answer_from_tools("", traces, language, question),
            procedure,
            language,
            model_fn,
        )
        return TurnResult(answer=answer, language=language, traces=traces)

    if not procedure and wants_general_knowledge(question, history):
        # Off unless the admin panel turns the regular knowledge base on.
        if not general_knowledge:
            return TurnResult(answer=NOT_FOUND[language], language=language, traces=traces, outcome="not_found")
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
            traces=traces,
            outcome="not_found",
        )

    messages: list[dict] = [{"role": "system", "content": system_prompt(language)}]
    messages.extend(history)
    messages.append({"role": "user", "content": question})
    nudged = False
    model_used_tool = False

    for _ in range(MAX_TOOL_ROUNDS):
        message = model_fn(messages, tools)
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            model_used_tool = True
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
                if not isinstance(outcome, dict):
                    outcome = {"result": outcome, "is_error": True}
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
        # A server search does not count: the model itself has not called a tool yet.
        have_order = any(trace.name == "get_order_status" and not trace.is_error for trace in traces)
        if not model_used_tool and not nudged and not procedure and not have_order:
            nudged = True
            if content:
                messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": NUDGE})
            continue
        answer = answer_from_tools(content, traces, language, question)
        shown = format_procedure(traces) if asks_for_procedure(question) else ""
        answer = _append_translation(answer, shown, language, model_fn)
        return TurnResult(answer=answer, language=language, traces=traces)

    return TurnResult(answer=FAILED[language], language=language, traces=traces)
