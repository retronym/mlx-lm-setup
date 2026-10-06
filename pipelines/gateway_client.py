"""Minimal client for the local gateway, stdlib only, so pipeline scripts run in any python and never load a model themselves.
The gateway starts the backend on first use and keeps it inside the memory budget; a 503 (backend starting, or no room yet)
is retried after the Retry-After the gateway sends.  GATEWAY_URL overrides http://127.0.0.1:8090.
"""
import json, os, time, urllib.error, urllib.request

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
            raise RuntimeError(f"POST {path}: {e.code} {msg}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"POST {path}: gateway not reachable at {base or BASE} ({e.reason}); start it with: .venv/bin/python -m gateway") from None


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
