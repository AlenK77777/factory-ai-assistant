"""Embeddings from the local Ollama server, shared by the indexer and the MCP server.

Calls Ollama's REST API with the Python standard library only (no extra packages).
nomic-embed-text expects a task prefix: "search_document: " for stored text and
"search_query: " for questions. Indexing and searching must use the same model
and prefixes, which is why both go through this module.
"""

import json
import os
import urllib.error
import urllib.request

EMBED_MODEL = "nomic-embed-text"
EMBED_DIMENSIONS = 768  # must match vector(768) in db/init/03_doc_chunks.sql
DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "

# Ollama runs locally: never send these requests through a system proxy.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class OllamaError(RuntimeError):
    """Ollama is not reachable or returned something unexpected."""


def ollama_url() -> str:
    return os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")


def post_json(path: str, payload: dict, timeout: float = 120) -> dict:
    """POST a JSON payload to Ollama and return the JSON answer."""
    request = urllib.request.Request(
        ollama_url() + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _opener.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise OllamaError(f"Ollama returned HTTP {error.code} for {path}: {detail}") from error
    except OSError as error:
        raise OllamaError(f"Cannot reach Ollama at {ollama_url()} ({error}). Is Ollama running?") from error


def embed(texts: list[str], prefix: str) -> list[list[float]]:
    """Return one 768-number vector per text."""
    if not texts:
        return []
    data = post_json("/api/embed", {"model": EMBED_MODEL, "input": [prefix + text for text in texts]})
    vectors = data.get("embeddings") or []
    if len(vectors) != len(texts) or any(len(vector) != EMBED_DIMENSIONS for vector in vectors):
        raise OllamaError(
            f"Expected {len(texts)} embeddings of {EMBED_DIMENSIONS} numbers from {EMBED_MODEL}, "
            f"got {len(vectors)}."
        )
    return vectors


def embed_documents(texts: list[str]) -> list[list[float]]:
    return embed(texts, DOCUMENT_PREFIX)


def embed_query(text: str) -> list[float]:
    return embed([text], QUERY_PREFIX)[0]


def to_pgvector(vector: list[float]) -> str:
    """Format a vector as pgvector text, e.g. '[0.1,-0.2]'. Pass it as a query parameter with ::vector."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"
