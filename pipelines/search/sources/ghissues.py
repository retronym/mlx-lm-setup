"""GitHub issues, pull requests and their comments (scala/bug, scala/scala), via `gh api`. Three incremental streams, each an
`updated`-ascending list filtered by `since=<cursor>`: issues-and-PRs (title + body), issue/PR conversation comments (one repo-wide
stream) and, for repos with PRs, inline review comments. Every item is its own chunk keyed by id, so a new comment embeds one new
chunk and an edit re-embeds that comment only. State is metadata (a PR is `merged`, `closed` or `open`; comments take their
issue's state at query time), so closing an issue re-embeds nothing.

Paging is explicit so it can be polite and resumable: after every page the chunks are committed and the stream's cursor saved, the
rate limit is checked every few pages (sleeping until the reset when little is left), and a 403/429 is waited out and retried. An
interrupted run just continues from the last committed page. Deletions are invisible to `since`; `reconcile()` lists all live
numbers and removes the rest."""
import json, subprocess, time
from store import Chunk
from sources.gitsrc import _split_big, MAX

LOW_WATER = 300               # sleep until the reset when fewer requests than this remain


def _gh(args):
    return subprocess.run(["gh", "api", *args], capture_output=True, text=True)


def rate_limit():
    r = _gh(["rate_limit", "--jq", ".resources.core | [.remaining, .reset] | @tsv"])
    rem, reset = (int(x) for x in r.stdout.split())
    return rem, reset


def wait_for_quota(log=print):
    rem, reset = rate_limit()
    if rem < LOW_WATER:
        wait = max(0, reset - time.time()) + 5
        log(f"  rate limit: {rem} requests left, sleeping {wait / 60:.1f} min until the reset")
        time.sleep(wait)


PAGE_LIMIT = 90               # GitHub refuses `page=` beyond about 100 pages (10,000 items) on big lists: re-anchor `since` before that


def pages(path, log=print, **params):
    """Yield the items of each page of a GitHub list endpoint, 100 per page, politely. Lists sorted by `updated` ascending are
    re-anchored every PAGE_LIMIT pages at the last item's `updated_at` (the boundary items repeat once; the store skips them)."""
    page = 1
    while True:
        q = "&".join(f"{k}={v}" for k, v in {"per_page": 100, "page": page, **params}.items())
        for attempt in range(6):
            r = _gh([f"{path}?{q}"])
            if r.returncode == 0:
                break
            if "rate limit" in r.stderr.lower() or "HTTP 403" in r.stderr or "HTTP 429" in r.stderr:
                log(f"  {path} page {page}: rate limited, waiting"); wait_for_quota(log); time.sleep(30 * (attempt + 1))
            else:
                raise RuntimeError(f"gh api {path}: {r.stderr.strip()[:300]}")
        else:
            raise RuntimeError(f"gh api {path}: still rate limited after retries")
        items = json.loads(r.stdout)
        if not items:
            return
        yield items
        if len(items) < 100:
            return
        time.sleep(0.2)
        if page % 25 == 0:
            wait_for_quota(log)
        if page >= PAGE_LIMIT and "since" in params and items[-1].get("updated_at"):
            params = {**params, "since": items[-1]["updated_at"]}
            page = 1
        else:
            page += 1


def gh_pages(path, **params):             # kept for callers that want a flat list
    return [i for p in pages(path, **params) for i in p]


class GhIssues:
    def __init__(self, repo="scala/bug", name="bug", reviews=False, default_since="2000-01-01T00:00:00Z"):
        self.repo, self.name, self.reviews, self.default_since = repo, name, reviews, default_since

    # ---- chunking ----
    def _chunks(self, id_prefix, doc, title, text, url, meta):
        for n, part in enumerate(_split_big((text or "").splitlines(), MAX)):
            body = "\n".join(part).strip()
            if body or n == 0:
                yield Chunk(f"{self.name}:{id_prefix}" + (f"~{n}" if n else ""), doc, title, body, url, meta)

    @staticmethod
    def _noise(c):
        body = (c.get("body") or "").strip()
        return (c.get("user") or {}).get("type") == "Bot" or not body or body.startswith("/")      # bots and "/rebuild"-style commands

    def _title(self, store, n):
        r = store.db.execute("SELECT title FROM chunks WHERE id=?", (f"{self.name}:issue:{n}",)).fetchone()
        return r[0].split(" ", 1)[1] if r else ""

    # ---- the three streams: each returns (items seen, [added, changed, deleted, unchanged]) ----
    def _stream(self, store, key, path, explicit, limit, to_chunks, log, **params):
        # each stream resumes from its own cursor; an explicit --since overrides all of them
        since = explicit or store.get(self.name, key) or store.get(self.name, "since") or self.default_since
        seen, tot = 0, [0, 0, 0, 0]
        for page in pages(path, log, sort="updated", direction="asc", since=since, **params):
            for it in page[:limit - seen if limit else None]:
                for k, v in enumerate(store.apply(self.name, list(to_chunks(it)))):
                    tot[k] += v
                seen += 1
            last = page[-1]["updated_at"]
            store.put(self.name, key, max(last, store.get(self.name, key) or ""))
            store.commit()
            log(f"  {self.name} {key}: {seen} items, up to {last}")
            if limit and seen >= limit:
                break
        return seen, tot

    def sync(self, store, since=None, limit=None, log=print):
        titles = {}

        def issue(i):
            n = i["number"]
            titles[n] = i["title"]
            pr = i.get("pull_request")
            state = "merged" if pr and pr.get("merged_at") else i["state"]
            yield from self._chunks(f"issue:{n}", f"issue:{n}", f"{self.repo}#{n} {i['title']}", i["body"], i["html_url"],
                                    {"state": state, "labels": [l["name"] for l in i["labels"]], "number": n, "kind": "pr" if pr else "issue",
                                     "updated": i["updated_at"]})

        def comment(c):
            if self._noise(c):
                return
            n = int(c["issue_url"].rsplit("/", 1)[1])
            t = titles.get(n) or self._title(store, n)
            yield from self._chunks(f"comment:{c['id']}", f"issue:{n}", f"{self.repo}#{n} {t}  (comment by {c['user']['login']})",
                                    c["body"], c["html_url"], {"kind": "comment", "number": n, "updated": c["updated_at"]})

        def review(c):
            if self._noise(c):
                return
            n = int(c["pull_request_url"].rsplit("/", 1)[1])
            t = titles.get(n) or self._title(store, n)
            hunk = "\n".join((c.get("diff_hunk") or "").splitlines()[-6:])
            yield from self._chunks(f"review:{c['id']}", f"issue:{n}", f"{self.repo}#{n} {t}  (review comment on {c['path']} by {c['user']['login']})",
                                    f"{hunk}\n\n{c['body']}" if hunk else c["body"], c["html_url"], {"kind": "review", "number": n, "updated": c["updated_at"]})

        out = [("issues", self._stream(store, "since_issues", f"repos/{self.repo}/issues", since, limit, issue, log, state="all")),
               ("comments", self._stream(store, "since_comments", f"repos/{self.repo}/issues/comments", since, limit, comment, log))]
        if self.reviews:
            out.append(("review comments", self._stream(store, "since_reviews", f"repos/{self.repo}/pulls/comments", since, limit, review, log)))
        store.commit()
        for what, (n, t) in out:
            log(f"{self.name} {what}: {n} touched -> +{t[0]} ~{t[1]} -{t[2]} ={t[3]} chunks")

    def reconcile(self, store, log=print):
        live = {i["number"] for p in pages(f"repos/{self.repo}/issues", log, state="all") for i in p}
        gone = [r[0] for r in store.db.execute("SELECT DISTINCT doc FROM chunks WHERE source=?", (self.name,)) if int(r[0].split(":")[1]) not in live]
        n = sum(store.delete_doc(self.name, d) for d in gone)
        store.commit()
        log(f"{self.name} reconcile: {len(live)} live, {len(gone)} issues removed ({n} chunks)")


SOURCES = {"bug": lambda: GhIssues("scala/bug", "bug"),
           "scalapr": lambda: GhIssues("scala/scala", "scalapr", reviews=True, default_since="2020-01-01T00:00:00Z")}
