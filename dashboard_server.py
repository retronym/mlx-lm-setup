"""Tiny localhost-only server for dashboard.html. Serves ONLY the dashboard and its two data files.

Usage: python3 dashboard_server.py [port]    (default 8766)
"""
import http.server, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ALLOWED = {
    "/": "dashboard.html",
    "/dashboard.html": "dashboard.html",
    "/data/status.json": "data/status.json",
    "/data/results.jsonl": "data/results.jsonl",
    "/book": "book.html",
    "/book.html": "book.html",
    "/data/book_words.json": "data/book_words.json",
    "/data/book_status.json": "data/book_status.json",
    "/data/book_events.jsonl": "data/book_events.jsonl",
}


class Handler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        rel = ALLOWED.get(path.split("?", 1)[0])
        return os.path.join(HERE, rel) if rel else os.path.join(HERE, "__missing__")

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"dashboard: http://127.0.0.1:{port}/", flush=True)
    srv.serve_forever()
