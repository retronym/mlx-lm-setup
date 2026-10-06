"""Discourse forums (contributors.scala-lang.org): one document per topic, one chunk per post (the opening post is a `topic`, replies are `post`s), over
the forum's public JSON API, anonymously and politely.

What the pilot crawl showed and what follows from it (DISCOURSE.md has the numbers):
- The listing `/latest.json?order=activity` is every public topic, most recently *bumped* first (a new reply bumps; an edit does not), 30 a page.
- `/t/{id}.json?include_raw=true` is the topic with its first 20 posts (`raw` is the markdown the author wrote, `cooked` the HTML Discourse made of it)
  and `stream`, the ids of all its posts; `/t/{id}/posts.json?post_ids[]=..` returns up to 100 more. So a topic costs 1 + ceil((posts - 20) / 100) requests.
  `?print=true` (every post in one request) is limited to 5 an hour per address: not used.
- Text comes from `raw` (code blocks and quotes as written); links from `cooked`, where Discourse has already made relative and auto-linked URLs absolute.
  A quote (`[quote="user, post:3, topic:9"]..[/quote]`, a third of all posts) repeats another post, so it is cut to its first words: the chunk says who
  it answers without carrying their text twice.
- Small-action posts (`post_type` 3: "closed this topic", "split this topic") and whispers are not text anyone wrote; they are skipped.

Two cursors, like the GitHub sources (kept in the project database under the source id):
  fwd      the newest `bumped_at` ingested: every run first walks the listing down to it, so new replies arrive at once (uncapped).
  back     the oldest `bumped_at` ingested by the backfill: it continues below that, newest-first, under `max_items_per_run` topics per run, until the
           `since` horizon or the end of the listing (`bf_done`). `top_date` / `back_date` feed the progress bar.
Everything bumped between `back` and `fwd` is indexed, because a topic's `bumped_at` only grows: one that is bumped while the walk runs is above `fwd` next time.
A topic seen again re-reads its first 20 posts (edits there are picked up) and fetches only the posts it does not have; a changed title re-reads all of it.
Edits further down a long topic and deletions of whole topics are only caught by `reconcile` (the whole listing; topics no longer listed are dropped).

Politeness (`search.json` `discourse`): one request per `delay_s` (1 s: Discourse's default limits are 200 a minute and 50 per 10 s per address), a
User-Agent that says who is asking, and a 429 or 503 waits out `Retry-After` and slows the rest of the run by half again."""
import json, re, time, urllib.error, urllib.parse, urllib.request
from store import Chunk
from sources.gitsrc import _split_big

POLITE = {"delay_s": 1.0, "user_agent": "scala-search-indexer (local research index; https://github.com/retronym)", "max_retries": 6, "timeout_s": 60}
QUOTE_CHARS = 160             # what is kept of a quoted post
MAX_URLS = 100                # links kept per post
SKIP_TYPES = {2, 3, 4}        # moderator actions, small actions ("closed this topic"), whispers


def configure(polite):
    POLITE.update(polite or {})


class Gone(Exception):
    """The topic is no longer public (deleted, moved to a private category)."""


class Client:
    """GET JSON from one Discourse site, at most one request per `delay_s`, waiting out rate limits."""

    def __init__(self, site, log=print, opener=None):
        self.base, self.log, self.delay = f"https://{site}", log, float(POLITE["delay_s"])
        self.opener = opener or urllib.request.urlopen
        self.last, self.requests, self.waited = 0.0, 0, 0.0

    def _pause(self, s):
        self.waited += s
        time.sleep(s)

    def get(self, path):
        for attempt in range(POLITE["max_retries"]):
            wait = self.delay - (time.monotonic() - self.last)
            if wait > 0:
                self._pause(wait)
            self.last = time.monotonic()
            self.requests += 1
            req = urllib.request.Request(self.base + path, headers={"User-Agent": POLITE["user_agent"], "Accept": "application/json"})
            try:
                with self.opener(req, timeout=POLITE["timeout_s"]) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code in (403, 404, 410):
                    raise Gone(f"{e.code} {path}") from None
                if e.code not in (429, 500, 502, 503, 504):
                    raise
                ra = e.headers.get("Retry-After") if e.headers else None
                s = float(ra) if ra and ra.isdigit() else min(300.0, 10.0 * 2 ** attempt)
                if e.code in (429, 503):
                    self.delay = min(10.0, self.delay * 1.5)
                self.log(f"  {self.base}: {e.code} on {path}; waiting {s:.0f} s, then one request per {self.delay:.1f} s")
                self._pause(s)
            except (urllib.error.URLError, TimeoutError) as e:
                s = min(120.0, 5.0 * 2 ** attempt)
                self.log(f"  {self.base}: {type(e).__name__} on {path}: {e}; retrying in {s:.0f} s")
                self._pause(s)
        raise RuntimeError(f"{self.base}{path}: gave up after {POLITE['max_retries']} attempts")


# ---- text ---------------------------------------------------------------------------------------------------------------------------
_QUOTE = re.compile(r"\[quote(?:=\"?([^\]\"]*)\"?)?\]((?:(?!\[quote[=\]]).)*?)\[/quote\]\s*", re.S | re.I)
_IMAGE = re.compile(r"!\[([^\]|]*)(?:\|[^\]]*)?\]\([^)]*\)")
_DETAILS = re.compile(r"\[details=\"?([^\]\"]*)\"?\]|\[/details\]", re.I)
_HREF = re.compile(r"<a\b[^>]*?\bhref=\"([^\"]+)\"", re.I)


def _quote(m):
    who = (m.group(1) or "").split(",")
    user = who[0].strip()
    post = next((p.split(":", 1)[1].strip() for p in who[1:] if p.strip().startswith("post:")), None)
    body = " ".join(m.group(2).split())
    cut = body[:QUOTE_CHARS].rsplit(" ", 1)[0] + " …" if len(body) > QUOTE_CHARS else body
    head = f"@{user}" + (f" (post {post})" if post else "") + ": " if user else ""
    return f"> {head}{cut}\n\n"


def clean(raw):
    """The markdown a post's author wrote, ready to index: quotes cut short (innermost first), images reduced to their alt text, details blocks opened."""
    t = raw or ""
    for _ in range(5):
        t2 = _QUOTE.sub(_quote, t)
        if t2 == t:
            break
        t = t2
    t = _IMAGE.sub(lambda m: f"[image: {m.group(1)}]" if m.group(1).strip() and m.group(1).strip() != "image" else "", t)
    t = _DETAILS.sub(lambda m: f"{m.group(1)}:" if m.group(1) else "", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def urls(cooked, site):
    """The links in a post's HTML, absolute, in order, each once: not @mentions, user cards, hashtags, avatars, uploads or the post's own anchors."""
    out = []
    for u in _HREF.findall(cooked or ""):
        u = u.replace("&amp;", "&")
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/"):
            if re.match(r"^/(u|c|tag|tags|g|uploads|user_avatar|badges)/", u):
                continue
            u = f"https://{site}{u}"
        if not u.startswith(("http://", "https://")) or "/uploads/" in u or "/user_avatar/" in u or u in out:
            continue
        out.append(u)
    return out[:MAX_URLS]


# ---- chunks -------------------------------------------------------------------------------------------------------------------------
def post_chunks(src, topic, cats, p, max_chars):
    """The chunks of one post of `topic` (the /t/{id}.json dict), or [] for posts that are not text someone wrote."""
    if p.get("post_type") in SKIP_TYPES or p.get("action_code") or p.get("hidden") or p.get("deleted_at") or p.get("user_deleted"):
        return []
    body = clean(p.get("raw") or "")
    if not body:
        return []
    tid, n = topic["id"], p["post_number"]
    first = n == 1
    cat = cats.get(topic.get("category_id"))
    title = topic["title"] if first else f"{topic['title']}  (reply #{n} by {p['username']})"
    url = f"https://{src.repo}/t/{topic['slug']}/{tid}" + ("" if first else f"/{n}")
    meta = {"kind": "topic" if first else "post", "topic": tid, "post": p["id"], "post_number": n, "author": p["username"], "author_name": p.get("name") or None,
            "created": p["created_at"], "updated": p.get("updated_at") or p["created_at"], "labels": [cat] if cat else [],
            "reply_to": p.get("reply_to_post_number"), "likes": next((a.get("count", 0) for a in p.get("actions_summary") or [] if a.get("id") == 2), 0),
            "urls": urls(p.get("cooked"), src.repo)}
    if first:
        meta.update(posts=topic.get("posts_count"), views=topic.get("views"), closed=bool(topic.get("closed")), tags=[t if isinstance(t, str) else t.get("name") for t in topic.get("tags") or []])
    out = []
    for k, part in enumerate(_split_big(body.splitlines(), max_chars)):
        text = "\n".join(part).strip()
        if text:
            out.append(Chunk(f"{src.id}:post:{p['id']}" + (f"~{k}" if k else ""), f"topic:{tid}", title, text, url, meta))
    return out


class Discourse:
    def __init__(self, src, max_chars=2400, client=None):
        self.src, self.name, self.max_chars = src, src.id, max_chars
        self.client = client
        self.cats = None

    def _client(self, log):
        if self.client is None:
            self.client = Client(self.src.repo, log)
        return self.client

    def _categories(self):
        if self.cats is None:
            d = self.client.get("/categories.json?include_subcategories=true")
            self.cats = {}
            for c in d["category_list"]["categories"]:
                self.cats[c["id"]] = c["slug"]
                for s in c.get("subcategory_list") or []:
                    self.cats[s["id"]] = f"{c['slug']}/{s['slug']}"
        return self.cats

    def listing(self):
        """Every public topic, most recently bumped first, one page (30) at a time: [topic dicts] per page."""
        page = 0
        while True:
            d = self.client.get(f"/latest.json?order=activity&no_definitions=true&page={page}")
            tl = d["topic_list"]["topics"]
            if not tl:
                return
            yield tl
            if not d["topic_list"].get("more_topics_url"):
                return
            page += 1

    def fetch(self, store, tid):
        """(topic dict with every post we need, the post ids it has now). Posts already stored are not fetched again unless the title changed."""
        t = self.client.get(f"/t/{tid}.json?include_raw=true")
        stream = t["post_stream"]["stream"]
        have = {p["id"] for p in t["post_stream"]["posts"]}
        row = store.db.execute("SELECT title FROM chunks WHERE source = ? AND doc = ? AND json_extract(meta, '$.kind') = 'topic'", (self.name, f"topic:{tid}")).fetchone()
        renamed = row is None or row[0] != t["title"]
        stored = set() if renamed else {r[0] for r in store.db.execute("SELECT DISTINCT json_extract(meta, '$.post') FROM chunks WHERE source = ? AND doc = ?", (self.name, f"topic:{tid}"))}
        want = [i for i in stream if i not in have and i not in stored]
        for k in range(0, len(want), 100):
            q = "&".join(f"post_ids[]={i}" for i in want[k:k + 100])
            t["post_stream"]["posts"] += self.client.get(f"/t/{tid}/posts.json?include_raw=true&{q}")["post_stream"]["posts"]
        return t, set(stream)

    def ingest(self, store, tid, tot):
        try:
            t, alive = self.fetch(store, tid)
        except Gone:
            tot[2] += store.delete_doc(self.name, f"topic:{tid}")
            return 0
        cats = self._categories()
        chunks = [c for p in sorted(t["post_stream"]["posts"], key=lambda p: p["post_number"]) for c in post_chunks(self.src, t, cats, p, self.max_chars)]
        for i, v in enumerate(store.apply(self.name, chunks)):
            tot[i] += v
        keep, read = {c.id for c in chunks}, {p["id"] for p in t["post_stream"]["posts"]}
        for cid, post in store.db.execute("SELECT id, json_extract(meta, '$.post') FROM chunks WHERE source = ? AND doc = ?", (self.name, f"topic:{tid}")).fetchall():
            if post not in alive or (post in read and cid not in keep):        # deleted upstream, or re-read and now skipped or shorter
                tot[2] += store.delete_chunk(cid)
        return len(t["post_stream"]["posts"])

    def sync(self, store, limit=None, since=None, log=print, meta_only=False):
        self._client(log)
        g, put = (lambda k: store.get(self.name, k)), (lambda k, v: store.put(self.name, k, v))
        cap = limit or self.src.max_items_per_run
        horizon = since or self.src.since or ""
        if g("horizon") and horizon < g("horizon"):
            put("bf_done", "")                                          # the config reaches further back now: keep filling
        put("horizon", horizon)
        fwd, back, bf_done = g("fwd"), g("back"), g("bf_done") == "1"
        req0, t0 = self.client.requests, time.time()
        todo = None
        if not bf_done:                                                  # how many topics the backfill has left, for the ETA (one request)
            total = self.client.get("/about.json")["about"]["stats"]["topics_count"]
            have = store.db.execute("SELECT count(DISTINCT doc) FROM chunks WHERE source = ?", (self.name,)).fetchone()[0]
            todo = max(0, total - have)
        tot, new_fwd, fetched, backfilled, posts, oldest = [0, 0, 0, 0], None, 0, 0, 0, None
        stop = False
        for page in self.listing():
            for t in page:
                b = t["bumped_at"]
                if t.get("pinned") or t.get("pinned_globally"):        # pinned topics head the list whatever their date: never a reason to stop
                    if not fwd or b > fwd:
                        posts += self.ingest(store, t["id"], tot); fetched += 1
                    continue
                new_fwd = new_fwd or b
                if fwd and b > fwd:                                      # forward: bumped since the last run
                    posts += self.ingest(store, t["id"], tot); fetched += 1
                elif fwd and back and b >= back:                         # the window already indexed
                    if bf_done:
                        stop = True
                        break
                elif bf_done or (horizon and b < horizon):               # below the horizon: the backfill is complete
                    put("bf_done", "1"); bf_done = stop = True
                    break
                elif cap and backfilled >= cap:                          # the per-run cap: the rest comes next run
                    stop = True
                    break
                else:                                                    # backfill, newest-first
                    posts += self.ingest(store, t["id"], tot); fetched += 1; backfilled += 1
                    oldest = b
                    put("back", b); put("back_date", b[:19] + "Z")
                    if not g("top_date"):
                        put("top_date", b[:19] + "Z")
                if fetched and fetched % 10 == 0:
                    store.commit()
                    el = time.time() - t0
                    rate = fetched / el if el else 0
                    left = max(0, (min(todo, cap) if cap else todo) - backfilled) if todo is not None else 0
                    log(f"  {self.src.key}: {fetched} topics ({posts} posts, {self.client.requests - req0} requests) in {el:.0f} s, {rate * 60:.0f} topics/min"
                        + (f", backfill at {oldest[:10]}, about {left} to go this run, ETA {left / rate / 60:.0f} min" if oldest and rate else "") + f"; +{tot[0]} ~{tot[1]} -{tot[2]} chunks")
            if stop:
                break
        else:
            put("bf_done", "1")                                          # walked off the end of the listing: everything is in
        if new_fwd and (not fwd or new_fwd > fwd):
            put("fwd", new_fwd)
        put("last_listing", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        store.commit()
        log(f"{self.src.key}: {fetched} topics fetched ({backfilled} backfilled{', history complete' if g('bf_done') == '1' else ''}), {posts} posts read, "
            f"{self.client.requests - req0} requests, {self.client.waited:.0f} s waiting -> +{tot[0]} ~{tot[1]} -{tot[2]} ={tot[3]} chunks")

    def reconcile(self, store, log=print):
        """Walk the whole listing (about 60 requests) and drop the topics that are no longer public."""
        self._client(log)
        listed, n = {}, 0
        for page in self.listing():
            for t in page:
                listed[t["id"]] = t
        tot = [0, 0, 0, 0]
        for (doc,) in store.db.execute("SELECT DISTINCT doc FROM chunks WHERE source = ?", (self.name,)).fetchall():
            tid = int(doc.split(":", 1)[1])
            if tid not in listed:
                tot[2] += store.delete_doc(self.name, doc)
                n += 1
        store.commit()
        log(f"{self.src.key}: reconcile: {len(listed)} topics listed, {n} no longer public -> -{tot[2]} chunks")
