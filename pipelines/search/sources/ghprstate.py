"""The volatile state of open pull requests, for the dashboard's triage view: draft, mergeability, CI, review decision, requested reviewers, the latest
review of each reviewer, base branch, milestone and diff size. None of it is in a PR's text and most of it changes without the PR's `updated` moving
(CI finishes, a branch goes stale), so it is fetched with GraphQL, 50 PRs per request, for open PRs only (a few hundred across the indexed repos), on every
sync that is older than `links.github.pr_state_max_age_hours`, and stored in the head chunk's metadata as `pr_state`:

  {draft, mergeable (MERGEABLE | CONFLICTING | UNKNOWN), ci (pass | fail | pending | none), decision (APPROVED | CHANGES_REQUESTED | REVIEW_REQUIRED | null),
   requested [login or team slug], reviews [{who, state}], base, milestone, add, del, files, fetched}

Metadata only: the chunk's hash is untouched, nothing is re-embedded. A PR that is edited is re-ingested without the key and so is fetched again."""
import json, time
from sources import ghissues, ghlinks

FIELDS = ("isDraft mergeable baseRefName additions deletions changedFiles reviewDecision milestone { title } "
          "reviewRequests(first: 10) { nodes { requestedReviewer { __typename ... on User { login } ... on Team { slug } } } } "
          "latestReviews(first: 15) { nodes { state author { login } } } "
          "commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }")
CI = {"SUCCESS": "pass", "FAILURE": "fail", "ERROR": "fail", "PENDING": "pending", "EXPECTED": "pending"}


def _query(owner, name, numbers):
    body = " ".join(f"p{n}: pullRequest(number: {n}) {{ {FIELDS} }}" for n in numbers)
    return f'query {{ repository(owner: "{owner}", name: "{name}") {{ {body} }} rateLimit {{ remaining resetAt }} }}'


def state_of(pr, now):
    """The `pr_state` dict for one GraphQL pullRequest node."""
    rollup = ((((pr.get("commits") or {}).get("nodes") or [{}])[0].get("commit") or {}).get("statusCheckRollup") or {}).get("state")
    req = [(n["requestedReviewer"].get("login") or n["requestedReviewer"].get("slug")) for n in pr["reviewRequests"]["nodes"] if n.get("requestedReviewer")]
    return {"draft": bool(pr["isDraft"]), "mergeable": pr["mergeable"], "ci": CI.get(rollup, "none"), "decision": pr["reviewDecision"], "requested": req,
            "reviews": [{"who": r["author"]["login"], "state": r["state"]} for r in pr["latestReviews"]["nodes"] if r.get("author")],
            "base": pr["baseRefName"], "milestone": (pr.get("milestone") or {}).get("title"), "add": pr["additions"], "del": pr["deletions"], "files": pr["changedFiles"],
            "fetched": ghissues._iso(now)}


def refresh(store, source_id, repo, max_age_hours=6, batch=50, log=print):
    """Fetch `pr_state` for the open PRs of `source_id` that have none or one older than `max_age_hours` (stalest first). Returns the number refreshed."""
    cutoff = ghissues._iso(time.time() - max_age_hours * 3600)
    rows = store.db.execute("""SELECT rowid, json_extract(meta, '$.number') FROM chunks WHERE source = ? AND json_extract(meta, '$.kind') = 'pr'
                               AND json_extract(meta, '$.state') = 'open' AND id NOT LIKE '%~%'
                               AND COALESCE(json_extract(meta, '$.pr_state.fetched'), '') <= ? ORDER BY COALESCE(json_extract(meta, '$.pr_state.fetched'), '')""", (source_id, cutoff)).fetchall()
    owner, name = repo.split("/")
    done = 0
    for i in range(0, len(rows), batch):
        part = rows[i:i + batch]
        got = ghlinks.run_query(repo, _query(owner, name, [n for _, n in part]), log)
        now = time.time()
        for rowid, n in part:
            pr = got.get(f"p{n}")
            if pr:                                                       # a PR GitHub no longer has keeps no state (the reconcile removes it)
                store.db.execute("UPDATE chunks SET meta = json_set(meta, '$.pr_state', json(?)) WHERE rowid = ?", (json.dumps(state_of(pr, now)), rowid))
        store.commit()
        done += len(part)
        log(f"  {repo} open PRs: state for {done} of {len(rows)}")
        time.sleep(ghissues.PAGE_DELAY)
    return done
