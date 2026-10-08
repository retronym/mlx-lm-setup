"""The G1 pilot: extract, run and judge reproducers for a calibration sample of fixed issues and a sample of open ones, then write a report.

  python pilot.py select            pick the samples (data/pilot.json)
  python pilot.py extract           LLM specs for every picked issue (cached in data/specs/<n>.json by thread text)
  python pilot.py run [-j 4]        the version matrix, judged (data/results.jsonl)
  python pilot.py report            data/report.md
  python pilot.py all

Calibration issues run on the release before the fix and the release that shipped it; open issues on the latest 2.12, 2.13 and 3."""
import hashlib, json, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import issues, extract, judge, run

D = Path(__file__).resolve().parent / "data"
SPECS, RUNS = D / "specs", D / "runs"


def select():
    cal, pool = issues.calibration()
    op, total = issues.open_sample()
    D.mkdir(parents=True, exist_ok=True)
    (D / "pilot.json").write_text(json.dumps({"calibration": cal, "open": op, "pool": {"calibration": pool, "open": total}}, indent=1))
    print(f"calibration {len(cal)} of {pool}, open {len(op)} of {total}")


def _picked():
    p = json.loads((D / "pilot.json").read_text())
    return [dict(x, set="calibration") for x in p["calibration"]] + [dict(x, set="open") for x in p["open"]]


def spec_for(n):
    th = issues.thread(n)
    h = hashlib.sha1(f"v{extract.VERSION}\n{th['text']}".encode()).hexdigest()[:12]
    f = SPECS / f"{n}.json"
    if f.exists():
        d = json.loads(f.read_text())
        if d.get("hash") == h:
            return d
    t0 = time.time()
    d = {"number": n, "title": th["title"], "hash": h, **extract.extract(th), "secs": round(time.time() - t0, 1)}
    SPECS.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(d, indent=1))
    return d


def do_extract():
    for i, x in enumerate(_picked(), 1):
        d = spec_for(x["number"])
        s = d["spec"] or {}
        print(f"[{i}] #{x['number']} {x['set']}: {'runnable' if s.get('runnable') else 'not runnable'} {s.get('symptom')} "
              f"attempts={d['attempts']} {'PROBLEMS ' + '; '.join(d['problems'])[:150] if d['problems'] else ''}", flush=True)


def versions(x):
    if x["set"] == "calibration":
        return [x["before"], x["after"]]
    return [run.LATEST["2.12"], run.LATEST["2.13"], run.LATEST["3"]]


def _one(x):
    d = spec_for(x["number"])
    s = d["spec"] or {}
    row = {**x, "title": d["title"], "symptom": s.get("symptom"), "runnable": bool(s.get("runnable")), "spec_ok": not d["problems"],
           "why_not": s.get("why_not"), "runs": []}
    if not (s.get("runnable") and not d["problems"]):
        return row
    for v in versions(x):
        r = run.run(s, v, RUNS / str(x["number"]) / v)
        r["verdict"], r["why"] = judge.judge(s, r)
        row["runs"].append(r)
    return row


def do_run(jobs=4):
    picked = _picked()
    for x in picked:                                                  # all extraction first: the LLM is one queue, the runs are CPU
        spec_for(x["number"])
    with ThreadPoolExecutor(jobs) as ex, open(D / "results.jsonl", "w") as out:
        for row in ex.map(_one, picked):
            out.write(json.dumps(row) + "\n"); out.flush()
            print(f"#{row['number']} {row['set']} {row['symptom']}: " + (", ".join(f"{r['version']}={r['verdict']}" for r in row["runs"]) or
                  ("spec failed" if not row["spec_ok"] else f"not runnable: {(row['why_not'] or '')[:80]}")), flush=True)


def _verdict_open(row):
    v = {r["version"]: r["verdict"] for r in row["runs"]}
    return v.get(run.LATEST["2.13"])


def report():
    rows = [json.loads(l) for l in open(D / "results.jsonl")]
    cal = [r for r in rows if r["set"] == "calibration"]
    op = [r for r in rows if r["set"] == "open"]
    L = ["# G1 pilot: reproducing scala/bug issues", ""]
    ran = [r for r in cal if r["runs"]]
    def pair(r):
        return tuple(x["verdict"] for x in r["runs"])
    good = [r for r in ran if pair(r) == ("reproduces", "not_reproduced")]
    wrong = [r for r in ran if pair(r)[1] == "reproduces" or pair(r)[0] == "not_reproduced"]
    L += ["## Calibration (fixed issues: release before the fix, release with it)", "",
          f"{len(cal)} issues; {len(ran)} with a runnable spec that passed the gates; **{len(good)} judged exactly right** "
          f"(bug before, gone after); {len(wrong)} judged wrongly in at least one run (the bug seen after the fix, or missing before it); "
          f"the rest differs, inconclusive or infra.", "",
          "| issue | symptom | before | after | fixed by |", "|---|---|---|---|---|"]
    for r in cal:
        cells = [f"{x['version']}: {x['verdict']}" for x in r["runs"]] or ["not run: " + ("spec failed" if not r["spec_ok"] else (r["why_not"] or "")[:60]), ""]
        L.append(f"| [scala/bug#{r['number']}](https://github.com/scala/bug/issues/{r['number']}) {r['title'][:60]} | {r['symptom']} | {cells[0]} | {cells[1]} | {r['pr']} |")
    L += ["", "## Open issues (latest 2.12, 2.13, 3)", ""]
    counts = {}
    for r in op:
        counts[_verdict_open(r) or "not run"] = counts.get(_verdict_open(r) or "not run", 0) + 1
    L += [", ".join(f"{k}: {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])), "",
          f"| issue | year | symptom | {run.LATEST['2.12']} | {run.LATEST['2.13']} | {run.LATEST['3']} |", "|---|---|---|---|---|---|"]
    for r in sorted(op, key=lambda r: (_verdict_open(r) != "not_reproduced", r["number"])):
        v = {x["version"]: x for x in r["runs"]}
        cells = [f"{v[k]['verdict']}" + (f" ({v[k]['why'][:40]})" if v[k]['verdict'] in ('differs', 'infra') else "") if k in v else "" for k in run.LATEST.values()]
        if not r["runs"]:
            cells = ["not run: " + ("spec failed" if not r["spec_ok"] else (r["why_not"] or "")[:60]), "", ""]
        L.append(f"| [scala/bug#{r['number']}](https://github.com/scala/bug/issues/{r['number']}) {r['title'][:60]} | {r['year']} | {r['symptom']} | " + " | ".join(cells) + " |")
    (D / "report.md").write_text("\n".join(L) + "\n")
    print(f"wrote {D / 'report.md'}")


if __name__ == "__main__":
    a = sys.argv[1:] or ["all"]
    jobs = int(a[a.index("-j") + 1]) if "-j" in a else 4
    if a[0] in ("select", "all"):
        select()
    if a[0] in ("extract", "all"):
        do_extract()
    if a[0] in ("run", "all"):
        do_run(jobs)
    if a[0] in ("report", "all"):
        report()
