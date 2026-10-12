"""Answer a question fully on-premise: local retrieval + local model via Ollama.

1. Find the closest work-instruction chunks (the same retrieval as the MCP tool).
2. Ask the local chat model (qwen3:8b) to answer ONLY from those chunks.
   Ollama's structured output ("format" = JSON schema) constrains the reply to
   {"answer": str, "sources": [str], "found": bool}.
3. Validate the reply with pydantic and check that it is consistent (sources are
   real retrieved files, found agrees with sources). If not, retry once and tell
   the model what was wrong. If the retry also fails, print a safe "not found" answer.

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

import psycopg
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent.parent
# Lets "python rag/ask_local.py" import project modules such as rag.retrieval.
sys.path.insert(0, str(ROOT))

from rag.embeddings import OllamaError, post_json  # noqa: E402
from rag.retrieval import search_chunks  # noqa: E402

MAX_ATTEMPTS = 2  # the first try plus one retry
NOT_FOUND_ANSWER = "No relevant work instruction was found for this question."
FAILED_ANSWER = "The local model did not return a valid answer. Please try again or ask a QA engineer."

# Without thinking: temperature 0 gives repeatable answers and latency measurements.
ANSWER_OPTIONS = {"temperature": 0, "num_ctx": 8192, "num_predict": 1024}
# With thinking: Qwen's recommended sampling for thinking mode (greedy decoding can loop),
# and room for the reasoning tokens, which count towards num_predict.
THINKING_OPTIONS = {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "num_ctx": 8192, "num_predict": 4096}

SYSTEM_PROMPT = f"""You answer questions from shift managers, QA engineers and planners in a PCB factory.
Use ONLY the work-instruction excerpts in the user's message. Never use general knowledge and never guess.

If the excerpts answer the question:
- set "found" to true;
- give a short, practical answer (numbered steps if it is a procedure);
- list in "sources" the file names of the excerpts you actually used, exactly as written after "source:".

If the excerpts do not answer the question:
- set "found" to false, "sources" to [] and "answer" to "{NOT_FOUND_ANSWER}"

Reply with JSON only: {{"answer": string, "sources": [string], "found": boolean}}."""


class LocalAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1)
    sources: list[str]
    found: bool


ANSWER_SCHEMA = LocalAnswer.model_json_schema()


def info(*parts) -> None:
    """Diagnostics go to stderr, so stdout stays pure JSON."""
    print(*parts, file=sys.stderr)


def build_messages(question: str, chunks: list[dict]) -> list[dict]:
    """The chat messages for the model: rules, then the excerpts and the question."""
    excerpts = "\n\n".join(
        f"[{number}] source: {chunk['source']}\n{chunk['content']}" for number, chunk in enumerate(chunks, 1)
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Work-instruction excerpts:\n\n{excerpts}\n\nQuestion: {question}"},
    ]


def parse_answer(content: str, allowed_sources: set[str]) -> LocalAnswer:
    """Validate the model's reply. Raises ValueError (pydantic's ValidationError is one) with the reason.

    When found is false the answer is replaced by the fixed NOT_FOUND_ANSWER, so a
    "not found" reply can never carry invented content.
    """
    answer = LocalAnswer.model_validate_json(content)
    sources = list(dict.fromkeys(source.strip() for source in answer.sources))

    unknown = [source for source in sources if source not in allowed_sources]
    if unknown:
        raise ValueError(f"sources {unknown} are not excerpt file names; use only {sorted(allowed_sources)}")
    if answer.found and not sources:
        raise ValueError('"found" is true but "sources" is empty')
    if not answer.found and sources:
        raise ValueError('"found" is false but "sources" is not empty')

    if not answer.found:
        return LocalAnswer(answer=NOT_FOUND_ANSWER, sources=[], found=False)
    return LocalAnswer(answer=answer.answer.strip(), sources=sources, found=True)


def chat_model() -> str:
    return os.getenv("OLLAMA_CHAT_MODEL", "qwen3:8b")


def call_model(messages: list[dict], think: bool) -> dict:
    """One non-streaming chat request with the answer schema as structured output."""
    return post_json(
        "/api/chat",
        {
            "model": chat_model(),
            "messages": messages,
            "format": ANSWER_SCHEMA,
            "think": think,
            "stream": False,
            "options": THINKING_OPTIONS if think else ANSWER_OPTIONS,
        },
        timeout=600,
    )


def ask_model(question: str, chunks: list[dict], think: bool) -> tuple[LocalAnswer | None, list[dict]]:
    """Ask the model, retrying once on an invalid reply. Returns (answer or None, raw responses)."""
    messages = build_messages(question, chunks)
    allowed_sources = {chunk["source"] for chunk in chunks}
    responses = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = call_model(messages, think)
        responses.append(response)
        content = response["message"]["content"]
        try:
            return parse_answer(content, allowed_sources), responses
        except ValueError as error:
            reason = str(error)[:300]
            info(f"Attempt {attempt}: invalid reply ({reason})")
            # Show the model its own reply and the problem; repeating the same request
            # at temperature 0 would just return the same reply again.
            messages = messages + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": f"Your reply was invalid: {reason}\nReply again with JSON only, following the rules."},
            ]
    return None, responses


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
    parser.add_argument("-k", type=int, default=4, choices=range(1, 11), metavar="1-10",
                        help="number of chunks to retrieve (default 4)")
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
        answer, responses = ask_model(question, chunks, args.think)
    except OllamaError as error:
        sys.exit(str(error))
    total_s = time.perf_counter() - started

    exit_code = 0
    if answer is None:
        answer = LocalAnswer(answer=FAILED_ANSWER, sources=[], found=False)
        exit_code = 1

    print(json.dumps(answer.model_dump(), indent=2, ensure_ascii=False))
    print_timing(retrieval_s, total_s, responses, args.think)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
