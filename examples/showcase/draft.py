"""Draft the narration locally: one scene at a time, a local LLM writes from the scene's brief and facts, through `iterate` gates.
Then every sentence is fact-checked against the scene's facts by the local NLI model, and every number is checked by code.

    .venv/bin/python examples/showcase/draft.py [--model gemma] [--only hook,deal]     # -> data/showcase/drafts.json, drafts.md
    .venv/bin/python examples/showcase/draft.py --compare                              # script.json (edited) vs the drafts
    .venv/bin/python examples/showcase/draft.py --check-script                         # fact-check the edited script, the same way

Uses the gateway's MCP endpoint (iterate is an MCP tool), so this is exactly what a Claude session would call.
"""
import argparse, asyncio, difflib, json, re
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
OUT = ROOT / "data/showcase"
CUE = re.compile(r"\[\[[A-Za-z0-9_-]+\]\]")

UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
                                     "sixteen seventeen eighteen nineteen".split())}
TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
SCALES = {"hundred": 100, "thousand": 1000, "million": 10 ** 6}


def numbers(text: str) -> list[str]:
    """Numbers mentioned in narration, as normalised strings: runs of number words ("seven hundred and forty eight" -> 748),
    "point" decimals ("zero point seven two" -> 0.72), and any digits."""
    toks = re.findall(r"[A-Za-z]+|\d+(?:\.\d+)?|[,;:.!?]", text.lower().replace("-", " "))     # punctuation ends a number
    out, i = [], 0
    while i < len(toks):
        t = toks[i]
        if re.fullmatch(r"\d+(?:\.\d+)?", t):
            out.append(t); i += 1; continue
        if t not in UNITS and t not in TENS:
            i += 1; continue
        total = cur = 0
        while i < len(toks) and (toks[i] in UNITS or toks[i] in TENS or toks[i] in SCALES or
                                 (toks[i] == "and" and i > 0 and toks[i - 1] in SCALES and i + 1 < len(toks) and (toks[i + 1] in UNITS or toks[i + 1] in TENS))):
            w = toks[i]
            if w in UNITS: cur += UNITS[w]
            elif w in TENS: cur += TENS[w]
            elif w in SCALES:
                cur = max(cur, 1) * SCALES[w]
                if SCALES[w] > 100: total += cur; cur = 0
            i += 1
        val = str(total + cur)
        if i + 1 < len(toks) and toks[i] == "point":
            digits = []
            i += 1
            while i < len(toks) and toks[i] in UNITS and UNITS[toks[i]] < 10:
                digits.append(str(UNITS[toks[i]])); i += 1
            val += "." + "".join(digits)
        out.append(val)
    return out


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


async def call(s: ClientSession, tool: str, args: dict) -> dict:
    r = await s.call_tool(tool, args)
    txt = r.content[0].text
    if r.isError:
        raise RuntimeError(f"{tool}: {txt}")
    return json.loads(txt)


async def draft(model: str, only: set[str] | None):
    brief = json.load(open(HERE / "brief.json"))
    old = {d["id"]: d for d in json.load(open(OUT / "drafts.json"))["scenes"]} if (OUT / "drafts.json").exists() else {}
    out, prev = [], None
    async with streamablehttp_client("http://127.0.0.1:8090/mcp", timeout=600) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for sc in brief["scenes"]:
                if only and sc["id"] not in only and sc["id"] in old:
                    out.append(old[sc["id"]]); prev = old[sc["id"]]["text"]; continue
                facts = "\n".join(f"- {f}" for f in sc["facts"])
                msg = (f"Write the narration for one scene, about {sc['words']} words.\nScene: {sc['brief']}\nFacts you may use, and nothing else:\n{facts}\n"
                       + (f"\nThe previous scene ended with: \"{prev[-300:]}\" Continue naturally from it; do not repeat it.\n" if prev else "")
                       + "\nReply with the narration only.")
                lo, hi = int(sc["words"] * 0.75 * 5.6), int(sc["words"] * 1.3 * 6.2)
                gates = [{"type": "length", "min_chars": lo, "max_chars": hi},
                         {"type": "regex", "pattern": "[0-9]", "mode": "absent"},
                         {"type": "regex", "pattern": "^\\s*([-*#]|\\d+\\.)|[!?]", "mode": "absent", "multiline": True},
                         {"type": "nli", "source": " ".join(sc["facts"])}]
                res = await call(s, "iterate", {"message": msg, "system": brief["style"], "gates": gates, "model": model,
                                                "max_attempts": 3, "temperature": 0.4, "max_tokens": 400})
                text = res["text"].strip().strip('"')
                # fact-check: each sentence against the scene's facts (local NLI); numbers by code
                sents = sentences(text)
                ent = await call(s, "entail", {"premise": " ".join(sc["facts"]), "hypotheses": sents})
                fact_nums = set(numbers(" ".join(sc["facts"])))
                checks = [{"sentence": r_["hypothesis"], "label": r_["label"], "entailment": r_["entailment"],
                           "numbers_unsupported": [n for n in numbers(r_["hypothesis"]) if n not in fact_nums]} for r_ in ent["results"]]
                d = {"id": sc["id"], "target_words": sc["words"], "words": len(text.split()), "text": text, "passed": res["passed"],
                     "attempts": [{k: a[k] for k in ("attempt", "model", "secs", "passed", "failures")} for a in res["attempts"]], "checks": checks}
                flags = [c for c in checks if c["label"] != "entailment" or c["numbers_unsupported"]]
                print(f"{sc['id']:<10} {d['words']:3}/{sc['words']} words  {'passed' if d['passed'] else 'FAILED'} in {len(d['attempts'])}  "
                      f"{sum(a['secs'] for a in d['attempts']):5.1f}s  flags: {len(flags)}", flush=True)
                out.append(d); prev = text
    (OUT / "drafts.json").write_text(json.dumps({"model": model, "scenes": out}, indent=1, ensure_ascii=False))
    md = ["# Local drafts\n", f"Drafted by `{model}` through `iterate`; fact-checked per sentence by the local NLI model; numbers checked by code.\n"]
    for d in out:
        md.append(f"## {d['id']} ({d['words']}/{d['target_words']} words, {'passed' if d['passed'] else 'FAILED gates'} in {len(d['attempts'])} attempt(s))\n\n{d['text']}\n")
        for c in d["checks"]:
            if c["label"] != "entailment" or c["numbers_unsupported"]:
                md.append(f"- ⚠ {c['label']} {c['entailment']:.2f}{' · numbers not in facts: ' + ', '.join(c['numbers_unsupported']) if c['numbers_unsupported'] else ''}: {c['sentence']}")
        md.append("")
    (OUT / "drafts.md").write_text("\n".join(md))


def compare():
    """How much of each local draft survives in the edited script: sentences kept verbatim, and word-level similarity."""
    drafts = {d["id"]: d["text"] for d in json.load(open(OUT / "drafts.json"))["scenes"]}
    script = {s["id"]: CUE.sub("", s["narration"]) for s in json.load(open(HERE / "script.json"))["scenes"]}
    norm = lambda t: re.sub(r"\s+", " ", t).strip()
    tot_s = kept_s = 0; tot_w = same_w = 0
    for sid, d in drafts.items():
        e = norm(script.get(sid, ""))
        ds = sentences(d)
        kept = sum(1 for x in ds if norm(x) in e)
        sm = difflib.SequenceMatcher(None, d.split(), e.split(), autojunk=False)
        same = sum(b.size for b in sm.get_matching_blocks())
        tot_s += len(ds); kept_s += kept; tot_w += len(e.split()); same_w += same
        print(f"{sid:<10} sentences kept {kept}/{len(ds)}   words from the draft {same}/{len(e.split())}")
    print(f"\nTOTAL: {kept_s}/{tot_s} draft sentences kept verbatim; {same_w}/{tot_w} words of the final script come from the local drafts ({same_w / max(tot_w, 1):.0%})")


async def check_script():
    """The edited script gets the same checks as the drafts: every sentence against its scene's facts (local NLI), numbers by code."""
    brief = {s["id"]: s for s in json.load(open(HERE / "brief.json"))["scenes"]}
    script = json.load(open(HERE / "script.json"))["scenes"]
    flagged = total = 0
    async with streamablehttp_client("http://127.0.0.1:8090/mcp", timeout=600) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for sc in script:
                facts = brief[sc["id"]]["facts"]
                sents = sentences(re.sub(r"\s+", " ", CUE.sub("", sc["narration"])))
                ent = await call(s, "entail", {"premise": " ".join(facts), "hypotheses": sents})
                fact_nums = set(numbers(" ".join(facts)))
                for r_ in ent["results"]:
                    total += 1
                    nums = [n for n in numbers(r_["hypothesis"]) if n not in fact_nums]
                    if r_["label"] != "entailment" or nums:
                        flagged += 1
                        print(f"{sc['id']:<10} {r_['label']:<13} {r_['entailment']:.2f}{'  numbers not in facts: ' + ','.join(nums) if nums else ''}  {r_['hypothesis']}")
    print(f"\n{flagged} of {total} sentences flagged")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemma")
    ap.add_argument("--only")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--check-script", action="store_true")
    a = ap.parse_args()
    if a.compare:
        compare()
    elif a.check_script:
        asyncio.run(check_script())
    else:
        asyncio.run(draft(a.model, set(a.only.split(",")) if a.only else None))
