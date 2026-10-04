"""Zero-shot triage of scala/scala PRs with OpenJev, scored against the PRs' real GitHub labels.

Reads data/prs.json (gh pr list ... --json number,title,body,labels,files,mergedAt,author),
appends one JSON line per PR to data/results.jsonl and keeps data/status.json current,
so dashboard.html can follow along live. Resumable: PRs already in results.jsonl are skipped.

Usage: .venv-jev/bin/python jev_triage.py [--limit N] [--fresh]
"""
import argparse, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
PRS, RESULTS, STATUS = (os.path.join(DATA, f) for f in ("prs.json", "results.jsonl", "status.json"))

# (key, short name, hypothesis shown to the model, GitHub labels that count as ground truth)
QUESTIONS = [
    ("internal", "Internal housekeeping",
     "This change is internal housekeeping (build, CI, tests, refactoring or tooling) with no user-visible effect.",
     ["internal"]),
    ("release_notes", "Needs release notes",
     "This change is user-visible and would warrant a mention in the release notes.",
     ["release-notes"]),
    ("docs", "Documentation",
     "This change only updates documentation.",
     ["documentation"]),
    ("collections", "Library: collections",
     "This change modifies the Scala standard library collections.",
     ["library:collections"]),
    ("repl", "REPL",
     "This change affects the Scala REPL.",
     ["tool:REPL"]),
    ("perf", "Performance",
     "This change is a performance optimization.",
     ["performance", "performance:do_not_allocate"]),
]


def premise(pr, max_body=1500, max_files=25):
    files = [f["path"] for f in pr.get("files", [])]
    shown = files[:max_files] + ([f"... (+{len(files) - max_files} more)"] if len(files) > max_files else [])
    body = (pr.get("body") or "").strip().replace("\r", "")[:max_body]
    return f"Pull request to the Scala compiler and standard library.\nTitle: {pr['title']}\nChanged files:\n" + \
           "\n".join(f"- {p}" for p in shown) + (f"\nDescription: {body}" if body else "")


def write_status(**kw):
    tmp = STATUS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(kw, f)
    os.replace(tmp, STATUS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--fresh", action="store_true", help="discard previous results")
    a = ap.parse_args()

    prs = json.load(open(PRS))
    if a.limit:
        prs = prs[:a.limit]
    if a.fresh and os.path.exists(RESULTS):
        os.remove(RESULTS)
    done = set()
    if os.path.exists(RESULTS):
        done = {json.loads(l)["number"] for l in open(RESULTS) if l.strip()}

    qdefs = [dict(key=k, name=n, hypothesis=h, labels=l) for k, n, h, l in QUESTIONS]
    base = dict(total=len(prs), questions=qdefs, model="AlexWortega/openjev qwen3.5-4b-nli-v5",
                started=time.time(), device=None)
    write_status(state="loading", done=len(done), **{k: v for k, v in base.items() if k != "started"}, started=base["started"])

    import torch
    from jev_check import Jev
    t = time.time()
    jev = Jev()
    base["device"] = str(jev.enc.device)
    base["load_s"] = round(time.time() - t, 1)
    base["started"] = time.time()
    hyps = [h for _, _, h, _ in QUESTIONS]

    n_done = len(done)
    t_infer = 0.0
    write_status(state="running", done=n_done, **base)
    with open(RESULTS, "a") as out:
        for pr in prs:
            if pr["number"] in done:
                continue
            labels = [l["name"] for l in pr["labels"]]
            t0 = time.time()
            probs = jev.enc.predict_hypotheses(premise(pr), hyps)  # [n_hyp, 3] = contradiction, entailment, neutral
            dt = time.time() - t0
            t_infer += dt
            rec = dict(
                number=pr["number"], title=pr["title"], author=(pr.get("author") or {}).get("login"),
                labels=labels, secs=round(dt, 3),
                truth={k: any(l in labels for l in ls) for k, _, _, ls in QUESTIONS},
                p={k: [round(float(x), 4) for x in probs[i]] for i, (k, _, _, _) in enumerate(QUESTIONS)},
            )
            out.write(json.dumps(rec) + "\n"); out.flush()
            n_done += 1
            write_status(state="running", done=n_done, infer_s=round(t_infer, 1), **base)
    write_status(state="finished", done=n_done, infer_s=round(t_infer, 1), **base)
    print(f"finished {n_done} PRs; inference {t_infer:.1f}s ({t_infer / max(1, n_done - len(done)):.2f}s/PR)")


if __name__ == "__main__":
    main()
