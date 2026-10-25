"""Minimal client for the local gateway, stdlib only, so pipeline scripts run in any python and never load a model themselves.
The gateway starts the backend on first use and keeps it inside the memory budget; a 503 (backend starting, or no room yet)
is retried after the Retry-After the gateway sends.  GATEWAY_URL overrides http://127.0.0.1:8090.
"""
import json, re, os, time, urllib.error, urllib.request

BASE = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8090")


def post(path, body, timeout=900, retries=30, base=None):
    """POST JSON to the gateway (or to `base`, another gateway: a draft one for a checkout's search index)."""
    data = json.dumps(body).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request((base or BASE) + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            if e.code == 503 and attempt < retries:
                time.sleep(float(e.headers.get("Retry-After") or 5))
                continue
            if e.code == 502 and re.search(r"Connect(Timeout|Error)", msg) and attempt < 3:      # the backend was not accepting connections (starting, or swapping): try again
                time.sleep(15 * (attempt + 1))
                continue
            raise RuntimeError(f"POST {path}: {e.code} {msg}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"POST {path}: gateway not reachable at {base or BASE} ({e.reason}); start it with: .venv/bin/python -m gateway") from None


def get(path, timeout=30, base=None):
    with urllib.request.urlopen((base or BASE) + path, timeout=timeout) as r:
        return json.load(r)


def _token():
    """The gateway token (stopping a backend needs it): GATEWAY_TOKEN, else <checkout>/.gateway/token. None when there is none."""
    if os.environ.get("GATEWAY_TOKEN"):
        return os.environ["GATEWAY_TOKEN"]
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".gateway", "token")) as f:
            return f.read().strip()
    except OSError:
        return None


def release(*kinds, base=None):
    """Stop the running, idle backends of these kinds ("llm", "nli", ...) now instead of when their TTL runs out, so that the next model a
    batch job needs has the memory to itself. Busy backends are left alone (another client is using them). Returns the names stopped; without
    a token, or with the gateway unreachable, nothing is stopped and the TTL does the job as before."""
    token = _token()
    if not token:
        return []
    try:
        running = [b["name"] for b in get("/api/backends", base=base)["backends"]
                   if b["kind"] in kinds and b["state"] in ("ready", "starting") and not b["busy"] and not b["pinned"]]
    except (OSError, ValueError, KeyError):
        return []
    stopped = []
    for name in running:
        req = urllib.request.Request(f"{base or BASE}/api/backends/{name}/stop", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=120):         # returns once the process has exited and its memory is back
                stopped.append(name)
        except (OSError, urllib.error.HTTPError):
            pass
    return stopped


def available_gb():
    """Memory macOS could hand out now (kern.memorystatus_level, the percentage the kernel's jetsam policy works from, of physical memory),
    in GB; None off macOS."""
    import subprocess
    try:
        r = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_level", "hw.memsize"], capture_output=True, text=True, timeout=5)
        pct, total = (int(x) for x in r.stdout.split())
        return round(pct / 100 * total / 2**30, 1)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _model(body, model):
    return {**body, "model": model} if model else body


def decide_many(state, questions, model=None):
    """Several questions about one state; the decision model computes the state once. Returns one result dict per question."""
    return post("/api/decide", _model({"state": state, "questions": questions}, model))


def entail(premise, hypotheses, model=None):
    """[[contradiction, entailment, neutral], ...] per hypothesis."""
    return post("/api/entail", _model({"premise": premise, "hypotheses": hypotheses}, model))["probs"]


def score(prompt, candidates, model=None):
    """log P(candidate | prompt) per candidate, from an LLM scoring backend."""
    return post("/api/score", _model({"prompt": prompt, "candidates": candidates}, model))["logprobs"]
