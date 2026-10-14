"""Find the work-instruction chunks closest in meaning to a question.

Shared by the MCP tool search_instructions and by rag/ask_local.py, so both
retrieve exactly the same way.
"""

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from rag.embeddings import embed_query, to_pgvector

# Measured with qwen3-embedding:0.6b on docs/instructions, 12 on-topic and
# 9 off-topic questions in English, Russian and Hebrew (best chunk each):
#   English  on-topic 0.63-0.82, off-topic 0.15-0.51
#   Russian  on-topic 0.49-0.63, off-topic 0.10-0.45
#   Hebrew   on-topic 0.48-0.63, off-topic 0.20-0.51
# 0.48 is the value with the fewest mistakes across the three languages.
# Hebrew on-topic and workplace off-topic still overlap, so "relevant" is a
# hint, not proof; whoever answers must still check that the text answers
# the question.
MIN_RELEVANT_SIMILARITY = 0.48

# Measured again after structure-aware chunking (12 EN/RU/HE questions).
# The immediate-actions list is now one chunk, but for
# "What should I do if delamination is found?" that chunk is rank 6.
#   k=4: file 12/12, 12/12, 11/12; required text 9/10, 10/10, 9/10
#   k=6: file 12/12 and required text 10/10 in every language
#   k=8: same hits as k=6
# k=6 is still the smallest value that always retrieves the required steps.
DEFAULT_K = 6

# Short check run after a re-index. Each question should surface this file.
RETRIEVAL_CHECKS = (
    ("What should I do if delamination is found?", "delamination-containment.md"),
    ("What are the ESD rules on the line?", "esd-rules.md"),
    ("How do I release an order that is on hold?", "order-hold-and-release.md"),
)

SEARCH_SQL = """
    SELECT source, chunk_index, content, embedding <=> %(query)s::vector AS distance
    FROM {}
    ORDER BY distance
    LIMIT %(k)s
"""


def active_embedding_model(conn) -> str:
    """Model stored with the live index. Not the setting that an Apply has not swapped in yet."""
    with conn.cursor(row_factory=dict_row) as cur:
        try:
            row = cur.execute("SELECT embedding_model FROM doc_index WHERE id = 1").fetchone()
        except psycopg.errors.UndefinedTable:
            row = None
    if not row or not row.get("embedding_model"):
        raise RuntimeError("The active index has no embedding model stored in doc_index.")
    return str(row["embedding_model"])


def search_chunks(
    conn,
    query: str,
    k: int = DEFAULT_K,
    min_similarity: float | None = None,
    embedding_model: str | None = None,
    table: str = "doc_chunks",
) -> list[dict]:
    """Return the closest chunks, closest first.

    k is used as given. Callers that should follow the admin panel pass that saved k.
    The query vector uses embedding_model, or the model stored on the active
    index when that argument is omitted. min_similarity marks the hint flag.
    """
    if min_similarity is None:
        from chat.settings import load_settings

        min_similarity = float(load_settings()["similarity_threshold"])
    k = int(k)
    if embedding_model is None:
        embedding_model = active_embedding_model(conn)
    query_vector = to_pgvector(embed_query(query, model=embedding_model))
    statement = sql.SQL(SEARCH_SQL).format(sql.Identifier(table))
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(statement, {"query": query_vector, "k": k}).fetchall()

    chunks = []
    for row in rows:
        similarity = 1 - row["distance"]
        chunks.append(
            {
                "source": row["source"],
                "chunk_index": row["chunk_index"],
                "content": row["content"],
                "distance": round(row["distance"], 3),
                "similarity": round(similarity, 3),
                "relevant": similarity >= min_similarity,
            }
        )
    return chunks
