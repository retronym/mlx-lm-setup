#!/usr/bin/env python3
"""A small evaluation set for the agentic query layer (ASK.md step 2): "find me the PR that ..." questions with the PR as ground truth.

Built from merged PRs that GitHub ties to an issue they close (the pairs of eval_links.py), so the links can be followed. A local LLM writes each question
from the PR's title and description, the way someone who remembers what the change did (not its title or number) would ask; the question is rejected
if it repeats a run of four title words. Half the questions carry a time hint ("around 2021", "in early 2024"), written by code from the PR's creation
date, so the planner's date handling is exercised.

usage: eval_ask.py build [--n 30] [--seed 3] [--out data/eval/ask.json] [--from fix|issue]   (issue: the question describes the symptom, from the closed issue)
       eval_ask.py search FILE [--k 10] [--no-rerank] [--kinds pr] [--years]   baseline: the question as a plain search query, rank of the PR
       eval_ask.py gate FILE [--label L] [--negatives 4] [--tier1-only]   the gate alone: the PR (positive) and the top other PRs (negatives) judged against the question
       eval_ask.py gate-list FILE [--label L] [--negatives 4] [--model M] [--chars 1200]   listwise: which of the PR and its negatives is the answer
       eval_ask.py gate-report RUN.jsonl
       eval_ask.py ask FILE [--label L] [--limit N] [--gate 10] [--rounds 2]    the whole loop (SEARCH_URL: a gateway with the date filters)
       eval_ask.py ask-report RUN.jsonl"""
import json, os, random, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config, llm, search
from llm import gw                                     # the gateway (GATEWAY_URL; a draft gateway for this checkout: draft.sh, :8091)
from eval_links import doc_of, TEMPLATED

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "eval", "ask.json")
PROMPT = """Below is a pull request to {repo}. Write the question a developer would type into a search box to find this pull request again,
months later, remembering what it changed or fixed but not its title, number or author. One sentence starting with "Find the PR that" or
"Which PR". Describe the behaviour or the symptom in your own words; do not copy phrases from the title; no numbers, no URLs, no names of people.
Answer with the question only.

Title: {title}

Description:
{body}"""


PROMPT_ISSUE = """Below is a bug report or feature request for {repo}. Write the question a developer would type into a search box to find the pull
request that fixed it, describing the problem as they experienced it (the symptom, the code that failed, the error), not the report's title,
number or author. One sentence starting with "Find the PR that fixed" or "Which PR fixed". Use your own words; do not copy phrases from the title;
no numbers, no URLs, no names of people. Answer with the question only.

Title: {title}

Report:
{body}"""


def _ngrams(s, n=4):
    w = re.findall(r"[a-z0-9]+", s.lower())
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def hint(created, rng):
    y, m = int(created[:4]), int(created[5:7])
    return rng.choice([f"around {y}", f"in {'early' if m <= 4 else 'mid' if m <= 8 else 'late'} {y}", f"in {y}"])


def build(cfg, n, seed, out, side="fix"):
    """Questions written from the PR itself (`side` "fix") or from the issue it closes ("issue": the symptom, so the PR's own words are not in the question)."""
    ev = json.load(open(os.path.join(os.path.dirname(OUT), "links-scala-zinc.json")))
    idx = search.Index(cfg, ev["universe"])
    rng = random.Random(seed)
    by_repo = {}
    for p in ev["pairs"]:
        by_repo.setdefault(p["fix"]["id"].split("#")[0], []).append(p)
    for g in by_repo.values():
        rng.shuffle(g)
    cases, tried = [], 0
    while len(cases) < n and any(by_repo.values()):
        for repo, g in by_repo.items():
            if not g or len(cases) >= n:
                continue
            p = g.pop()
            node = idx.links.node(p["fix"]["id"])
            src = idx.links.node(p[side]["id"])
            title, body = doc_of(idx, src)
            if len(body or "") < 200 or TEMPLATED.search(title):                  # nothing to write a question from
                continue
            tried += 1
            q = llm.chat([{"role": "user", "content": (PROMPT if side == "fix" else PROMPT_ISSUE).format(repo=repo, title=title, body=body[:3000])}], cfg.search["llm"]["eval_questions"]["model"],
                         max_tokens=120, temperature=0.3).splitlines()[0].strip().strip('"')
            if _ngrams(q) & _ngrams(title) or not q.lower().startswith(("find the pr", "which pr")):
                print(f"  rejected: {q[:120]}", flush=True)
                continue
            created = node["created"] or ""
            if created and len(cases) % 2 == 0:
                q = q.rstrip("?.") + f", {hint(created, rng)}?"
            cases.append({"q": q, "want": p["fix"]["id"], "from": side, "title": doc_of(idx, node)[0], "issue": p["issue"]["id"], "created": created[:10], "overlap": p["overlap"]})
            print(f"{len(cases):3}. {q}\n     -> {p['fix']['id']} {title}", flush=True)
    idx.close()
    json.dump({"universe": ev["universe"], "built": time.strftime("%Y-%m-%d"), "cases": cases, "tried": tried}, open(out, "w"), indent=1)
    print(f"{len(cases)} questions ({tried} written) -> {out}")


def rank_of(hits, want):
    return next((i + 1 for i, h in enumerate(hits) if h.get("node") == want), None)


def recall(ranks, k):
    return sum(1 for r in ranks if r and r <= k) / len(ranks) if ranks else 0.0


def year_range(q):
    """The planner's simplest date reading, by hand: a year in the question's hint, +-1 year."""
    m = re.search(r"\b(20\d\d)\?$", q)
    return {"since": str(int(m[1]) - 1), "until": str(int(m[1]) + 1)} if m else {}


def baseline(path, k, rerank, kinds=None, years=False):
    """The question as a plain search query; with `kinds` and `years`, what a planner that only sets filters would get."""
    ev = json.load(open(path))
    ranks = []
    for i, c in enumerate(ev["cases"]):
        body = {"query": c["q"], "k": k, "rerank": rerank, "text_chars": 50, "related": False, "universe": ev["universe"], **({"kinds": kinds} if kinds else {}),
                **(year_range(c["q"]) if years else {})}
        hits = gw.post("/api/search", body)["results"]
        ranks.append(rank_of(hits, c["want"]))
        print(f"{i + 1:3}/{len(ev['cases'])} rank {ranks[-1] or '-':>2}  {c['q'][:110]}", flush=True)
    print(f"plain search, k={k}, rerank {'on' if rerank else 'off'}, kinds {kinds or 'any'}, years {'+-1' if years else 'no'}: recall@1 {recall(ranks, 1):.2f}  @5 {recall(ranks, 5):.2f}  @{k} {recall(ranks, k):.2f}")
    return ranks


def gate_set(path, negatives):
    """Per case: the wanted PR and the top non-wanted PRs a kinds=pr search returns for the question, as refs."""
    ev = json.load(open(path))
    out = []
    for c in ev["cases"]:
        want = gw.post("/api/search/links", {"ref": c["want"], "limit": 1})["node"]["get_ref"]
        hits = gw.post("/api/search", {"query": c["q"], "k": negatives + 1, "kinds": ["pr"], "text_chars": 20, "related": False, "universe": ev["universe"]})["results"]
        out.append({"q": c["q"], "pos": want, "neg": [h["ref"] for h in hits if h["ref"] != want][:negatives]})
    return out


def gate_eval(path, label, negatives, use_tier2):
    """The gate alone: every judgement with its tier-1 probability and final verdict to data/eval/runs/gate-<label>.jsonl, and the confusion table.
    A negative here is "not the PR GitHub recorded as the fix": some are plausible alternatives (a backport, a follow-up), so precision is a floor."""
    import gate
    rows, t0 = [], time.time()
    gs = gate_set(path, negatives)
    n = sum(1 + len(g["neg"]) for g in gs)
    out = open(os.path.join(os.path.dirname(path), "runs", f"gate-{label}.jsonl"), "w")
    for i, g in enumerate(gs):
        for ref, pos in [(g["pos"], True)] + [(r, False) for r in g["neg"]]:
            t = time.time()
            r = gate.judge(g["q"], ref, use_tier2=use_tier2)
            rows.append({"case": i, "ref": ref, "pos": pos, **r, "s": round(time.time() - t, 1)})
            out.write(json.dumps(rows[-1]) + "\n"); out.flush()
            done = len(rows)
            print(f"{done:3}/{n} {'POS' if pos else 'neg'} {r['verdict']:5} p={r['p']:.2f} t{r['tier']} {rows[-1]['s']:4.1f}s  {ref}  "
                  f"(eta {(time.time() - t0) / done * (n - done) / 60:.0f} min)", flush=True)
    report_gate(rows)


def gate_list_eval(path, label, negatives, model, chars):
    """Listwise: the PR and its negatives, shuffled, judged together. Accuracy = the PR is `best`; also how often the model picks a wrong one or none."""
    import gate
    rng = random.Random(7)
    gs = gate_set(path, negatives)
    res, t0 = [], time.time()
    out = open(os.path.join(os.path.dirname(path), "runs", f"gatelist-{label}.jsonl"), "w")
    for i, g in enumerate(gs):
        refs = [g["pos"]] + g["neg"]
        rng.shuffle(refs)
        t = time.time()
        r = gate.choose(g["q"], refs, model=model, chars=chars)
        r.update({"case": i, "pos": g["pos"], "ok": r["best"] == g["pos"], "s": round(time.time() - t, 1), "also_has_pos": g["pos"] in r["also"]})
        res.append(r); out.write(json.dumps(r) + "\n"); out.flush()
        print(f"{i + 1:3}/{len(gs)} {'OK ' if r['ok'] else 'none' if not r['best'] else 'BAD'} {r['s']:4.1f}s best={r['best']} want={g['pos']}{' (unquoted)' if r['unquoted'] else ''}", flush=True)
    n = len(res)
    print(f"listwise {model}, {chars} chars per candidate: best = the PR {sum(r['ok'] for r in res) / n:.2f}, a wrong one {sum(bool(r['best']) and not r['ok'] for r in res) / n:.2f}, "
          f"none {sum(not r['best'] for r in res) / n:.2f}; PR among best+also {sum(r['ok'] or r['also_has_pos'] for r in res) / n:.2f}; "
          f"{sum(len(r['also']) for r in res) / n:.1f} others also; {(time.time() - t0) / n:.1f} s per question")


def ask_eval(path, label, limit, gate_budget, rounds):
    """The whole loop per question: rank of the PR in the answers, whether it was confirmed, whether something else was, seconds; to runs/ask-<label>.jsonl
    (resumable)."""
    import ask
    ev = json.load(open(path))
    cases = ev["cases"][:limit] if limit else ev["cases"]
    jl = os.path.join(os.path.dirname(path), "runs", f"ask-{label}.jsonl")
    done = {json.loads(x)["i"]: json.loads(x) for x in open(jl)} if os.path.exists(jl) else {}
    res, t0, new = list(done.values()), time.time(), 0
    for i, c in enumerate(cases):
        if i in done:
            continue
        r = ask.ask(c["q"], universe=ev["universe"], k=10, rounds=rounds, gate_budget=gate_budget)
        a = r["answers"]
        row = {"i": i, "want": c["want"], "rank": rank_of(a, c["want"]), "confirmed": [x["node"] for x in a if x["verdict"] == "yes"], "s": r["seconds"],
               "candidates": r["candidates"], "shown": r["shown"], "plan": r["plan"], "timing": r.get("timing"), "rounds": max(t.get("round", 1) for t in r["trace"])}
        row["right"] = c["want"] in row["confirmed"]
        res.append(row); new += 1
        open(jl, "a").write(json.dumps(row) + "\n")
        print(f"{len(res):3}/{len(cases)} rank {row['rank'] or '-':>2} {'CONFIRMED' if row['right'] else 'other confirmed' if row['confirmed'] else 'none confirmed':15} "
              f"{row['s']:5.1f}s r{row['rounds']} {row['candidates']} cands | running @1 {recall([x['rank'] for x in res], 1):.2f} @5 {recall([x['rank'] for x in res], 5):.2f} "
              f"| eta {(time.time() - t0) / new * (len(cases) - len(res)) / 60:.0f} min", flush=True)
    report_ask(res)


def report_ask(res):
    import statistics
    ranks = [r["rank"] for r in res]
    print(f"ask: recall@1 {recall(ranks, 1):.2f}  @5 {recall(ranks, 5):.2f}  @10 {recall(ranks, 10):.2f}; confirmed the PR {sum(r['right'] for r in res) / len(res):.2f}, "
          f"confirmed something else {sum(bool(r['confirmed']) and not r['right'] for r in res) / len(res):.2f}, nothing {sum(not r['confirmed'] for r in res) / len(res):.2f}; "
          f"median {statistics.median(r['s'] for r in res):.0f} s, second round in {sum(r['rounds'] > 1 for r in res)}")


def report_gate(rows):
    import statistics
    P = [r for r in rows if r["pos"]]; N = [r for r in rows if not r["pos"]]
    for name, sel in (("positives", P), ("negatives", N)):
        c = {v: sum(1 for r in sel if r["verdict"] == v) for v in ("yes", "maybe", "no")}
        print(f"{name:9} {len(sel):3}: yes {c['yes']:3}  maybe {c['maybe']:3}  no {c['no']:3}   tier-1 p median {statistics.median(r['p'] for r in sel):.2f}")
    pairs = [(p["p"], n["p"]) for p in P for n in N if n["case"] == p["case"]]
    print(f"tier-1 ranking: the PR above a same-question negative in {sum(a > b for a, b in pairs) / max(1, len(pairs)):.2f} of pairs")
    yes = [r for r in rows if r["verdict"] == "yes"]
    print(f"yes precision {sum(r['pos'] for r in yes) / max(1, len(yes)):.2f}, yes recall {sum(r['verdict'] == 'yes' for r in P) / max(1, len(P)):.2f}, "
          f"positives not rejected {sum(r['verdict'] != 'no' for r in P) / max(1, len(P)):.2f}; tier 2 used on {sum(r['tier'] == 2 for r in rows)} of {len(rows)}; "
          f"{sum(r['s'] for r in rows) / max(1, len(rows)):.1f} s per judgement")


if __name__ == "__main__":
    a = sys.argv[1:]
    def opt(name, default=None):
        if name in a:
            i = a.index(name); v = a[i + 1]; del a[i:i + 2]; return v
        return default
    cfg = config.load()
    cmd = a.pop(0) if a else "help"
    if cmd == "build":
        build(cfg, int(opt("--n", 30)), int(opt("--seed", 3)), opt("--out", OUT), opt("--from", "fix"))
    elif cmd == "search":
        kinds = opt("--kinds")
        baseline(a[0], int(opt("--k", 10)), "--no-rerank" not in a, kinds and kinds.split(","), "--years" in a)
    elif cmd == "gate":
        gate_eval(a[0], opt("--label", "default"), int(opt("--negatives", 4)), "--tier1-only" not in a)
    elif cmd == "gate-list":
        gate_list_eval(a[0], opt("--label", "default"), int(opt("--negatives", 4)), opt("--model", "qwen3-coder"), int(opt("--chars", 1200)))
    elif cmd == "ask":
        ask_eval(a[0], opt("--label", "default"), int(opt("--limit", 0)), int(opt("--gate", 10)), int(opt("--rounds", 2)))
    elif cmd == "ask-report":
        report_ask([json.loads(x) for x in open(a[0])])
    elif cmd == "gate-report":
        report_gate([json.loads(x) for x in open(a[0])])
    else:
        print(__doc__)
