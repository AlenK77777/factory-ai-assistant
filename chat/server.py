"""Web chat for plant employees. http://127.0.0.1:7777

The browser talks only to this process. This process is an MCP client of
factory-db and calls the local Ollama model. It does not open the database.

Start (from the project root, venv active):
    python chat/server.py
Stop: Ctrl+C in that window.
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from chat.loop import run_turn  # noqa: E402
from chat.mcp_bridge import McpBridge  # noqa: E402
from rag.embeddings import OllamaError, ollama_url, post_json  # noqa: E402
from rag.language import detect_language  # noqa: E402

HOST = "127.0.0.1"
PORT = 7777
# Four earlier turns (user + assistant), enough for a follow-up.
MAX_HISTORY_MESSAGES = 8
STATIC_PAGE = Path(__file__).resolve().parent / "static" / "index.html"

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
bridge = McpBridge()
conversations: dict[str, list[dict]] = {}
conversations_lock = threading.Lock()


def chat_model() -> str:
    return os.getenv("OLLAMA_CHAT_MODEL", "qwen3:8b")


def ollama_problem() -> str | None:
    """None if Ollama is up and the configured chat model is pulled."""
    model = chat_model()
    request = urllib.request.Request(ollama_url() + "/api/tags")
    try:
        with _opener.open(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        return f"Ollama is not reachable at {ollama_url()} ({error}). Start Ollama and retry."
    names = [item.get("name", "") for item in payload.get("models", [])]
    if model not in names:
        return f"Ollama is running, but the model {model} is not pulled. Run: ollama pull {model}"
    return None


def model_fn(messages: list[dict], tools: list[dict]) -> dict:
    """One Ollama chat call with tools. Thinking is off so tool calls stay in tool_calls."""
    data = post_json(
        "/api/chat",
        {
            "model": chat_model(),
            "messages": messages,
            "tools": tools,
            "think": False,
            "stream": False,
            "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 1024},
        },
        timeout=600,
    )
    message = data.get("message")
    if not isinstance(message, dict):
        raise OllamaError("Ollama returned no message.")
    return message


def remember(conversation_id: str, question: str, answer: str) -> None:
    with conversations_lock:
        history = conversations.setdefault(conversation_id, [])
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer})
        del history[:-MAX_HISTORY_MESSAGES]


class ChatHandler(BaseHTTPRequestHandler):
    server_version = "factory-chat/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, STATIC_PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if self.path == "/api/health":
            self._send_json(200, self._health())
            return
        self._send_json(404, {"error": "That page does not exist."})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/chat":
            self._send_json(404, {"error": "That page does not exist."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "The request was not valid JSON."})
            return

        question = str(body.get("message") or "").strip()
        if not question:
            self._send_json(400, {"error": "Type a question first."})
            return
        conversation_id = str(body.get("conversation_id") or "").strip() or str(uuid.uuid4())

        ollama_error = ollama_problem()
        if ollama_error:
            self._send_json(503, {"error": ollama_error})
            return
        if not bridge.ready:
            self._send_json(503, {"error": bridge.error or "The MCP server is not available."})
            return

        with conversations_lock:
            history = list(conversations.get(conversation_id, []))
        language = detect_language(question)
        started = time.perf_counter()
        try:
            result = run_turn(
                question,
                history,
                bridge.tools,
                language,
                model_fn,
                bridge.call_tool,
            )
        except OllamaError as error:
            self._send_json(503, {"error": str(error)})
            return
        elapsed = round(time.perf_counter() - started, 2)
        remember(conversation_id, question, result.answer)
        self._send_json(
            200,
            {
                "conversation_id": conversation_id,
                "answer": result.answer,
                "language": result.language,
                "model": chat_model(),
                "elapsed_s": elapsed,
                "tools": [
                    {
                        "name": trace.name,
                        "arguments": trace.arguments,
                        "result": trace.result,
                        "is_error": trace.is_error,
                    }
                    for trace in result.traces
                ],
            },
        )

    def _health(self) -> dict:
        return {
            "ollama": ollama_problem(),
            "mcp": None if bridge.ready else (bridge.error or "The MCP server is not available."),
            "model": chat_model(),
        }

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[chat] {self.address_string()} {fmt % args}", file=sys.stderr)


def main() -> None:
    print("Starting the factory-db MCP server...", file=sys.stderr)
    bridge.start()
    if bridge.ready:
        names = ", ".join(tool["function"]["name"] for tool in bridge.tools)
        print(f"MCP ready, tools: {names}", file=sys.stderr)
    else:
        print(bridge.error, file=sys.stderr)

    try:
        httpd = ThreadingHTTPServer((HOST, PORT), ChatHandler)
    except OSError as error:
        sys.exit(f"Cannot listen on {HOST}:{PORT} ({error}). Is the chat already running?")
    print(f"Chat is at http://{HOST}:{PORT}", flush=True)
    print("Stop it with Ctrl+C in this window.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.", file=sys.stderr)
    finally:
        httpd.server_close()
        bridge.close()


if __name__ == "__main__":
    main()
