"""Judgement for the dashboard: what kind of change an open PR is and how risky, what kind of report a new issue is and whether it can be acted on. Typed
choices from the local decision model (`/api/decide`, several questions in one forward pass over one text, about half a second), each with its probability, so
the page shows only the labels the model is confident about and the rest as unsure. Nothing is generated, so nothing needs a faithfulness check.

The text is the title, labels, a head of the description and, for a PR, its size and base branch; no diff. Results are cached in `cache.db` keyed by the
model, the question set's version and that text, so a PR that did not change costs nothing the second time and an edited one is judged again. The page asks
for a few items at a time and fills the rows in as the answers come back."""
import hashlib, json, os, sqlite3, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gateway_client as gw

VERSION = 1                                                    # bump when a question or option changes: old answers are not reused
BODY_CHARS = 1500
MIN_P = 0.5                                                    # below this a label is shown as unsure, not asserted
QUESTIONS = {
    "pr": [("kind", "What kind of change is this pull request?",
            {"bugfix": "fixes a defect or crash", "feature": "adds a new capability, API or language feature", "performance": "makes something faster or allocate less",
             "refactor": "internal cleanup with no visible effect", "docs": "documentation or comments only", "build": "build, CI, release or tooling changes",
             "dependency": "a dependency or version bump", "backport": "a backport or forward-port of a change made elsewhere", "experiment": "a work in progress, experiment or proposal for discussion"}),
           ("risk", "How risky is this change for users and for binary or source compatibility?",
            {"api": "changes public API or binary compatibility", "semantics": "changes language semantics, type inference or implicit resolution", "backend": "changes code generation or the runtime",
             "local": "local and low risk"})],
    "issue": [("kind", "What kind of report is this issue?",
               {"bug": "a defect, crash or wrong behaviour", "regression": "something that worked in an earlier version and now does not", "feature": "a request for new behaviour or API",
                "question": "a question or request for help", "tracking": "a tracking issue, roadmap or umbrella for other work"}),
              ("triage", "Can someone act on this issue as it stands?",
               {"actionable": "has a clear reproduction or a concrete proposal", "needs-info": "is missing a reproduction, versions or details", "discussion": "is open-ended discussion with no concrete action"})],
}


def view(kind, title, text, meta):
    """The text the model reads for one item."""
    head = f"{'Pull request' if kind == 'pr' else 'Issue'} to the Scala compiler, library or build tools.\nTitle: {title}\n"
    if meta.get("labels"):
        head += f"Labels: {', '.join(meta['labels'])}\n"
    s = meta.get("pr_state") or {}
    if s:
        head += f"Base branch: {s['base']}. Size: +{s['add']} -{s['del']} in {s['files']} files.{' Draft.' if s.get('draft') else ''}\n"
    body = (text or "").strip().replace("\r", "")[:BODY_CHARS]
    return head + (f"Description: {body}" if body else "")


def _key(model, kind, v):
    return hashlib.sha1(f"{VERSION}\0{model}\0{kind}\0{v}".encode()).hexdigest()


def _cache(cfg):
    con = sqlite3.connect(cfg.data_path("cache.db"), timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS judge(k TEXT PRIMARY KEY, v TEXT, made REAL)")
    return con


def decide(kind, v, model=None):
    """{question: {"label", "p", "top": [[name, p], ...]}} for one text."""
    qs = QUESTIONS[kind]
    res = gw.decide_many(v, [{"t": "choice", "ins": q, "crit": opts} for _, q, opts in qs], model)
    out = {}
    for (name, _, _), r in zip(qs, res):
        top = sorted(r["probabilities"].items(), key=lambda kv: -kv[1])[:3]
        out[name] = {"label": r["answer"] if r["top_probability"] >= MIN_P else None, "guess": r["answer"], "p": round(r["top_probability"], 3), "top": [[k, round(p, 3)] for k, p in top]}
    return out


def find(cfg, project, items):
    """{(repo, number): (kind, title, text, meta)} for the head chunks of the wanted (repo, number) pairs in a project's database."""
    db = cfg.project_db(project)
    if not db.exists() or not items:
        return {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        want = {(r, int(n)) for r, n in items}
        docs = sorted({f"issue:{n}" for _, n in want})
        out = {}
        for i in range(0, len(docs), 400):
            part = docs[i:i + 400]
            for title, text, meta in con.execute(f"""SELECT title, text, meta FROM chunks WHERE doc IN ({','.join('?' * len(part))}) AND id NOT LIKE '%~%'
                                                      AND json_extract(meta, '$.kind') IN ('pr', 'issue')""", part):
                repo, _, rest = title.partition("#")
                n = int(rest.split(" ", 1)[0]) if rest.split(" ", 1)[0].isdigit() else None
                if (repo, n) in want:
                    m = json.loads(meta)
                    out[(repo, n)] = (m["kind"], rest.split(" ", 1)[1] if " " in rest else "", text, m)
        return out
    finally:
        con.close()


def judge(cfg, refs, model=None, budget_s=None):
    """Judge `refs` ([{project, repo, number}], any mix of PRs and issues): {"repo#number": {"type": "pr" | "issue", "cached": bool, <question>: {...}}}. Items
    still unjudged when `budget_s` seconds have passed are left out (the page asks again), so one request never holds a connection for long."""
    t0, out = time.time(), {}
    by_project = {}
    for r in refs:
        by_project.setdefault(r["project"], []).append((r["repo"], r["number"]))
    cache = _cache(cfg)
    try:
        for project, items in by_project.items():
            for (repo, n), (kind, title, text, meta) in find(cfg, project, items).items():
                v = view(kind, title, text, meta)
                k = _key(model or "", kind, v)
                row = cache.execute("SELECT v FROM judge WHERE k = ?", (k,)).fetchone()
                if row:
                    out[f"{repo}#{n}"] = {"type": kind, "cached": True, **json.loads(row[0])}
                    continue
                if budget_s and time.time() - t0 > budget_s:
                    continue
                d = decide(kind, v, model)
                cache.execute("INSERT OR REPLACE INTO judge VALUES(?, ?, ?)", (k, json.dumps(d), time.time()))
                cache.commit()
                out[f"{repo}#{n}"] = {"type": kind, "cached": False, **d}
    finally:
        cache.close()
    return out
