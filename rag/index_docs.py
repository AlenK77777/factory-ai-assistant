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
from psycopg import sql

ROOT = Path(__file__).resolve().parent.parent
# Lets "python rag/index_docs.py" import project modules such as rag.embeddings.
sys.path.insert(0, str(ROOT))

from chat.event_log import log_event  # noqa: E402
from rag.embeddings import OllamaError, embed_documents, to_pgvector  # noqa: E402
from rag.retrieval import RETRIEVAL_CHECKS, minimum_k  # noqa: E402

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


LIVE = "doc_chunks"
BUILDING = "doc_chunks_building"
PREVIOUS = "doc_chunks_previous"
HOLDING = "doc_chunks_holding"
INDEX_LIVE = "doc_index"
INDEX_BUILDING = "doc_index_building"
INDEX_PREVIOUS = "doc_index_previous"
INDEX_HOLDING = "doc_index_holding"


def connect() -> psycopg.Connection:
    """Connect as the index role. It owns the chunk tables and is not a superuser."""
    load_dotenv(ROOT / ".env")
    try:
        return psycopg.connect(
            host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
            port=os.getenv("POSTGRES_PORT", "5432"),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["INDEX_DB_USER"],
            password=os.environ["INDEX_DB_PASSWORD"],
            connect_timeout=5,
        )
    except KeyError as error:
        raise RuntimeError(f"Missing setting {error} in .env (see .env.example).") from error
    except psycopg.OperationalError as error:
        raise RuntimeError(
            "Cannot connect as the index role.\n"
            f"{error}\n"
            "Create it with db/init/04_indexer_role.sql if this database was created earlier."
        ) from error


def _owner_connect() -> psycopg.Connection:
    """Owner connection used only to create the index role. It does not write chunks."""
    load_dotenv(ROOT / ".env")
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        connect_timeout=5,
    )


def ensure_indexer_role() -> None:
    """Create the index role if needed and hand it the chunk tables.

    The database owner runs this. The rebuild itself connects as INDEX_DB_USER.
    """
    load_dotenv(ROOT / ".env")
    indexer = os.environ["INDEX_DB_USER"]
    password = os.environ["INDEX_DB_PASSWORD"]
    readonly = os.environ["MCP_DB_USER"]
    with _owner_connect() as conn:
        conn.execute(
            sql.SQL(
                """
                DO $$ BEGIN
                    CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
                EXCEPTION WHEN duplicate_object THEN NULL;
                END $$
                """
            ).format(role=sql.Identifier(indexer))
        )
        # DO blocks cannot take parameters for the password. Use a separate statement.
        conn.execute(
            sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(sql.Identifier(indexer), sql.Literal(password))
        )
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(os.environ["POSTGRES_DB"]), sql.Identifier(indexer)
        ))
        conn.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(sql.Identifier(indexer)))
        conn.execute(sql.SQL("REVOKE ALL ON TABLE orders, defects, ingest_runs FROM {}").format(sql.Identifier(indexer)))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS doc_index (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                embedding_model TEXT NOT NULL,
                dimensions INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO doc_index (id, embedding_model, dimensions)
            VALUES (1, 'qwen3-embedding:0.6b', 1024)
            ON CONFLICT (id) DO NOTHING
            """
        )
        conn.execute(sql.SQL("ALTER TABLE doc_chunks OWNER TO {}").format(sql.Identifier(indexer)))
        conn.execute(sql.SQL("ALTER TABLE doc_index OWNER TO {}").format(sql.Identifier(indexer)))
        conn.execute(sql.SQL("GRANT SELECT ON doc_chunks, doc_index TO {}").format(sql.Identifier(readonly)))
        conn.execute(
            sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT ON TABLES TO {}").format(
                sql.Identifier(indexer), sql.Identifier(readonly)
            )
        )


def grant_select(conn, table: str) -> None:
    """Let the read-only role see a chunk table after it has been swapped in."""
    role = os.environ["MCP_DB_USER"]
    conn.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(sql.Identifier(table), sql.Identifier(role)))


def relation_exists(conn, name: str):
    return conn.execute("SELECT to_regclass(%s)", (f"public.{name}",)).fetchone()[0]


def rename_table(conn, source: str, target: str) -> None:
    conn.execute(
        sql.SQL("ALTER TABLE {} RENAME TO {}").format(sql.Identifier(source), sql.Identifier(target))
    )


def drop_table(conn, name: str) -> None:
    conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(name)))


def exchange_tables(conn, live: str, previous: str, holding: str) -> None:
    """Swap live and previous. The previous table must already exist."""
    drop_table(conn, holding)
    rename_table(conn, live, holding)
    rename_table(conn, previous, live)
    rename_table(conn, holding, previous)


def install_built_tables(conn) -> None:
    """Rename the building tables onto the live names. One transaction, callers commit."""
    drop_table(conn, PREVIOUS)
    if relation_exists(conn, LIVE):
        rename_table(conn, LIVE, PREVIOUS)
    rename_table(conn, BUILDING, LIVE)
    drop_table(conn, INDEX_PREVIOUS)
    if relation_exists(conn, INDEX_LIVE):
        rename_table(conn, INDEX_LIVE, INDEX_PREVIOUS)
    rename_table(conn, INDEX_BUILDING, INDEX_LIVE)
    grant_select(conn, LIVE)
    grant_select(conn, INDEX_LIVE)


def rollback_index() -> bool:
    """Point the live names back at the previous index. False if there is nothing to restore."""
    with connect() as conn:
        if not relation_exists(conn, PREVIOUS):
            return False
        exchange_tables(conn, LIVE, PREVIOUS, HOLDING)
        if relation_exists(conn, INDEX_PREVIOUS):
            exchange_tables(conn, INDEX_LIVE, INDEX_PREVIOUS, INDEX_HOLDING)
        grant_select(conn, LIVE)
        if relation_exists(conn, INDEX_LIVE):
            grant_select(conn, INDEX_LIVE)
    return True


def _top_hit(rows: list[dict], expected: str) -> tuple[str, float | None, bool]:
    if not rows:
        return "", None, False
    return rows[0]["source"], rows[0]["similarity"], any(row["source"] == expected for row in rows)


def _compare_indexes(conn, new_model: str, k: int, threshold: float) -> list[dict]:
    """Same questions on the live table and the table just built, before the swap."""
    from rag.retrieval import search_chunks

    old_model = conn.execute("SELECT embedding_model FROM doc_index WHERE id = 1").fetchone()[0]
    checks = []
    for question, expected in RETRIEVAL_CHECKS:
        before = search_chunks(
            conn, question, k=k, min_similarity=threshold, embedding_model=old_model, table=LIVE
        )
        after = search_chunks(
            conn, question, k=k, min_similarity=threshold, embedding_model=new_model, table=BUILDING
        )
        before_top, before_similarity, before_hit = _top_hit(before, expected)
        after_top, after_similarity, after_hit = _top_hit(after, expected)
        checks.append(
            {
                "question": question,
                "expected": expected,
                "before_top": before_top,
                "before_similarity": before_similarity,
                "before_hit": before_hit,
                "after_top": after_top,
                "after_similarity": after_similarity,
                "after_hit": after_hit,
            }
        )
    return checks


def _index_documents(settings: dict, on_progress=None) -> dict:
    """Embed every instruction into a new table, then swap it in.

    Embeddings are built first. The live doc_chunks table is not changed until
    the new table is full. A failure leaves the old index in place.
    Queries keep using the model stored in doc_index until that swap commits.
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
    if dimensions is None:
        raise RuntimeError("No chunks were produced, so the live index was left unchanged.")

    report("save", f"Saving {len(rows)} chunks into a new table")
    k = int(settings.get("retrieval_k") or minimum_k())
    threshold = float(settings.get("similarity_threshold") or 0.48)
    try:
        with connect() as conn:
            drop_table(conn, BUILDING)
            conn.execute(
                sql.SQL(
                    """
                    CREATE TABLE {} (
                        id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        source TEXT NOT NULL,
                        chunk_index INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        embedding vector({}) NOT NULL,
                        UNIQUE (source, chunk_index)
                    )
                    """
                ).format(sql.Identifier(BUILDING), sql.Literal(int(dimensions)))
            )
            with conn.cursor() as cur:
                cur.executemany(
                    sql.SQL("INSERT INTO {} (source, chunk_index, content, embedding) VALUES (%s, %s, %s, %s::vector)").format(
                        sql.Identifier(BUILDING)
                    ),
                    rows,
                )
            drop_table(conn, INDEX_BUILDING)
            conn.execute(
                sql.SQL(
                    """
                    CREATE TABLE {} (
                        id INTEGER PRIMARY KEY,
                        embedding_model TEXT NOT NULL,
                        dimensions INTEGER NOT NULL
                    )
                    """
                ).format(sql.Identifier(INDEX_BUILDING))
            )
            conn.execute(
                sql.SQL("INSERT INTO {} (id, embedding_model, dimensions) VALUES (1, %s, %s)").format(
                    sql.Identifier(INDEX_BUILDING)
                ),
                (model, int(dimensions)),
            )
            report("check", "Comparing retrieval before and after the new index")
            checks = _compare_indexes(conn, model, k, threshold)
            lines = ["Retrieval check (expected file in the top k):"]
            for item in checks:
                lines.append(
                    f"{item['question']}\n"
                    f"  expected {item['expected']}\n"
                    f"  before: {item['before_top'] or 'none'} "
                    f"({item['before_similarity']}) {'hit' if item['before_hit'] else 'miss'}\n"
                    f"  after: {item['after_top'] or 'none'} "
                    f"({item['after_similarity']}) {'hit' if item['after_hit'] else 'miss'}"
                )
            report("check", "\n".join(lines), checks=checks)
            install_built_tables(conn)
    except psycopg.errors.UndefinedTable as error:
        raise RuntimeError(
            "Table doc_chunks does not exist. Create it with:\n"
            "  docker compose exec db psql -U factory -d factory "
            "-f /docker-entrypoint-initdb.d/03_doc_chunks.sql"
        ) from error
    report("save", f"Swapped in {len(rows)} chunks from {len(files)} files. The previous index is kept for rollback.")
    return {
        "chunks": len(rows),
        "files": len(files),
        "dimensions": dimensions,
        "migrated": False,
        "model": model,
        "checks": checks,
    }


def index_documents(settings: dict, on_progress=None) -> dict:
    """Embed every instruction into a new table, swap it in, and record the run."""
    ensure_indexer_role()
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
