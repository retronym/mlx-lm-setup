#!/usr/bin/env python3
"""A retrieval check for code chunking: can the first sentence of a definition's doc comment find that definition? Queries are built once from the
managed clone (tree-sitter: documented classes, objects, traits and defs), stored in a JSON file, and run through the gateway's /api/search, so the
same set measures an index before and after it is re-chunked and re-embedded. A hit is right when it is in the expected file and its text contains
the definition (its name as a whole word, preceded by def/val/class/object/trait/type/interface or a Java type).

usage: eval_code.py build <project/source> [--n 150] [--seed 1] [--out FILE]     (default FILE: data/eval/<project>-<source>.json)
       eval_code.py run <FILE> [--k 10] [--mode hybrid|bm25|vec] [--rerank] [--mask] [--label NAME]   prints recall@1/5/10, MRR, the mean hit size, and saves data/eval/runs/"""
import json, os, random, re, sys, time
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config, repos

DEFS = {"scala": ("function_definition", "class_definition", "object_definition", "trait_definition"), "java": ("method_declaration", "class_declaration", "interface_declaration")}


def first_sentence(doc):
    lines = [re.sub(r"^\s*(/\*\*|\*/|\*)\s?", "", ln).rstrip() for ln in doc.splitlines()]
    text = []
    for ln in lines:
        if not ln.strip() and text:
            break
        if ln.strip().startswith("@") and text or ln.strip().startswith(("{{{", "```", "<pre>", "@")):
            break
        text.append(ln.strip())
    t = " ".join(x for x in text if x)
    t = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", r"\1", t)
    t = re.sub(r"\{@(?:link|code)\s+([^}]+)\}", r"\1", t)
    t = re.sub(r"<[^>]+>", "", t)
    m = re.match(r"(.+?[.!?])(\s+[A-Z]|$)", t)
    return (m.group(1) if m else t).strip()


def build(cfg, src, n, seed):
    from sources import gitsrc, treesitter
    from tree_sitter import Parser
    d = repos.path_for(cfg, src.repo)
    gs = gitsrc.GitSource(src, d)
    files = {p: b for p, b in gitsrc.tree(d, src.ref, gs.prefixes, gs.suffixes).items() if not any(r.match(p) for r in gs.exclude)}
    cands = []
    for p in sorted(files):
        lang = "scala" if p.endswith(".scala") else "java" if p.endswith(".java") else None
        if lang is None:
            continue
        lg = treesitter.lang(lang)
        data = gitsrc.git(d, "show", f"{src.ref}:{p}")
        tree = lg.parser.parse(data)
        stack = [tree.root_node]
        while stack:
            nd = stack.pop()
            stack.extend(nd.children)
            if nd.type not in DEFS[lang]:
                continue
            prev = nd.prev_sibling
            if prev is None or prev.type not in lg.comments or not prev.text.startswith(b"/**") or prev.end_point[0] < nd.start_point[0] - 1:
                continue
            name = lg.name(nd)
            q = first_sentence(prev.text.decode(errors="replace"))
            words = q.split()
            if 6 <= len(words) <= 35 and re.fullmatch(r"[A-Za-z_$][\w$]*", name) and len(name) > 2 and not q.lower().startswith(("see ", "returns the", "@")):
                cands.append({"q": q, "path": p, "name": name, "line": nd.start_point[0] + 1})
    random.Random(seed).shuffle(cands)
    seen, out = set(), []
    for c in cands:                               # one query per (file, name): overloads would make "right" ambiguous
        if (c["path"], c["name"]) not in seen:
            seen.add((c["path"], c["name"])); out.append(c)
    return {"source": src.key, "built": time.strftime("%Y-%m-%d"), "queries": out[:n], "available": len(out)}


def is_right(hit, c):
    path = hit["url"].split("/blob/", 1)[-1].split("/", 1)[-1].split("#")[0]
    if path != c["path"]:
        return False
    return bool(re.search(r"\b(def|val|var|class|object|trait|type|interface|enum|record|[\w>\]]+)\s+" + re.escape(c["name"]) + r"\b", hit["text"]))


def run(path, k, mode, rerank, label, mask=False):
    import gateway_client
    ev = json.load(open(path))
    pid, sid = ev["source"].split("/")
    ranks, sizes, t0 = [], [], time.time()
    for c in ev["queries"]:
        q = re.sub(re.escape(c["name"]), "", c["q"], flags=re.I) if mask else c["q"]          # --mask: the definition's own name is removed from its description
        r = gateway_client.post("/api/search", {"query": q, "k": k, "mode": mode, "rerank": rerank, "projects": [pid], "sources": [f"{pid}/{sid}"], "kinds": ["file"]}, timeout=300)
        hit = next((i + 1 for i, h in enumerate(r["results"]) if is_right(h, c)), None)
        ranks.append(hit)
        sizes += [len(h["text"]) for h in r["results"][:3]]
    n = len(ranks)
    rec = lambda j: sum(1 for x in ranks if x and x <= j) / n
    res = {"label": label, "source": ev["source"], "queries": n, "k": k, "mode": mode, "rerank": rerank, "mask": mask, "recall@1": round(rec(1), 3), "recall@5": round(rec(5), 3),
           f"recall@{k}": round(rec(k), 3), "mrr": round(sum(1 / x for x in ranks if x) / n, 3), "mean_top3_chars": round(sum(sizes) / max(1, len(sizes))), "secs": round(time.time() - t0)}
    out = os.path.join(os.path.dirname(path), "runs"); os.makedirs(out, exist_ok=True)
    json.dump({**res, "ranks": ranks}, open(os.path.join(out, f"{os.path.basename(path)[:-5]}-{label}.json"), "w"))
    print(json.dumps(res))


if __name__ == "__main__":
    a = sys.argv[1:]
    opt = lambda name, d=None: a[a.index(name) + 1] if name in a else d
    if len(a) < 2 or a[0] not in ("build", "run"):
        sys.exit(__doc__)
    if a[0] == "build":
        cfg = config.load()
        src = next((s for p in cfg.projects.values() for s in p.sources if s.key == a[1] and s.type == "git"), None)
        if src is None:
            sys.exit(f"no git source {a[1]}")
        ev = build(cfg, src, int(opt("--n", 150)), int(opt("--seed", 1)))
        out = opt("--out") or str(cfg.data_path("eval", a[1].replace("/", "-") + ".json"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        json.dump(ev, open(out, "w"), indent=1)
        print(f"{len(ev['queries'])} queries (of {ev['available']} documented definitions) -> {out}")
    else:
        run(a[1], int(opt("--k", 10)), opt("--mode", "hybrid"), "--rerank" in a, opt("--label", "run"), "--mask" in a)
