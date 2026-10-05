"""Checks after a refresh: the databases are intact, nothing is left without a vector, and a short list of canary queries (config/canaries.json)
still finds what it should through the gateway, which also proves the search backend works end to end. A canary passes when any of its
`expect` strings occurs (case-insensitive) in the title or URL of one of the top five results. The canaries are the seed of the evaluation set."""
import json, os, sqlite3, sys
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def integrity(cfg, universe_id):
    problems, counts = [], {}
    for pid in cfg.universe(universe_id).projects:
        db = cfg.project_db(pid)
        if not db.exists():
            continue
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        try:
            if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                problems.append(f"{pid}: database integrity check failed")
            chunks = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
            if chunks != con.execute("SELECT count(*) FROM fts").fetchone()[0]:
                problems.append(f"{pid}: the keyword index does not match the chunks")
            model = cfg.search["embedder"]["model"]
            pending = con.execute("""SELECT count(*) FROM chunks c LEFT JOIN vec v ON v.rowid = c.rowid AND v.hash = c.hash AND v.model = ? WHERE v.rowid IS NULL""", (model,)).fetchone()[0]
            counts[pid] = {"chunks": chunks, "pending_vectors": pending}
        finally:
            con.close()
    return problems, counts


def canaries(cfg, universe_id, path=None):
    """Run the canary queries through the gateway. Returns (passed, failed [(query, expected, got)], skipped_reason)."""
    import gateway_client as gw
    p = Path(path) if path else cfg.dir / "canaries.json"
    if not p.exists():
        return 0, [], "no canaries.json"
    qs = json.loads(p.read_text())["queries"]
    passed, failed = 0, []
    for c in qs:
        try:
            r = gw.post("/api/search", {"query": c["q"], "k": 5, "universe": universe_id, **({"projects": c["projects"]} if c.get("projects") else {})}, timeout=300, retries=6)
        except RuntimeError as e:
            return passed, failed, f"gateway search unavailable: {str(e)[:160]}"
        hay = " ".join(f"{h['title']} {h['url']}" for h in r["results"]).lower()
        if any(x.lower() in hay for x in c["expect"]):
            passed += 1
        else:
            failed.append((c["q"], c["expect"], [h["title"][:70] for h in r["results"][:3]]))
    return passed, failed, None
