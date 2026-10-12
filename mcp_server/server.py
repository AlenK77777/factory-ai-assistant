"""MCP server: lets an AI assistant look up production orders and defects.

Runs over stdio; Cursor starts it from .cursor/mcp.json. It connects with the
read-only database role (MCP_DB_USER from .env), so it cannot change any data.
stdout is the MCP channel, so all logging goes to stderr.

Tools:
    get_order_status(order_id)                 one order + its defect count
    list_late_orders(as_of_date=None)          orders past due_date and not Shipped
    defect_summary(days=30, group_by=...)      defect counts and share of total
    search_instructions(query, k=4)            work-instruction chunks closest in meaning

Built with the official MCP Python SDK (v2, where FastMCP is called MCPServer).
search_instructions needs Ollama running locally (query embeddings).
"""

import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Literal

import psycopg
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from psycopg import sql
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
# Lets this script import project modules such as rag.embeddings.
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from rag.embeddings import OllamaError, embed_query, to_pgvector  # noqa: E402

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("factory-db")

REQUIRED_SETTINGS = ("POSTGRES_DB", "MCP_DB_USER", "MCP_DB_PASSWORD")

# group_by value -> (table alias, column). Only these columns can ever reach the SQL.
GROUP_COLUMNS = {
    "station": ("d", "station"),
    "defect_type": ("d", "defect_type"),
    "customer_sector": ("o", "customer_sector"),
}

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

# Measured with nomic-embed-text on docs/instructions (best chunk per question):
# on-topic questions 0.64-0.84, clearly unrelated ones 0.44-0.59, but other
# workplace questions (parking, IT) reach 0.63-0.68. So "relevant" is a hint,
# not proof; the assistant must still check that the text answers the question.
MIN_RELEVANT_SIMILARITY = 0.65

server = MCPServer(
    "factory-db",
    instructions=(
        "Read-only access to SYNTHETIC production orders, defects and work instructions of a PCB plant. "
        "Answer only from tool results. If a tool returns nothing or an error, say so; "
        "do not guess numbers. For procedures use search_instructions with the user's full question. "
        "Answer only from returned text that really answers the question, and name the instruction file. "
        "If found=false, or no returned text answers the question, say that no relevant work instruction "
        "was found; never fill the gap from general knowledge."
    ),
)


# ---------------------------------------------------------------------------
# Result shapes (become the tools' JSON output schemas)
# ---------------------------------------------------------------------------


class OrderStatus(BaseModel):
    order_id: str
    customer_sector: str
    product_type: str
    layers: int
    quantity: int = Field(description="Ordered boards")
    status: str
    production_line: str
    created_date: date
    due_date: date
    is_late: bool = Field(description="due_date is before today and the order is not Shipped")
    defect_count: int = Field(description="Number of defect records logged for this order")
    defective_units: int = Field(description="Sum of the quantities of those defect records")


class LateOrder(BaseModel):
    order_id: str
    customer_sector: str
    product_type: str
    status: str
    production_line: str
    due_date: date
    days_late: int


class LateOrders(BaseModel):
    as_of_date: date
    count: int
    orders: list[LateOrder] = Field(description="Most overdue first")


class DefectGroup(BaseModel):
    name: str = Field(description="Station, defect type or customer sector")
    defect_count: int = Field(description="Number of defect records")
    defective_units: int = Field(description="Sum of defect quantities")
    share_pct: float = Field(description="Share of all defect records in the period, in percent")


class DefectSummary(BaseModel):
    group_by: str
    days: int
    period_start: date
    period_end: date
    total_defects: int
    total_units: int
    groups: list[DefectGroup] = Field(description="Largest group first")


class InstructionChunk(BaseModel):
    source: str = Field(description="Instruction file name in docs/instructions")
    chunk_index: int = Field(description="Position of the chunk in the file, from 0")
    content: str
    distance: float = Field(description="Cosine distance to the query (0 = same meaning)")
    similarity: float = Field(description="1 - distance; higher means closer in meaning")
    relevant: bool = Field(description=f"similarity >= {MIN_RELEVANT_SIMILARITY} (a hint, not proof)")


class InstructionSearch(BaseModel):
    query: str
    found: bool = Field(description="True if at least one chunk is above the similarity threshold")
    min_similarity: float = Field(description="Threshold used for 'relevant'")
    results: list[InstructionChunk] = Field(description="Closest first")


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------


def db_settings() -> dict:
    """Connection settings for the read-only role, from .env."""
    return {
        "host": os.getenv("POSTGRES_HOST", "127.0.0.1"),
        "port": os.getenv("POSTGRES_PORT", "5432"),
        "dbname": os.environ["POSTGRES_DB"],
        "user": os.environ["MCP_DB_USER"],
        "password": os.environ["MCP_DB_PASSWORD"],
        "connect_timeout": 5,
        # Stop any single query after 5 seconds.
        "options": "-c statement_timeout=5000",
    }


def fetch_all(query, params=()) -> list[dict]:
    """Run one parameterised SELECT in a read-only session and return the rows as dicts."""
    try:
        with psycopg.connect(**db_settings(), row_factory=dict_row) as conn:
            conn.read_only = True
            return conn.execute(query, params).fetchall()
    except psycopg.OperationalError as error:
        log.error("Database error: %s", error)
        raise ToolError(
            "The database is not reachable right now. Check that the db container is running "
            "(docker compose ps)."
        ) from error


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@server.tool(annotations=READ_ONLY)
def get_order_status(order_id: str) -> OrderStatus:
    """Get one production order by id (for example PO-10001): its status, dates,
    production line, whether it is late, and how many defects were logged for it."""
    order_id = order_id.strip().upper()
    log.info("get_order_status(%s)", order_id)
    if not order_id:
        raise ToolError("order_id is empty. Order ids look like PO-10001.")

    rows = fetch_all(
        """
        SELECT o.order_id, o.customer_sector, o.product_type, o.layers, o.quantity,
               o.status, o.production_line, o.created_date, o.due_date,
               COUNT(d.defect_id) AS defect_count,
               COALESCE(SUM(d.quantity), 0) AS defective_units
        FROM orders o
        LEFT JOIN defects d ON d.order_id = o.order_id
        WHERE o.order_id = %s
        GROUP BY o.order_id
        """,
        (order_id,),
    )
    if not rows:
        raise ToolError(f"Order {order_id} was not found. Order ids look like PO-10001.")

    order = rows[0]
    is_late = order["due_date"] < date.today() and order["status"] != "Shipped"
    return OrderStatus(**order, is_late=is_late)


@server.tool(annotations=READ_ONLY)
def list_late_orders(as_of_date: date | None = None) -> LateOrders:
    """List orders whose due_date is before as_of_date (YYYY-MM-DD, default today)
    and that are not Shipped yet, most overdue first."""
    as_of = as_of_date or date.today()
    log.info("list_late_orders(as_of_date=%s)", as_of)

    rows = fetch_all(
        """
        SELECT order_id, customer_sector, product_type, status, production_line, due_date,
               %(as_of)s::date - due_date AS days_late
        FROM orders
        WHERE due_date < %(as_of)s AND status <> 'Shipped'
        ORDER BY due_date, order_id
        """,
        {"as_of": as_of},
    )
    orders = [LateOrder(**row) for row in rows]
    return LateOrders(as_of_date=as_of, count=len(orders), orders=orders)


@server.tool(annotations=READ_ONLY)
def defect_summary(
    days: Annotated[int, Field(ge=1, le=3650, description="Look back this many days, including today")] = 30,
    group_by: Literal["station", "defect_type", "customer_sector"] = "station",
) -> DefectSummary:
    """Count defects detected in the last `days` days, grouped by station, defect type
    or customer sector, with each group's share of the total. Use it for questions like
    "which station had the most defects?"."""
    period_end = date.today()
    period_start = period_end - timedelta(days=days - 1)
    log.info("defect_summary(days=%s, group_by=%s)", days, group_by)

    query = sql.SQL(
        """
        SELECT {column} AS name,
               COUNT(*) AS defect_count,
               SUM(d.quantity) AS defective_units
        FROM defects d
        JOIN orders o ON o.order_id = d.order_id
        WHERE d.detected_date BETWEEN %s AND %s
        GROUP BY {column}
        ORDER BY defect_count DESC, name
        """
    ).format(column=sql.Identifier(*GROUP_COLUMNS[group_by]))
    rows = fetch_all(query, (period_start, period_end))

    total_defects = sum(row["defect_count"] for row in rows)
    groups = [
        DefectGroup(**row, share_pct=round(100 * row["defect_count"] / total_defects, 1))
        for row in rows
    ]
    return DefectSummary(
        group_by=group_by,
        days=days,
        period_start=period_start,
        period_end=period_end,
        total_defects=total_defects,
        total_units=sum(group.defective_units for group in groups),
        groups=groups,
    )


@server.tool(annotations=READ_ONLY)
def search_instructions(
    query: str,
    k: Annotated[int, Field(ge=1, le=10, description="Number of chunks to return")] = 4,
) -> InstructionSearch:
    """Search the plant's work instructions (procedures, limits, escalation rules) by meaning.
    Pass the user's full question; short keyword queries score lower.
    Returns the k closest text chunks with their source file and similarity score.
    `relevant`/`found` are a similarity hint: use a chunk only if its text answers the
    question. If nothing does, say no relevant instruction was found; do not answer
    from general knowledge."""
    query = query.strip()
    log.info("search_instructions(%r, k=%s)", query, k)
    if not query:
        raise ToolError("query is empty.")

    try:
        query_vector = to_pgvector(embed_query(query))
    except OllamaError as error:
        log.error("Ollama error: %s", error)
        raise ToolError("Ollama is not reachable, so instructions cannot be searched. Start Ollama and retry.") from error

    try:
        rows = fetch_all(
            """
            SELECT source, chunk_index, content, embedding <=> %(query)s::vector AS distance
            FROM doc_chunks
            ORDER BY distance
            LIMIT %(k)s
            """,
            {"query": query_vector, "k": k},
        )
    except psycopg.errors.UndefinedTable as error:
        raise ToolError("The doc_chunks table does not exist yet (see db/init/03_doc_chunks.sql).") from error
    if not rows:
        raise ToolError("No work instructions are indexed yet. Run: python rag/index_docs.py")

    results = []
    for row in rows:
        similarity = 1 - row["distance"]
        results.append(
            InstructionChunk(
                source=row["source"],
                chunk_index=row["chunk_index"],
                content=row["content"],
                distance=round(row["distance"], 3),
                similarity=round(similarity, 3),
                relevant=similarity >= MIN_RELEVANT_SIMILARITY,
            )
        )
    return InstructionSearch(
        query=query,
        found=any(result.relevant for result in results),
        min_similarity=MIN_RELEVANT_SIMILARITY,
        results=results,
    )


def main() -> None:
    missing = [name for name in REQUIRED_SETTINGS if not os.getenv(name)]
    if missing:
        log.error("Missing settings in %s: %s (see .env.example)", ROOT / ".env", ", ".join(missing))
        sys.exit(1)

    log.info("Starting factory-db MCP server (stdio) as database role %s", os.environ["MCP_DB_USER"])
    server.run()


if __name__ == "__main__":
    main()
