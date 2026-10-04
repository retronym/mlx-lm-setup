"""Tiny shared JSON-over-HTTP scaffolding for backend servers (stdlib only, so it runs in any pinned venv).

Contract with the supervisor: the process listens on 127.0.0.1:<port> ONLY AFTER the model is loaded, so a successful
GET /health means "ready". Requests are serialized with a lock (these models are not safe to hammer concurrently).
"""
import json, threading, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 8 * 1024 * 1024


class Raw:
    """A route may return ``Raw(bytes, content_type)`` instead of a JSON-serialisable object (audio, for instance)."""
    def __init__(self, body: bytes, content_type: str):
        self.body, self.content_type = body, content_type


def serve(port: int, health: dict, routes: dict, max_body: int = MAX_BODY) -> None:
    lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code: int, obj) -> None:
            if isinstance(obj, Raw):
                body, ctype = obj.body, obj.content_type
            else:
                body, ctype = json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8"
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"status": "ok", **health})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            fn = routes.get(self.path)
            if fn is None:
                return self._send(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length", 0))
            if n > max_body:
                return self._send(413, {"error": "body too large"})
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError as e:
                return self._send(400, {"error": f"invalid JSON: {e}"})
            try:
                with lock:
                    out = fn(req)
                self._send(200, out)
            except (ValueError, KeyError, TypeError) as e:        # client errors: bad question, over budget, missing field
                self._send(400, {"error": f"{type(e).__name__}: {e}"})
            except Exception as e:                                # noqa: BLE001
                traceback.print_exc()
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    print(f"READY on 127.0.0.1:{port}", flush=True)
    srv.serve_forever()
