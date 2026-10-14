"""Retrieval tests that do not need Ollama or PostgreSQL."""

from contextlib import contextmanager

from mcp_server.server import search_instructions
from rag.retrieval import instruction_found, search_chunks


def _rows():
    return [
        {
            "source": "delamination-containment.md",
            "chunk_index": index,
            "content": f"step {index}",
            "distance": 0.2,
        }
        for index in range(8)
    ]


class Cursor:
    def __init__(self, captured):
        self.captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, query, params=None):
        if params and "k" in params:
            self.captured["k"] = params["k"]
            self._rows = _rows()[: params["k"]]
        return self

    def fetchall(self):
        return getattr(self, "_rows", [])

    def fetchone(self):
        return {"embedding_model": "qwen3-embedding:0.6b"}


class Connection:
    def __init__(self, captured):
        self.captured = captured

    def cursor(self, **kwargs):
        return Cursor(self.captured)


def _patch_embed(monkeypatch):
    monkeypatch.setattr("rag.retrieval.embed_query", lambda query, model=None: [0.0])
    monkeypatch.setattr("rag.retrieval.to_pgvector", lambda vector: "[0]")


def test_instruction_found_is_true_only_when_a_chunk_is_relevant():
    assert instruction_found({"found": True, "results": [{"relevant": False}, {"relevant": True}]}) is True
    assert instruction_found({"found": True, "results": [{"relevant": False}]}) is False
    assert instruction_found({"found": False, "results": []}) is False


def test_search_chunks_uses_the_k_it_is_given(monkeypatch):
    """No floor. A direct call searches the k it was given."""
    _patch_embed(monkeypatch)
    captured = {}
    connection = Connection(captured)
    search_chunks(connection, "What should I do if delamination is found?", k=3, min_similarity=0.48, embedding_model="qwen3-embedding:0.6b")
    assert captured["k"] == 3
    search_chunks(connection, "What should I do if delamination is found?", k=8, min_similarity=0.48, embedding_model="qwen3-embedding:0.6b")
    assert captured["k"] == 8


def test_search_instructions_uses_the_saved_k_exactly(monkeypatch):
    """The panel value is the search. A caller's k is not raised or kept."""
    _patch_embed(monkeypatch)
    monkeypatch.setattr("rag.retrieval.active_embedding_model", lambda conn: "qwen3-embedding:0.6b")
    captured = {}

    @contextmanager
    def fake_db():
        yield Connection(captured)

    monkeypatch.setattr("mcp_server.server.read_only_db", fake_db)
    saved = {"retrieval_k": 6, "similarity_threshold": 0.48}
    monkeypatch.setattr("chat.settings.load_settings", lambda: saved)

    result = search_instructions("What should I do if delamination is found?", k=3)
    assert result.k_requested == 3
    assert result.k_used == 6
    assert captured["k"] == 6

    result = search_instructions("What should I do if delamination is found?", k=8)
    assert result.k_requested == 8
    assert result.k_used == 6
    assert captured["k"] == 6

    saved["retrieval_k"] = 4
    result = search_instructions("What should I do if delamination is found?", k=3)
    assert result.k_requested == 3
    assert result.k_used == 4
    assert captured["k"] == 4


def test_query_uses_the_active_index_model_not_the_saved_setting(monkeypatch):
    seen = {}

    def fake_embed(text, model=None):
        seen["model"] = model
        return [0.0]

    monkeypatch.setattr("rag.retrieval.embed_query", fake_embed)
    monkeypatch.setattr("rag.retrieval.to_pgvector", lambda vector: "[0]")
    monkeypatch.setattr(
        "chat.settings.load_settings",
        lambda: {"retrieval_k": 1, "similarity_threshold": 0.48, "embedding_model": "other-model"},
    )
    search_chunks(Connection({}), "What are the ESD rules?", k=1, min_similarity=0.48)
    assert seen["model"] == "qwen3-embedding:0.6b"


