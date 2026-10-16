"""Authenticated localhost bridge for controlling the user's existing Edge tabs.

The extension polls this service; no browser profile is opened or cloned. The
service binds only to loopback and requires a per-install bearer token.
"""
from __future__ import annotations

import json
import os
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_HOST = "127.0.0.1"
_PORT = 18763
_MAX_BODY = 128 * 1024
_ALLOWED_ACTIONS = {
    "go_to", "search", "new_tab", "click", "type", "scroll", "fill_form",
    "smart_click", "smart_type", "get_text", "get_url", "press", "close_tab",
    "back", "forward", "reload",
}


class BrowserBridge:
    """Small authenticated command queue consumed by the Mark-LV Edge extension."""

    def __init__(self, host: str = _HOST, port: int = _PORT):
        self.host, self.port = host, port
        self._pending: queue.Queue[dict[str, Any]] = queue.Queue()
        self._results: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.token_path = Path.home() / ".mark-lv" / "browser_bridge.token"
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        if self.token_path.exists():
            self.token = self.token_path.read_text(encoding="utf-8").strip()
        else:
            self.token = secrets.token_urlsafe(32)
            self.token_path.write_text(self.token, encoding="utf-8")
            try:
                os.chmod(self.token_path, 0o600)
            except OSError:
                pass
        if len(self.token) < 32:
            raise RuntimeError(f"Browser bridge token is invalid. Remove {self.token_path} and restart Mark-LV.")

    def start(self) -> bool:
        if self._server:
            return True
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "MarkLVBrowserBridge/1.0"

            def log_message(self, *_args):
                return

            def _json(self, status: int, value: dict[str, Any]):
                body = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                origin = self.headers.get("Origin", "")
                if origin.startswith("chrome-extension://") or origin.startswith("moz-extension://"):
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                    self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self._json(204, {})

            def _authorized(self) -> bool:
                supplied = self.headers.get("Authorization", "")
                expected = "Bearer " + bridge.token
                return secrets.compare_digest(supplied, expected)

            def do_GET(self):
                if not self._authorized():
                    self._json(401, {"error": "unauthorized"})
                    return
                route = urlparse(self.path).path
                if route == "/health":
                    self._json(200, {"ok": True, "service": "mark-lv-browser-bridge"})
                elif route == "/poll":
                    try:
                        item = bridge._pending.get(timeout=15)
                        self._json(200, {"command": item})
                    except queue.Empty:
                        self._json(200, {"command": None})
                else:
                    self._json(404, {"error": "not_found"})

            def do_POST(self):
                if not self._authorized():
                    self._json(401, {"error": "unauthorized"})
                    return
                route = urlparse(self.path).path
                if route == "/heartbeat":
                    bridge.heartbeat()
                    self._json(200, {"ok": True})
                    return
                if route != "/result":
                    self._json(404, {"error": "not_found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 1 or length > _MAX_BODY:
                        raise ValueError("invalid body size")
                    data = json.loads(self.rfile.read(length).decode("utf-8"))
                    command_id = str(data.get("id", ""))
                    if not command_id or len(command_id) > 100:
                        raise ValueError("invalid command id")
                    result = {"result": str(data.get("result", ""))[:20000],
                              "ok": bool(data.get("ok", True))}
                    with bridge._lock:
                        bridge._results[command_id] = (time.monotonic(), result)
                    self._json(200, {"ok": True})
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._json(400, {"error": str(exc)})

        try:
            self._server = ThreadingHTTPServer((self.host, self.port), Handler)
            self._server.daemon_threads = True
            self._thread = threading.Thread(target=self._server.serve_forever,
                                            name="MarkLVBrowserBridge", daemon=True)
            self._thread.start()
            return True
        except OSError as exc:
            self._server = None
            print(f"[BrowserBridge] Could not start localhost bridge: {exc}")
            return False

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def connected(self) -> bool:
        # The extension refreshes this heartbeat via /heartbeat.
        with self._lock:
            self._prune()
            return bool(getattr(self, "_last_heartbeat", 0) and
                        time.monotonic() - self._last_heartbeat < 35)

    def submit(self, action: str, parameters: dict[str, Any], timeout: float = 25) -> str | None:
        if action not in _ALLOWED_ACTIONS or not self.connected():
            return None
        command_id = secrets.token_urlsafe(12)
        command = {"id": command_id, "action": action, "parameters": parameters,
                   "created": time.time()}
        self._pending.put(command)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                self._prune()
                entry = self._results.pop(command_id, None)
            if entry:
                return entry[1].get("result", "")
            time.sleep(0.1)
        return "The Edge extension did not respond in time. Check that Mark-LV Bridge is enabled in Edge."

    def heartbeat(self):
        with self._lock:
            self._last_heartbeat = time.monotonic()

    def _prune(self):
        now = time.monotonic()
        self._results = {key: val for key, val in self._results.items()
                         if now - val[0] < 120}
