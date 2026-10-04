#!/usr/bin/env python3
"""Standalone progress dashboard for the search index: per source, how much is synced and how much is embedded, live.
Reads data/search.db read-only (never writes, never loads a model), so it is safe to run next to sync.py and embed.py.

usage: python3 dashboard.py [port]     (default 8767; stdlib only)  ->  http://127.0.0.1:8767/
"""
import collections, json, os, sqlite3, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "search.db")
sys.path.insert(0, HERE)

# source -> (label, kind, horizon): git sources are synced when every file of the tree is indexed; GitHub streams walk `updated`
# ascending from the horizon to now, so the cursor's position on that timeline is the progress.
SOURCES = {
    "scalac": ("scala/scala code and spec", "git", None),
    "scala3": ("scala/scala3 code", "git", None),
    "scala3docs": ("Scala 3 docs", "git", None),
    "bug": ("scala/bug issues and comments", "gh", "2000-01-01T00:00:00Z"),
    "scalapr": ("scala/scala PRs and comments", "gh", "2020-01-01T00:00:00Z"),
}
STREAMS = [("since_issues", "issues"), ("since_comments", "comments"), ("since_reviews", "review comments")]
_lock = threading.Lock()
_hist = collections.deque(maxlen=90)      # (time, embedded, chunks) samples, for rates
_tree = {}                                # source -> (time, files in tree)


def tree_files(name):
    """Files a git source would index, cached for a minute (a git call per poll is wasteful)."""
    t, n = _tree.get(name, (0, None))
    if time.time() - t > 60:
        try:
            from sources.gitsrc import SOURCES as GIT
            s = GIT[name]()
            from sources.gitsrc import tree
            n = len(tree(s.repo, s.ref, s.prefixes, s.suffixes))
        except Exception:                                                  # noqa: BLE001  the checkout may be absent
            n = None
        _tree[name] = (time.time(), n)
    return n


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def frac(cursor, horizon, now):
    a, b, c = (time.mktime(time.strptime(x, "%Y-%m-%dT%H:%M:%SZ")) for x in (horizon, cursor, now))
    return max(0.0, min(1.0, (b - a) / (c - a))) if c > a else 1.0


def running():
    out = []
    for what, pat in (("sync", "sync.py"), ("embed", "embed.py")):
        r = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
        pids = [p for p in r.stdout.split() if p != str(os.getpid())]
        if pids:
            out.append(what)
    return out


def tail(path, n=240):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); f.seek(max(0, f.tell() - 2000))
            lines = [l.strip() for l in f.read().decode(errors="replace").replace("\x00", "").replace("\r", "\n").splitlines() if l.strip() and "Loading weights" not in l]
        return lines[-1][:n] if lines else ""
    except OSError:
        return ""


def progress():
    if not os.path.exists(DB):
        return {"indexed": False}
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    try:
        rows = {r[0]: r[1:] for r in con.execute("""SELECT c.source, count(*), count(v.rowid), max(c.updated) FROM chunks c
                                                    LEFT JOIN vec v ON v.rowid = c.rowid AND v.hash = c.hash GROUP BY c.source""")}
        state = {(s, k): v for s, k, v in con.execute("SELECT source, k, v FROM state WHERE k NOT LIKE 'file:%'")}
        files = dict(con.execute("SELECT source, count(*) FROM state WHERE k LIKE 'file:%' GROUP BY source").fetchall())
    finally:
        con.close()
    now = time.time()
    out = []
    for name, (label, kind, horizon) in SOURCES.items():
        chunks, emb, upd = rows.get(name, (0, 0, None))
        d = {"source": name, "label": label, "kind": kind, "chunks": chunks, "embedded": emb, "updated": upd,
             "active": bool(upd and now - upd < 15)}
        if kind == "git":
            total = tree_files(name)
            d["sync"] = {"done": files.get(name, 0), "total": total, "frac": (min(1.0, files.get(name, 0) / total) if total else None),
                         "text": f"{files.get(name, 0):,} of {total:,} files" if total else f"{files.get(name, 0):,} files", "position": state.get((name, "head"), "")[:10]}
        else:
            streams = []
            for key, what in STREAMS:
                cur = state.get((name, key))
                if cur is None and key == "since_issues" and (name, "since") in state:
                    cur = state[(name, "since")]               # written by the first, single-cursor version
                if cur is not None:
                    streams.append({"name": what, "cursor": cur, "frac": frac(cur, horizon, iso(now))})
            d["sync"] = {"streams": streams, "horizon": horizon, "frac": (min(s["frac"] for s in streams) if streams else 0.0) if streams else 0.0}
        out.append(d)
    te, tc = sum(d["embedded"] for d in out), sum(d["chunks"] for d in out)
    with _lock:
        if not _hist or now - _hist[-1][0] >= 1:
            _hist.append((now, te, tc))
        old = next((h for h in _hist if now - h[0] <= 45), _hist[0])
    dt = max(1e-6, now - old[0])
    erate, crate = (te - old[1]) / dt, (tc - old[2]) / dt
    pending = tc - te
    return {"indexed": True, "sources": out, "now": iso(now), "running": running(),
            "embed": {"embedded": te, "chunks": tc, "rate": round(erate, 1), "eta_s": round(pending / erate) if erate > 0.5 and pending else None},
            "ingest_rate": round(crate, 1),
            "logs": {"sync": tail(os.path.join(HERE, "data", "backfill.log")), "embed": tail(os.path.join(HERE, "data", "embed.log"))}}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/dashboard.html"):
            self._send(200, open(os.path.join(HERE, "dashboard.html"), "rb").read(), "text/html; charset=utf-8")
        elif path == "/api/progress":
            try:
                self._send(200, json.dumps(progress()).encode(), "application/json")
            except Exception as e:                                          # noqa: BLE001
                self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")
        else:
            self._send(404, b'{"error":"not found"}', "application/json")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8767
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"search index dashboard: http://127.0.0.1:{port}/", flush=True)
    srv.serve_forever()
