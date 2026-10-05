#!/usr/bin/env python3
"""Refresh a universe: bring every project's index up to date, using local models wherever a model helps.

  refresh.py [universe | project | project/source ...] [--tier high|normal|low | --max-priority N] [--budget-hours H]
             [--only phase,...] [--skip phase,...] [--local] [--force] [--dry-run]

Phases, in order (LLM work runs before embedding so the big LLM and the embedder do not evict each other from the gateway's memory budget):
  sync       git fetch + sync every source, in priority order (GitHub: forward walk, then the capped newest-first backfill)
  reconcile  drop issues and PRs deleted upstream (weekly, `refresh.reconcile_every_days`; or --only reconcile)
  enrich     local-LLM thread summaries for long threads, checked against the thread by the NLI model (off unless llm.thread_summaries.enabled)
  digest     a local-LLM digest of what changed since the last refresh, checked against the facts (llm.digest.enabled)
  embed      vectors for everything new, through the gateway's embedder (--local: in this process)
  neighbours duplicate candidates and topic clusters over issues and PRs, from the vectors (numpy, no model; skipped when nothing changed; neighbours.enabled)
  verify     database integrity, nothing left without a vector, canary queries through the gateway

Tiers choose by source priority (search.json refresh.tiers). `--budget-hours` stops starting new sources or LLM items after that long; the
rest comes first next time. One run at a time (a lock); what it is doing is in <data>/run.json for the web page, and the history of runs in
<data>/refresh.json. Exit status: 0 ok, 1 something failed, 3 another run is active."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config, runstate
from embed import targets, run_embed, load as load_embedder
from sync import run_sync

PHASES = ["sync", "reconcile", "enrich", "digest", "embed", "neighbours", "verify"]
DEFAULT_SINCE_S = 26 * 3600


def state_path(cfg):
    return cfg.data_path("refresh.json")


def load_state(cfg):
    try:
        return json.loads(state_path(cfg).read_text())
    except (OSError, ValueError):
        return {}


def save_state(cfg, st):
    tmp = state_path(cfg).with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(state_path(cfg))


def plan(cfg, universe, srcs, args, state, now):
    """Which phases run, with a reason for each decision: [(phase, run?, why)]."""
    llm = cfg.search["llm"]
    due_reconcile = now - state.get("last_reconcile", 0) >= cfg.search["refresh"]["reconcile_every_days"] * 86400
    out = []
    for ph in PHASES:
        if args["only"] and ph not in args["only"]:
            out.append((ph, False, "not in --only")); continue
        if ph in args["skip"]:
            out.append((ph, False, "--skip")); continue
        if ph == "reconcile" and not (due_reconcile or "reconcile" in args["only"]):
            out.append((ph, False, f"done {int((now - state.get('last_reconcile', 0)) / 86400)} day(s) ago; every {cfg.search['refresh']['reconcile_every_days']}")); continue
        if ph == "enrich" and not (llm["thread_summaries"]["enabled"] or "enrich" in args["only"]):
            out.append((ph, False, "llm.thread_summaries.enabled is false")); continue
        if ph == "digest" and not (llm["digest"]["enabled"] or "digest" in args["only"]):
            out.append((ph, False, "llm.digest.enabled is false")); continue
        if ph == "neighbours" and not (cfg.search["neighbours"]["enabled"] or "neighbours" in args["only"]):
            out.append((ph, False, "neighbours.enabled is false")); continue
        out.append((ph, True, ""))
    return out


def main(argv):
    a = list(argv)
    def opt(name, default=None):
        if name in a:
            i = a.index(name); v = a[i + 1]; del a[i:i + 2]; return v
        return default
    cfg = config.load()
    tier, maxp = opt("--tier"), opt("--max-priority")
    if tier and tier not in cfg.search["refresh"]["tiers"]:
        raise SystemExit(f"unknown tier {tier!r} (have: {', '.join(cfg.search['refresh']['tiers'])})")
    max_priority = int(maxp) if maxp else cfg.search["refresh"]["tiers"][tier]["max_priority"] if tier else 9
    budget = opt("--budget-hours")
    budget = float(budget) if budget else cfg.search["refresh"]["budget_hours"]
    args = {"only": set((opt("--only") or "").split(",")) - {""}, "skip": set((opt("--skip") or "").split(",")) - {""}}
    for ph in args["only"] | args["skip"]:
        if ph not in PHASES:
            raise SystemExit(f"unknown phase {ph!r} (have: {', '.join(PHASES)})")
    local, force, dry = (x in a for x in ("--local", "--force", "--dry-run"))
    a = [x for x in a if not x.startswith("--")]
    universe = next((x for x in a if x in cfg.universes), None) or (cfg.default_universe().id if not a else None)
    srcs = targets(cfg, a, max_priority)
    uid = universe or cfg.default_universe().id
    now = time.time()
    state = load_state(cfg)
    steps = plan(cfg, uid, srcs, args, state, now)
    if dry:
        print(f"refresh plan for {uid}: {len(srcs)} sources" + (f" (tier {tier}: priority <= {max_priority})" if tier or maxp else "") + (f", budget {budget} h" if budget else ""))
        for ph, on, why in steps:
            print(f"  {'run ' if on else 'skip'} {ph:10} {why}")
        for s in srcs:
            print(f"    p{s.priority} {s.key:22} {s.type:16}" + (f" cap {s.max_items_per_run}/run" if s.max_items_per_run else "") + (f" every >= {s.min_interval_hours:g} h" if s.min_interval_hours else ""))
        return 0
    deadline = now + budget * 3600 if budget else None
    result = {"started": now, "universe": uid, "phases": {}, "tier": tier, "max_priority": max_priority}
    try:
        with runstate.lock(cfg):
            run = runstate.Run(cfg, "refresh", [s.key for s in srcs])
            run.phases([ph for ph, on, _ in steps if on])
            failed = False
            for ph, on, why in steps:
                if not on:
                    continue
                run.phase(ph)
                t0, info, ok = time.time(), {}, True
                try:
                    if ph in ("sync", "reconcile"):
                        gh_or_git = [s for s in srcs if s.type == "github"] if ph == "reconcile" else srcs
                        n = run_sync(cfg, gh_or_git, run, force=force, reconcile=(ph == "reconcile"), deadline=deadline)
                        ok, info = n == 0, {"failed_sources": n}
                        if ph == "reconcile" and ok:
                            state["last_reconcile"] = time.time()
                    elif ph == "enrich":
                        import enrich
                        w, f, _ = enrich.summarize_threads(cfg, srcs, run, deadline=deadline, force_enabled="enrich" in args["only"])
                        info = {"summaries": w, "unfaithful": f}
                    elif ph == "digest":
                        import digest
                        since = state.get("last_run", {}).get("started") or now - DEFAULT_SINCE_S
                        d = digest.make_digest(cfg, uid, since, run)
                        info = {"digest": bool(d), "checked": bool(d and d["checked"]), "facts": d["facts"] if d else 0}
                        run.log("digest written" + ("" if not d or d["checked"] else " (NOT fully faithful to the facts after retries; flagged on the page)") if d else "digest: nothing changed")
                    elif ph == "embed":
                        run.phase("embed", "loading the embedding model")
                        emb = load_embedder(cfg, "local" if local else None)
                        n = run_embed(cfg, srcs, run, emb, deadline=deadline)
                        info = {"embedded": n, "via": emb.dev}
                    elif ph == "neighbours":
                        import neighbours
                        info = neighbours.compute(cfg, uid, run, force=force) or {"unchanged": True}
                    elif ph == "verify":
                        import verify
                        problems, counts = verify.integrity(cfg, uid)
                        passed, bad, skipped = verify.canaries(cfg, uid)
                        pending = sum(c["pending_vectors"] for c in counts.values())
                        ok = not problems and not bad
                        info = {"problems": problems, "pending_vectors": pending, "canaries_passed": passed, "canaries_failed": [b[0] for b in bad], "canaries_skipped": skipped}
                        for p in problems:
                            run.error(p)
                        for q, expect, got in bad:
                            run.error(f"canary failed: {q!r} expected one of {expect}, got {got}")
                        run.log(f"verify: {passed} canaries passed, {len(bad)} failed" + (f", skipped: {skipped}" if skipped else "") + f"; {pending} chunks still without a vector")
                except Exception as e:                           # noqa: BLE001  a failing phase must not stop the later ones
                    ok, info = False, {"error": f"{type(e).__name__}: {e}"}
                    run.error(f"{ph}: {type(e).__name__}: {e}")
                result["phases"][ph] = {"ok": ok, "secs": round(time.time() - t0, 1), **info}
                failed = failed or not ok
            result.update(finished=time.time(), ok=not failed)
            run.finish(not failed)
    except runstate.Busy as e:
        print(e, file=sys.stderr)
        return 3
    state["last_run"] = result
    state["history"] = ([{k: result[k] for k in ("started", "finished", "ok", "universe", "tier")} | {"secs": round(result["finished"] - result["started"])}] + state.get("history", []))[:20]
    save_state(cfg, state)
    print(json.dumps({k: v for k, v in result.items() if k != "phases"}) + "\n" + "\n".join(f"  {p}: {'ok' if v['ok'] else 'FAILED'} in {v['secs']} s" for p, v in result["phases"].items()))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
