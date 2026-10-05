"""Commit messages: the "why" behind the code. One chunk per commit of a ref in the managed bare clone: the full message (subject, body, trailers)
plus the paths it changed, so a query can match what was said or where. Merge commits are left out (their message is just a PR title that the PR
source already has) unless the source asks for them, and so are bots (`skip_authors`: dependency-bump commits).

History is walked newest-first with two cursors kept in the project database, like the GitHub sources:
  head       the commit the ref pointed at last time: each run first ingests `head..ref`, so new commits arrive at once (a rewritten branch, where
             `head` is no longer an ancestor, is handled by re-walking: chunks are keyed by sha, so nothing is stored twice);
  back       the oldest commit ingested so far: the backfill continues from its parent, newest-first, until the `since` horizon or the root, under the
             per-run cap (`max_items_per_run`). `top_date`, `back_date`, `bf_done` and `horizon` feed the progress bar.
Widening `since` re-opens the backfill. Commits that have since disappeared from the branch (a force-push) stay indexed; they are still real history."""
import re, subprocess, time
from store import Chunk
from sources.gitsrc import _split_big, MAX

_REF = re.compile(r"(?<!\w)(?:[\w.-]+/[\w.-]+)?#(\d{1,6})\b")           # #123, (#123), scala/bug#123, Fixes sbt/zinc#123
_NOREPLY = re.compile(r"^(?:\d+\+)?([A-Za-z0-9][A-Za-z0-9-]*)@users\.noreply\.github\.com$", re.I)
MAX_FILES = 20
BATCH = 500


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts)))


def _refs(text):
    return sorted({int(n) for n in _REF.findall(text or "")})[:50]


def handle_from_email(email):
    """The GitHub handle behind a `12345+login@users.noreply.github.com` (or `login@users.noreply.github.com`) address; None for any other email."""
    m = _NOREPLY.match(email or "")
    return m.group(1) if m else None


def _git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check, errors="replace")


class GitLog:
    def __init__(self, src, repo_dir, max_chars=None):
        self.src, self.name, self.repo, self.max_chars = src, src.id, repo_dir, max_chars or MAX
        self.skip = {a.lower() for a in src.skip_authors}

    def _log(self, rev, *extra):
        """Commits reachable from `rev` (newest first) as [(sha, author, email, epoch, message, [paths])]."""
        args = ["log", "--name-only", "--format=%x1e%H%x1f%an%x1f%ae%x1f%at%x1f%B%x1f", f"--since={self.src.since}"] + ([] if self.src.merges else ["--no-merges"]) + list(extra) + [rev]
        if self.src.paths:
            args += ["--", *self.src.paths]
        out = _git(self.repo, *args).stdout
        res = []
        for rec in out.split("\x1e"):
            if not rec.strip():
                continue
            sha, author, email, at, rest = rec.split("\x1f", 4)
            msg, _, files = rest.rpartition("\x1f")
            res.append((sha.strip(), author, email, int(at), msg.strip(), [f for f in files.split("\n") if f.strip()]))
        return res

    def _chunks(self, sha, author, email, at, msg, files):
        if author.lower() in self.skip or not msg:
            return []
        subject = msg.splitlines()[0][:120]
        fl = ", ".join(files[:MAX_FILES]) + (f" and {len(files) - MAX_FILES} more" if len(files) > MAX_FILES else "")
        title = f"{self.src.repo} commit {sha[:8]} {subject}"
        meta = {"kind": "commit", "sha": sha, "author_name": author, "author": handle_from_email(email), "updated": _iso(at), "created": _iso(at),
                "files": len(files), "refs": _refs(msg)}
        url, doc = f"https://github.com/{self.src.repo}/commit/{sha}", f"commit:{sha}"
        text = f"{msg}\n\nFiles changed: {fl}" if fl else msg
        return [Chunk(f"{self.name}:commit:{sha}" + (f"~{n}" if n else ""), doc, title, "\n".join(part).strip(), url, meta)
                for n, part in enumerate(_split_big(text.splitlines(), self.max_chars))]

    def _ingest(self, store, commits, tot, existing_only=False):
        for i, c in enumerate(commits, 1):
            for k, v in enumerate(store.apply(self.name, self._chunks(*c), existing_only=existing_only)):
                tot[k] += v
            if i % BATCH == 0:
                store.commit()
        store.commit()

    def sync(self, store, limit=None, since=None, log=print, meta_only=False):
        g, put = (lambda k: store.get(self.name, k)), (lambda k, v: store.put(self.name, k, v))
        cap = limit or self.src.max_items_per_run
        ref = _git(self.repo, "rev-parse", self.src.ref).stdout.strip()
        if meta_only:                                                  # refresh the metadata of commits already indexed; ingest nothing, move no cursor
            tot = [0, 0, 0, 0]
            self._ingest(store, self._log(ref), tot, existing_only=True)
            store.commit()
            log(f"{self.src.key} @ {ref[:8]}: metadata refreshed -> ~{tot[1]} ={tot[3]} chunks")
            return
        tot, seen = [0, 0, 0, 0], 0
        if g("horizon") and self.src.since < g("horizon"):
            put("bf_done", "")                                         # the config reaches further back now: keep filling
        put("horizon", self.src.since)
        head = g("head")
        restart = not head                                             # first run, or the branch was rewritten: walk down from the ref
        # 1. forward: everything new since the last run (uncapped: it is the recent past)
        if head and head != ref:
            if _git(self.repo, "merge-base", "--is-ancestor", head, ref, check=False).returncode == 0:
                new = self._log(f"{head}..{ref}")
                self._ingest(store, new, tot)
                seen += len(new)
                log(f"  {self.src.key}: forward, {len(new)} new commits")
            else:
                log(f"  {self.src.key}: {head[:8]} is no longer an ancestor of {self.src.ref} (rewritten history): walking down from {ref[:8]} again")
                restart = True
        put("head", ref)
        # 2. backfill, newest-first, from the ref (restart) or from just below the frontier
        back = g("back")
        if restart:
            rev = ref
            put("bf_done", "")
        elif g("bf_done") != "1" and back:
            rev = f"{back}^"
            if _git(self.repo, "rev-parse", "--verify", "-q", rev, check=False).returncode != 0:
                put("bf_done", "1")                                    # the frontier commit is the root: nothing older
                rev = None
        else:
            rev = None
        if rev:
            batch = self._log(rev, *(["-n", str(cap)] if cap else []))
            self._ingest(store, batch, tot)
            seen += len(batch)
            if restart and batch and not g("top_date"):
                put("top_date", _iso(batch[0][3]))
            if batch:
                put("back", batch[-1][0]); put("back_date", _iso(batch[-1][3]))
            if not batch or not cap or len(batch) < cap:
                put("bf_done", "1")
            log(f"  {self.src.key}: backfill, {len(batch)} commits back to {_iso(batch[-1][3])[:10] if batch else '(none)'}" + (" (history complete)" if g("bf_done") == "1" else ""))
        store.commit()
        log(f"{self.src.key} @ {ref[:8]}: {seen} commits touched -> +{tot[0]} ~{tot[1]} -{tot[2]} ={tot[3]} chunks")
