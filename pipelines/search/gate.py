"""The gate of the agentic query layer (ASK.md): does a document satisfy a question's criterion? Retrieval says "on topic"; this says "this is the one".

A candidate is judged on a *view* of its whole document, not the chunk that matched: title, kind, state, author and date, the head of the body, and for a PR
the issues it closes (a PR's own text rarely describes the symptom its issue reported). Two tiers:

  1. the decision model (`decide`, yes/no with a probability) on the view: cheap; a clear yes or no ends here (`tier1_band`);
  2. the LLM reads the view and answers yes / no / maybe with a quote from the view that supports a yes. The quote must be in the view (verbatim up to
     case and punctuation), so the model cannot invent its support; a yes without one becomes maybe. (Not an NLI check: "this document is what was
     asked for" is a judgement about relevance, not a claim the text entails; NLI is for checking generated prose, and the gate generates none.)

Verdicts are cached on (criterion, view hash) for the life of the process. Everything goes through the gateway (gateway_client), so nothing here loads a model."""
import hashlib, json, os, re

from llm import chat, gw

VIEW_CHARS, ISSUE_CHARS = 1800, 700
SEARCH_URL = os.environ.get("SEARCH_URL")    # the gateway with the index (documents, links); models come from gateway_client's own BASE
TIER1_BAND = (0.2, 0.9)          # below: no, above: yes, between: ask the LLM
_cache = {}


def view(ref):
    """The text the gate judges for a hit `ref` (`project/chunk id`): the document head, plus what a PR closes. None if the ref is gone."""
    d = gw.post("/api/search/get", {"refs": [ref], "max_chars": VIEW_CHARS}, base=SEARCH_URL)["results"][0]
    if not d.get("found"):
        return None
    head = f"{d['kind']} {d['title']}\nstate: {d.get('state') or '-'} · author: {d.get('author') or '-'} · created: {(d.get('created') or '')[:10]}\n\n{d['text']}"
    if d["kind"] == "pr":
        ln = gw.post("/api/search/links", {"ref": ref, "types": ["closes"], "limit": 3}, base=SEARCH_URL)
        for x in ln.get("links", []):
            if x.get("get_ref"):
                i = gw.post("/api/search/get", {"refs": [x["get_ref"]], "max_chars": ISSUE_CHARS}, base=SEARCH_URL)["results"][0]
                head += f"\n\n--- closes {x['id']}: {x.get('title', '')}\n{i.get('text', '') if i.get('found') else ''}"
            else:
                head += f"\n\n--- closes {x['id']}: {x.get('title', '')}"
    return head


def tier1(criterion, v):
    r = gw.post("/api/decide", {"state": v[:4000], "questions": [{"t": "noul", "ins": f"This document is exactly what this request asks for: {criterion}"}]})[0]
    return r["probabilities"]["true"]


PROMPT = """You judge search results. Request: {criterion}

Candidate document:
<<<
{view}
>>>

Is this document exactly what the request asks for (not merely related: for "the PR that fixed X", it must be the pull request that fixes X, not a
backport, a test-only change or a different fix in the same area)? Answer with JSON only:
{{"verdict": "yes" | "no" | "maybe", "quote": "<a short verbatim quote from the document that shows it, or empty>", "reason": "<one sentence>"}}"""


def _norm(s):
    return " ".join(re.findall(r"[a-z0-9]+", s.lower()))


def tier2(criterion, v, model):
    out = chat([{"role": "user", "content": PROMPT.format(criterion=criterion, view=v)}], model, max_tokens=200, temperature=0.0)
    m = re.search(r"\{.*\}", out, re.S)
    try:
        j = json.loads(m[0]) if m else {}
    except ValueError:
        j = {}
    verdict = j.get("verdict") if j.get("verdict") in ("yes", "no", "maybe") else "maybe"
    quote = (j.get("quote") or "").strip()
    quoted = bool(quote) and len(quote) >= 12 and _norm(quote) in _norm(v)
    if verdict == "yes" and not quoted:
        verdict = "maybe"                                                   # a yes needs support that is really in the document
    return {"verdict": verdict, "quote": quote if quoted else "", "reason": j.get("reason", "")[:300], "raw": None if j else out[:200]}


_views, _p = {}, {}
MAX_CACHED = 5000


def trim():
    """Bound the caches of a long-running process (the gateway): views and tier-1 scores are cheap to recompute, a stale view is worse."""
    for c in (_views, _p, _cache):
        if len(c) > MAX_CACHED:
            c.clear()


def prescreen(criterion, ref):
    """(view, tier-1 probability), both cached: the controller ranks every candidate by this before spending LLM calls on the best."""
    if ref not in _views:
        _views[ref] = view(ref)
    v = _views[ref]
    if v is None:
        return None, 0.0
    key = (criterion, hashlib.sha1(v.encode()).hexdigest())
    if key not in _p:
        _p[key] = tier1(criterion, v)
    return v, _p[key]


def judge(criterion, ref, model="qwen3-coder", band=TIER1_BAND, use_tier2=True):
    """{verdict: yes | no | maybe, p (tier 1), tier, quote, reason}."""
    v, p = prescreen(criterion, ref)
    if v is None:
        return {"verdict": "no", "p": 0.0, "tier": 0, "reason": "not found"}
    key = (criterion, hashlib.sha1(v.encode()).hexdigest(), band, use_tier2)
    if key in _cache:
        return _cache[key]
    if p < band[0]:
        r = {"verdict": "no", "p": p, "tier": 1}
    elif p > band[1] or not use_tier2:
        r = {"verdict": "yes" if p > band[1] else "maybe", "p": p, "tier": 1}
    else:
        r = {"p": p, "tier": 2, **tier2(criterion, v, model)}
    _cache[key] = r
    return r


LIST_PROMPT = """You judge search results. Request: {criterion}

Candidates:
{views}

Which candidate is exactly what the request asks for? Not merely related: for "the PR that fixed X" it must be the pull request that fixes X itself,
not a backport, a follow-up, a test-only change, or a fix of a different problem in the same area. Compare the candidates with each other and with the
request's specifics (the symptom, the construct, the error). If none is, say so. Answer with JSON only:
{{"best": <candidate number or null>, "also": [<other numbers that would equally answer it>], "quote": "<short verbatim quote from the best candidate that shows it>", "reason": "<one sentence>"}}"""


def choose(criterion, refs, model="qwen3-coder", chars=1200):
    """Listwise tier 2: which of `refs` (if any) is the answer. {best: ref | None, also: [ref], quote, reason}; a best whose quote is not in its view
    is kept but flagged `unquoted`."""
    vs = [(r, prescreen(criterion, r)[0]) for r in refs]
    vs = [(r, v) for r, v in vs if v]
    text = "\n\n".join(f"[{i}] {v[:chars]}" for i, (_, v) in enumerate(vs, 1))
    out = chat([{"role": "user", "content": LIST_PROMPT.format(criterion=criterion, views=text)}], model, max_tokens=250, temperature=0.0)
    m = re.search(r"\{.*\}", out, re.S)
    try:
        j = json.loads(m[0]) if m else {}
    except ValueError:
        j = {}
    pick = lambda n: vs[n - 1][0] if isinstance(n, int) and 1 <= n <= len(vs) else None
    best = pick(j.get("best"))
    quote = (j.get("quote") or "").strip()
    return {"best": best, "also": [x for x in map(pick, j.get("also") or []) if x and x != best], "quote": quote, "reason": (j.get("reason") or "")[:300],
            "unquoted": bool(best) and not (len(quote) >= 12 and _norm(quote) in _norm(dict(vs)[best])), "raw": None if j else out[:200]}
