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
            prompt = " ".join(m["content"] if isinstance(m.get("content"), str) else " ".join(p.get("text", "") for p in m["content"] if p.get("type") == "text")
                              for m in req.get("messages", []))
            if "into natural, fluent " in prompt:  # translate: the text between <text> tags (or "IMAGE"), "MALFORMED" -> not JSON once
                src = prompt.split("<text>\n", 1)[1].split("\n</text>", 1)[0] if "<text>" in prompt else "IMAGE"
                bad = "MALFORMED" in src and len(req["messages"]) == 1
                content = "sorry" if bad else json.dumps({"source_language": "Testish", "translation": f"EN[{src}]", "summary": "a summary"})
                return self._json({"id": "x", "created": 1, "model": "fake", "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]})
            parts = [p for m in req.get("messages", []) if isinstance(m.get("content"), list) for p in m["content"]]
            urls = [p["image_url"]["url"] for p in parts if p.get("type") == "image_url"]
            if urls:                                   # a vision request: answer with JSON about the images (or not JSON, on "FAIL")
                text = " ".join(p.get("text", "") for p in parts if p.get("type") == "text")
                content = "no json here" if "FAIL" in text else json.dumps({"ok": len(urls) == 1 and "#page=" not in urls[0], "issues": [], "images": [u[:40] for u in urls],
                                                                              "image_tokens": req.get("image_tokens")})
                return self._json({"id": "x", "created": 1, "model": "fake", "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}, "x_timing": {"secs": 0.01}})
            return self._json({"model": req.get("model"), "received": req, "pid": os.getpid(), "choices": [{"message": {"role": "assistant", "content": f"echo:{len(req.get('messages', []))}"}}]})
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
        if self.path == "/search":
            if not req.get("query"):
                return self._err(400, {"error": "ValueError: query must be a non-empty string"})
            hits = [{"source": req.get("source") or "scalac", "title": f"hit {i} for {req['query']}", "url": "https://github.com/scala/scala", "doc": "d",
                     "state": None, "labels": None, "text": "x" * 900, "truncated": False, "bm25": i + 1, "rerank": 0.9} for i in range(req.get("k", 8))]
            return self._json({"query": req["query"], "mode": req.get("mode", "hybrid"), "reranked": req.get("rerank", True), "results": hits,
                               "timing_ms": {"total": 7}, "echo": req})
        if self.path == "/embed":
            texts = [req["input"]] if isinstance(req["input"], str) else req["input"]
            return self._json({"model": "fake-embed", "dim": 2, "embeddings": [[float(len(t)), 1.0] for t in texts], "kind": req.get("kind", "document")})
        if self.path == "/rerank":
            return self._json({"model": "fake-rerank", "scores": [1.0 / (1 + i) for i in range(len(req["documents"]))]})
        if self.path == "/score":
            return self._json({"logprobs": [-float(len(c)) for c in req["candidates"]], "echo": req})
        if self.path == "/speak":
            if not req.get("text"):
                return self._err(400, {"error": "ValueError: text is empty"})
            return self._json({"path": "/tmp/fake.wav", "duration_s": 1.5, "sample_rate": 24000, "cached": False, "echo": req,
                               "segments": [{"text": req["text"], "start_s": 0.0, "end_s": 1.5}]})
        if self.path == "/v1/audio/speech":
            b = b"RIFFfakewav"
            self.send_response(200); self.send_header("Content-Type", "audio/wav"); self.send_header("Content-Length", str(len(b))); self.end_headers()
            return self.wfile.write(b)
        if self.path == "/transcribe":
            return self._json({"text": "hello world", "language": "en", "duration_s": 1.0, "segments": [], "echo": req,
                               "words": [{"word": "hello", "start_s": 0.0, "end_s": 0.5}, {"word": "world", "start_s": 0.5, "end_s": 1.0}]})
        t0 = time.time(); time.sleep(req.get("sleep", 0))
        self._json({"pid": os.getpid(), "t0": t0, "t1": time.time()})
    def _err(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)


if a.crash_after:
    threading.Timer(a.crash_after, lambda: os._exit(7)).start()
print(f"READY {a.port}", flush=True)
ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
