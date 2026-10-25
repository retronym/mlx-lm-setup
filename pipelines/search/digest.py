"""The refresh digest: what happened in the universe since the last refresh. The facts are read out of the indexes (an issue or PR, its state, how
many new comments, a link; new releases per project) and shown as bullets, exact by construction. A local LLM adds a short overview of what stands out,
written only from those facts and checked against them by the NLI model (the NLI model is good at checking prose, weak at bullet lists full of numbers,
which is why the bullets are not model-written). It lands in <data>/digest.json (what the web page shows) and <data>/digests/<universe>/<timestamp>.md
(history). With nothing to report no model is called."""
import json, re, sqlite3, time
from pathlib import Path
import llm

SYSTEM = ("You write the opening paragraph of a digest of activity in Scala compiler and build-tool projects, for the people who work on them. "
          "Use only the facts and context you are given. Do not add background, opinions or guesses.")
PROMPT = ("Below are the facts about what changed since {since}, and context on the busiest items. Write 2 to 4 sentences on what stands out. "
          "Group related work into themes, across projects where it fits (for example support for a new JDK, or a build migration). Say in plain words "
          "what each change does, then cite it in parentheses as owner/repo#number, copied exactly from the facts. Mention a state only when it matters "
          "(merged, or a release). Do not count comments or commits, do not go project by project, and do not try to cover everything.\n\n"
          "The style, with invented items: \"Support for JDK 99 moved forward: CI now tests it (example/lib#11) and a new warning flags its "
          "value-based classes (example/lib#12, merged). The most discussed change is a rewrite of compiler settings handling (example/compiler#30), "
          "still under review.\"\n\n"
          "<facts>\n{facts}\n</facts>\n\n<context>\n{context}\n</context>")
REF = re.compile(r"\b[\w.-]+/[\w.-]+#\d+")
TOP_PER_PROJECT = 7
CONTEXT_PER_PROJECT = 3          # the busiest items of each project get a line of context: their thread summary, or the start of their body
CONTEXT_CHARS = 280


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _gist(body):
    """The first prose paragraph of an issue or PR body, shortened. Skipped: comments, code, headings, quotes, lists and checkboxes, an answer to a
    template question (a heading ending in "?"), and a lead-in to a list (ends with ":")."""
    body = re.sub(r"<!--.*?-->|```.*?```", "", body or "", flags=re.S)
    after_question = False
    for para in re.split(r"\n\s*\n", body):
        heads = [x.strip() for x in para.splitlines() if x.strip().startswith("#")]
        para = " ".join(x.strip() for x in para.splitlines() if x.strip() and not re.match(r"[#>|\-*]|\[[ x]\]", x.strip()))
        if heads:
            after_question = heads[-1].endswith("?")
        elif after_question:
            after_question = False
            continue
        if len(para.split()) >= 6 and not para.endswith(":"):
            return para if len(para) <= CONTEXT_CHARS else para[:CONTEXT_CHARS].rsplit(" ", 1)[0] + "..."
    return ""


def facts(cfg, universe_id, since_ts, context=None):
    """[(project title, [bullet lines])] for everything GitHub-active or released after `since_ts`, read from the project databases. When
    `context` is a list, a line of substance for the busiest items of each project is appended to it ("owner/repo#n: ...")."""
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
                                           AND json_extract(c.meta, '$.updated') >= ?) AS fresh, i.source
                                   FROM chunks i WHERE json_extract(meta, '$.kind') IN ('issue', 'pr') AND json_extract(meta, '$.updated') >= ?
                                   GROUP BY i.source, i.doc                       -- a long body is several chunks: one item per issue or PR
                                   ORDER BY fresh DESC, json_extract(meta, '$.updated') DESC""", (since, since)).fetchall()
            releases = con.execute("""SELECT json_extract(meta, '$.tag'), title, url, json_extract(meta, '$.published') FROM chunks
                                      WHERE json_extract(meta, '$.kind') = 'release' AND id LIKE '%:header' AND json_extract(meta, '$.published') >= ? ORDER BY 4 DESC""",
                                   (since[:10],)).fetchall()
            commits = con.execute("""SELECT title FROM chunks WHERE json_extract(meta, '$.kind') = 'commit' AND id NOT LIKE '%~%' AND json_extract(meta, '$.updated') >= ?
                                     ORDER BY json_extract(meta, '$.updated') DESC""", (since,)).fetchall()
            if context is not None:
                for num, _, _, title, _, _, src in items[:CONTEXT_PER_PROJECT]:
                    row = (con.execute("SELECT text FROM chunks WHERE id = ?", (f"{src}:summary:{num}",)).fetchone()
                           or con.execute("SELECT text FROM chunks WHERE id = ?", (f"{src}:issue:{num}",)).fetchone())
                    gist = _gist(row[0]) if row else ""
                    if gist:
                        context.append(f"{title.split(' ')[0]}: {gist}")
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
        for num, kind, state, title, url, fresh, _ in items[:TOP_PER_PROJECT]:
            t = title.split(" ", 1)[1] if " " in title else title
            lines.append(f"{'PR' if kind == 'pr' else 'issue'} {title.split(' ')[0]} ({state}): {t[:110]}" + (f"; {fresh} new comment{'s' if fresh != 1 else ''}" if fresh else "") + f" {url}")
        if lines:
            out.append((cfg.projects[pid].title, lines))
    return out


def checks(text_facts):
    """Mechanical gates on the overview, run before the NLI check: length, a complete last sentence, references copied from the facts, no counts."""
    known = set(REF.findall(text_facts))
    def check(text):
        bad = []
        if len([s for s in llm._SENT.split(" ".join(text.split())) if s.strip()]) > 4:
            bad.append("write at most 4 sentences")
        if not text.rstrip().endswith((".", "!", "?", ")")):
            bad.append("the answer is cut off: finish the last sentence")
        unknown = sorted(set(REF.findall(text)) - known)
        if unknown:
            bad.append(f"these references are not in the facts, copy them exactly (owner/repo#number) or leave them out: {', '.join(unknown[:5])}")
        if re.search(r"\b[\w.-]+/[\w.-]+/\d+\b", text):
            bad.append("write references as owner/repo#number, not owner/repo/number")
        if re.search(r"\b\d+ (new )?(comments?|commits?)\b", text):
            bad.append("do not count comments or commits; say what the work is about")
        if text.lstrip().startswith("#") or "\n- " in text:
            bad.append("write one paragraph of prose, no headings or bullets")
        return bad
    return check


def make_digest(cfg, universe_id, since_ts, run=None):
    """Write the digest (see module docstring): a short overview written by the local LLM and checked against the facts by the NLI model (lines it
    cannot support are dropped; if nothing faithful is left the overview is left out), followed by the facts themselves rendered as bullets, which
    are exact by construction. Returns the digest dict, or None when there was nothing to report."""
    conf = cfg.search["llm"]["digest"]
    ctx = []
    fx = facts(cfg, universe_id, since_ts, context=ctx)
    if not fx:
        return None
    text_facts = "\n\n".join(f"## {title}\n" + "\n".join(f"- {l}" for l in lines) for title, lines in fx)
    text_ctx = "\n".join(f"- {c}" for c in ctx) or "(none)"
    since = _iso(since_ts)
    if run:
        run.log(f"digest: {sum(len(l) for _, l in fx)} facts from {len(fx)} projects, asking {conf['model']} for an overview")
    overview, ok, tries, pruned = llm.grounded_pruned([{"role": "system", "content": SYSTEM},
                                                       {"role": "user", "content": PROMPT.format(since=since, facts=text_facts, context=text_ctx)}],
                                                      text_facts + "\n\n" + text_ctx, conf["model"], attempts=4, max_chars=900, max_tokens=500,
                                                      checks=checks(text_facts), serial=cfg.search["refresh"]["memory"]["serial_models"])
    overview = overview if ok else ""                                  # an overview that cannot be made faithful is left out, never shown
    text = (overview + "\n\n" if overview else "") + text_facts
    d = {"universe": universe_id, "generated": time.time(), "since": since, "model": conf["model"], "checked": True, "overview": bool(overview), "attempts": tries,
         "pruned": pruned, "facts": sum(len(l) for _, l in fx), "text": text, "source_facts": text_facts,
         "context": text_ctx}
    out = cfg.data_path("digests", universe_id)
    out.mkdir(parents=True, exist_ok=True)
    note = "overview checked against the facts" if overview else "overview left out: it did not pass the check against the facts"
    (out / f"{time.strftime('%Y-%m-%d-%H%M', time.gmtime())}.md").write_text(f"# {universe_id} digest since {since}\n\n{text}\n\n---\n{note} ({tries} tries, {conf['model']})\n")
    tmp = cfg.data_path("digest.json.tmp")
    tmp.write_text(json.dumps(d))
    tmp.replace(cfg.data_path("digest.json"))
    return d
