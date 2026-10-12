"""Retrieval tests that do not need Ollama or PostgreSQL."""

from contextlib import contextmanager

from mcp_server.server import search_instructions
from rag.retrieval import DEFAULT_K, search_chunks


def test_k_of_3_still_returns_at_least_the_measured_minimum(monkeypatch):
    """The chat model has asked for k=3. The server must still return 6 chunks."""
    monkeypatch.setattr("rag.retrieval.embed_query", lambda query: [0.0])
    monkeypatch.setattr("rag.retrieval.to_pgvector", lambda vector: "[0]")
    rows = [
        {
            "source": "delamination-containment.md",
            "chunk_index": index,
            "content": f"step {index}",
            "distance": 0.2,
        }
        for index in range(8)
    ]
    captured = {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, query, params):
            captured["k"] = params["k"]
            self._rows = rows[: params["k"]]
            return self

        def fetchall(self):
            return self._rows

    class Connection:
        def cursor(self, **kwargs):
            return Cursor()

    chunks = search_chunks(Connection(), "What should I do if delamination is found?", k=3, min_similarity=0.48)

    assert captured["k"] == 3
    assert len(chunks) == 3

    @contextmanager
    def fake_db():
        yield Connection()

    monkeypatch.setattr("mcp_server.server.read_only_db", fake_db)
    monkeypatch.setattr(
        "chat.settings.load_settings",
        lambda: {"retrieval_k": DEFAULT_K, "similarity_threshold": 0.48},
    )
    result = search_instructions("What should I do if delamination is found?", k=3)
    assert result.k_requested == 3
    assert result.k_used == DEFAULT_K
    assert len(result.results) >= DEFAULT_K
