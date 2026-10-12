"""Find the work-instruction chunks closest in meaning to a question.

Shared by the MCP tool search_instructions and by rag/ask_local.py, so both
retrieve exactly the same way.
"""

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
