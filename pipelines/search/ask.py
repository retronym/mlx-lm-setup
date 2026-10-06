#!/usr/bin/env python3
"""The agentic query layer (ASK.md): plan, retrieve along several routes, gate, and decide whether to go on.

  plan      plan.plan(question): criterion, kinds, 1..3 queries, dates, authors, open_only
  retrieve  every query with the plan's filters, and the question as asked; the first query with no filters at all (a wrong filter cannot hide the answer); and, when the answer is
            a PR, the issues the first query finds and the PRs that close them (an issue describes the symptom, its PR often only the change)
  gate      every candidate of the wanted kinds through the decision model (cheap), then the best `gate_budget` of them by retrieval and tier-1 score
            shown together to the LLM, which picks the one that answers (gate.choose; judging one at a time, it said yes to most related documents)
  continue  no confirmed answer: one more round with the plan's dates and authors dropped and queries reworded from what was rejected

Returns {"plan", "answers": [{ref, node, title, url, kind, verdict, p, quote, reason, routes}], "trace": [...], "seconds"}: confirmed (yes) first, then
the rest by tier-1 probability and retrieval (`verdict` null: not shown to the LLM; no: shown, not picked); `trace` records every search with its filters, what each route added and every verdict.

usage: ask.py "question" [--k 5] [--rounds 2] [--gate 10]       SEARCH_URL: the gateway that serves /api/search (default GATEWAY_URL)"""
import json, os, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate, plan as planner
from llm import chat, gw

SEARCH_URL = os.environ.get("SEARCH_URL")
RRF_K = 60


_tl = threading.local()                                                  # seconds per phase of this thread's `ask` (the gateway runs several at once)


def _timed(phase, f, *a, **kw):
    t = time.time()
    try:
        return f(*a, **kw)
    finally:
        T = _tl.__dict__.setdefault("T", {})
        T[phase] = T.get(phase, 0) + time.time() - t


def _search(body):
    """No reranker: the tier-1 model and the LLM choice do its job here, and with an LLM resident it costs 2-8 s a search instead of 0.4 s."""
    return _timed("search", gw.post, "/api/search", {"text_chars": 200, "related": False, "rerank": False, **body}, base=SEARCH_URL)["results"]


def retrieve(p, universe, rnd, trace, cands, k=10, drop_filters=False, question=None):
    """Add the candidates of one round to `cands` {ref: {..., routes: {route: rank}}}. `question`: also search it as asked (the planner's queries are terse
    and drop specifics the embedding would have matched)."""
    kinds = p["kinds"]
    filt = {} if drop_filters else {x: p[x] for x in ("since", "until", "authors") if p.get(x)}
    filt.update({"open_only": True} if p.get("open_only") else {})
    def add(route, h, rank, via=None):
        if kinds and h["kind"] not in kinds:
            return
        c = cands.setdefault(h["ref"], {"ref": h["ref"], "node": h.get("node") or h.get("id"), "title": h["title"], "url": h.get("url"), "kind": h["kind"],
                                        "state": h.get("state"), "routes": {}})
        c["routes"].setdefault(route, rank)
        if via:
            c.setdefault("via", via)
    def linked(route, h, rank):
        for x in (h.get("links") or {}).get("top", []):
            if x["rel"] == "closed_by" and x.get("indexed") and x.get("get_ref"):
                add(route, {"ref": x["get_ref"], "node": x["id"], "title": x["title"], "url": x.get("url"), "kind": x["kind"], "state": x.get("state")}, rank,
                    via=f"closes {h.get('node')}")
    def run(route, body, follow=False):
        hs = _search({"universe": universe, "k": k, **body})
        trace.append({"round": rnd, "route": route, "search": {x: v for x, v in body.items() if x != "universe"}, "hits": [h["ref"] for h in hs]})
        for i, h in enumerate(hs, 1):
            add(route, h, i)
            if follow and h["kind"] == "issue":
                linked(route + "+closed_by", h, i)
    for j, q in enumerate(p["queries"]):
        run(f"r{rnd}.q{j}", {"query": q, **({"kinds": kinds} if kinds else {}), **filt})
    if question:
        run(f"r{rnd}.asked", {"query": question, **({"kinds": kinds} if kinds else {}), **filt})
    run(f"r{rnd}.open", {"query": p["queries"][0]}, follow=True)                                    # no filters, and its issues lead to their fixes
    if "pr" in kinds:
        run(f"r{rnd}.issues", {"query": p["queries"][0], "kinds": ["issue"]}, follow=True)


def prior(c):
    return sum(1 / (RRF_K + r) for r in c["routes"].values())


REWORD = """We are searching an index of Scala compiler and Zinc issues, PRs and commits. Request: {criterion}
These searches found nothing that satisfies it: {queries}
Documents that were rejected: {rejected}
Write 2 new search texts that would find the right document: different terms (the words a developer would use in its title), identifiers, error
messages. Answer with a JSON list of 2 strings only."""


def reword(p, rejected):
    out = chat([{"role": "user", "content": REWORD.format(criterion=p["criterion"], queries=p["queries"], rejected=rejected[:8])}], "qwen3-coder", max_tokens=150)
    try:
        qs = json.loads(out[out.index("["):out.rindex("]") + 1])
        return [q for q in qs if isinstance(q, str) and q.strip()][:2]
    except ValueError:
        return []


def line(a):
    """One line a client can show as is: verdict, state, title, kind, how it was reached, link."""
    say = {"yes": "confirmed", "no": "not picked", None: "unchecked"}[a.get("verdict")]
    state = f"[{a['state']}] " if a.get("state") in ("open", "closed", "merged") else ""
    return f"{say}: {state}{a['title']} · {a['kind']}" + (f" · via {a['via']}" if a.get("via") else "") + f" · {a.get('url') or a['ref']}"


def ask(question, universe="scala-zinc", k=5, rounds=2, gate_budget=10, model="qwen3-coder", log=lambda *_: None):
    t0, trace = time.time(), []
    _tl.T = {}
    gate.trim()
    gate.SEARCH_URL = SEARCH_URL
    p = _timed("plan", planner.plan, question, model=model)
    log(f"plan: {json.dumps(p)}")
    cands, shown, picked = {}, set(), None
    for rnd in range(1, rounds + 1):
        if rnd > 1:
            rejected = [cands[r]["title"] for r in shown]
            new = _timed("reword", reword, p, rejected)
            trace.append({"round": rnd, "reworded": new, "dropped_filters": {x: p[x] for x in ("since", "until", "authors") if p.get(x)}})
            log(f"round {rnd}: reworded {new}")
            if not new:
                break
            p = {**p, "queries": new}
        retrieve(p, universe, rnd, trace, cands, drop_filters=rnd > 1, question=question if rnd == 1 else None)
        for c in cands.values():                                               # tier 1 for everything new: about 0.1 s each
            if "p" not in c:
                c["p"] = _timed("tier1", gate.prescreen, p["criterion"], c["ref"])[1]
        order = [c for c in sorted(cands.values(), key=lambda c: -(c["p"] + 50 * prior(c))) if c["ref"] not in shown][:gate_budget]
        if not order:
            break
        shown.update(c["ref"] for c in order)
        ch = _timed("choose", gate.choose, p["criterion"], [c["ref"] for c in order], model=model)
        trace.append({"round": rnd, "shown": [c["ref"] for c in order], "choice": {x: ch[x] for x in ("best", "also", "quote", "reason", "unquoted")}})
        log(f"  shown {[c['ref'] for c in order]}\n  best {ch['best']} also {ch['also']}: {ch['reason']}")
        if ch["best"]:
            picked = ch
            break
    score = lambda c: c["p"] + 50 * prior(c)
    confirmed = ([picked["best"]] + picked["also"]) if picked else []
    answers = [cands[r] for r in confirmed] + sorted((c for r, c in cands.items() if r not in confirmed), key=lambda c: -score(c))
    out = []
    for c in answers[:k]:
        v = "yes" if c["ref"] in confirmed else "no" if c["ref"] in shown else None
        out.append({**c, "verdict": v, **({"quote": picked["quote"], "reason": picked["reason"]} if picked and c["ref"] == picked["best"] else {})})
    for a in out:
        a["line"] = line(a)
    return {"plan": p, "answers": out, "trace": trace, "seconds": round(time.time() - t0, 1), "timing": {x: round(v, 1) for x, v in _tl.T.items()},
            "candidates": len(cands), "shown": len(shown)}


if __name__ == "__main__":
    a = sys.argv[1:]
    def opt(name, default):
        if name in a:
            i = a.index(name); v = a[i + 1]; del a[i:i + 2]; return v
        return default
    k, rounds, gb = int(opt("--k", 5)), int(opt("--rounds", 2)), int(opt("--gate", 10))
    r = ask(" ".join(a), k=k, rounds=rounds, gate_budget=gb, log=lambda s: print(s, flush=True))
    print(f"\n{r['candidates']} candidates, {r['shown']} shown to the LLM, {r['seconds']} s {r['timing']}")
    for n, c in enumerate(r["answers"], 1):
        print(f"{n}. [{c.get('verdict', '-')}] {c['title']}  {c['url']}\n   {c.get('quote') or c.get('reason') or ''}")
