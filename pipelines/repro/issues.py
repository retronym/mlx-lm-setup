"""scala/bug threads from the search index, and the pilot's two samples:

  calibration  closed issues that GitHub says a merged scala/scala PR closed, where the PR shipped in a 2.12.x or 2.13.x patch release: the release
               before it should show the bug and the release itself should not. This is the labelled set the judge is measured on.
  open         open issues with a code fence, spread over the years they were reported in.

Reads the index and the links database read-only; starts no model."""
import json, os, random, re, sqlite3, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEARCH = HERE.parent / "search"
DATA = Path(os.environ.get("SEARCH_DATA_DIR", SEARCH / "data"))
INDEX = DATA / "projects" / "scala2" / "index.db"
LINKS = DATA / "links" / "scala-zinc.db"
PATCH = re.compile(r"^v(2\.1[23])\.(\d+)$")


def _ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


def thread(n, max_chars=14000):
    """{number, title, state, created, labels, text}: the issue body then its comments, oldest first, each under a `### @author (date)` header."""
    con = _ro(INDEX)
    rows = con.execute("""SELECT id, title, text, meta FROM chunks WHERE source = 'issues' AND doc = ?
                          ORDER BY COALESCE(json_extract(meta, '$.created'), ''), id""", (f"issue:{n}",)).fetchall()
    con.close()
    if not rows:
        return None
    head = next((json.loads(m) for i, _, _, m in rows if i == f"issues:issue:{n}"), {})
    title = next((t for i, t, _, _ in rows if i == f"issues:issue:{n}"), rows[0][1])
    parts, last = [], None
    for cid, _, text, meta in rows:
        m = json.loads(meta)
        key = cid.split("~")[0]
        if key != last:                                               # a split chunk (`~1`) continues its predecessor under the same header
            parts.append(f"### @{m.get('author') or '?'} ({(m.get('created') or '')[:10]}){' [issue]' if m.get('kind') == 'issue' else ''}")
            last = key
        parts.append(text)
    text = _fold_traces("\n\n".join(parts))
    if len(text) > max_chars:                                         # keep the report and the newest comments, where a minimised repro usually is
        text = text[:max_chars // 2] + "\n\n[... middle of the thread left out ...]\n\n" + text[-max_chars // 2:]
    return {"number": n, "title": re.sub(r"^scala/bug#\d+\s*", "", title), "state": head.get("state"), "created": head.get("created"),
            "labels": head.get("labels") or [], "text": text}


FRAME = re.compile(r"^\s*(at [\w$.<>/-]+\(|\.\.\. \d+ more)")


def _fold_traces(text, keep=6):
    """Long stack traces crowd the reproducer out of a bounded prompt: keep the first `keep` frames of each, say how many were left out."""
    out, run = [], 0
    for line in text.splitlines():
        if FRAME.match(line):
            run += 1
            if run == keep + 1:
                out.append("\t... (more frames left out)")
            if run > keep:
                continue
        else:
            run = 0
        out.append(line)
    return "\n".join(out)


def _releases(con):
    """{"2.13": [0, 1, ...]} patch numbers that exist as releases or tags."""
    out = {}
    for (nid,) in con.execute("SELECT id FROM nodes WHERE id LIKE 'release:scala/scala@v2.1%'"):
        m = PATCH.match(nid.split("@", 1)[1])
        if m:
            out.setdefault(m.group(1), set()).add(int(m.group(2)))
    return out


def calibration(k=30, seed=1):
    """Closed issues closed by a merged PR that shipped in a patch release with a release before it in the same series: [{number, before, after, pr}]."""
    con = _ro(LINKS)
    rel = _releases(con)
    rows = con.execute("""SELECT c.dst, c.src, s.dst FROM edges c JOIN edges s ON s.src = c.src AND s.type = 'shipped_in'
                          JOIN nodes p ON p.id = c.src AND p.state = 'merged'
                          WHERE c.type = 'closes' AND c.conf >= 0.9 AND c.src LIKE 'scala/scala#%' AND c.dst LIKE 'scala/bug#%'""").fetchall()
    con.close()
    first = {}                                                        # issue -> (series, patch, pr): the earliest release a closing PR shipped in
    for issue, pr, release in rows:
        m = PATCH.match(release.split("@", 1)[1])
        if not m:
            continue
        series, patch = m.group(1), int(m.group(2))
        if issue not in first or (series, patch) < first[issue][:2]:
            first[issue] = (series, patch, pr)
    idx = _ro(INDEX)
    pool = []
    for issue, (series, patch, pr) in sorted(first.items()):
        before = max((p for p in rel.get(series, ()) if p < patch), default=None)
        if before is None or (series == "2.12" and before < 1):       # 2.12.0 and earlier are not worth a JVM 8 matrix in a pilot
            continue
        n = int(issue.split("#")[1])
        body = idx.execute("SELECT text, json_extract(meta, '$.state') FROM chunks WHERE id = ?", (f"issues:issue:{n}",)).fetchone()
        if body and body[1] == "closed" and "```" in body[0]:
            pool.append({"number": n, "before": f"{series}.{before}", "after": f"{series}.{patch}", "pr": pr})
    idx.close()
    random.Random(seed).shuffle(pool)
    by = {"2.12": [x for x in pool if x["after"].startswith("2.12")], "2.13": [x for x in pool if x["after"].startswith("2.13")]}
    half = k // 2
    pick = by["2.12"][:half] + by["2.13"][:k - min(half, len(by["2.12"]))]
    return sorted(pick[:k], key=lambda x: x["number"]), len(pool)


def open_sample(k=50, seed=1):
    """Open issues whose report has a code fence, about evenly over the years they were opened in: [{number, year}]."""
    con = _ro(INDEX)
    rows = con.execute("""SELECT json_extract(meta, '$.number'), substr(json_extract(meta, '$.created'), 1, 4) FROM chunks
                          WHERE source = 'issues' AND json_extract(meta, '$.kind') = 'issue' AND json_extract(meta, '$.state') = 'open'
                          AND id NOT LIKE '%~%' AND text LIKE '%```%'""").fetchall()
    con.close()
    years = {}
    for n, y in rows:
        years.setdefault(y, []).append(n)
    rnd = random.Random(seed)
    for ns in years.values():
        rnd.shuffle(ns)
    out, i = [], 0
    while len(out) < k and any(i < len(v) for v in years.values()):    # round-robin over the years
        for y in sorted(years):
            if i < len(years[y]) and len(out) < k:
                out.append({"number": years[y][i], "year": y})
        i += 1
    return sorted(out, key=lambda x: x["number"]), len(rows)


if __name__ == "__main__":
    if sys.argv[1:2] == ["thread"]:
        print(json.dumps(thread(int(sys.argv[2])), indent=1)[:4000])
    else:
        cal, pool = calibration()
        op, total = open_sample()
        print(f"calibration: {len(cal)} of {pool} candidates; open: {len(op)} of {total} with a code fence")
        print(json.dumps({"calibration": cal, "open": op}, indent=1)[:1500])
