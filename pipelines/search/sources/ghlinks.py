"""What GitHub itself knows about a pull request that its text does not say: the issues it closes (the sidebar's "Development" links and closing
keywords, as GitHub resolved them) and the commit it was merged as. Fetched with GraphQL, 50 PRs per request (about 6 points of the 5,000 an hour),
and stored in the PR chunk's metadata, so `links.py` only reads the indexes:

  closes     ["owner/repo#N", ...]   issues GitHub will close (or did) when the PR merges
  merge_sha  the merge or squash commit (null for an unmerged PR)
  gh_links   when it was fetched

The marker is what selects work: a PR chunk without `gh_links` is fetched, newest first, up to `links.github.max_prs_per_run` per run. A PR that is
edited is re-ingested by the sync with fresh metadata (no marker), so it is fetched again; an unchanged one never is. Metadata only: the chunk's
hash is untouched, nothing is re-embedded."""
import json, subprocess, time
from sources import ghissues


def _graphql(query):
    return subprocess.run(["gh", "api", "graphql", "-f", f"query={query}"], capture_output=True, text=True)


def _query(owner, name, numbers, first):
    body = " ".join(f"p{n}: pullRequest(number: {n}) {{ mergeCommit {{ oid }} closingIssuesReferences(first: {first}) {{ nodes {{ number repository {{ nameWithOwner }} }} }} }}"
                    for n in numbers)
    return f'query {{ repository(owner: "{owner}", name: "{name}") {{ {body} }} rateLimit {{ remaining resetAt }} }}'


def run_query(repo, query, log=print):
    """The `repository` object of a GraphQL query over one repo's PRs (`p<N>: pullRequest(number: N) {...}` aliases), retrying a rate limit and sleeping to
    the reset when few points are left. A PR GitHub no longer has comes back null; any other error is raised."""
    for attempt in range(6):
        r = _graphql(query)
        try:
            d = json.loads(r.stdout)
        except ValueError:
            d = None
        errors = (d or {}).get("errors") or []
        if d and d.get("data") and all(e.get("type") == "NOT_FOUND" for e in errors):
            break
        text = (r.stderr + " " + json.dumps(errors)).lower()
        if "rate limit" in text or "http 403" in text or "http 429" in text or "secondary" in text:
            log(f"  {repo} graphql: rate limited, waiting"); time.sleep(60 * (attempt + 1))
        else:
            raise RuntimeError(f"gh api graphql {repo}: {(r.stderr or json.dumps(errors)).strip()[:300]}")
    else:
        raise RuntimeError(f"gh api graphql {repo}: still rate limited after retries")
    rl = d["data"].get("rateLimit") or {}
    if rl.get("remaining", 9999) < ghissues.LOW_WATER:
        wait = max(0, ghissues._ts(rl["resetAt"].replace("+00:00", "Z")) - time.time()) + 5
        log(f"  graphql rate limit: {rl['remaining']} points left, sleeping {wait / 60:.1f} min until the reset"); time.sleep(wait)
    return d["data"]["repository"]


def fetch(repo, numbers, first=10, log=print):
    """{number: {"closes": [...], "merge_sha": sha | None}} for the PR numbers that exist (a deleted or transferred PR is simply absent)."""
    owner, name = repo.split("/")
    got = run_query(repo, _query(owner, name, numbers, first), log)
    out = {}
    for n in numbers:
        pr = got.get(f"p{n}")
        if pr:
            out[n] = {"closes": [f"{x['repository']['nameWithOwner']}#{x['number']}" for x in pr["closingIssuesReferences"]["nodes"]],
                      "merge_sha": (pr.get("mergeCommit") or {}).get("oid")}
    return out


def enrich(store, source_id, repo, max_prs=3000, batch=50, first=10, log=print):
    """Fetch closing references and merge commits for the PR chunks of `source_id` that have none yet. Returns the number of PRs done."""
    rows = store.db.execute("""SELECT rowid, json_extract(meta, '$.number') FROM chunks WHERE source = ? AND json_extract(meta, '$.kind') = 'pr' AND id NOT LIKE '%~%'
                               AND json_extract(meta, '$.gh_links') IS NULL ORDER BY json_extract(meta, '$.updated') DESC LIMIT ?""", (source_id, max_prs)).fetchall()
    done = 0
    for i in range(0, len(rows), batch):
        part = rows[i:i + batch]
        got = fetch(repo, [n for _, n in part], first, log)
        now = ghissues._iso(time.time())
        for rowid, n in part:
            g = got.get(n, {"closes": [], "merge_sha": None})
            store.db.execute("UPDATE chunks SET meta = json_set(meta, '$.closes', json(?), '$.merge_sha', ?, '$.gh_links', ?) WHERE rowid = ?",
                             (json.dumps(g["closes"]), g["merge_sha"], now, rowid))
        store.commit()
        done += len(part)
        log(f"  {repo} PRs: closing references and merge commits for {done} of {len(rows)}")
        time.sleep(ghissues.PAGE_DELAY)
    return done
