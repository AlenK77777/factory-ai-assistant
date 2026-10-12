"""Index the work instructions for search (RAG).

Reads docs/instructions/*.md, splits every file into ~500-character chunks with
~50 characters of overlap, gets an embedding for each chunk from Ollama
(qwen3-embedding:0.6b, 1024 dimensions) and stores them in the doc_chunks table.

Re-indexing replaces all old chunks in one transaction. All embeddings are
fetched first, so if Ollama fails, the existing index is left untouched.

Usage (from the project root, venv active, Ollama running):
    python rag/index_docs.py
"""

import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# Lets "python rag/index_docs.py" import project modules such as rag.embeddings.
sys.path.insert(0, str(ROOT))

from rag.embeddings import OllamaError, embed_documents, to_pgvector  # noqa: E402

DOCS_DIR = ROOT / "docs" / "instructions"
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50


def split_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into chunks of at most `size` characters that overlap by about `overlap`.

    A chunk ends at the last paragraph break, line break, sentence end or space in
    its second half, so words and (where possible) sentences stay whole.
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

        # Step back by the overlap, then forward to the next word start.
        next_start = end - overlap
        space = text.find(" ", next_start, end)
        start = space + 1 if space != -1 else next_start
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
        sys.exit(f"Missing setting {error} in .env (see .env.example).")
    except psycopg.OperationalError as error:
        sys.exit(f"Cannot connect to PostgreSQL.\n{error}\nIs the database running? Check: docker compose ps")


def main() -> None:
    load_dotenv(ROOT / ".env")
    files = sorted(DOCS_DIR.glob("*.md"))
    if not files:
        sys.exit(f"No .md files found in {DOCS_DIR}")

    rows = []
    print(f"Embedding chunks with Ollama ({len(files)} files)")
    for path in files:
        text = path.read_text(encoding="utf-8")
        title = document_title(text, fallback=path.stem)
        chunks = split_text(text)
        try:
            vectors = embed_documents([text_to_embed(title, chunk) for chunk in chunks])
        except OllamaError as error:
            sys.exit(f"{error}\nNothing was changed in the database.")

        for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
            rows.append((path.name, index, chunk, to_pgvector(vector)))
        print(f"  {path.name}: {len(chunks)} chunks")

    # The "with" block commits at the end, or rolls back (keeping the old chunks) on an error.
    try:
        with connect() as conn:
            conn.execute("DELETE FROM doc_chunks")
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO doc_chunks (source, chunk_index, content, embedding) "
                    "VALUES (%s, %s, %s, %s::vector)",
                    rows,
                )
    except psycopg.errors.UndefinedTable:
        sys.exit(
            "Table doc_chunks does not exist. Create it with:\n"
            "  docker compose exec db psql -U factory -d factory -f /docker-entrypoint-initdb.d/03_doc_chunks.sql"
        )

    print(f"Stored {len(rows)} chunks from {len(files)} files in doc_chunks (old chunks replaced).")


if __name__ == "__main__":
    main()
