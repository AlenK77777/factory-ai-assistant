"""A long-lived MCP client for the factory-db server.

The web app calls tools only through this bridge. It never opens the database.
"""

import asyncio
import sys
import threading
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parent.parent


def ollama_tools_from_mcp(tools) -> list[dict]:
    """Turn MCP tool definitions into the tool list Ollama expects."""
    converted = []
    for tool in tools:
        schema = tool.input_schema
        if hasattr(schema, "model_dump"):
            schema = schema.model_dump(by_alias=True, exclude_none=True)
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": schema,
                },
            }
        )
    return converted


class McpBridge:
    """One stdio MCP session, shared by the web server's request threads."""

    def __init__(self) -> None:
        self.tools: list[dict] = []
        self.error: str | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="mcp-bridge", daemon=True)
        self._lock = threading.Lock()
        self._session: ClientSession | None = None
        self._stdio = None
        self._thread.start()

    @property
    def ready(self) -> bool:
        return self._session is not None and self.error is None

    def start(self) -> None:
        """Start the MCP server subprocess. On failure, error is set and the web UI can say so."""
        try:
            future = asyncio.run_coroutine_threadsafe(self._open(), self._loop)
            future.result(timeout=30)
        except Exception as error:  # noqa: BLE001 - startup must become a message, not a crash
            self.error = (
                "The factory-db MCP server did not start. "
                f"{error} Check that the database is running (docker compose ps) and .env is filled in."
            )
            self._session = None

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Run one tool. Returns {"result", "is_error"}."""
        if not self.ready or self._session is None:
            return {"result": self.error or "The MCP server is not available.", "is_error": True}
        with self._lock:
            future = asyncio.run_coroutine_threadsafe(self._call(name, arguments), self._loop)
            try:
                return future.result(timeout=120)
            except Exception as error:  # noqa: BLE001
                self.error = f"The MCP server stopped responding ({error}). Restart the chat."
                return {"result": self.error, "is_error": True}

    def restart(self) -> None:
        """Stop the MCP process and start it again, so it reloads settings."""
        with self._lock:
            try:
                future = asyncio.run_coroutine_threadsafe(self._restart(), self._loop)
                future.result(timeout=30)
            except Exception as error:  # noqa: BLE001
                self.error = f"The MCP server did not restart. {error}"
                self._session = None
                raise RuntimeError(self.error) from error

    def close(self) -> None:
        if self._session is not None:
            future = asyncio.run_coroutine_threadsafe(self._close(), self._loop)
            try:
                future.result(timeout=10)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)

    async def _open(self) -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(ROOT / "mcp_server" / "server.py")],
            cwd=str(ROOT),
        )
        self._stdio = stdio_client(params)
        read, write = await self._stdio.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        listed = await self._session.list_tools()
        self.tools = ollama_tools_from_mcp(listed.tools)
        self.error = None

    async def _call(self, name: str, arguments: dict) -> dict:
        assert self._session is not None
        result = await self._session.call_tool(name, arguments)
        if getattr(result, "is_error", False):
            text = " ".join(getattr(block, "text", "") for block in result.content).strip()
            return {"result": text or "The tool returned an error.", "is_error": True}
        if getattr(result, "structured_content", None) is not None:
            return {"result": result.structured_content, "is_error": False}
        text = " ".join(getattr(block, "text", "") for block in getattr(result, "content", [])).strip()
        return {"result": text, "is_error": False}

    async def _restart(self) -> None:
        await self._close()
        await self._open()

    async def _close(self) -> None:
        if self._session is not None:
            await self._session.__aexit__(None, None, None)
            self._session = None
        if self._stdio is not None:
            await self._stdio.__aexit__(None, None, None)
            self._stdio = None
