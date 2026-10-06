"""The planner of the agentic query layer (ASK.md): a natural-language question -> a structured plan the controller runs.

The LLM writes JSON; code validates it against the search parameters (unknown keys, kinds and date formats are errors fed back for another try, as in
`iterate`) and fills what the model must not decide: relative dates are resolved against today, and a vague "recently" gets a stated default.

  {"intent": "one" (a single document answers: "find the PR that fixed X"), "list" (everything that matches: "my PRs this year", "open issues about X"),
             or "topic" (an ordinary search: "where is eta expansion handled"),
   "criterion": what a document must be to answer (the gate's question),
   "kinds": [what the answer is: pr, issue, commit, ...] or [],
   "queries": [1..3 search texts: the question's content words, a rewording, identifiers it names; [] for a list with no topic],
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
- "intent": "one" when a single document answers ("find the PR that fixed X", "which issue reported Y"); "list" when the asker wants everything that
  matches, usually newest first ("anything by me this year", "my PRs about the optimizer", "open issues about pattern matching reported recently");
  "topic" for an ordinary question or keyword search ("where is eta expansion handled", "typedApply").
- "criterion": one sentence: what a document must be to answer the question, e.g. "the pull request that fixes <the problem>".
- "kinds": what the answer is, from {kinds}; [] when any kind will do ("anything", "everything", no kind named). "the PR that ..." is ["pr"]; "the issue / bug report / ticket" is ["issue"];
  "the commit" is ["commit"]; "where in the code / which class" is ["file"].
- "queries": 1 to 3 search texts. The first: the question's technical content without filler ("find the PR that fixed"). Others: a rewording with the
  terms a developer would use in a title, or identifiers, error messages or flags the question names. Short, no dates, no names of people.
  For a "list" with no topic ("anything by me this year") write [].
- "since", "until": a date range only if the question gives one ("in 2021" -> since 2021, until 2021; "in early 2024" -> since 2024-01, until 2024-05;
  "around 2020" -> since 2019, until 2021; "after 2.13.0" is not a date: leave it out). Format YYYY, YYYY-MM or YYYY-MM-DD. For relative times write
  the phrase itself as "since" and omit "until": "recent" (recently, lately, new), "this year", "last year", "this month", "last month", "this week",
  "last week"; the code works out the dates.
- "authors": only GitHub logins or names of people written in the question ("by <login>"); ["me"] when the asker means their own work ("by me",
  "my PRs", "what did I change"); else omit.
- "open_only": true only if the question asks for open / unresolved / not yet fixed items."""


def relative(phrase, today):
    """(since, until) for a relative time phrase, or None. Weeks are the last 7 days and the 7 before; `until` None means up to now."""
    month = lambda d: d.strftime("%Y-%m")
    prev = today.replace(day=1) - datetime.timedelta(days=1)
    return {"recent": ((today - datetime.timedelta(days=RECENT_DAYS)).isoformat(), None),
            "this year": (str(today.year), None), "last year": (str(today.year - 1), str(today.year - 1)),
            "this month": (month(today), None), "last month": (month(prev), month(prev)),
            "this week": ((today - datetime.timedelta(days=7)).isoformat(), None),
            "last week": ((today - datetime.timedelta(days=14)).isoformat(), (today - datetime.timedelta(days=8)).isoformat())}.get((phrase or "").strip().lower())


def _resolve(plan, today, notes):
    for key in ("since", "until"):
        r = relative(plan.get(key), today)
        if r:
            phrase = plan[key]
            plan["since"], plan["until"] = r
            if r[1] is None:
                plan.pop("until")
            notes.append(f"'{phrase}' read as {r[0]} to {r[1] or 'now'}" + (f" (the last {RECENT_DAYS} days)" if phrase == "recent" else ""))
            break
    return plan


_KIND_WORDS = re.compile(r"\b(prs?|pull requests?|issues?|bugs?|tickets?|commits?|releases?|tags?|reviews?|comments?|code|files?|classes|methods?)\b", re.I)
_WHEN = re.compile(r"\b(19|20)\d\d\b|\b(recent(ly)?|lately|new(est|ly)?|this (year|month|week)|last (year|month|week|\d+ (days|weeks|months|years)))\b", re.I)


def validate(j, today, notes, question=""):
    """(plan, problems). Authors the question does not name are dropped (a model fills the slot from its examples), and so are dates when the
    question names no year and no "recently" (the model invents a range)."""
    problems = []
    if not isinstance(j, dict):
        return None, ["the answer must be one JSON object"]
    extra = set(j) - {"intent", "criterion", "kinds", "queries", "since", "until", "authors", "open_only"}
    if extra:
        problems.append(f"unknown keys {sorted(extra)}")
    if not isinstance(j.get("criterion"), str) or len(j["criterion"].split()) < 4:
        problems.append("criterion must be a sentence")
    kinds = j.get("kinds") or []
    if not isinstance(kinds, list) or any(k not in KINDS for k in kinds):
        problems.append(f"kinds must be a list from {KINDS}")
    intent = j.get("intent", "one")
    if intent not in ("one", "list", "topic"):
        problems.append('intent must be "one", "list" or "topic"')
    qs = j.get("queries")
    lo = 0 if intent == "list" else 1
    if not isinstance(qs, list) or not lo <= len(qs) <= 3 or not all(isinstance(q, str) and q.strip() for q in qs):
        problems.append(f"queries must be a list of {lo} to 3 non-empty strings")
    j = dict(j)
    if (j.get("since") or j.get("until")) and question and not _WHEN.search(question):
        notes.append(f"dropped a date range the question does not give: {j.get('since')}..{j.get('until')}")
        j.pop("since", None); j.pop("until", None)
    j = _resolve(j, today, notes)
    if re.search(r"\b(by me|my|mine)\b", question, re.I) and "me" not in [str(a).lower() for a in j.get("authors") or []]:
        j["authors"] = [*(j.get("authors") or []), "me"]
        notes.append('"me" added: the question says by me / my / mine')
    if kinds and re.search(r"\b(anything|everything)\b", question, re.I) and not _KIND_WORDS.search(question):
        notes.append(f"any kind: the question says anything / everything (the model chose {', '.join(kinds)})")
        kinds = []
    if isinstance(j.get("authors"), list):
        mine = re.search(r"\b(me|my|mine|I)\b", question)
        named = [a for a in j["authors"] if isinstance(a, str) and (a.strip().lstrip("@").lower() in question.lower() or (a.strip().lower() == "me" and mine))]
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
    return {"intent": intent, "criterion": j["criterion"].strip(), "kinds": kinds, "queries": [q.strip() for q in qs], **(where or {}), "open_only": bool(j.get("open_only")),
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
    return {"intent": "topic", "criterion": question, "kinds": [], "queries": [question], "open_only": False, "notes": [f"planner failed ({'; '.join(problems)}): searching the question as is"]}
