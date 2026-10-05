"""What the indexer is doing right now, as one small JSON file (<data dir>/run.json) that the web page reads: no pgrep, no log scraping.
sync.py and embed.py (later refresh.py, with more phases) write it; the gateway only reads it. A run whose process has died without
finishing is reported as stale (crashed or killed), not as running."""
import contextlib, fcntl, json, os, time
from pathlib import Path


def path(cfg):
    return cfg.data_path("run.json")


class Run:
    def __init__(self, cfg, kind, plan=()):
        self.path, self.last_write = path(cfg), 0.0
        self.s = {"pid": os.getpid(), "kind": kind, "started": time.time(), "updated": time.time(), "phase": "starting", "source": None,
                  "plan": list(plan), "done": [], "phases": [], "phases_done": [], "message": "", "finished": None, "ok": None, "errors": []}
        self._write(force=True)

    def _write(self, force=False):
        now = time.time()
        if not force and now - self.last_write < 0.5:
            return
        self.last_write, self.s["updated"] = now, now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.s))
            tmp.replace(self.path)                     # atomic: a reader never sees half a file
        except OSError:
            pass                                       # status is best effort; never let it break indexing

    def phases(self, names):
        """The phases a refresh will go through, so the page can show where it is."""
        self.s["phases"] = list(names)
        self._write(force=True)

    def phase(self, name, message=""):
        if self.s["phase"] in self.s["phases"] and self.s["phase"] != name and self.s["phase"] not in self.s["phases_done"]:
            self.s["phases_done"].append(self.s["phase"])
        self.s.update(phase=name, source=None, message=message)
        self._write(force=True)

    def source(self, key, phase=None):
        if self.s["source"] and self.s["source"] != key:
            self.s["done"].append(self.s["source"])
        self.s.update(source=key, message="", **({"phase": phase} if phase else {}))
        self._write(force=True)

    def log(self, msg):
        """A `log` callback for the sources: prints, and keeps the latest line for the page."""
        print(msg, flush=True)
        self.s["message"] = str(msg).strip()[:300]
        self._write()

    def error(self, msg):
        self.s["errors"].append(str(msg)[:300])
        self._write(force=True)

    def finish(self, ok=True):
        if self.s["source"]:
            self.s["done"].append(self.s["source"])
        if self.s["phase"] in self.s["phases"] and self.s["phase"] not in self.s["phases_done"]:
            self.s["phases_done"].append(self.s["phase"])
        self.s.update(source=None, phase="finished", finished=time.time(), ok=ok and not self.s["errors"])
        self._write(force=True)


def read(cfg):
    """The run state, with `running` decided by whether its process is still alive. None if nothing ever ran."""
    try:
        s = json.loads(path(cfg).read_text())
    except (OSError, ValueError):
        return None
    alive = False
    if s.get("finished") is None:
        try:
            os.kill(s["pid"], 0)
            alive = True
        except (OSError, KeyError):
            pass
    s["running"], s["stale"] = alive, s.get("finished") is None and not alive
    return s


class Busy(RuntimeError):
    pass


@contextlib.contextmanager
def lock(cfg):
    """Only one indexer run at a time (sync, embed or refresh): they share the databases and the GitHub quota. Raises Busy, saying who holds it."""
    p = cfg.data_path("indexer.lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        s = read(cfg) or {}
        f.close()
        raise Busy(f"another indexer run is active ({s.get('kind', '?')}, pid {s.get('pid', '?')}); wait for it or stop it first") from None
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
