"""Phase 2 evaluation: decode phase 0b's held-out bodies in several arms and typecheck the results.

Arms (all greedy, same prompt, the assistant turn pre-filled up to the def's `=`):
  plain       no compiler
  draft       trie drafts only (lossless: must equal plain, up to numerics of batched verification)
  mask        selection mask
  mask+draft  selection mask, with drafts
  retry       plain, then up to 2 retries with the compiler's errors fed back (the baseline the mask must beat)

Arms are interleaved per body, so machine load (other sessions' JVMs) hits them alike. Results: <work>/arms/<arm>.jsonl (resumable);
then each arm's spliced files are typechecked with jvm/Tool.scala as in phase 0b and reported.

Usage: python eval.py <work> [--arms plain,mask,...] [--n 100] [--only b012,b013]
"""
import argparse, collections, json, os, sys, time

from phase0b import MASKABLE, SYNTAX, jsonl, splice, tool

ARMS = {"plain": dict(), "draft": dict(draft=True), "mask": dict(mask=True), "mask+draft": dict(mask=True, draft=True), "retry": dict()}


def body_errors(task, body, check):
    text, s, e = splice(task["src"], task, body)
    return [x for x in check(task["uri"], text) if s <= x["offset"] < e]


def run_arm(D, task, arm, log):
    import compiler_client
    t0 = time.time()
    events = collections.Counter()
    on = lambda ev: events.update([ev["type"]])
    body, st = D.generate(task, on_event=on, **ARMS[arm])
    attempts = [dict(st)]
    if arm == "retry":
        for _ in range(2):
            errs = body_errors(task, body, compiler_client.check)
            if not errs:
                break
            fb = [f"line {x['line'] - task['line']}: {x['msg'].splitlines()[0]}" for x in errs[:8]]
            body, st = D.generate(task, on_event=on, feedback=(body, fb))
            attempts.append(dict(st))
    tot = {k: sum(a.get(k, 0) for a in attempts) for k in ("tokens", "steps", "triggers", "interventions", "drafted", "accepted", "compiler_ms", "decode_s", "prefill_s")}
    return {"id": task["id"], "arm": arm, "body": body, "attempts": len(attempts), "hit_max": attempts[-1]["hit_max"], "wall_s": time.time() - t0, **tot}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("--arms", default="plain,mask,mask+draft,draft,retry")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--only", default="")
    ap.add_argument("--report-only", action="store_true")
    a = ap.parse_args()
    arms = a.arms.split(",")
    from decode import make_task
    gen = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "phase0b", "gen")
    ids = sorted(os.listdir(gen))[:a.n]
    if a.only:
        ids = a.only.split(",")
    tasks = [make_task(json.load(open(os.path.join(gen, i, "meta.json")))) for i in ids]
    out = os.path.join(a.work, "arms")
    os.makedirs(out, exist_ok=True)
    done = {arm: {r["id"] for r in jsonl(os.path.join(out, f"{arm}.jsonl"))} if os.path.exists(os.path.join(out, f"{arm}.jsonl")) else set() for arm in arms}
    todo = [(t, arm) for t in tasks for arm in arms if t["id"] not in done[arm]]
    if todo and not a.report_only:
        from decode import Decoder, acquire_memory
        print("memory:", acquire_memory(), flush=True)
        D = Decoder()
        t0 = time.time()
        for k, (t, arm) in enumerate(todo):
            r = run_arm(D, t, arm, print)
            with open(os.path.join(out, f"{arm}.jsonl"), "a") as fh:
                fh.write(json.dumps(r) + "\n")
            el = time.time() - t0
            print(f"[{k + 1}/{len(todo)}] {t['id']} {arm:10} tok {r['tokens']:4} trig {r['triggers']:2} mask {r['interventions']:2} "
                  f"draft {r['accepted']}/{r['drafted']} compiler {r['compiler_ms'] / 1000:.1f}s decode {r['decode_s']:.1f}s  "
                  f"elapsed {el / 60:.0f}m eta {el / (k + 1) * (len(todo) - k - 1) / 60:.0f}m", flush=True)
    report(a.work, arms, {t["id"]: t for t in tasks})


def report(work, arms, tasks):
    base = {r["file"]: r for r in jsonl(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "phase0b", "baseline.jsonl"))}
    rows = {}
    for arm in arms:
        rs = [r for r in jsonl(os.path.join(work, "arms", f"{arm}.jsonl")) if r["id"] in tasks]
        d = os.path.join(work, "spliced", arm)
        paths, spans = [], {}
        for r in rs:
            t = tasks[r["id"]]
            text, s, e = splice(t["src"], t, r["body"])
            p = os.path.join(d, r["id"], os.path.basename(t["file"]))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if not os.path.exists(p) or open(p).read() != text:
                open(p, "w").write(text)
                if os.path.exists(os.path.join(d, "check.jsonl")):
                    os.remove(os.path.join(d, "check.jsonl"))
            paths.append(p)
            spans[r["id"]] = (s, e)
        cp = os.path.join(d, "check.jsonl")
        if not os.path.exists(cp):
            tool("check", *paths, out=cp)
        checks = {r["file"].split("/")[-2]: r for r in jsonl(cp)}
        agg = collections.Counter()
        for r in rs:
            t, c = tasks[r["id"]], checks[r["id"]]
            s, e = spans[r["id"]]
            inb_all = [x for x in c["errors"] if s <= x["offset"] < e]
            outside = len(c["errors"]) - len(inb_all) - len(base[t["file"]]["errors"])
            inb = list({(x["id"], x["offset"]): x for x in inb_all}.values())
            agg["bodies"] += 1
            if r["hit_max"]:
                agg["runaway"] += 1
            elif outside > 0 or any(x["id"] in SYNTAX for x in inb):
                agg["syntax"] += 1
            agg["with_errors"] += bool(inb)
            agg["errors"] += len(inb)
            agg["name_errors"] += sum(x["id"] in MASKABLE for x in inb)
            agg["not_a_member"] += sum(x["id"] == "NotAMemberID" for x in inb)
            for k in ("tokens", "decode_s", "compiler_ms", "triggers", "interventions", "drafted", "accepted", "steps", "attempts", "wall_s"):
                agg[k] += r[k]
        rows[arm] = agg
    print(f"\n{'arm':11} {'bodies':>6} {'w/ err':>6} {'errors':>6} {'name':>5} {'NAM':>4} {'runaway':>7} {'syntax':>6} {'tok':>6} {'tok/s':>6} "
          f"{'steps':>6} {'trig':>5} {'comp s':>6} {'mask':>5} {'draft acc':>10} {'wall s':>7}")
    for arm, g in rows.items():
        tps = g["tokens"] / max(1e-9, g["decode_s"] - g["compiler_ms"] / 1000)
        print(f"{arm:11} {g['bodies']:6} {g['with_errors']:6} {g['errors']:6} {g['name_errors']:5} {g['not_a_member']:4} {g['runaway']:7} {g['syntax']:6} "
              f"{g['tokens']:6} {tps:6.1f} {g['steps']:6} {g['triggers']:5} {g['compiler_ms'] / 1000:6.0f} {g['interventions']:5} "
              f"{g['accepted']:4}/{g['drafted']:<5} {g['wall_s']:7.0f}")
    print("\ntok/s excludes compiler time; name = not-found + not-a-member errors in bodies; NAM = not-a-member only")


if __name__ == "__main__":
    main()
