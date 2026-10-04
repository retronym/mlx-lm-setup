"""A controllable fake backend for supervisor tests. Speaks the same contract as the real ones: listens only when ready.

  --ready-delay S    sleep before listening        --crash-after S    exit(7) S seconds after becoming ready
  --die-on-start     exit(3) immediately           --ignore-term      ignore SIGTERM (forces SIGKILL path)
  --spawn-child      start a grandchild that sleeps forever (tests process-group kill)
GET /health -> 200;  GET /info -> pids;  POST /work {"sleep": S} -> sleeps then returns {"pid", "t0", "t1"}
"""
import argparse, json, os, signal, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int)
ap.add_argument("--ready-delay", type=float, default=0)
ap.add_argument("--crash-after", type=float, default=0)
ap.add_argument("--die-on-start", action="store_true")
ap.add_argument("--ignore-term", action="store_true")
ap.add_argument("--spawn-child", action="store_true")
ap.add_argument("--sleep-forever", action="store_true")
a = ap.parse_args()

if a.sleep_forever:
    while True:
        time.sleep(3600)
if a.die_on_start:
    print("fake backend: dying on start", flush=True)
    sys.exit(3)
if a.ignore_term:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, __file__, "--sleep-forever"]) if a.spawn_child else None
time.sleep(a.ready_delay)


class H(BaseHTTPRequestHandler):
    def log_message(self, *x): pass
    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/v1/models":
            return self._json({"data": [{"id": "fake-model"}]})
        self._json({"pid": os.getpid(), "child": child.pid if child else None} if self.path == "/info" else {"status": "ok"})
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); req = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/v1/chat/completions":
            if req.get("stream"):
                self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
                try:
                    for i in range(req.get("n_chunks", 5)):
                        c = ("data: " + json.dumps({"model": req.get("model"), "choices": [{"delta": {"content": f"tok{i} "}}]}) + "\n\n").encode()
                        self.wfile.write(f"{len(c):x}\r\n".encode() + c + b"\r\n"); self.wfile.flush(); time.sleep(req.get("delay", 0.2))
                    c = b"data: [DONE]\n\n"; self.wfile.write(f"{len(c):x}\r\n".encode() + c + b"\r\n0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            return self._json({"model": req.get("model"), "pid": os.getpid(), "choices": [{"message": {"role": "assistant", "content": f"echo:{len(req.get('messages', []))}"}}]})
        if self.path == "/decide":
            if req.get("question") == "bad":
                return self._err(400, {"error": "QuestionError: bad"})
            opts = req.get("options") or ["x"]
            names = list(opts)
            probs = {n: (0.6 if i == 0 else 0.4 / max(1, len(names) - 1)) for i, n in enumerate(names)}
            return self._json({"answer": names[0], "top_probability": probs[names[0]], "probabilities": probs, "echo": req})
        if self.path == "/score_many":
            def one(q):
                crit = q.get("crit") or {"false": None, "true": None}
                names = list(crit) if isinstance(crit, dict) else [str(i) for i in range(len(crit))]
                return {"answer": names[0], "top_probability": 0.7, "probabilities": {n: (0.7 if i == 0 else 0.3 / max(1, len(names) - 1)) for i, n in enumerate(names)}}
            return self._json([one(q) for q in req["questions"]])
        if self.path == "/entail":
            return self._json({"labels": ["contradiction", "entailment", "neutral"], "probs": [[0, 1, 0] for _ in req["hypotheses"]]})
        t0 = time.time(); time.sleep(req.get("sleep", 0))
        self._json({"pid": os.getpid(), "t0": t0, "t1": time.time()})
    def _err(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)


if a.crash_after:
    threading.Timer(a.crash_after, lambda: os._exit(7)).start()
print(f"READY {a.port}", flush=True)
ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
