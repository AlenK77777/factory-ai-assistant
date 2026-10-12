"""Answer a question fully on-premise: local retrieval + local model via Ollama.

1. Find the closest work-instruction chunks (the same retrieval as the MCP tool).
2. Ask the local chat model (qwen3:8b) to answer ONLY from those chunks.
   Ollama's structured output ("format" = JSON schema) constrains the reply to
   {"language": <detected>, "answer": str, "sources": [str], "found": bool}.
   language is pinned in the schema to the question's script (en / ru / he), so
   the model has to commit to that language before it writes the answer.
3. Validate the reply with pydantic and check that it is consistent (sources are
   real retrieved files, found agrees with sources, answer is in the expected
   script). If not, retry once and tell the model what was wrong. If the retry
   also fails, print a safe "not found" answer in the question's language.

Thinking: qwen3 is a thinking model, and Ollama lets it think by default, so this
script always sets Ollama's "think" option explicitly:
    default   think=false: the model does not think at all (lowest latency).
    --think   think=true: Ollama returns the reasoning in a separate field
              (message.thinking). It is only counted, never parsed; only
              message.content is validated as the answer.

Output: the JSON answer on stdout; retrieved chunks and timing on stderr.

Usage (from the project root, venv active, Ollama and the db running):
    python rag/ask_local.py "What should I do if delamination is found?"
    python rag/ask_local.py --think "Who may release an order that is on hold?"
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Literal

import psycopg
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, create_model

ROOT = Path(__file__).resolve().parent.parent
# Lets "python rag/ask_local.py" import project modules such as rag.retrieval.
sys.path.insert(0, str(ROOT))

from rag.embeddings import OllamaError, post_json  # noqa: E402
from rag.language import (  # noqa: E402
    FAILED,
    LANGUAGE_NAMES,
    NOT_FOUND,
    Language,
    answer_in_language,
    detect_language,
    unused_sources,
)
from rag.retrieval import DEFAULT_K, search_chunks  # noqa: E402

MAX_ATTEMPTS = 2  # the first try plus one retry

# Without thinking: temperature 0 gives repeatable answers and latency measurements.
ANSWER_OPTIONS = {"temperature": 0, "num_ctx": 8192, "num_predict": 1024}
# With thinking: Qwen's recommended sampling for thinking mode (greedy decoding can loop),
# and room for the reasoning tokens, which count towards num_predict.
THINKING_OPTIONS = {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "num_ctx": 8192, "num_predict": 4096}

SYSTEM_PROMPT = """You answer questions from shift managers, QA engineers and planners in a PCB factory.
Use ONLY the work-instruction excerpts in the user's message. Never use general knowledge and never guess.

If the excerpts answer the question:
- set "found" to true;
- give a short, practical answer (numbered steps if it is a procedure);
- write "answer" in the language named in the JSON schema's "language" field (translate the facts from the excerpts; do not add any);
- list in "sources" only the file names of excerpts you actually used, exactly as written after "source:". Do not list a file you did not use.

If the excerpts do not answer the question:
- set "found" to false and "sources" to [];
- set "answer" to the "not found" sentence given in the user message.

Reply with JSON only."""


def answer_model_for(language: Language) -> type[BaseModel]:
    """Pydantic model whose first field is language, pinned to one value.

    Ollama's structured output uses this model's JSON schema, so the model must
    emit that language code before it writes the answer.
    """
    language_field = Literal[language]  # type: ignore[valid-type]
    return create_model(
        "LocalAnswer",
        __config__=ConfigDict(extra="forbid"),
        language=(
            language_field,
            Field(description=f"Must be '{language}'. Write 'answer' in {LANGUAGE_NAMES[language]}."),
        ),
        answer=(
            str,
            Field(min_length=1, description=f"The answer, written in {LANGUAGE_NAMES[language]}."),
        ),
        sources=(list[str], ...),
        found=(bool, ...),
    )


def info(*parts) -> None:
    """Diagnostics go to stderr, so stdout stays pure JSON."""
    print(*parts, file=sys.stderr)


def build_messages(question: str, chunks: list[dict], language: Language) -> list[dict]:
    """The chat messages for the model: rules, then the excerpts and the question."""
    excerpts = "\n\n".join(
        f"[{number}] source: {chunk['source']}\n{chunk['content']}" for number, chunk in enumerate(chunks, 1)
    )
    name = LANGUAGE_NAMES[language]
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Work-instruction excerpts:\n\n{excerpts}\n\n"
                f"Question: {question}\n\n"
                f'The JSON schema requires "language": "{language}". '
                f'Write "answer" in {name}. '
                f'If nothing in the excerpts answers the question, set found=false, sources=[], '
                f'and answer="{NOT_FOUND[language]}"'
            ),
        },
    ]


def parse_answer(content: str, chunks: list[dict], language: Language) -> BaseModel:
    """Validate the model's reply. Raises ValueError (pydantic's ValidationError is one) with the reason.

    When found is false the answer is replaced by the fixed NOT_FOUND text in the
    question's language, so a "not found" reply can never carry invented content.
    """
    model = answer_model_for(language)
    answer = model.model_validate_json(content)
    sources = list(dict.fromkeys(source.strip() for source in answer.sources))
    allowed_sources = {chunk["source"] for chunk in chunks}

    unknown = [source for source in sources if source not in allowed_sources]
    if unknown:
        raise ValueError(f"sources {unknown} are not excerpt file names; use only {sorted(allowed_sources)}")
    if answer.found and not sources:
        raise ValueError('"found" is true but "sources" is empty')
    if not answer.found and sources:
        raise ValueError('"found" is false but "sources" is not empty')
    unused = unused_sources(answer.answer, sources, chunks)
    if unused:
        raise ValueError(
            f"sources {unused} are listed but the answer does not use them "
            f"(no shared plant code or number such as HOLD, Q2, 30)"
        )
    if answer.found and not answer_in_language(answer.answer, language):
        raise ValueError(
            f'"answer" must be written in {LANGUAGE_NAMES[language]} '
            f'(language="{language}"); do not mix in letters from another script'
        )

    if not answer.found:
        return model(language=language, answer=NOT_FOUND[language], sources=[], found=False)
    return model(language=language, answer=answer.answer.strip(), sources=sources, found=True)


def chat_model() -> str:
    return os.getenv("OLLAMA_CHAT_MODEL", "qwen3:8b")


def call_model(messages: list[dict], think: bool, schema: dict) -> dict:
    """One non-streaming chat request with the answer schema as structured output."""
    return post_json(
        "/api/chat",
        {
            "model": chat_model(),
            "messages": messages,
            "format": schema,
            "think": think,
            "stream": False,
            "options": THINKING_OPTIONS if think else ANSWER_OPTIONS,
        },
        timeout=600,
    )


def ask_model(question: str, chunks: list[dict], think: bool) -> tuple[BaseModel | None, list[dict], Language]:
    """Ask the model, retrying once on an invalid reply. Returns (answer or None, raw responses, language)."""
    language = detect_language(question)
    schema = answer_model_for(language).model_json_schema()
    messages = build_messages(question, chunks, language)
    responses = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = call_model(messages, think, schema)
        responses.append(response)
        content = response["message"]["content"]
        try:
            return parse_answer(content, chunks, language), responses, language
        except ValueError as error:
            reason = str(error)[:300]
            info(f"Attempt {attempt}: invalid reply ({reason})")
            # Show the model its own reply and the problem; repeating the same request
            # at temperature 0 would just return the same reply again.
            messages = messages + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": f"Your reply was invalid: {reason}\nReply again with JSON only, following the rules."},
            ]
    return None, responses, language


def connect_read_only() -> psycopg.Connection:
    """Retrieval only reads, so connect with the read-only role (the same one the MCP server uses)."""
    try:
        conn = psycopg.connect(
            host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
            port=os.getenv("POSTGRES_PORT", "5432"),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["MCP_DB_USER"],
            password=os.environ["MCP_DB_PASSWORD"],
            connect_timeout=5,
        )
    except KeyError as error:
        sys.exit(f"Missing setting {error} in .env (see .env.example).")
    except psycopg.OperationalError as error:
        sys.exit(f"Cannot connect to PostgreSQL.\n{error}\nIs the database running? Check: docker compose ps")
    conn.read_only = True
    return conn


def print_timing(retrieval_s: float, total_s: float, responses: list[dict], think: bool) -> None:
    """Wall-clock times plus Ollama's own measurements (nanoseconds in the API), summed over attempts."""
    def seconds(key: str) -> float:
        return sum(response.get(key, 0) for response in responses) / 1e9

    prompt_tokens = sum(response.get("prompt_eval_count", 0) for response in responses)
    output_tokens = sum(response.get("eval_count", 0) for response in responses)
    generate_s = seconds("eval_duration")
    speed = output_tokens / generate_s if generate_s else 0.0
    thinking_chars = sum(len(response["message"].get("thinking") or "") for response in responses)

    info(f"Model {chat_model()}, think={'on' if think else 'off'}, attempts: {len(responses)}")
    info(f"Time: {total_s:.2f} s total = {retrieval_s:.2f} s retrieval + {total_s - retrieval_s:.2f} s model")
    info(
        f"  model detail: load {seconds('load_duration'):.2f} s, "
        f"prompt {prompt_tokens} tokens in {seconds('prompt_eval_duration'):.2f} s, "
        f"output {output_tokens} tokens in {generate_s:.2f} s ({speed:.1f} tokens/s)"
    )
    if think:
        info(f"  thinking: {thinking_chars} characters in message.thinking (counted, not parsed)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Answer a question from the work instructions with the local model.")
    parser.add_argument("question", nargs="+", help="the question (quotes are optional)")
    parser.add_argument("-k", type=int, default=DEFAULT_K, choices=range(1, 11), metavar="1-10",
                        help=f"chunks to retrieve (default {DEFAULT_K}; smaller values are raised to {DEFAULT_K})")
    parser.add_argument("--think", action="store_true", help="let qwen3 think before answering (slower)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    question = " ".join(args.question).strip()
    # UTF-8 so non-ASCII text (e.g. "°C" in the instructions) survives when output is redirected.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    load_dotenv(ROOT / ".env")

    started = time.perf_counter()
    try:
        with connect_read_only() as conn:
            chunks = search_chunks(conn, question, args.k)
    except OllamaError as error:
        sys.exit(str(error))
    except psycopg.errors.UndefinedTable:
        sys.exit("Table doc_chunks does not exist yet (see db/init/03_doc_chunks.sql).")
    retrieval_s = time.perf_counter() - started
    if not chunks:
        sys.exit("No work instructions are indexed yet. Run: python rag/index_docs.py")

    info(f"Retrieved {len(chunks)} chunks in {retrieval_s:.2f} s:")
    for chunk in chunks:
        info(f"  {chunk['similarity']:.3f}  {chunk['source']} #{chunk['chunk_index']}")

    try:
        answer, responses, language = ask_model(question, chunks, args.think)
    except OllamaError as error:
        sys.exit(str(error))
    total_s = time.perf_counter() - started

    exit_code = 0
    if answer is None:
        answer = answer_model_for(language)(
            language=language, answer=FAILED[language], sources=[], found=False
        )
        exit_code = 1

    print(json.dumps(answer.model_dump(), indent=2, ensure_ascii=False))
    print_timing(retrieval_s, total_s, responses, args.think)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
