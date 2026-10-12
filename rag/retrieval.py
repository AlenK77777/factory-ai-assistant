"""Find the work-instruction chunks closest in meaning to a question.

Shared by the MCP tool search_instructions and by rag/ask_local.py, so both
retrieve exactly the same way.
"""

from psycopg.rows import dict_row

from rag.embeddings import embed_query, to_pgvector

# Measured with nomic-embed-text on docs/instructions (best chunk per question):
# on-topic questions 0.64-0.84, clearly unrelated ones 0.44-0.59, but other
# workplace questions (parking, IT) reach 0.63-0.68. So "relevant" is a hint,
# not proof; whoever answers must still check that the text answers the question.
MIN_RELEVANT_SIMILARITY = 0.65

SEARCH_SQL = """
    SELECT source, chunk_index, content, embedding <=> %(query)s::vector AS distance
    FROM doc_chunks
    ORDER BY distance
    LIMIT %(k)s
"""


def search_chunks(conn, query: str, k: int = 4) -> list[dict]:
    """Return the k closest chunks, closest first.

    Each chunk is a dict with source, chunk_index, content, distance (cosine,
    pgvector <=>), similarity (1 - distance) and relevant (similarity above the
    threshold). Raises OllamaError if the question cannot be embedded.
    """
    query_vector = to_pgvector(embed_query(query))
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(SEARCH_SQL, {"query": query_vector, "k": k}).fetchall()

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
                "relevant": similarity >= MIN_RELEVANT_SIMILARITY,
            }
        )
    return chunks
