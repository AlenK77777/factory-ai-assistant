"""Web chat for plant employees. http://127.0.0.1:7777

The browser talks only to this process. This process is an MCP client of
factory-db and calls the local Ollama model. Tool calls do not open the
database. The dashboard reads it with the read-only role.

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
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from chat.admin_auth import basic_auth_matches, basic_username  # noqa: E402
from chat.event_log import classify_outcome, event_source, get_log, log_event, request_source  # noqa: E402
from chat.loop import run_turn  # noqa: E402
from chat.mcp_bridge import McpBridge  # noqa: E402
from chat.settings import (  # noqa: E402
    field_specs,
    load_settings,
    needs_reindex,
    restore_previous,
    review,
    reindex_confirmation_error,
    update_saved,
)
from dashboard.render import render_excel, render_html, snapshot  # noqa: E402
from rag.embeddings import OllamaError, ollama_url, post_json  # noqa: E402
from rag.index_docs import index_documents, rollback_index  # noqa: E402
from rag.language import detect_language  # noqa: E402

HOST = "127.0.0.1"
PORT = 7777
STATIC_PAGE = Path(__file__).resolve().parent / "static" / "index.html"
PID_FILE = ROOT / "logs" / "chat.pid"

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
bridge = McpBridge()
conversations: dict[str, list[dict]] = {}
conversations_lock = threading.Lock()
apply_lock = threading.Lock()


def chat_settings() -> dict:
    return load_settings()


def chat_model() -> str:
    return str(chat_settings()["chat_model"])


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
    """One Ollama chat call with the admin panel's model settings."""
    settings = chat_settings()
    think = bool(settings["think"])
    data = post_json(
        "/api/chat",
        {
            "model": settings["chat_model"],
            "messages": messages,
            "tools": tools,
            "think": think,
            "stream": False,
            "keep_alive": settings["keep_alive"],
            # Thinking tokens count toward num_predict, so leave room when it is on.
            "options": {
                "temperature": settings["temperature"],
                "num_ctx": settings["num_ctx"],
                "num_predict": 4096 if think else 1024,
            },
        },
        timeout=600,
    )
    message = data.get("message")
    if not isinstance(message, dict):
        raise OllamaError("Ollama returned no message.")
    return message


def remember(conversation_id: str, question: str, answer: str) -> None:
    turns = int(chat_settings()["history_turns"])
    with conversations_lock:
        history = conversations.setdefault(conversation_id, [])
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer})
        keep = turns * 2
        if keep <= 0:
            history.clear()
        else:
            del history[:-keep]


def ollama_json(path: str) -> dict | None:
    """GET a small Ollama endpoint. None when Ollama is down."""
    request = urllib.request.Request(ollama_url() + path)
    try:
        with _opener.open(request, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def installed_models() -> list[str]:
    payload = ollama_json("/api/tags")
    if payload is None:
        return []
    return [item.get("name", "") for item in payload.get("models", []) if item.get("name")]


def gpu_models() -> list[dict]:
    """Models currently loaded. VRAM size comes from Ollama; no secrets."""
    payload = ollama_json("/api/ps")
    if payload is None:
        return []
    loaded = []
    for item in payload.get("models", []):
        vram = item.get("size_vram")
        loaded.append(
            {
                "name": item.get("name", ""),
                "vram_mb": round(vram / (1024 * 1024)) if isinstance(vram, (int, float)) else None,
            }
        )
    return loaded


def readonly_info() -> dict:
    """Facts the panel shows and never edits. Passwords are not included."""
    return {
        "database_host": os.getenv("POSTGRES_HOST", "127.0.0.1"),
        "database_port": os.getenv("POSTGRES_PORT", "5432"),
        "mcp_tools": [
            {"name": tool["function"]["name"], "description": tool["function"].get("description") or ""}
            for tool in bridge.tools
        ],
        "gpu_models": gpu_models(),
    }


def read_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0"))
    return json.loads(handler.rfile.read(length).decode("utf-8"))


class ChatHandler(BaseHTTPRequestHandler):
    server_version = "factory-chat/1.0"
    _seen_admins: set[str] = set()

    def _admin_required(self) -> bool:
        """True for admin, dashboards and logs. The employee chat is not included."""
        path = self.path.split("?")[0]
        if path in ("/admin", "/admin/", "/dashboards", "/dashboards/", "/logs", "/logs/"):
            return True
        return (
            path == "/api/settings"
            or path.startswith("/api/settings/")
            or path.startswith("/api/mcp/")
            or path.startswith("/api/dashboards")
            or path.startswith("/api/logs")
        )

    def _reject_admin(self) -> None:
        body = b'{"error":"Admin password required."}'
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Factory admin"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _allow_admin(self) -> bool:
        """False when this request was rejected. The employee chat never calls this."""
        if not self._admin_required():
            return True
        user = os.getenv("ADMIN_USER", "admin")
        password = os.getenv("ADMIN_PASSWORD", "")
        path = self.path.split("?")[0]
        if not password or password == "change-me-admin":
            log_event("ERROR", "admin", "login", outcome="error", path=path, detail="ADMIN_PASSWORD is not set")
            self._send_json(503, {"error": "Set ADMIN_PASSWORD in .env, then run scripts\\generate_certs.ps1."})
            return False
        if basic_auth_matches(self.headers.get("Authorization"), user, password):
            if user not in ChatHandler._seen_admins:
                ChatHandler._seen_admins.add(user)
                log_event("INFO", "admin", "login", outcome="success", user=user, path=path)
            return True
        # The header is not written. Only the user name, when it can be read.
        log_event(
            "WARNING",
            "admin",
            "login",
            outcome="failure",
            user=basic_username(self.headers.get("Authorization")),
            path=path,
        )
        self._reject_admin()
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._allow_admin():
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path in ("/", "/index.html", "/admin", "/admin/", "/dashboards", "/dashboards/", "/logs", "/logs/"):
            self._send(200, STATIC_PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self._send_json(200, self._health())
            return
        if path in ("/api/logs", "/api/logs/"):
            self._logs(query)
            return
        if path == "/api/logs/download":
            self._download_log()
            return
        if path in ("/api/dashboards", "/api/dashboards/"):
            self._dashboard(query)
            return
        if path == "/api/dashboards/excel":
            self._dashboard_excel(query)
            return
        if path == "/api/settings":
            current = chat_settings()
            self._send_json(
                200,
                {
                    "values": {key: value for key, value in current.items() if key != "version"},
                    "version": current["version"],
                    "fields": field_specs(),
                    "models": installed_models(),
                    "readonly": readonly_info(),
                },
            )
            return
        self._send_json(404, {"error": "That page does not exist."})

    def do_POST(self) -> None:  # noqa: N802
        if not self._allow_admin():
            return
        if self.path == "/api/shutdown":
            self._shutdown()
            return
        if self.path == "/api/settings/preview":
            self._preview()
            return
        if self.path == "/api/settings/apply":
            self._apply()
            return
        if self.path == "/api/settings/rollback":
            self._rollback()
            return
        if self.path == "/api/mcp/restart":
            self._restart_mcp()
            return
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
        # source is a label for statistics, not a security control.
        # Missing means the chat page. An unknown value is rejected, not stored as user.
        source = request_source(body.get("source")) if "source" in body else "user"
        if source is None:
            self._send_json(400, {"error": "source must be user, smoke, or test."})
            return
        conversation_id = str(body.get("conversation_id") or "").strip() or str(uuid.uuid4())

        language = detect_language(question)
        ollama_error = ollama_problem()
        if ollama_error:
            self._log_question(question, language, "error", 0, [], ollama_error, source)
            self._send_json(503, {"error": ollama_error})
            return
        if not bridge.ready:
            reason = bridge.error or "The MCP server is not available."
            self._log_question(question, language, "error", 0, [], reason, source)
            self._send_json(503, {"error": reason})
            return

        with conversations_lock:
            history = list(conversations.get(conversation_id, []))
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
            elapsed = round(time.perf_counter() - started, 2)
            self._log_question(question, language, "error", elapsed, [], str(error), source)
            self._send_json(503, {"error": str(error)})
            return
        elapsed = round(time.perf_counter() - started, 2)
        outcome = classify_outcome(result.answer, any(trace.is_error for trace in result.traces))
        self._log_question(question, result.language, outcome, elapsed, result.traces, source=source)
        remember(conversation_id, question, result.answer)
        self._send_json(
            200,
            {
                "conversation_id": conversation_id,
                "answer": result.answer,
                "language": result.language,
                "model": chat_model(),
                "settings_version": chat_settings()["version"],
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

    def _preview(self) -> None:
        try:
            body = read_body(self)
        except (ValueError, UnicodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "The request was not valid JSON."})
            return
        models = installed_models()
        if not models:
            self._send_json(200, {"ok": False, "errors": [ollama_problem() or "Ollama returned no models."], "warnings": [], "changes": [], "needs_reindex": False, "check_vector_size": False})
            return
        result = review(chat_settings(), body.get("values", body), models)
        result.pop("values", None)
        self._send_json(200, result)

    def _apply(self) -> None:
        try:
            body = read_body(self)
        except (ValueError, UnicodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "The request was not valid JSON."})
            return
        models = installed_models()
        if not models:
            self._send_json(503, {"error": ollama_problem() or "Ollama returned no models."})
            return
        current = chat_settings()
        result = review(current, body.get("values", body), models)
        if not result["ok"]:
            self._send_json(400, {"error": " ".join(result["errors"]), "errors": result["errors"]})
            return
        if not result["changes"]:
            self._send_json(200, {"ok": True, "unchanged": True, "version": current["version"]})
            return
        blocked = reindex_confirmation_error(result["needs_reindex"], bool(body.get("confirm_reindex")))
        if blocked:
            self._send_json(400, {"error": blocked})
            return
        if not apply_lock.acquire(blocking=False):
            self._send_json(409, {"error": "Another settings change is already running."})
            return

        self.protocol_version = "HTTP/1.0"
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.end_headers()

        def emit(event: dict) -> None:
            self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()

        saved_ok = False
        try:
            values = result["values"]
            # Save first so a re-index check uses the new k. A failed build restores this file.
            saved = update_saved(values)
            saved_ok = True
            changes = "; ".join(
                f"{change['label']}: {change['before']} -> {change['after']}" for change in result["changes"]
            )
            if result["needs_reindex"]:
                emit({"stage": "index", "message": "Building a new instruction index. The live chat still uses the old one."})
                try:
                    index_documents(values, on_progress=emit)
                except Exception:
                    restore_previous()
                    saved_ok = False
                    raise
            log_event(
                "INFO",
                "settings",
                "apply",
                version=saved["version"],
                changes=changes,
                reindex=bool(result["needs_reindex"]),
            )
            if result["needs_restart"]:
                with conversations_lock:
                    conversations.clear()
                emit({"stage": "mcp", "message": "Restarting the MCP server."})
                try:
                    bridge.restart()
                except RuntimeError as error:
                    log_event("ERROR", "mcp", "restart", outcome="error", reason="settings", error=str(error))
                    raise
                log_event("INFO", "mcp", "restart", outcome="success", reason="settings")
                done = f"Settings version {saved['version']} is active. The chat was reset."
            else:
                done = f"Settings version {saved['version']} is active."
            emit({"stage": "done", "message": done, "version": saved["version"]})
        except Exception as error:  # noqa: BLE001 - the panel shows the failure, the process stays up
            if not saved_ok:
                log_event("ERROR", "settings", "apply", error=str(error))
            emit({"stage": "error", "message": str(error), "retry": bool(result.get("needs_restart"))})
        finally:
            apply_lock.release()

    def _rollback(self) -> None:
        """Restore the previous settings version and, if needed, the previous index."""
        if not apply_lock.acquire(blocking=False):
            self._send_json(409, {"error": "Another settings change is already running."})
            return
        try:
            current = chat_settings()
            restored = restore_previous()
            index_restored = False
            if needs_reindex(current, restored):
                index_restored = rollback_index()
            log_event(
                "INFO",
                "settings",
                "rollback",
                version=restored["version"],
                index_restored=index_restored,
            )
            self._send_json(
                200,
                {
                    "ok": True,
                    "version": restored["version"],
                    "index_restored": index_restored,
                    "message": f"Restored settings version {restored['version']}.",
                },
            )
        except RuntimeError as error:
            self._send_json(409, {"error": str(error)})
        finally:
            apply_lock.release()

    def _restart_mcp(self) -> None:
        """Try the MCP connection again. Used by the Retry button after Apply."""
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self._send_json(403, {"error": "Retry is only allowed from this computer."})
            return
        try:
            bridge.restart()
        except RuntimeError as error:
            log_event("ERROR", "mcp", "restart", outcome="error", reason="retry", error=str(error))
            self._send_json(503, {"ok": False, "error": str(error)})
            return
        log_event("INFO", "mcp", "restart", outcome="success", reason="retry")
        self._send_json(200, {"ok": True, "tools": [tool["function"]["name"] for tool in bridge.tools]})

    def _shutdown(self) -> None:
        """Stop this process. stop-chat.bat calls this from the same computer."""
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self._send_json(403, {"error": "Shutdown is only allowed from this computer."})
            return
        self._send_json(200, {"ok": True, "message": "Stopping the chat server."})
        # Let this response finish, then stop serve_forever from another thread.
        threading.Timer(0.3, self.server.shutdown).start()

    def _health(self) -> dict:
        ollama = ollama_problem()
        mcp = None if bridge.ready else (bridge.error or "The MCP server is not available.")
        log_event(
            "INFO" if not ollama and not mcp else "WARNING",
            "server",
            "health",
            ollama="ok" if not ollama else ollama,
            mcp="ok" if not mcp else mcp,
            model=chat_model(),
        )
        return {"ollama": ollama, "mcp": mcp, "model": chat_model()}

    def _log_question(self, question: str, language: str, outcome: str, elapsed: float, traces, error: str = "", source: str = "user") -> None:
        settings = chat_settings()
        details = {
            "language": language,
            "model": settings.get("chat_model", ""),
            "settings_version": settings.get("version", ""),
            "latency_s": elapsed,
            "outcome": outcome,
            "source": source,
            "question": question,
            "tools": [{"name": trace.name, "arguments": trace.arguments} for trace in traces],
        }
        if error:
            details["error"] = error
        log_event("ERROR" if outcome == "error" else "INFO", "chat", "question", **details)

    def _logs(self, query: dict) -> None:
        level = (query.get("level") or [""])[0].strip()
        component = (query.get("component") or [""])[0].strip()
        if level and level not in ("INFO", "WARNING", "ERROR"):
            self._send_json(400, {"error": "Level must be INFO, WARNING or ERROR."})
            return
        try:
            limit = int((query.get("limit") or ["200"])[0])
        except ValueError:
            limit = 200
        limit = max(1, min(limit, 1000))
        events = get_log().read_events(level or None, component or None, limit)
        for event in events:
            event["source"] = event_source(event)
        self._send_json(200, {"events": events}, no_store=True)

    def _download_log(self) -> None:
        body = get_log().read_text().encode("utf-8")
        self._send(200, body, "text/plain; charset=utf-8", 'attachment; filename="events.txt"', no_store=True)

    def _include_test(self, query: dict) -> bool:
        flag = (query.get("include_test") or ["0"])[0].strip().lower()
        return flag in ("1", "true", "yes")

    def _dashboard(self, query: dict) -> None:
        try:
            report = snapshot(include_test=self._include_test(query))
            document = render_html(report)
        except Exception as error:  # noqa: BLE001 - the page shows a failure, the process stays up
            log_event("ERROR", "dashboard", "build", error=str(error))
            self._send_json(500, {"error": "The dashboard could not be built."})
            return
        self._send_json(200, {"generated_at": report["generated_at"], "html": document}, no_store=True)

    def _dashboard_excel(self, query: dict) -> None:
        try:
            report = snapshot(include_test=self._include_test(query))
            blob = render_excel(report)
        except Exception as error:  # noqa: BLE001
            log_event("ERROR", "dashboard", "excel", error=str(error))
            self._send_json(500, {"error": "The workbook could not be built."})
            return
        self._send(
            200,
            blob,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            'attachment; filename="factory-dashboard.xlsx"',
            no_store=True,
        )

    def _send_json(self, status: int, payload: dict, no_store: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", no_store=no_store)

    def _send(self, status: int, body: bytes, content_type: str, disposition: str | None = None, no_store: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if disposition:
            self.send_header("Content-Disposition", disposition)
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[chat] {self.address_string()} {fmt % args}", file=sys.stderr)


def write_pid() -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")


def remove_pid() -> None:
    try:
        if PID_FILE.exists() and PID_FILE.read_text(encoding="ascii").strip() == str(os.getpid()):
            PID_FILE.unlink()
    except OSError:
        pass


def main() -> None:
    print("Starting the factory-db MCP server...", file=sys.stderr)
    bridge.start()
    if bridge.ready:
        names = ", ".join(tool["function"]["name"] for tool in bridge.tools)
        print(f"MCP ready, tools: {names}", file=sys.stderr)
        log_event("INFO", "mcp", "start", outcome="success", tools=names)
    else:
        print(bridge.error, file=sys.stderr)
        log_event("ERROR", "mcp", "start", outcome="error", error=bridge.error or "not available")
    log_event("INFO", "server", "start", host=HOST, port=PORT)

    try:
        httpd = ThreadingHTTPServer((HOST, PORT), ChatHandler)
    except OSError as error:
        sys.exit(f"Cannot listen on {HOST}:{PORT} ({error}). Is the chat already running?")
    write_pid()
    print(f"Chat is at http://{HOST}:{PORT}", flush=True)
    print("Stop it with stop-chat.bat or Ctrl+C in this window.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.", file=sys.stderr)
    finally:
        log_event("INFO", "server", "stop")
        log_event("INFO", "mcp", "stop")
        httpd.server_close()
        bridge.close()
        remove_pid()


if __name__ == "__main__":
    main()
