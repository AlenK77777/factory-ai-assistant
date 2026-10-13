"""A long-lived MCP client for the factory-db server.

The web app calls tools only through this bridge. It never opens the database.

The stdio connection uses a cancel scope, which must be entered and exited by
the same task. Every open, call, restart and close therefore runs on one
worker task. Restarting from a different task crashes with
"Attempted to exit cancel scope in a different task than it was entered in".
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
        self._session: ClientSession | None = None
        self._stdio = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="mcp-bridge", daemon=True)
        self._lock = threading.Lock()
        self._queue: asyncio.Queue | None = None
        self._thread.start()
        started = threading.Event()

        async def boot() -> None:
            self._queue = asyncio.Queue()
            started.set()
            await self._worker()

        asyncio.run_coroutine_threadsafe(boot(), self._loop)
        if not started.wait(timeout=5):
            raise RuntimeError("The MCP bridge thread did not start.")

    @property
    def ready(self) -> bool:
        return self._session is not None and self.error is None

    def start(self) -> None:
        """Start the MCP server subprocess. On failure, error is set and the web UI can say so."""
        try:
            self._submit("open", timeout=30)
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
            try:
                return self._submit("call", timeout=120, name=name, arguments=arguments)
            except Exception as error:  # noqa: BLE001
                self.error = f"The MCP server stopped responding ({error}). Use Retry on the page."
                return {"result": self.error, "is_error": True}

    def restart(self) -> None:
        """Stop the MCP process and start it again, on the same worker task."""
        with self._lock:
            try:
                self._submit("restart", timeout=30)
            except Exception as error:  # noqa: BLE001
                self.error = f"The MCP server did not restart. {error}"
                self._session = None
                raise RuntimeError(self.error) from error

    def close(self) -> None:
        try:
            self._submit("close", timeout=15)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)

    def _submit(self, op: str, timeout: float, **fields):
        """Queue one job for the worker and wait for it."""

        async def go():
            future = self._loop.create_future()
            await self._queue.put({"op": op, "future": future, **fields})
            return await future

        return asyncio.run_coroutine_threadsafe(go(), self._loop).result(timeout)

    async def _worker(self) -> None:
        """Own the MCP connection for the life of the bridge."""
        while True:
            job = await self._queue.get()
            future = job["future"]
            try:
                if job["op"] == "close":
                    await self._close()
                    if not future.done():
                        future.set_result(None)
                    return
                if job["op"] == "open":
                    await self._open()
                elif job["op"] == "restart":
                    await self._close()
                    await self._open()
                elif job["op"] == "call":
                    result = await self._call(job["name"], job["arguments"])
                    if not future.done():
                        future.set_result(result)
                    continue
                if not future.done():
                    future.set_result(None)
            except Exception as error:
                if not future.done():
                    future.set_exception(error)

    async def _open(self) -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(ROOT / "mcp_server" / "server.py")],
            cwd=str(ROOT),
        )
        self._stdio = stdio_client(params)
        try:
            read, write = await self._stdio.__aenter__()
            self._session = ClientSession(read, write)
            await self._session.__aenter__()
            await self._session.initialize()
            listed = await self._session.list_tools()
            self.tools = ollama_tools_from_mcp(listed.tools)
            self.error = None
        except Exception:
            await self._close()
            raise

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

    async def _close(self) -> None:
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None
        if self._stdio is not None:
            try:
                await self._stdio.__aexit__(None, None, None)
            except Exception:
                pass
            self._stdio = None
