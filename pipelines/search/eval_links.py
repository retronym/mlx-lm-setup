#!/usr/bin/env python3
"""Do links help search? (LINKS.md step 5.) Ground truth is what GitHub itself recorded: a merged PR and the issue it closes. Two questions per pair,
asked with the title of one side (never the body, never the other's number) and judged by whether the other side is found:

  fix    issue title -> the PR that closed it     (the user has the problem and wants the fix)
  issue  PR title    -> the issue it closed        (the user has the change and wants the reason)

Found means: among the top 10 hits (`primary`), among the `related` group the top hits seed (`related`), or either (`combined`). The lift of links is what
`related` finds that `primary` did not. Pairs are also bucketed by the word overlap of the two titles: a PR titled like its issue is easy for text
search, a PR titled by what it changed is where the links should matter. Variants of the expansion (depth, seeds, relation weights) are evaluated on the
same primary results; the boost is evaluated by re-running the search, with a regression check on the canary queries and the code-retrieval set.

usage: eval_links.py build [--n 300] [--seed 1] [--universe U] [--out FILE]
       eval_links.py run FILE [--label NAME] [--variants] [--boosts 0.25,0.5,1.0] [--no-rerank] [--limit PAIRS] [--every N]    (resumable; progress and running metrics as it goes)
       eval_links.py report LABEL                         the tables from whatever is done so far
       eval_links.py regress [--boosts 0.25,0.5,1.0] [--code data/eval/zinc-code.json]
       eval_links.py edges [--sample 30]                  precision of text-derived `closes` against GitHub's closing references; samples of other edges to judge"""
import json, os, random, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config, search
from linkdb import LinkDB

TEMPLATED = re.compile(r"^\W*(bump|update|upgrade|release|merge|revert|backport|\[backport\]|\[nomerge\]|dependabot|scala-steward|sync)\b|^[\d.]+ ?(release|merge)", re.I)
WORD = re.compile(r"[a-z][a-z0-9_]{2,}")
STOPW = set("the and for with from that this not are was when use using into fix add remove update support error warning".split())


def words(t):
    return {w for w in WORD.findall((t or "").lower()) if w not in STOPW}


def jaccard(a, b):
    a, b = words(a), words(b)
    return len(a & b) / len(a | b) if a | b else 0.0


def doc_of(idx, node):
    """(title without the `repo#N ` prefix, text) of a node's main chunk."""
    row = idx.stores[node["project"]].db.execute("SELECT title, text FROM chunks WHERE id = ?", (node["chunk"],)).fetchone()
    title = " ".join(row[0].split())
    title = re.sub(r"^\S+#\d+ ", "", title)
    title = re.sub(r"^\S+ commit [0-9a-f]{8} ", "", title)
    return title, row[1]


def build(cfg, universe, n, seed):
    idx = search.Index(cfg, universe)
    try:
        ld = idx.links
        rows = ld.db.execute("""SELECT e.src, e.dst, e.conf, e.how FROM edges e JOIN nodes a ON a.id = e.src JOIN nodes b ON b.id = e.dst
                                WHERE e.type = 'closes' AND e.how = 'github closing ref' AND a.indexed = 1 AND b.indexed = 1 AND a.kind = 'pr' AND b.kind = 'issue'""").fetchall()
        groups, skipped = {}, 0
        for src, dst, conf, how in rows:
            fix, issue = ld.node(src), ld.node(dst)
            ft, fb = doc_of(idx, fix)
            it, ib = doc_of(idx, issue)
            if TEMPLATED.search(ft) or TEMPLATED.search(it) or len(it.split()) < 3 or len(ft.split()) < 3:
                skipped += 1
                continue
            groups.setdefault((fix["repo"], issue["repo"]), []).append({
                "fix": {"id": fix["id"], "title": ft, "state": fix["state"]}, "issue": {"id": issue["id"], "title": it, "state": issue["state"]},
                "overlap": round(jaccard(ft, it), 3), "pair": f"{fix['repo']} -> {issue['repo']}"})
        rng, out = random.Random(seed), []
        for g in groups.values():
            rng.shuffle(g)
        while len(out) < n and any(groups.values()):                      # round robin over (PR repo, issue repo): the big pair does not take everything
            for g in groups.values():
                if g and len(out) < n:
                    out.append(g.pop())
        return {"universe": idx.universe.id, "built": time.strftime("%Y-%m-%d"), "pairs": out, "available": len(rows) - skipped, "skipped_templated": skipped}
    finally:
        idx.close()


def tercile(pairs):
    o = sorted(p["overlap"] for p in pairs)
    return o[len(o) // 3], o[2 * len(o) // 3]


def load_models(cfg, rerank):
    import embed
    emb = embed.load(cfg, "local")
    rr = None
    if rerank:
        import rerank as rr_mod
        from cache import CachedReranker
        rr = CachedReranker(rr_mod.Reranker(cfg.search["reranker"]["model"]), cfg.data_path("eval", "cache.db"), 500000, salt=rr_mod.TASK)
    return emb, rr


def nodes_of(items):
    return [h["node"] for h in items if h.get("node")]


def rank_in(items, nid):
    return next((i + 1 for i, h in enumerate(items) if h.get("node") == nid), None)


def mem_gb():
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**30             # bytes on macOS


def free_gpu_cache():
    """The MPS allocator keeps every shape it has seen: a long loop of reranker batches of varying size grew one run to 41 GB and a standstill."""
    import gc
    gc.collect()
    try:
        import torch
        torch.mps.empty_cache()
    except Exception:                                                                # noqa: BLE001  no MPS (or no torch): nothing to free
        pass


def variants_for(base_cfg, full):
    V = {"default": {}}
    if full:
        V.update({"no depth 2": {"depth2": False}, "3 seeds": {"seeds": 3}, "10 seeds": {"seeds": 10}, "no mentions": {"weights": {"mentions": 0, "mentioned_by": 0}},
                  "closes only": {"weights": {k: 0 for k in base_cfg["weights"] if k not in ("closes", "closed_by", "merged_as", "merge_of")}},
                  "no touches/ships/shipped": {"weights": {"touches": 0, "touched_by": 0, "ships": 0, "shipped_in": 0}}, "20 related": {"limit": 20}})
    return V


def run(cfg, path, label, variants, boosts, rerank, limit=None, every=10):
    """Evaluate the pairs of `path`. Every question's result is appended to data/eval/runs/links-<label>.jsonl as it is done, so a kill loses nothing and a rerun
    with the same label resumes; progress (rate, ETA, memory, cache hits) and the running metrics are printed as it goes; `report <label>` prints the tables
    from whatever is done, at any time."""
    ev = json.load(open(path))
    pairs = ev["pairs"][:limit] if limit else ev["pairs"]
    out_dir = os.path.join(os.path.dirname(path), "runs")
    os.makedirs(out_dir, exist_ok=True)
    jl = os.path.join(out_dir, f"links-{label}.jsonl")
    lo, hi = tercile(ev["pairs"])
    base_cfg = dict(cfg.search["related"])
    V = variants_for(base_cfg, variants)
    done = {}
    if os.path.exists(jl):
        for line in open(jl):
            r = json.loads(line)
            if "case" in r:
                done[(r["case"]["i"], r["case"]["direction"])] = r["case"]
        print(f"resuming {jl}: {len(done)} questions already done", flush=True)
    else:
        open(jl, "w").write(json.dumps({"meta": {"file": path, "variants": list(V), "boosts": boosts, "lo": lo, "hi": hi, "rerank": rerank}}) + "\n")
    idx = search.Index(cfg, ev["universe"])
    emb, rr = load_models(cfg, rerank)
    tune = search.tuning(cfg)
    total = 2 * len(pairs)
    cases = list(done.values())
    t0, new, last = time.time(), 0, 0.0
    print(f"{total} questions ({len(pairs)} pairs x 2), {len(V)} expansion variants, boosts {boosts or 'none'}, rerank {'on' if rerank else 'off'}; "
          f"progress every {every} questions", flush=True)
    for i, p in enumerate(pairs):
        for direction, qside, want in (("fix", p["issue"], p["fix"]), ("issue", p["fix"], p["issue"])):
            if (i, direction) in done:
                continue
            kw = dict(k=10, mode="hybrid", embedder=emb, reranker=rr, refs_in_query=False, text_chars=50, link_boost=0.0, **tune)
            hits = search.hits(idx, qside["title"], **kw)
            answers = [h for h in hits if h.get("node") != qside["id"]]           # the question's own document is not an answer
            c = {"i": i, "pair": p["pair"], "direction": direction, "overlap": p["overlap"], "q": qside["title"], "want": want["id"], "primary": rank_in(answers, want["id"]),
                 "n_primary": len(answers), "own": rank_in(hits, qside["id"]), "related": {}, "boost": {}}
            for name, over in V.items():
                cfg.search["related"].clear()
                cfg.search["related"].update({**base_cfg, **{k: ({**base_cfg["weights"], **v} if k == "weights" else v) for k, v in over.items()}})
                t = time.time()
                rel = search.related(idx, hits)
                c["related"][name] = {"rank": rank_in(rel, want["id"]), "n": len(rel), "ms": round((time.time() - t) * 1000, 1)}
            cfg.search["related"].clear(); cfg.search["related"].update(base_cfg)
            for b in boosts:
                c["boost"][str(b)] = rank_in([h for h in search.hits(idx, qside["title"], **{**kw, "link_boost": b}) if h.get("node") != qside["id"]], want["id"])
            open(jl, "a").write(json.dumps({"case": c}) + "\n")
            cases.append(c)
            new += 1
            if new % 10 == 0:
                free_gpu_cache()
            if new % every == 0 or time.time() - last > 60:
                last = time.time()
                el = last - t0
                eta = el / new * (total - len(cases))
                n = len(cases)
                r = lambda f: sum(1 for x in cases if f(x))
                prim = r(lambda x: x["primary"] and x["primary"] <= 10)
                rel = r(lambda x: x["related"]["default"]["rank"])
                comb = r(lambda x: (x["primary"] and x["primary"] <= 10) or x["related"]["default"]["rank"])
                cache = f", rerank cache {100 * rr.hits / max(1, rr.hits + rr.misses):.0f}% hit" if rr is not None and hasattr(rr, "hits") else ""
                print(f"  {n}/{total} questions, {el / new:.1f} s each, ETA {eta / 60:.0f} min, {mem_gb():.1f} GB{cache} | primary@10 {pct(prim, n)}  related {pct(rel, n)}  "
                      f"combined {pct(comb, n)}  lift {pct(comb - prim, n)}", flush=True)
    idx.close()
    return report_from(jl)


def report_from(jl):
    lines = [json.loads(x) for x in open(jl)]
    meta = lines[0]["meta"]
    cases = [x["case"] for x in lines[1:] if "case" in x]
    V = {name: None for name in meta["variants"]}
    return report(cases, V, meta["boosts"], meta["lo"], meta["hi"])


def pct(x, n):
    return f"{100 * x / n:5.1f}%" if n else "   -  "


def report(cases, V, boosts, lo, hi):
    def block(title, cs):
        n = len(cs)
        r = lambda f: sum(1 for c in cs if f(c))
        prim = lambda k: (lambda c: c["primary"] and c["primary"] <= k)
        print(f"\n{title}  (n={n})")
        print(f"  primary   recall@1 {pct(r(prim(1)), n)}  @5 {pct(r(prim(5)), n)}  @10 {pct(r(prim(10)), n)}")
        out = {"n": n, "primary@1": r(prim(1)), "primary@5": r(prim(5)), "primary@10": r(prim(10))}
        for name in V:
            rel = r(lambda c: c["related"][name]["rank"])
            comb = r(lambda c: prim(10)(c) or c["related"][name]["rank"])
            size = sum(c["related"][name]["n"] for c in cs) / max(1, n)
            ms = sum(c["related"][name]["ms"] for c in cs) / max(1, n)
            print(f"  related   {name:26} found {pct(rel, n)}  combined@10 {pct(comb, n)}  lift {pct(comb - r(prim(10)), n)}  (avg {size:.1f} items, {ms:.1f} ms)")
            out[name] = {"related": rel, "combined": comb, "avg_items": round(size, 2)}
        for b in boosts:
            def rk(c):
                return c["boost"][str(b)]
            at = lambda k: r(lambda c: rk(c) and rk(c) <= k)
            mrr = sum(1 / rk(c) for c in cs if rk(c)) / max(1, n)
            base_mrr = sum(1 / c["primary"] for c in cs if c["primary"]) / max(1, n)
            better = r(lambda c: rk(c) and (not c["primary"] or rk(c) < c["primary"]))
            worse = r(lambda c: c["primary"] and (not rk(c) or rk(c) > c["primary"]))
            print(f"  boost {b:<5} recall@1 {pct(at(1), n)}  @5 {pct(at(5), n)}  @10 {pct(at(10), n)}  MRR {mrr:.3f} (base {base_mrr:.3f})  better {better}, worse {worse}")
            out[f"boost {b}"] = {"@1": at(1), "@5": at(5), "@10": at(10), "mrr": round(mrr, 4), "better": better, "worse": worse}
        return out
    res = {}
    for d in ("fix", "issue"):
        res[d] = block(f"{d}: " + ("issue title -> the PR that closed it" if d == "fix" else "PR title -> the issue it closed"), [c for c in cases if c["direction"] == d])
    res["all"] = block("all", cases)
    for label, f in (("low overlap of the two titles", lambda c: c["overlap"] <= lo), ("middle", lambda c: lo < c["overlap"] <= hi), ("high", lambda c: c["overlap"] > hi)):
        res[label] = block(f"overlap: {label} (jaccard {'<= %.2f' % lo if label.startswith('low') else '> %.2f' % hi if label == 'high' else '%.2f-%.2f' % (lo, hi)})", [c for c in cases if f(c)])
    for label, f in (("the question's own document is in the top 3 (the seed is there)", lambda c: c.get("own") and c["own"] <= 3), ("it is not (links have no seed to start from)", lambda c: not c.get("own") or c["own"] > 3)):
        cs = [c for c in cases if f(c)]
        if cs:
            res[label] = block(f"seed: {label}", cs)
    pairs = {}
    for c in cases:
        pairs.setdefault(c["pair"], []).append(c)
    for name, cs in sorted(pairs.items(), key=lambda x: -len(x[1])):
        res[name] = block(f"{name}", cs)
    return res


def regress(cfg, boosts, code_path):
    """Canaries and the code-retrieval set with the boost on: it must not make them worse."""
    import eval_code
    idx = search.Index(cfg)
    emb, rr = load_models(cfg, True)
    tune = search.tuning(cfg)
    canaries = json.load(open(cfg.dir / "canaries.json"))["queries"]
    code = json.load(open(code_path)) if code_path and os.path.exists(code_path) else None
    for b in [0.0, *boosts]:
        ok = 0
        for c in canaries:
            hits = search.hits(idx, c["q"], k=c.get("k", 5), mode="hybrid", embedder=emb, reranker=rr, projects=c.get("projects"), link_boost=b, **tune)
            hay = " ".join(f"{h['title']} {h['url']}" for h in hits).lower()
            ok += any(x.lower() in hay for x in c["expect"])
        line = f"boost {b:<5} canaries {ok}/{len(canaries)}"
        if code:
            pid, sid = code["source"].split("/")
            ranks, t0 = [], time.time()
            for j, c in enumerate(code["queries"], 1):
                hits = search.hits(idx, c["q"], k=10, mode="hybrid", embedder=emb, reranker=None, projects=[pid], sources=[f"{pid}/{sid}"], kinds=["file"], text_chars=50000, link_boost=b, **tune)
                ranks.append(next((i + 1 for i, h in enumerate(hits) if eval_code.is_right(h, c)), None))
                if j % 25 == 0:
                    el = time.time() - t0
                    print(f"  boost {b}: code {j}/{len(code['queries'])}, {el / j:.1f} s each, ETA {el / j * (len(code['queries']) - j):.0f} s | recall@10 so far {sum(1 for x in ranks if x) / j:.3f}", flush=True)
                if j % 10 == 0:
                    free_gpu_cache()
            n = len(ranks)
            line += f"   code {code['source']} (no rerank): recall@1 {sum(1 for x in ranks if x == 1) / n:.3f}  @5 {sum(1 for x in ranks if x and x <= 5) / n:.3f}  @10 {sum(1 for x in ranks if x) / n:.3f}  MRR {sum(1 / x for x in ranks if x) / n:.3f}"
        print(line, flush=True)
    idx.close()


def edges(cfg, universe, sample):
    """Text-derived `closes` of PRs against GitHub's closing references, for the PRs GitHub was asked about; then random edges of the other types to judge by eye."""
    from refs import make_parser
    import json as _j, sqlite3
    lk = cfg.search["links"]
    parse, alias = make_parser(lk["legacy_prefixes"]), lk["repo_aliases"]
    tp = fp = fn = 0
    by = {}
    bad = []
    for pid in cfg.universe(universe).projects:
        db = cfg.project_db(pid)
        if not db.exists():
            continue
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        for cid, sid, title, text, meta in con.execute("SELECT id, source, title, text, meta FROM chunks WHERE json_extract(meta, '$.kind') = 'pr' AND id NOT LIKE '%~%' AND json_extract(meta, '$.gh_links') IS NOT NULL"):
            m = _j.loads(meta)
            repo = cfg.projects[pid].source(sid).repo
            canon = lambda r, k: f"{alias.get(r, r)}#{k}"
            gh = {f"{alias.get(t.split('#')[0], t.split('#')[0])}#{t.split('#')[1]}" for t in m.get("closes") or []}
            mine = set()
            for ref in parse(re.sub(r"^\S+#\d+ ", "", title), lists=False) + parse(text, lists=False):
                if ref.kind == "issue" and ref.closing:
                    mine.add(canon(ref.repo or repo, ref.key))
            # a bare number of a scala/scala PR may mean scala/bug (the configured fallback): count it as the same
            mine_fb = {x for x in mine} | {f"{r}#{x.split('#')[1]}" for x in mine for r in lk["bare_fallbacks"].get(repo, [])}
            hit = {x for x in mine if x in gh or any(f"{r}#{x.split('#')[1]}" in gh for r in lk["bare_fallbacks"].get(repo, []))}
            a, b, c = len(hit), len(mine) - len(hit), len(gh - mine_fb)
            tp, fp, fn = tp + a, fp + b, fn + c
            s = by.setdefault(repo, [0, 0, 0]); s[0] += a; s[1] += b; s[2] += c
            if b and len(bad) < sample:
                bad.append((f"{repo}#{m['number']}", sorted(mine - hit), sorted(gh)))
        con.close()
    p, r = tp / max(1, tp + fp), tp / max(1, tp + fn)
    print(f"text `closes` of PRs vs GitHub's closing references: precision {p:.3f} recall {r:.3f}  (tp {tp}, fp {fp}, fn {fn})")
    for repo, (a, b, c) in sorted(by.items()):
        print(f"  {repo:22} precision {a / max(1, a + b):.3f} recall {a / max(1, a + c):.3f}  (tp {a}, fp {b}, fn {c})")
    print("text says closes, GitHub does not:")
    for x in bad:
        print("  ", x)


def main(argv):
    a = list(argv)
    opt = lambda name, d=None: a[a.index(name) + 1] if name in a else d
    if not a or a[0] not in ("build", "run", "regress", "edges", "report"):
        sys.exit(__doc__)
    cfg = config.load()
    boosts = [float(x) for x in opt("--boosts", "0.25,0.5,1.0").split(",") if x]
    if a[0] == "build":
        uni = opt("--universe") or cfg.default_universe().id
        ev = build(cfg, uni, int(opt("--n", 300)), int(opt("--seed", 1)))
        out = opt("--out") or str(cfg.data_path("eval", f"links-{uni}.json"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        json.dump(ev, open(out, "w"), indent=1)
        print(f"{len(ev['pairs'])} pairs (of {ev['available']} closing pairs, {ev['skipped_templated']} templated skipped) -> {out}")
        from collections import Counter
        print(dict(Counter(p["pair"] for p in ev["pairs"])))
    elif a[0] == "run":
        run(cfg, a[1], opt("--label", "run"), "--variants" in a, boosts if opt("--boosts") or "--boosts" not in a else [], "--no-rerank" not in a,
            int(opt("--limit")) if opt("--limit") else None, int(opt("--every", 10)))
    elif a[0] == "report":
        report_from(os.path.join(str(cfg.data_path("eval", "runs")), f"links-{a[1]}.jsonl"))
    elif a[0] == "regress":
        regress(cfg, boosts, opt("--code", str(cfg.data_path("eval", "zinc-code.json"))))
    else:
        edges(cfg, cfg.default_universe().id, int(opt("--sample", 30)))


if __name__ == "__main__":
    main(sys.argv[1:])
