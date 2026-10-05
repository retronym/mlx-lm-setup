#!/usr/bin/env python3
"""Plan (and nothing else) a move of a git source from the heuristic chunkers to the tree-sitter ones: chunk every file of the source both ways
from the managed clone, without touching any database, and report what would change and what the embedding pass would cost. The move itself is
one line of config (`"chunkers": {".scala": "scala_ts"}`) followed by `sync.py <source>` and `embed.py <source>`: sync re-chunks the files once and
replaces their chunks, embed fills only what is new.

usage: rechunk.py <project/source>... [--sample N] [--chars-per-s 24000]
  <project/source>   e.g. zinc/code; `all` plans every git source whose chunkers have a tree-sitter counterpart, smallest first
  --sample N         also print N random new chunks (id, title, size, first lines) to look at
  --chars-per-s      embedding throughput used for the time estimate (measured on this machine: ~80 chunks/s of ~300 chars)"""
import os, random, sys, time
sys.path.insert(0, os.path.dirname(__file__))
import config, repos
from sources import gitsrc

UPGRADE = {"scala": "scala_ts", "java": "java_ts"}


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))] if xs else 0


def line(name, sizes):
    n = len(sizes)
    return f"  {name:9} {n:7} chunks  median {pct(sizes, .5):5}  p10 {pct(sizes, .1):5}  p90 {pct(sizes, .9):5}  under 100 chars {100 * sum(x < 100 for x in sizes) / max(1, n):4.1f}%  {sum(sizes) / 1e6:6.2f} M chars"


def plan(cfg, src, sample, rate):
    chunkers = {suf: UPGRADE.get(name, name) for suf, name in src.chunkers.items()}
    todo = {suf for suf, name in src.chunkers.items() if name in UPGRADE}
    if not todo:
        print(f"{src.key}: nothing to upgrade ({', '.join(sorted(set(src.chunkers.values())))})")
        return None
    d = repos.path_for(cfg, src.repo)
    if not d.exists():
        print(f"{src.key}: no managed clone of {src.repo} yet (run sync.py first)")
        return None
    gs = gitsrc.GitSource(src, d, cfg.search["chunking"]["max_chars"], cfg.search["chunking"]["pack_chars"])
    from sources import treesitter
    treesitter.configure(gs.max_chars, gs.pack_chars)
    files = {p: b for p, b in gitsrc.tree(d, src.ref, gs.prefixes, gs.suffixes).items() if not any(r.match(p) for r in gs.exclude)}
    old, new, fallback, picked, t0 = [], [], 0, [], time.time()
    old_ids = 0
    for p in files:
        suf = next(s for s in gs.suffixes if p.endswith(s))
        if suf not in todo:
            continue
        text = gitsrc.git(d, "show", f"{src.ref}:{p}").decode(errors="replace")
        o, n = gitsrc.CHUNKERS[src.chunkers[suf]](p, text), gitsrc.CHUNKERS[chunkers[suf]](p, text)
        old += [len(c[2]) for c in o]
        new += [len(c[2]) for c in n]
        fallback += bool(n and n[0][4:] and n[0][4].get("fallback"))
        picked += [(p, c) for c in n]
    print(f"{src.key}: {len(files)} files, {sum(1 for p in files if any(p.endswith(s) for s in todo))} with a tree-sitter counterpart, {fallback} fall back to the heuristic chunker (parse errors); planned in {time.time() - t0:.0f} s")
    print(line("current", old)); print(line("tree-sitter", new))
    est = sum(new) / rate
    print(f"  embedding: {len(new)} new chunks (the {len(old)} current ones lose their vectors) ~ {est / 60:.0f} min at {rate:.0f} chars/s; keyword search works throughout")
    for p, c in random.Random(7).sample(picked, min(sample, len(picked))):
        print(f"  - {c[0]} | {c[1].split('  ')[-1]} | {len(c[2])} chars | {c[2][:90].strip()!r}")
    print(f"  to apply: set the chunkers of {src.key} to {chunkers} in config/projects/{src.project}.json, then\n"
          f"    sync.py {src.key} && embed.py {src.key}")
    return est


def main(a):
    sample = int(a[a.index("--sample") + 1]) if "--sample" in a else 0
    rate = float(a[a.index("--chars-per-s") + 1]) if "--chars-per-s" in a else 24000.0
    names = [x for i, x in enumerate(a) if not x.startswith("--") and (i == 0 or a[i - 1] not in ("--sample", "--chars-per-s"))]
    cfg = config.load()
    srcs = [s for p in cfg.projects.values() for s in p.sources if s.type == "git"]
    if names != ["all"]:
        srcs = [s for s in srcs if s.key in names or s.project in names]
        missing = [n for n in names if not any(s.key == n or s.project == n for s in srcs)]
        if missing:
            sys.exit(f"no such git source: {', '.join(missing)} (have: {', '.join(s.key for p in cfg.projects.values() for s in p.sources if s.type == 'git')})")
    srcs.sort(key=lambda s: (s.priority, s.key))
    total = 0.0
    for s in srcs:
        total += plan(cfg, s, sample, rate) or 0
    if len(srcs) > 1:
        print(f"total embedding estimate: ~{total / 60:.0f} min")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__)
    main(sys.argv[1:])
