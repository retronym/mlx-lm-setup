"""Local-model helpers for the indexer's own jobs (thread summaries, the refresh digest), all through the gateway: a local LLM writes, the NLI
model checks that what it wrote is supported by the source text, and a failed check is fed back for another try. Nothing here leaves the Mac.
Same faithfulness rule as the gateway's `iterate` NLI gate: every sentence must be entailed (>= min_entailment) and not contradicted
(< max_contradiction)."""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gateway_client as gw

PREMISE_CHARS = 8000            # the NLI model reads a bounded premise
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`#*\-])")


def chat(messages, model, max_tokens=700, temperature=0.2):
    d = gw.post("/v1/chat/completions", {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature, "stream": False})
    return (d["choices"][0]["message"].get("content") or "").strip()


def claims(text, limit=24):
    """The checkable sentences of `text` (bullets and short fragments are skipped)."""
    out = []
    for line in text.splitlines():
        line = re.sub(r"^[\s>*#\-]+", "", line).strip()
        for s in _SENT.split(line):
            if len(s.split()) >= 5:
                out.append(s.strip())
    return out[:limit]


def faithful(source, text, min_entailment=0.5, max_contradiction=0.5):
    """(ok, problems): is every sentence of `text` supported by `source`?"""
    cs = claims(text)
    if not cs:
        return True, []
    probs = gw.entail(source[:PREMISE_CHARS], cs)                     # [[contradiction, entailment, neutral], ...]
    bad = []
    for c, (contra, ent, _) in zip(cs, probs):
        if contra >= max_contradiction:
            bad.append(f'contradicted by the source ({contra:.2f}): "{c[:160]}"')
        elif ent < min_entailment:
            bad.append(f'not supported by the source ({ent:.2f}): "{c[:160]}"')
    return not bad, bad


def grounded(messages, source, model, attempts=3, max_chars=None, **kw):
    """Ask the LLM, check the answer against `source`, retry with the problems. Returns (text, ok, tries). A last unfaithful answer is returned
    with ok=False: the caller decides whether to keep it (the indexer does not)."""
    convo, text, tries = list(messages), "", 0
    for tries in range(1, attempts + 1):
        text = chat(convo, model, **kw)
        problems = []
        if max_chars and len(text) > max_chars:
            problems.append(f"the answer is {len(text)} characters; keep it under {max_chars}")
        ok, bad = faithful(source, text)
        problems += bad
        if not problems:
            return text, True, tries
        convo += [{"role": "assistant", "content": text},
                  {"role": "user", "content": "Fix these problems and answer again, using only facts from the source text: " + " | ".join(problems[:5])}]
    return text, False, tries


def prune(source, text, min_entailment=0.5, max_contradiction=0.5):
    """Remove the lines of `text` that contain a sentence the source does not support. Returns (pruned text, lines removed); headings left with
    nothing under them go too."""
    lines = text.splitlines()
    per_line = [claims(x, limit=8) for x in lines]
    flat = [c for cs in per_line for c in cs][:48]
    if not flat:
        return text, 0
    bad = {c for c, (contra, ent, _) in zip(flat, gw.entail(source[:PREMISE_CHARS], flat)) if contra >= max_contradiction or ent < min_entailment}
    kept = [x for x, cs in zip(lines, per_line) if not any(c in bad for c in cs)]
    out = re.sub(r"^(#+ .*)\n(?=\s*(#+ |\Z))", "", "\n".join(kept) + "\n", flags=re.M).strip()
    return out, len(lines) - len(kept)


def grounded_pruned(messages, source, model, **kw):
    """`grounded`, and when the last answer still has unsupported lines, drop those lines instead of giving up: (text, ok, tries, removed).
    If nothing faithful is left, the unfaithful answer is returned with ok=False so the failure stays visible."""
    text, ok, tries = grounded(messages, source, model, **kw)
    if ok:
        return text, True, tries, 0
    pruned, removed = prune(source, text)
    if pruned and removed and faithful(source, pruned)[0]:
        return pruned, True, tries, removed
    return text, False, tries, 0
