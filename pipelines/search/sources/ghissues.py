"""GitHub issues, pull requests and their comments, via `gh api`. One `GhRepo` per repository in a project serves all the project's sources
on that repo: three streams (issues-and-PRs, conversation comments, inline review comments) are walked once and each item is routed to the
source whose `include` asks for it. Every item is its own chunk keyed by id, so a new comment embeds one new chunk and an edit re-embeds that
comment only. State is metadata (a PR is `merged`, `closed` or `open`; comments take their issue's state at query time).

Each stream keeps two cursors in the project database (under the pseudo-source `gh:<repo>`):
  fwd_<s>   the newest `updated_at` ingested: every run first walks ascending from here, so new and edited items arrive immediately;
  back_<s>  the backfill frontier: history is filled newest-first in time windows [frontier - window, frontier) until the horizon
            (`since`), under the per-run item cap, so a huge tracker drains over several runs with the recent past first;
  top_<s>, horizon_<s>, bf_done_<s>, win_<s>  where the backfill started, how far it goes, whether it finished, the window size.
Widening a source's `since` in the config re-opens the backfill. Paging is explicit so it is polite and resumable: every page is committed
and the cursors saved, the rate limit is checked every few pages (sleeping to the reset when little is left), a 403/429 is waited out, and
long lists are re-anchored before GitHub's 10,000-item `page=` cap. Deletions are invisible to `since`; `reconcile()` lists all live numbers."""
import calendar, json, subprocess, time
from store import Chunk
from sources.gitsrc import _split_big, MAX

LOW_WATER = 300               # sleep until the reset when fewer requests than this remain (search.json github.min_remaining)
PAGE_DELAY = 0.2


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


PAGE_LIMIT = 90               # (search.json github.page_limit) GitHub refuses `page=` beyond about 100 pages (10,000 items) on big lists: re-anchor `since` before that


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
        time.sleep(PAGE_DELAY)
        if page % 25 == 0:
            wait_for_quota(log)
        if page >= PAGE_LIMIT and "since" in params and items[-1].get("updated_at"):
            params = {**params, "since": items[-1]["updated_at"]}
            page = 1
        else:
            page += 1


def configure(github):
    """Apply search.json's `github` settings (politeness knobs) to this module."""
    global LOW_WATER, PAGE_DELAY, PAGE_LIMIT
    LOW_WATER, PAGE_DELAY, PAGE_LIMIT = github["min_remaining"], github["page_delay_s"], github["page_limit"]


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _ts(iso):
    return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))


class GhRepo:
    """All the `github` sources of one project that read the same repository. `members` are config Sources; kinds of item are routed to
    the member whose `include` asks for them (config validation guarantees at most one)."""

    STREAMS = (("issues", "issues", {"issues", "prs"}), ("comments", "issues/comments", {"comments"}), ("reviews", "pulls/comments", {"reviews"}))

    def __init__(self, members, max_chars=None):
        self.members, self.repo, self.owner = list(members), members[0].repo, f"gh:{members[0].repo}"
        self.max_chars = max_chars or MAX
        self.horizon = min(m.since for m in members)
        caps = [m.max_items_per_run for m in members]
        self.cap = None if None in caps else max(caps)                 # a group with an uncapped member is uncapped

    @property
    def key(self):
        return f"{self.members[0].project}/{'+'.join(m.id for m in self.members)}"

    def member(self, *needs):
        return next((m for m in self.members if set(needs) <= set(m.include)), None)

    def _chunks(self, member, id_prefix, doc, title, text, url, meta):
        for n, part in enumerate(_split_big((text or "").splitlines(), self.max_chars)):
            body = "\n".join(part).strip()
            if body or n == 0:
                yield Chunk(f"{member.id}:{id_prefix}" + (f"~{n}" if n else ""), doc, title, body, url, meta)

    @staticmethod
    def _noise(c):
        body = (c.get("body") or "").strip()
        return (c.get("user") or {}).get("type") == "Bot" or not body or body.startswith("/")      # bots and "/rebuild"-style commands

    def _title(self, store, n):
        for m in self.members:
            r = store.db.execute("SELECT title FROM chunks WHERE id=?", (f"{m.id}:issue:{n}",)).fetchone()
            if r:
                return r[0].split(" ", 1)[1]
        return ""

    def _router(self, store, stream, titles):
        """item -> (member id, chunks), or None when no source wants it."""
        if stream == "issues":
            def route(i):
                pr = i.get("pull_request")
                m = self.member("prs" if pr else "issues")
                if not m:
                    return None
                n = i["number"]
                titles[n] = i["title"]
                state = "merged" if pr and pr.get("merged_at") else i["state"]
                return m.id, list(self._chunks(m, f"issue:{n}", f"issue:{n}", f"{self.repo}#{n} {i['title']}", i["body"], i["html_url"],
                                               {"state": state, "labels": [l["name"] for l in i["labels"]], "number": n, "kind": "pr" if pr else "issue",
                                                "updated": i["updated_at"], "created": i.get("created_at"), "author": (i.get("user") or {}).get("login")}))
        elif stream == "comments":
            def route(c):
                m = self.member("comments", "prs" if "/pull/" in c["html_url"] else "issues")
                if not m or self._noise(c):
                    return None
                n = int(c["issue_url"].rsplit("/", 1)[1])
                t = titles.get(n) or self._title(store, n)
                return m.id, list(self._chunks(m, f"comment:{c['id']}", f"issue:{n}", f"{self.repo}#{n} {t}  (comment by {c['user']['login']})", c["body"],
                                               c["html_url"], {"kind": "comment", "number": n, "updated": c["updated_at"], "created": c.get("created_at"),
                                                               "author": c["user"]["login"]}))
        else:
            def route(c):
                m = self.member("reviews")
                if not m or self._noise(c):
                    return None
                n = int(c["pull_request_url"].rsplit("/", 1)[1])
                t = titles.get(n) or self._title(store, n)
                hunk = "\n".join((c.get("diff_hunk") or "").splitlines()[-6:])
                return m.id, list(self._chunks(m, f"review:{c['id']}", f"issue:{n}", f"{self.repo}#{n} {t}  (review comment on {c['path']} by {c['user']['login']})",
                                               f"{hunk}\n\n{c['body']}" if hunk else c["body"], c["html_url"], {"kind": "review", "number": n, "updated": c["updated_at"], "created": c.get("created_at"),
                                               "author": c["user"]["login"]}))
        return route

    def _ingest(self, store, route, items, tot, existing_only=False):
        n = 0
        for it in items:
            r = route(it)
            if r:
                for k, v in enumerate(store.apply(r[0], r[1], existing_only=existing_only)):
                    tot[k] += v
            n += 1
        return n

    # ---- one stream: forward walk, then newest-first backfill ----
    def _sync_stream(self, store, stream, path, route, explicit, cap, log, extra):
        g = lambda k: store.get(self.owner, f"{k}_{stream}")
        put = lambda k, v: store.put(self.owner, f"{k}_{stream}", v)
        tot = [0, 0, 0, 0]
        if g("fwd") is None:
            legacy = next((store.get(m.id, f"since_{stream}") for m in self.members if store.get(m.id, f"since_{stream}")), None)
            if legacy:                                              # an ascending single-cursor sync already covered [horizon, legacy]
                for k, v in (("fwd", legacy), ("top", legacy), ("back", self.horizon), ("bf_done", "1")):
                    put(k, v)
            else:                                                   # first run: everything older than now is the backfill's job
                now = _iso(time.time())
                for k, v in (("fwd", now), ("top", now), ("back", now)):
                    put(k, v)
        if g("horizon") and self.horizon < g("horizon"):
            put("bf_done", "")                                      # the config now reaches further back: keep filling
        put("horizon", self.horizon)
        store.commit()
        seen = 0
        # 1. forward: new and edited items since the newest one ingested (or since an explicit --since, a repair re-walk)
        for page in pages(path, log, sort="updated", direction="asc", since=explicit or g("fwd"), **extra):
            seen += self._ingest(store, route, page, tot)
            put("fwd", max(page[-1]["updated_at"], g("fwd") or ""))
            store.commit()
            log(f"  {self.repo} {stream}: forward, {seen} items, up to {page[-1]['updated_at'][:10]}")
        # 2. backfill, newest windows first, until the horizon or the cap
        done_items = 0
        while g("bf_done") != "1":
            hi = g("back")
            win = int(g("win") or 30)
            lo = max(self.horizon, _iso(_ts(hi) - win * 86400))
            n, stop = 0, False
            for page in pages(path, log, sort="updated", direction="asc", since=lo, **extra):
                fresh = [i for i in page if i["updated_at"] < hi]
                n += self._ingest(store, route, fresh, tot)
                store.commit()
                if len(fresh) < len(page):                          # reached the part the frontier already covers
                    stop = True
                    break
            put("back", lo)
            if lo <= self.horizon:
                put("bf_done", "1")
            put("win", str(min(730, win * 2) if n < 300 else max(1, win // 2) if n > 1500 else win))
            store.commit()
            done_items += n
            log(f"  {self.repo} {stream}: backfill window {lo[:10]} .. {hi[:10]}: {n} items" + (" (history complete)" if g("bf_done") == "1" else ""))
            if cap and done_items >= cap and g("bf_done") != "1":
                log(f"  {self.repo} {stream}: per-run cap of {cap} items reached, continuing next run from {lo[:10]}")
                break
        return seen + done_items, tot

    def _meta_walk(self, store, stream, path, route, since, log, extra):
        """Re-read the items from the part of history that is indexed (the backfill frontier up to now) and refresh the metadata of chunks that
        already exist, ingesting nothing new and touching no cursor. This is how new metadata fields reach data indexed before they existed."""
        tot, seen = [0, 0, 0, 0], 0
        start = since or store.get(self.owner, f"back_{stream}") or self.horizon
        for page in pages(path, log, sort="updated", direction="asc", since=start, **extra):
            seen += self._ingest(store, route, page, tot, existing_only=True)
            store.commit()
            log(f"  {self.repo} {stream}: metadata, {seen} items up to {page[-1]['updated_at'][:10]}")
        return seen, tot

    def sync(self, store, since=None, limit=None, log=print, meta_only=False):
        cap = limit or self.cap
        titles, out = {}, []
        for stream, path, needs in self.STREAMS:
            if not any(needs & set(m.include) for m in self.members):
                continue
            extra = {"state": "all"} if stream == "issues" else {}
            if meta_only:
                out.append((stream, self._meta_walk(store, stream, f"repos/{self.repo}/{path}", self._router(store, stream, titles), since, log, extra)))
                continue
            out.append((stream, self._sync_stream(store, stream, f"repos/{self.repo}/{path}", self._router(store, stream, titles), since, cap, log, extra)))
        store.commit()
        for what, (n, t) in out:
            log(f"{self.key} {what}: {n} touched -> +{t[0]} ~{t[1]} -{t[2]} ={t[3]} chunks")

    def reconcile(self, store, log=print):
        live = {i["number"] for p in pages(f"repos/{self.repo}/issues", log, state="all") for i in p}
        n = gone = 0
        for m in self.members:
            docs = [r[0] for r in store.db.execute("SELECT DISTINCT doc FROM chunks WHERE source=?", (m.id,)) if int(r[0].split(":")[1]) not in live]
            gone += len(docs)
            n += sum(store.delete_doc(m.id, d) for d in docs)
        store.commit()
        log(f"{self.key} reconcile: {len(live)} live, {gone} issues removed ({n} chunks)")


def groups(sources):
    """Group `github` sources by (project, repo): one GhRepo (one set of streams) per group."""
    by = {}
    for s in sources:
        by.setdefault((s.project, s.repo), []).append(s)
    return list(by.values())
