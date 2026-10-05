"""The refresh digest: what happened in the universe since the last refresh. The facts are read out of the indexes (an issue or PR, its state, how
many new comments, a link; new releases per project) and shown as bullets, exact by construction. A local LLM adds a short overview of what stands out,
written only from those facts and checked against them by the NLI model (the NLI model is good at checking prose, weak at bullet lists full of numbers,
which is why the bullets are not model-written). It lands in <data>/digest.json (what the web page shows) and <data>/digests/<universe>/<timestamp>.md
(history). With nothing to report no model is called."""
import json, sqlite3, time
from pathlib import Path
import llm

SYSTEM = ("You write a short, factual overview of activity in a set of Scala compiler and build-tool projects, for the people who work on them. "
          "Use only the facts you are given. Do not add background, opinions or guesses.")
PROMPT = ("Here are the facts about what changed since {since}. In 2 to 4 sentences, say what stands out: the most important releases, merged PRs and "
          "discussions with the most new comments, naming them by number (like sbt/zinc#1500) and saying whether they were merged, closed or are open. "
          "Do not list everything, and say nothing that is not in the facts: no remarks about earlier reports or about things not changing, no background.\n\n"
          "<facts>\n{facts}\n</facts>")
TOP_PER_PROJECT = 7


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def facts(cfg, universe_id, since_ts):
    """[(project title, [bullet lines])] for everything GitHub-active or released after `since_ts`, read from the project databases."""
    since = _iso(since_ts)
    uni = cfg.universe(universe_id)
    out = []
    for pid in uni.projects:
        db = cfg.project_db(pid)
        if not db.exists():
            continue
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        try:
            items = con.execute("""SELECT json_extract(meta, '$.number'), json_extract(meta, '$.kind'), json_extract(meta, '$.state'), title, url,
                                          (SELECT count(*) FROM chunks c WHERE c.doc = i.doc AND c.source = i.source AND json_extract(c.meta, '$.kind') IN ('comment', 'review')
                                           AND json_extract(c.meta, '$.updated') >= ?) AS fresh
                                   FROM chunks i WHERE json_extract(meta, '$.kind') IN ('issue', 'pr') AND json_extract(meta, '$.updated') >= ?
                                   GROUP BY i.source, i.doc                       -- a long body is several chunks: one item per issue or PR
                                   ORDER BY fresh DESC, json_extract(meta, '$.updated') DESC""", (since, since)).fetchall()
            releases = con.execute("""SELECT json_extract(meta, '$.tag'), title, url, json_extract(meta, '$.published') FROM chunks
                                      WHERE json_extract(meta, '$.kind') = 'release' AND id LIKE '%:header' AND json_extract(meta, '$.published') >= ? ORDER BY 4 DESC""",
                                   (since[:10],)).fetchall()
            commits = con.execute("""SELECT title FROM chunks WHERE json_extract(meta, '$.kind') = 'commit' AND id NOT LIKE '%~%' AND json_extract(meta, '$.updated') >= ?
                                     ORDER BY json_extract(meta, '$.updated') DESC""", (since,)).fetchall()
        finally:
            con.close()
        lines = [f"release {t}: {title} (published {when}) {url}" for t, title, url, when in releases[:5]]
        if commits:
            subjects = "; ".join(c[0].split(" ", 3)[3][:80] for c in commits[:4] if len(c[0].split(" ", 3)) > 3)
            lines.append(f"{len(commits)} commit{'s' if len(commits) != 1 else ''} landed on the main branch" + (f"; latest: {subjects}" if subjects else ""))
        merged = sum(1 for i in items if i[2] == "merged")
        closed = sum(1 for i in items if i[2] == "closed")
        if items:
            lines.append(f"active issues and PRs: {len(items)} ({merged} PRs merged, {closed} closed without merging, {sum(1 for i in items if i[2] == 'open')} open)")
        for num, kind, state, title, url, fresh in items[:TOP_PER_PROJECT]:
            t = title.split(" ", 1)[1] if " " in title else title
            lines.append(f"{'PR' if kind == 'pr' else 'issue'} {title.split(' ')[0]} ({state}): {t[:110]}" + (f"; {fresh} new comment{'s' if fresh != 1 else ''}" if fresh else "") + f" {url}")
        if lines:
            out.append((cfg.projects[pid].title, lines))
    return out


def make_digest(cfg, universe_id, since_ts, run=None):
    """Write the digest (see module docstring): a short overview written by the local LLM and checked against the facts by the NLI model (lines it
    cannot support are dropped; if nothing faithful is left the overview is left out), followed by the facts themselves rendered as bullets, which
    are exact by construction. Returns the digest dict, or None when there was nothing to report."""
    conf = cfg.search["llm"]["digest"]
    fx = facts(cfg, universe_id, since_ts)
    if not fx:
        return None
    text_facts = "\n\n".join(f"## {title}\n" + "\n".join(f"- {l}" for l in lines) for title, lines in fx)
    since = _iso(since_ts)
    if run:
        run.log(f"digest: {sum(len(l) for _, l in fx)} facts from {len(fx)} projects, asking {conf['model']} for an overview")
    overview, ok, tries, pruned = llm.grounded_pruned([{"role": "system", "content": SYSTEM}, {"role": "user", "content": PROMPT.format(since=since, facts=text_facts)}],
                                                      text_facts, conf["model"], attempts=3, max_chars=900, max_tokens=300)
    overview = overview if ok else ""                                  # an overview that cannot be made faithful is left out, never shown
    text = (overview + "\n\n" if overview else "") + text_facts
    d = {"universe": universe_id, "generated": time.time(), "since": since, "model": conf["model"], "checked": True, "overview": bool(overview), "attempts": tries,
         "pruned": pruned, "facts": sum(len(l) for _, l in fx), "text": text, "source_facts": text_facts}
    out = cfg.data_path("digests", universe_id)
    out.mkdir(parents=True, exist_ok=True)
    note = "overview checked against the facts" if overview else "overview left out: it did not pass the check against the facts"
    (out / f"{time.strftime('%Y-%m-%d-%H%M', time.gmtime())}.md").write_text(f"# {universe_id} digest since {since}\n\n{text}\n\n---\n{note} ({tries} tries, {conf['model']})\n")
    tmp = cfg.data_path("digest.json.tmp")
    tmp.write_text(json.dumps(d))
    tmp.replace(cfg.data_path("digest.json"))
    return d
