"""Re-index failure leaves the live index alone. The grant test needs the database."""

import os

import pytest
from dotenv import load_dotenv

from rag.index_docs import DOCS_DIR, index_documents


def test_embedding_failure_does_not_open_the_database(monkeypatch):
    monkeypatch.setattr("rag.index_docs.ensure_indexer_role", lambda: None)

    def boom(texts, model=None):
        raise RuntimeError("ollama down")

    monkeypatch.setattr("rag.index_docs.embed_documents", boom)
    monkeypatch.setattr("rag.index_docs.connect", lambda: (_ for _ in ()).throw(AssertionError("database opened")))
    settings = {
        "embedding_model": "qwen3-embedding:0.6b",
        "chunking_mode": "structure",
        "chunk_size": 500,
        "chunk_overlap": 50,
        "retrieval_k": 6,
        "similarity_threshold": 0.48,
    }
    assert list(DOCS_DIR.glob("*.md"))
    with pytest.raises(RuntimeError, match="ollama down"):
        index_documents(settings)


@pytest.mark.integration
def test_readonly_role_can_select_a_table_after_the_swap():
    """factory_readonly can read a table the indexer created and swapped in."""
    import psycopg
    from psycopg import sql

    from rag.index_docs import ROOT, drop_table, grant_select, rename_table

    load_dotenv(ROOT / ".env")
    indexer = os.environ["INDEX_DB_USER"]
    readonly = os.environ["MCP_DB_USER"]
    common = dict(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.environ["POSTGRES_DB"],
        connect_timeout=5,
    )
    live, building, previous, holding = (
        "citest_chunks_live",
        "citest_chunks_building",
        "citest_chunks_previous",
        "citest_chunks_holding",
    )
    with psycopg.connect(**common, user=indexer, password=os.environ["INDEX_DB_PASSWORD"]) as conn:
        for name in (live, building, previous, holding):
            drop_table(conn, name)
        conn.execute(sql.SQL("CREATE TABLE {} (id integer)").format(sql.Identifier(live)))
        conn.execute(sql.SQL("CREATE TABLE {} (id integer)").format(sql.Identifier(building)))
        conn.execute(sql.SQL("INSERT INTO {} (id) VALUES (7)").format(sql.Identifier(building)))
        drop_table(conn, previous)
        rename_table(conn, live, previous)
        rename_table(conn, building, live)
        grant_select(conn, live)
    try:
        with psycopg.connect(**common, user=readonly, password=os.environ["MCP_DB_PASSWORD"]) as reader:
            found = reader.execute(sql.SQL("SELECT id FROM {}").format(sql.Identifier(live))).fetchone()[0]
        assert found == 7
    finally:
        with psycopg.connect(**common, user=indexer, password=os.environ["INDEX_DB_PASSWORD"]) as conn:
            for name in (live, previous, holding):
                drop_table(conn, name)
