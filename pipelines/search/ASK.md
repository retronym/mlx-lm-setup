# Ask: an agentic query layer over the search index

## Problem

`search` takes a query and a handful of filters and returns ranked passages. Questions people actually have are looser and need judgement: "the PR that fixed the eta-expansion crash, recently", "open issues that look like this one", "who touched this area lately". Answering them takes translating the question into filters, running more than one query, following links, and *deciding* whether a candidate really is what was asked for. Ranking says "on topic"; it cannot say "this is the one".

## Key decisions

1. **Plan, retrieve, gate, decide whether to continue.** A planner turns the question into structured queries (text plus filters); the existing search runs them; a gate judges each candidate document against the question's criterion; a controller decides to stop, reformulate, widen or follow links. The gate carries the precision, retrieval carries the recall (a gate only removes false positives, so a document no query retrieves cannot be recovered; hence reformulation and link-following).
2. **The gate reads the document, not the chunk.** A chunk of a PR rarely shows that the PR fixes the thing. Candidates are expanded with `get scope=doc` (thread, with comments and reviews) before judging.
3. **Two-tier gate.** `decide` (yes/no with a probability) on title plus the head of the body; only the uncertain band goes to a full-thread LLM judgement. The judgement must quote the supporting sentence, and the quote is checked against the source with `entail`; a justification that is not entailed fails the candidate (the same pattern as thread summaries). Verdicts are cached on (criterion, document hash), like reranker scores.
4. **The planner is `iterate` with a schema gate.** Output is JSON validated against the search parameters (unknown keys fail). Relative dates ("recently", "last release") are resolved by code to concrete bounds, with the default stated and echoed in the result, never silent. Every filtered query is accompanied by one unfiltered query, so a wrong filter cannot hide the answer.
5. **The loop is bounded and owned by the controller, not the model.** Budgets on queries, documents gated and wall-clock time; it stops at N confirmed hits or when a round adds no new candidates. The model proposes the next step (reformulate, follow a link, widen a date range); the controller enforces the budget and termination.
6. **The graph is a tool.** The planner may ask for `links` / `get` as steps: for "the PR that fixed bug X" the cheap, precise route is find the issue, follow `closes`, gate the PR. A reference in the query already ranks first, so many questions need one hop and no loop.
7. **One tool, not a replacement.** `ask(question, budget)` returns confirmed hits with refs, the evidence quote and the probability for each, plus a trace of queries, filters applied and gate verdicts (also shown on the page). A caller such as Claude can still drive `search` / `get` / `links` itself; the service earns its place for unattended use (saved watches in the refresh), the web page's single answer box, and keeping the work on local models.
8. **Local models, with escalation.** Planner and gate are bounded, checkable jobs, which suits the local models. When the gate cannot reach a verdict (uncertain after tier two) the candidate is returned as `maybe` rather than guessed, and the caller can judge it.

## Prior art in this repo

`iterate` (retry until gates pass, including an NLI faithfulness gate), `decide`, `entail`, the thread summaries and digest overview (`enrich.py`, `digest.py`), `links` / `get`, the reranker cache (`cache.py`), `eval_links.py` (an evaluation set built from GitHub's own closing references).

## Risks

- **Latency.** About 20 candidates per round with full threads is tens of seconds on local models. The two-tier gate, the verdict cache and parallel candidates keep it tolerable; the trace shows where time went.
- **Bad plans** (over-narrow date, wrong kind). Mitigated by the unfiltered companion query and by reporting the applied filters.
- **No ground truth.** Without an evaluation set there is no telling whether the gate helps, so the set comes before prompt tuning (step 2).

## Steps

1. **DONE** Prerequisites: the date (`since` / `until`, on `created` and `updated`) and author filters on `search`, as `chunk_filter` conditions (this is the parked search-metadata-filters item); the same on the page and MCP `search`. Tests in `test_search`.
2. **TODO** Evaluation set `eval_ask.py`: questions written from the PRs that GitHub's closing references tie to issues ("the PR that fixed <issue title>"), the expected PR as ground truth, plus a hand-written set of looser questions with a date and a state. Report recall@k of the confirmed list, precision of the gate (confirmed documents that are the expected one or a plausible alternative), and queries and seconds per question.
3. **TODO** The gate alone (`gate.py`): `(criterion, ref)` to `{verdict: yes|no|maybe, p, quote, tier}`; tier one `decide`, tier two LLM with a quoted justification checked by `entail`; verdict cache. Measured on the evaluation set's positives and sampled negatives before anything else is built on it.
4. **TODO** The planner (`plan.py`): `iterate` with the parameter schema, date resolution in code, the unfiltered companion query, a stated default for vague terms. Tests with a fake gateway, as in `test_refresh`.
5. **TODO** The controller and the tool (`ask.py`, MCP `ask`, `POST /api/search/ask`): budgets, parallel gating, link-following steps, stop rules, the trace. Page: an answer box above the search form with the trace expandable. Compare against plain `search` on the evaluation set; keep it only if it wins on the numbers.
6. **TODO** Saved watches: a question plus a criterion, run by a refresh phase after `links`, new confirmed hits added to the digest ("new issues that look like <known regression>"). Only seen documents are re-gated (the verdict cache).

## Future work

- Per-commit symbol summaries (touched definitions from tree-sitter, not hunks) so questions about *what changed* can be answered, and `touches` edges become symbol-level.
- Expertise questions ("who knows this area") from authors of the nearest neighbours' commits, reviews and fixing PRs.
- Label and owner suggestion for a new issue from its neighbours.
