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
        self._json({"pid": os.getpid(), "child": child.pid if child else None} if self.path == "/info" else {"status": "ok"})
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); req = json.loads(self.rfile.read(n) or b"{}")
        t0 = time.time(); time.sleep(req.get("sleep", 0))
        self._json({"pid": os.getpid(), "t0": t0, "t1": time.time()})


if a.crash_after:
    threading.Timer(a.crash_after, lambda: os._exit(7)).start()
print(f"READY {a.port}", flush=True)
ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
