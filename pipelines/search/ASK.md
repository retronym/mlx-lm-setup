# Ask: an agentic query layer over the search index

## Problem

`search` takes a query and a handful of filters and returns ranked passages. Questions people actually have are looser and need judgement: "the PR that fixed the eta-expansion crash, recently", "open issues that look like this one", "who touched this area lately". Answering them takes translating the question into filters, running more than one query, following links, and *deciding* whether a candidate really is what was asked for. Ranking says "on topic"; it cannot say "this is the one".

## Key decisions

1. **Plan, retrieve, gate, decide whether to continue.** A planner turns the question into structured queries (text plus filters); the existing search runs them; a gate judges each candidate document against the question's criterion; a controller decides to stop, reformulate, widen or follow links. The gate carries the precision, retrieval carries the recall (a gate only removes false positives, so a document no query retrieves cannot be recovered; hence reformulation and link-following).
2. **The gate reads the document, not the chunk.** A chunk of a PR rarely shows that the PR fixes the thing. A candidate is judged on a *view*: title, state, author, date, the head of its document, and for a PR the issues it closes (the symptom is in the issue, the PR describes the change).
3. **Two-tier gate, the second tier listwise.** Tier 1: `decide` (yes/no with a probability) on every candidate's view, about 0.1 s each, used to rank. Tier 2: the best ten (by tier 1 and retrieval) are shown *together* to the LLM, which picks the one that answers, others that equally do, or none, with a quote that must be in the chosen view verbatim. Revised after measuring (below): judged one at a time, Qwen3-Coder said yes to a third of the wrong PRs ("matches perfectly"); compared side by side it picks the right one. The quote is checked for presence, not with NLI: "this is the document asked for" is a relevance judgement, not a claim the text entails.
4. **The planner is `iterate` with a schema gate.** Output is JSON validated against the search parameters (unknown keys fail). Relative dates ("recently", "last release") are resolved by code to concrete bounds, with the default stated and echoed in the result, never silent. Every filtered query is accompanied by one unfiltered query, so a wrong filter cannot hide the answer.
5. **The loop is bounded and owned by the controller, not the model.** A fixed set of routes per round, one listwise choice per round, at most two rounds; the second (queries reworded by the LLM from what was rejected, dates and authors dropped) runs only when the choice is "none". The model proposes queries; the controller decides what runs and when to stop.
6. **The graph is a route, taken by the controller.** For "the PR that fixed X" the issues a search finds lead to the PRs that close them (`closed_by` in each hit's links), which is how most symptom questions are answered. The controller takes this route itself whenever the answer is a PR, rather than leaving it to the model.
7. **One tool, not a replacement.** `ask(question, budget)` returns confirmed hits with refs, the evidence quote and the probability for each, plus a trace of queries, filters applied and gate verdicts (also shown on the page). A caller such as Claude can still drive `search` / `get` / `links` itself; the service earns its place for unattended use (saved watches in the refresh), the web page's single answer box, and keeping the work on local models.
8. **Local models, and an honest "not confirmed".** Planner and gate are bounded, checkable jobs, which suits the local models (Qwen3-Coder for both; the 35B model was no better on the gate). Answers the LLM did not pick come back unconfirmed (`verdict` null if never shown, `no` if shown and not picked) rather than guessed, and the caller can judge them.
9. **Every round searches the question as asked, and once with no filters.** The planner's queries are terse and lose specifics that the embedding would have matched, and the planner sometimes sets filters the question does not support. Two cheap searches make both mistakes recoverable. Mechanical guards on the plan: authors the question does not name and dates when it names no year or "recently" are dropped (noted in the plan).
10. **No reranker inside the loop.** With an LLM resident, a reranked search took 2-8 s instead of 0.4 s, and tier 1 plus the listwise choice do its job.

## Measurements (2026-10-06)

Two sets of 30 questions (`eval_ask.py`, from GitHub's closing references, so the PR that fixed the issue is ground truth), written by Qwen3-Coder: **fix** from the PR's own title and description ("Find the PR that ..."; easy for search: the question paraphrases the document) and **symptom** from the issue it closes (the question describes what went wrong, the PR describes the change). Half carry a time hint ("around 2021"). Small sets: a difference of 0.07 is two questions.

| | fix @1 | fix @5 | symptom @1 | symptom @5 |
|---|---|---|---|---|
| plain search (question as query, reranked) | .53 | .77 | .07 | .43 |
| search, `kinds=pr` | .80 | 1.00 | .37 | .53 |
| search, `kinds=pr`, year +-1 | .87 | 1.00 | .37 | .50 |
| ask, 6 shown, reranked routes | | | .47 | .67 |
| ask, 10 shown, no rerank | .67 | .90 | .50 | .77 |
| ask, + question as asked, date guard (current) | .73 | 1.00 | .53 | .73 |

The gate alone (the PR and four other PRs from a `kinds=pr` search, symptom set): tier 1 ranks the PR above a same-question negative in 81% of pairs, its argmax picks it in 57%; one-at-a-time LLM verdicts: yes on 80% of the PRs and on 36% of the others; listwise: Qwen3-Coder picks the PR in 77% (3 s), Qwen3.6-35B-A3B in 80% (5 s). Most listwise "errors" are defensible: a fix for the same symptom in the other compiler, a later fix in the same area, the PR that superseded the recorded one. The time is now about 16 s a question (median): plan 1.5, searches 3, tier 1 4 (views: a `get` and a `links` call per candidate), choice 4.

What the numbers say: for a question that paraphrases its answer, the planner's filters are the whole gain, and the listwise choice costs a little at rank 1 (.73 against .87; in most of the misses the recorded PR is under `also` and the pick is a sibling, such as the PR that superseded it); for a question that describes the problem rather than the change, the issue-to-fix route and the choice are what work. The choice never says "none" on these sets (every question has an answer in the index), so the second round has not been exercised.

## Prior art in this repo

`iterate` (retry until gates pass, including an NLI faithfulness gate), `decide`, `entail`, the thread summaries and digest overview (`enrich.py`, `digest.py`), `links` / `get`, the reranker cache (`cache.py`), `eval_links.py` (an evaluation set built from GitHub's own closing references).

## Risks

- **Latency.** About 20 candidates per round with full threads is tens of seconds on local models. The two-tier gate, the verdict cache and parallel candidates keep it tolerable; the trace shows where time went.
- **Bad plans** (over-narrow date, wrong kind). Mitigated by the unfiltered companion query and by reporting the applied filters.
- **No ground truth.** Without an evaluation set there is no telling whether the gate helps, so the set comes before prompt tuning (step 2).

## Steps

1. **DONE** Prerequisites: the date (`since` / `until`, on `created` and `updated`) and author filters on `search`, as `chunk_filter` conditions (this is the parked search-metadata-filters item); the same on the page and MCP `search`. Tests in `test_search`.
2. **DONE (small)** Evaluation set `eval_ask.py`: two sets of 30 "find the PR that ..." questions written by the local LLM, from the PR (`--from fix`) and from the issue it closes (`--from issue`), with the PR GitHub recorded as ground truth; baselines (`search`, with `--kinds` and `--years`), the gate alone (`gate`, `gate-list`) and the loop (`ask`), resumable with progress. **Future:** looser questions (open issues about X, what someone changed) need judged answers, not a single ground truth.
3. **DONE** The gate alone (`gate.py`): views, tier 1 `prescreen` (`decide`), tier 2 one at a time (`judge`, kept for comparison: too generous) and listwise (`choose`), measured before the loop was built on it (see Measurements).
4. **DONE** The planner (`plan.py`): JSON validated against the search parameters with retries and a fallback plan, "recently" resolved by code to a stated 180 days, invented authors and dates dropped. Tests with a fake gateway (`test_ask`).
5. **DONE** The controller (`ask.py`: routes, the listwise choice, a second round on "none", the trace and timings; CLI; `test_ask`) and the tool: MCP `ask` and `POST /api/search/ask` run the controller in a worker thread of the gateway, which it calls back over HTTP for search, documents and links, and for models (a draft gateway takes its models from the main one, `GATEWAY_URL`); the page's **Ask** button shows the plan with the code's notes, the answers with verdict, quote and the issue they were reached through, and the trace. `authors: ["me"]` is the owner configured in `search.json` `me`; the planner writes it for "my" / "I".
6. **DONE** One box on the page (not a second UI): the planner gives an `intent` (one / list / topic), relative times ("this year", "last month", "recently") are resolved by code, and "by me / my / mine" and "anything / everything" are enforced by rules when the model misses them; the page interprets natural-language text into the existing controls, shows the reading with an undo, and lists, searches or asks accordingly. Search gained listing by filters (an empty query) and `sort: "recent"`.
7. **TODO** Saved watches: a question plus a criterion, run by a refresh phase after `links`, new confirmed hits added to the digest ("new issues that look like <known regression>"). Only seen documents are re-gated (the verdict cache).

## Future work

- Per-commit symbol summaries (touched definitions from tree-sitter, not hunks) so questions about *what changed* can be answered, and `touches` edges become symbol-level.
- Expertise questions ("who knows this area") from authors of the nearest neighbours' commits, reviews and fixing PRs.
- Label and owner suggestion for a new issue from its neighbours.
