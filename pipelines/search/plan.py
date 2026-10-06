"""The planner of the agentic query layer (ASK.md): a natural-language question -> a structured plan the controller runs.

The LLM writes JSON; code validates it against the search parameters (unknown keys, kinds and date formats are errors fed back for another try, as in
`iterate`) and fills what the model must not decide: relative dates are resolved against today, and a vague "recently" gets a stated default.

  {"criterion": what a document must be to answer (the gate's question),
   "kinds": [what the answer is: pr, issue, commit, ...] or [],
   "queries": [1..3 search texts: the question's content words, a rewording, identifiers it names],
   "since", "until": YYYY[-MM[-DD]] or absent, "authors": [..] or absent, "open_only": bool,
   "notes": [what the code decided for the model, e.g. the default it gave "recently"]}"""
import datetime, json, re

from llm import chat
from store import check_where

KINDS = ["file", "issue", "pr", "comment", "review", "summary", "commit", "release", "tag"]
RECENT_DAYS = 180
PROMPT = """Turn a question about the Scala compiler, standard library or the Zinc incremental compiler into a search plan. Today is {today}.

Question: {question}

Answer with JSON only, these keys:
- "criterion": one sentence: what a document must be to answer the question, e.g. "the pull request that fixes <the problem>".
- "kinds": what the answer is, from {kinds}; [] when any kind will do. "the PR that ..." is ["pr"]; "the issue / bug report / ticket" is ["issue"];
  "the commit" is ["commit"]; "where in the code / which class" is ["file"].
- "queries": 1 to 3 search texts. The first: the question's technical content without filler ("find the PR that fixed"). Others: a rewording with the
  terms a developer would use in a title, or identifiers, error messages or flags the question names. Short, no dates, no names of people.
- "since", "until": a date range only if the question gives one ("in 2021" -> since 2021, until 2021; "in early 2024" -> since 2024-01, until 2024-05;
  "around 2020" -> since 2019, until 2021; "after 2.13.0" is not a date: leave it out). Format YYYY, YYYY-MM or YYYY-MM-DD. Write "recent" as the value
  of "since" for "recently", "lately", "new".
- "authors": only GitHub logins or names of people written in the question ("by <login>"), else omit.
- "open_only": true only if the question asks for open / unresolved / not yet fixed items."""


def _resolve(plan, today, notes):
    if plan.get("since") == "recent":
        plan["since"] = (today - datetime.timedelta(days=RECENT_DAYS)).isoformat()
        notes.append(f"'recently' read as the last {RECENT_DAYS} days (since {plan['since']})")
    return plan


_WHEN = re.compile(r"\b(19|20)\d\d\b|\b(recent(ly)?|lately|new(est|ly)?|this (year|month|week)|last (year|month|week|\d+ (days|weeks|months|years)))\b", re.I)


def validate(j, today, notes, question=""):
    """(plan, problems). Authors the question does not name are dropped (a model fills the slot from its examples), and so are dates when the
    question names no year and no "recently" (the model invents a range)."""
    problems = []
    if not isinstance(j, dict):
        return None, ["the answer must be one JSON object"]
    extra = set(j) - {"criterion", "kinds", "queries", "since", "until", "authors", "open_only"}
    if extra:
        problems.append(f"unknown keys {sorted(extra)}")
    if not isinstance(j.get("criterion"), str) or len(j["criterion"].split()) < 4:
        problems.append("criterion must be a sentence")
    kinds = j.get("kinds") or []
    if not isinstance(kinds, list) or any(k not in KINDS for k in kinds):
        problems.append(f"kinds must be a list from {KINDS}")
    qs = j.get("queries")
    if not isinstance(qs, list) or not 1 <= len(qs) <= 3 or not all(isinstance(q, str) and q.strip() for q in qs):
        problems.append("queries must be a list of 1 to 3 non-empty strings")
    j = dict(j)
    if (j.get("since") or j.get("until")) and question and not _WHEN.search(question):
        notes.append(f"dropped a date range the question does not give: {j.get('since')}..{j.get('until')}")
        j.pop("since", None); j.pop("until", None)
    j = _resolve(j, today, notes)
    if isinstance(j.get("authors"), list):
        named = [a for a in j["authors"] if isinstance(a, str) and a.strip().lstrip("@").lower() in question.lower()]
        if len(named) < len(j["authors"]):
            notes.append(f"dropped authors the question does not name: {[a for a in j['authors'] if a not in named]}")
        j["authors"] = named
    try:
        where = check_where({k: j.get(k) for k in ("since", "until", "authors")})
    except ValueError as e:
        problems.append(str(e))
        where = None
    if problems:
        return None, problems
    return {"criterion": j["criterion"].strip(), "kinds": kinds, "queries": [q.strip() for q in qs], **(where or {}), "open_only": bool(j.get("open_only")),
            "notes": notes}, []


def plan(question, model="qwen3-coder", attempts=3, today=None):
    """The plan for `question`, with `attempts` tries; on failure a fallback plan (the question as the only query, no filters) and its problems in notes."""
    today = today or datetime.date.today()
    convo = [{"role": "user", "content": PROMPT.format(today=today.isoformat(), question=question, kinds=KINDS)}]
    problems = []
    for _ in range(attempts):
        out = chat(convo, model, max_tokens=400, temperature=0.0)
        m = re.search(r"\{.*\}", out, re.S)
        try:
            j = json.loads(m[0]) if m else None
        except ValueError:
            j = None
        p, problems = validate(j, today, [], question)
        if p:
            return p
        convo += [{"role": "assistant", "content": out}, {"role": "user", "content": "Fix these problems and answer with the JSON again: " + "; ".join(problems)}]
    return {"criterion": question, "kinds": [], "queries": [question], "open_only": False, "notes": [f"planner failed ({'; '.join(problems)}): searching the question as is"]}
