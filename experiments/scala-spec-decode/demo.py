"""Demo page: watch Qwen3-Coder write a held-out scala3 method body, with the compiler's part shown inline between the tokens.

  python demo.py [port]      (needs the compiler service, ./run_service.sh; loads the model, ~17.5 GB)

GET /                      the page
GET /api/tasks             the held-out bodies from phase 0b
GET /api/context?id=       the code before the body, and the original body
GET /api/run?id=&arm=      server-sent events of one decode: prompt, trigger, mask, draft, verify, token, done, check
"""
import json, os, queue, sys, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import compiler_client
from decode import Decoder, acquire_memory, make_task
from eval import ARMS
from phase0b import splice

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(HERE, "data", "phase0b", "gen")
TASKS = {}
for i in sorted(os.listdir(GEN)):
    TASKS[i] = json.load(open(os.path.join(GEN, i, "meta.json")))
LOCK = threading.Lock()                                    # one decode at a time: one model, one compiler Run
D = None


def context(tid):
    t = make_task(TASKS[tid])
    src = t["src"]
    ls = src.rfind("\n", 0, t["defStart"]) + 1
    before = src[:ls].split("\n")[-14:]
    return {"id": tid, "name": t["name"], "file": os.path.relpath(t["file"], "/Users/jzaugg-virtus/code/scala/scala3"),
            "before": "\n".join(before), "header": t["header"], "orig": src[t["eq"] + 1:t["rhsEnd"]]}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/":
            return self.send(200, open(os.path.join(HERE, "demo.html")).read(), "text/html; charset=utf-8")
        if u.path == "/api/tasks":
            return self.send(200, json.dumps([{"id": k, "name": m["name"], "file": os.path.basename(m["file"]), "lines": m["rhsLines"]}
                                              for k, m in TASKS.items()]))
        if u.path == "/api/context":
            return self.send(200, json.dumps(context(q["id"])))
        if u.path == "/api/run":
            return self.run(q["id"], q.get("arm", "mask"))
        self.send(404, "{}")

    def run(self, tid, arm):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        events = queue.Queue()

        def work():
            with LOCK:
                try:
                    t = make_task(TASKS[tid])
                    body, st = D.generate(t, on_event=events.put, **ARMS[arm])
                    text, s, e = splice(t["src"], t, body)
                    errs = [x for x in compiler_client.check(t["uri"], text) if s <= x["offset"] < e]
                    errs = list({(x["id"], x["offset"]): x for x in errs}.values())
                    events.put({"type": "check", "errors": [{"id": x["id"], "line": x["line"] - t["line"], "msg": x["msg"]} for x in errs]})
                except Exception as ex:
                    events.put({"type": "error", "msg": repr(ex)})
                events.put(None)

        threading.Thread(target=work, daemon=True).start()
        try:
            while (ev := events.get()) is not None:
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8772
    print("memory:", acquire_memory(), flush=True)
    D = Decoder()
    print(f"demo on http://127.0.0.1:{port}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
