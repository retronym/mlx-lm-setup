"""Thread summaries: for long GitHub threads (many comments) a local LLM writes a short factual summary of the problem, the discussion and the
outcome, checked against the thread text by the NLI model, and it is indexed as one more chunk of that thread (`<source>:summary:<n>`, same
document, so results still show one hit per thread). It gives retrieval a dense, high-signal target for "what was decided about X?" without
re-reading forty comments. Newest-updated threads first, capped per run (llm.thread_summaries.max_per_run), a thread is re-summarised when its
comment count has grown by half (or five). Summaries that fail the faithfulness check after three tries are not indexed."""
import json, math, time
import llm
from store import Chunk

SYSTEM = ("You summarise GitHub issue and pull request threads about the Scala compiler, standard library and build tools, for a search index. "
          "Be factual and specific. State only what the thread says; keep identifiers, version numbers and names exactly as written.")
PROMPT = ("Summarise this thread as one paragraph of at most 120 words: the problem or proposal, the main points of the discussion (who argued what), "
          "and the outcome or current state.\n\n<thread>\n{thread}\n</thread>")
HEAD_CHARS, TAIL_CHARS = 3500, 8500


def candidates(st, source_ids, min_comments):
    """Threads with at least `min_comments` comments that have no summary yet or have grown: [(source, doc, comments)], newest first."""
    marks = ",".join("?" * len(source_ids))
    rows = st.db.execute(f"""SELECT source, doc, sum(json_extract(meta, '$.kind') IN ('comment', 'review')), max(json_extract(meta, '$.updated'))
                             FROM chunks WHERE source IN ({marks}) AND json_extract(meta, '$.kind') IN ('issue', 'pr', 'comment', 'review')
                             GROUP BY source, doc HAVING sum(json_extract(meta, '$.kind') IN ('comment', 'review')) >= ? ORDER BY max(json_extract(meta, '$.updated')) DESC""",
                         (*source_ids, min_comments)).fetchall()
    out = []
    for src, doc, n, _ in rows:
        old = st.db.execute("SELECT json_extract(meta, '$.comments') FROM chunks WHERE id = ?", (f"{src}:summary:{doc.split(':')[1]}",)).fetchone()
        if old is None or n >= max(old[0] * 1.5, old[0] + 5):
            out.append((src, doc, n))
    return out


def thread_text(st, src, doc):
    """The thread as plain text, chronologically: title, body, then each comment with its author; the middle is cut when it is very long."""
    rows = st.db.execute("""SELECT title, text, json_extract(meta, '$.kind'), json_extract(meta, '$.updated') FROM chunks WHERE source = ? AND doc = ?
                            AND json_extract(meta, '$.kind') IN ('issue', 'pr', 'comment', 'review') ORDER BY CASE WHEN json_extract(meta, '$.kind') IN ('issue', 'pr') THEN 0 ELSE 1 END,
                            json_extract(meta, '$.updated'), id""", (src, doc)).fetchall()
    parts, head = [], rows[0][0] if rows else ""
    for title, text, kind, _ in rows:
        who = title.rsplit("(", 1)[-1].rstrip(")") if kind != "issue" and kind != "pr" and "(" in title else None
        parts.append(f"[{who}] {text}" if who else f"{title}\n{text}")
    full = "\n\n".join(parts)
    if len(full) > HEAD_CHARS + TAIL_CHARS:
        full = full[:HEAD_CHARS] + "\n\n[... middle of the thread omitted ...]\n\n" + full[-TAIL_CHARS:]
    return head, full


def summarize_threads(cfg, srcs, run, *, deadline=None, force_enabled=False, room=None):
    """Returns (written, failed, skipped_unfaithful). All candidates go to the LLM as one batch (llm.grounded_many), so with
    refresh.memory.serial_models the LLM and the NLI model take turns instead of sharing memory. `room()`: see grounded_many."""
    from store import Store
    conf = cfg.search["llm"]["thread_summaries"]
    if not (conf["enabled"] or force_enabled):
        return 0, 0, 0
    by_project = {}
    for s in srcs:
        if s.type == "github" and cfg.project_db(s.project).exists():
            by_project.setdefault(s.project, []).append(s)
    todo = []                                                            # (project, members, store, src, doc, comments, head, text)
    for pid, members in by_project.items():
        st = Store(cfg.project_db(pid))
        for src, doc, n in candidates(st, [m.id for m in members], conf["min_comments"]):
            if len(todo) >= conf["max_per_run"]:
                break
            todo.append((pid, members, st, src, doc, n, *thread_text(st, src, doc)))
    if not todo:
        return 0, 0, 0
    serial = cfg.search["refresh"]["memory"]["serial_models"]
    run.log(f"enrich: {len(todo)} threads to summarise with {conf['model']}" + (", the LLM and the NLI model taking turns" if serial else ""))
    results = llm.grounded_many([([{"role": "system", "content": SYSTEM}, {"role": "user", "content": PROMPT.format(thread=text)}], text) for *_, text in todo],
                                conf["model"], attempts=3, max_chars=1000, max_tokens=400, serial=serial, room=room,
                                go=lambda: not deadline or time.time() <= deadline)
    written = failed = 0
    for (pid, members, st, src, doc, n, head, _), res in zip(todo, results):
        if res is None:                                                  # out of time or memory: first in line next run
            continue
        summary, ok, tries = res
        run.source(f"{pid}/{src}", "enrich")
        num = doc.split(":")[1]
        if not ok:
            failed += 1
            run.log(f"{pid}/{src}#{num}: summary not faithful after {tries} tries, not indexed")
            continue
        repo = next(m.repo for m in members if m.id == src)
        title = f"{head.split('  ')[0] if head else f'{repo}#{num}'}  (thread summary)"
        ch = Chunk(f"{src}:summary:{num}", doc, title, summary, f"https://github.com/{repo}/issues/{num}",
                   {"kind": "summary", "number": int(num), "comments": n, "model": conf["model"], "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        st.apply(src, [ch])
        st.commit()
        written += 1
        run.log(f"{pid}/{src}#{num}: summarised ({n} comments, {tries} tr{'y' if tries == 1 else 'ies'})")
    if results.count(None):
        run.log(f"enrich: {results.count(None)} threads left for the next run (" + ("time budget" if deadline and time.time() > deadline else "not enough free memory") + ")")
    return written, failed, 0
