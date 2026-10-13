"""Index the work instructions for search (RAG).

Reads docs/instructions/*.md and splits each file on Markdown headings.
A numbered list stays in one chunk. The character limit is only a fallback for
a long section that has no list to keep whole. Embeddings come from Ollama
(qwen3-embedding:0.6b, 1024 dimensions) and are stored in doc_chunks.

Re-indexing replaces all old chunks in one transaction. All embeddings are
fetched first, so if Ollama fails, the existing index is left untouched.

Usage (from the project root, venv active, Ollama running):
    python rag/index_docs.py
"""

import os
import re
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# Lets "python rag/index_docs.py" import project modules such as rag.embeddings.
sys.path.insert(0, str(ROOT))

from chat.event_log import log_event  # noqa: E402
from rag.embeddings import OllamaError, embed_documents, to_pgvector  # noqa: E402

DOCS_DIR = ROOT / "docs" / "instructions"
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50

_HEADING = re.compile(r"^#{1,6}\s+\S")
_NUMBERED = re.compile(r"^\d+\.\s")


def _is_heading(line: str) -> bool:
    return _HEADING.match(line) is not None


def _is_numbered(line: str) -> bool:
    return _NUMBERED.match(line) is not None


def split_sections(text: str) -> list[str]:
    """One piece per Markdown heading. Text before the first heading stays together."""
    sections: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if _is_heading(line) and any(part.strip() for part in current):
            sections.append(current)
            current = [line]
        else:
            current.append(line)
    if any(part.strip() for part in current):
        sections.append(current)
    return ["\n".join(lines).strip() for lines in sections]


def _blocks(section: str) -> tuple[str, list[tuple[str, bool]]]:
    """Split a section into a heading and blank-line blocks. A block is a numbered list if it contains one."""
    lines = section.splitlines()
    heading = ""
    if lines and _is_heading(lines[0]):
        heading = lines[0]
        lines = lines[1:]

    blocks: list[tuple[str, bool]] = []
    buffer: list[str] = []

    def flush() -> None:
        if any(line.strip() for line in buffer):
            body = "\n".join(buffer).strip()
            blocks.append((body, any(_is_numbered(line) for line in buffer)))
        buffer.clear()

    for line in lines:
        if not line.strip():
            flush()
        else:
            buffer.append(line)
    flush()

    # A blank line must not break one numbered list into two chunks.
    merged: list[tuple[str, bool]] = []
    for body, numbered in blocks:
        if numbered and merged and merged[-1][1]:
            merged[-1] = (merged[-1][0] + "\n\n" + body, True)
        else:
            merged.append((body, numbered))
    return heading, merged


def split_by_size(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Fallback for a long block that is not a numbered list.

    Cuts at a paragraph, line, sentence or word in the second half of the window,
    and overlaps the next chunk by about `overlap` characters.
    """
    text = text.strip()
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            for separator in ("\n\n", "\n", ". ", " "):
                cut = text.rfind(separator, start + size // 2, end)
                if cut != -1:
                    end = cut + len(separator)
                    break

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break

        next_start = end - overlap
        space = text.find(" ", next_start, end)
        start = space + 1 if space != -1 else next_start
    return chunks


def _with_heading(heading: str, body: str) -> str:
    return f"{heading}\n\n{body}".strip() if heading else body


def split_section(section: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Keep a short section whole. A longer one is packed by block, and a numbered list is never cut."""
    section = section.strip()
    if len(section) <= size:
        return [section]

    heading, blocks = _blocks(section)
    chunks: list[str] = []
    current: list[str] = []
    heading_len = len(heading) + 2 if heading else 0
    current_len = heading_len

    def emit() -> None:
        nonlocal current, current_len
        if current:
            chunks.append(_with_heading(heading, "\n\n".join(current)))
            current = []
            current_len = heading_len

    for body, numbered in blocks:
        if numbered or len(body) <= size:
            extra = len(body) if not current else len(body) + 2
            if current and current_len + extra > size:
                emit()
            current.append(body)
            current_len += len(body) if len(current) == 1 else len(body) + 2
            continue

        emit()
        for piece in split_by_size(body, size, overlap):
            chunks.append(_with_heading(heading, piece))
    emit()
    return chunks


def split_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split on Markdown headings. Never split a numbered list. Use `size` only as a fallback."""
    text = text.strip()
    if not text:
        return []
    chunks = []
    for section in split_sections(text):
        chunks.extend(split_section(section, size, overlap))
    return chunks


def document_title(text: str, fallback: str) -> str:
    """The first '# ' heading of a markdown file, or the fallback."""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def text_to_embed(title: str, chunk: str) -> str:
    """Put the document title in front of every chunk, so mid-document chunks keep their topic."""
    return chunk if chunk.startswith("# ") else f"{title}\n{chunk}"


def chunk_document(text: str, settings: dict) -> list[str]:
    """Split one instruction with the admin chunking settings."""
    size = int(settings["chunk_size"])
    overlap = int(settings["chunk_overlap"])
    if settings["chunking_mode"] == "fixed":
        return split_by_size(text, size, overlap)
    return split_text(text, size, overlap)


def connect() -> psycopg.Connection:
    """Connect as the owner role (POSTGRES_USER): indexing writes to the database."""
    try:
        return psycopg.connect(
            host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
            port=os.getenv("POSTGRES_PORT", "5432"),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            connect_timeout=5,
        )
    except KeyError as error:
        raise RuntimeError(f"Missing setting {error} in .env (see .env.example).") from error
    except psycopg.OperationalError as error:
        raise RuntimeError(
            f"Cannot connect to PostgreSQL.\n{error}\nIs the database running? Check: docker compose ps"
        ) from error


def _index_documents(settings: dict, on_progress=None) -> dict:
    """Embed every instruction and replace doc_chunks in one transaction.

    Embeddings are built first. If the new model uses a different vector size,
    the column is changed in that same transaction, so a failure keeps the old index.
    on_progress receives {"stage", "message", ...} and is optional.
    """

    def report(stage: str, message: str, **extra) -> None:
        if on_progress is not None:
            on_progress({"stage": stage, "message": message, **extra})

    files = sorted(DOCS_DIR.glob("*.md"))
    if not files:
        raise RuntimeError(f"No .md files found in {DOCS_DIR}")

    rows = []
    dimensions = None
    model = settings["embedding_model"]
    for number, path in enumerate(files, start=1):
        report("index", f"Embedding {path.name} ({number}/{len(files)})", done=number - 1, total=len(files))
        text = path.read_text(encoding="utf-8")
        title = document_title(text, fallback=path.stem)
        chunks = chunk_document(text, settings)
        if not chunks:
            continue
        vectors = embed_documents([text_to_embed(title, chunk) for chunk in chunks], model=model)
        dimensions = len(vectors[0])
        for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
            if len(vector) != dimensions:
                raise RuntimeError(f"{model} returned vectors of different lengths.")
            rows.append((path.name, index, chunk, to_pgvector(vector)))

    report("save", f"Saving {len(rows)} chunks")
    migrated = False
    try:
        with connect() as conn:
            current = conn.execute(
                """
                SELECT atttypmod FROM pg_attribute
                WHERE attrelid = 'doc_chunks'::regclass AND attname = 'embedding'
                """
            ).fetchone()[0]
            if dimensions is not None and int(current) != int(dimensions):
                report("migrate", f"Changing the vector column from {current} to {dimensions} numbers, then re-indexing")
                conn.execute("TRUNCATE doc_chunks")
                conn.execute(f"ALTER TABLE doc_chunks ALTER COLUMN embedding TYPE vector({int(dimensions)})")
                migrated = True
            conn.execute("DELETE FROM doc_chunks")
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO doc_chunks (source, chunk_index, content, embedding) VALUES (%s, %s, %s, %s::vector)",
                    rows,
                )
    except psycopg.errors.UndefinedTable as error:
        raise RuntimeError(
            "Table doc_chunks does not exist. Create it with:\n"
            "  docker compose exec db psql -U factory -d factory "
            "-f /docker-entrypoint-initdb.d/03_doc_chunks.sql"
        ) from error
    report("save", f"Stored {len(rows)} chunks from {len(files)} files", chunks=len(rows))
    return {"chunks": len(rows), "files": len(files), "dimensions": dimensions, "migrated": migrated}


def index_documents(settings: dict, on_progress=None) -> dict:
    """Embed every instruction, replace doc_chunks, and record the run in the event log."""
    try:
        result = _index_documents(settings, on_progress)
    except Exception as error:
        log_event(
            "ERROR",
            "index",
            "reindex",
            model=str(settings.get("embedding_model", "")),
            error=str(error),
        )
        raise
    log_event(
        "INFO",
        "index",
        "reindex",
        model=str(settings.get("embedding_model", "")),
        files=result.get("files"),
        chunks=result.get("chunks"),
        dimensions=result.get("dimensions"),
        migrated=bool(result.get("migrated")),
    )
    return result


def main() -> None:
    load_dotenv(ROOT / ".env")
    from chat.settings import load_settings

    try:
        result = index_documents(load_settings(), on_progress=lambda event: print(event["message"]))
    except (OllamaError, RuntimeError) as error:
        sys.exit(f"{error}\nNothing was changed in the database." if "Nothing was changed" not in str(error) else str(error))
    print(f"Stored {result['chunks']} chunks from {result['files']} files in doc_chunks (old chunks replaced).")


if __name__ == "__main__":
    main()
