"""Live data for the film, all from the local models through the gateway's MCP endpoint:
- the job deck sorted by the decision model (generate / decide / check / judgment), shown on screen as it came out, misses included;
- one pull request asked four questions in one pass (the decide mechanism);
- the same questions answered by a generating model, with its real token count and time (the contrast);
- a token estimate for the PR triage (the cost scene).

    .venv/bin/python examples/showcase/cards.py           # -> data/showcase/cards.json
"""
import asyncio, json
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data/showcase"

# The deck: real small jobs from working in this repo, with the kind the lead would assign (the "truth" the sort is scored on).
DECK = [
    {"id": "summarise", "text": "Summarise gateway/supervisor.py in three sentences", "truth": "generate", "private": True},
    {"id": "housekeeping", "text": "Is PR #11285 “Update sbt-mima-plugin to 1.2.1” just internal housekeeping?", "truth": "decide", "private": False},
    {"id": "sentiment", "text": "Is this review positive, negative or mixed?", "truth": "decide", "private": False},
    {"id": "match", "text": "Does this summary match the code it describes?", "truth": "check", "private": True},
    {"id": "extract", "text": "Extract the method names from Foo.scala as JSON", "truth": "generate", "private": True},
    {"id": "label", "text": "Which label fits this pull request description?", "truth": "decide", "private": True},
    {"id": "design", "text": "Design the gateway's memory eviction policy", "truth": "judgment", "private": False},
    {"id": "review", "text": "Review this concurrency fix in the supervisor", "truth": "judgment", "private": True},
]
KINDS = {"generate": "write new text or code: a summary, an extraction, boilerplate",
         "decide": "pick one answer from a short closed list of labels or options",
         "check": "verify whether a claim is supported by a given source text",
         "judgment": "open-ended design or review that needs expert judgment"}

PR = "Pull request #11285 to scala/scala: \"Update sbt-mima-plugin to 1.2.1 in 2.12.x\". Changed files: project/plugins.sbt. Author: scala-steward."
QUESTIONS = [
    {"t": "choice", "ins": "What kind of change is this?", "crit": {"dependency update": None, "bug fix": None, "new feature": None, "refactoring": None, "documentation": None}},
    {"t": "score", "ins": "How risky is this change for users?", "crit": ["no risk", "low risk", "moderate risk", "high risk", "very high risk"]},
    {"t": "noul", "ins": "This change needs a release note for users."},
    {"t": "noul", "ins": "This change is internal housekeeping with no user-visible effect."},
]


async def call(s, tool, args):
    r = await s.call_tool(tool, args)
    if r.isError:
        raise RuntimeError(f"{tool}: {r.content[0].text}")
    return json.loads(r.content[0].text)


async def main():
    out = {"deck": [], "counts": {"jevstyle-2b": 0, "gemma-4-26b-a4b": 0}}
    async with streamablehttp_client("http://127.0.0.1:8090/mcp", timeout=600) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for c in DECK:
                d = await call(s, "decide", {"state": f"Job: {c['text']}", "question": "What kind of job is this for a language model?",
                                             "options": KINDS, "top_k": 4})
                out["counts"]["jevstyle-2b"] += 1
                res = d["results"][0]
                out["deck"].append({**c, "sorted": res["answer"], "p": res["top_probability"], "top": res["top"]})
                print(f"{c['id']:<13} truth {c['truth']:<9} sorted {res['answer']:<9} {res['top_probability']:.2f}")
            d = await call(s, "decide", {"state": PR, "questions": QUESTIONS, "top_k": 0})
            out["counts"]["jevstyle-2b"] += 1
            out["pr"] = {"state": PR, "questions": [{"q": q["ins"], "kind": q["t"], "answer": r_["answer"], "p": r_["top_probability"], "top": r_["top"]}
                                                    for q, r_ in zip(QUESTIONS, d["results"])]}
            g = await call(s, "chat", {"model": "gemma", "temperature": 0.3, "max_tokens": 300,
                                       "message": PR + "\nAnswer four questions about it: what kind of change is it, how risky is it for users, "
                                                       "does it need a release note, and is it internal housekeeping?"})
            out["counts"]["gemma-4-26b-a4b"] += 1
            out["generated"] = {"text": g["text"], "usage": g.get("usage"), "secs": g["secs"]}
            print("generated:", g.get("usage"), g["secs"], "s")
    # cost: the PR triage's input volume (premise = title + files + first 1.5k chars of body, as in jev_triage.py; 6 hypotheses each)
    prs = json.load(open(ROOT / "data/prs.json"))
    chars = sum(len(p["title"]) + sum(len(f["path"]) + 1 for f in p.get("files", [])) + len((p.get("body") or "")[:1500]) for p in prs)
    out["triage"] = {"prs": len(prs), "questions": 6, "judgments": 6 * len(prs), "input_chars": 6 * chars,
                     "input_tokens_est": round(6 * chars / 4, -3), "estimate": "characters / 4"}
    print("triage:", out["triage"])
    (OUT / "cards.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
