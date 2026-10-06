# Dashboard tab (design, not started)

A tab on `/search` that builds a dashboard for the selected projects and a time range: what happened (activity), and which open PRs need a decision (triage). It is generated on request from the indexes, in layers: the first paint is plain SQL and takes well under a second, model-written parts arrive as they finish.

## Why

The refresh digest ([digest.py](digest.py)) answers "what changed since the last refresh" for one universe, once a night. The PR queue artifact for scala/scala answers "which PRs need me", but it is a snapshot someone has to refresh, its labels come from Claude, and it covers one repo. Everything it shows is either already in the indexes (titles, bodies, comments, reviews, authors, dates, links, vectors) or one cheap GitHub call away. A tab that computes it on demand, for any project selection and range, over local models, makes it free to keep and lets the same page answer "what happened in zinc last quarter".

## Key decisions

1. **Layered, progressive.** Three layers, each rendered when it arrives:
   - *Facts* (SQL, <1 s, no models): activity counts and time series, top authors, merged/opened/closed, releases and tags, most-discussed threads, commit volume, open-PR table with staleness.
   - *Cheap model passes* (seconds, cached): per-PR and per-thread labels from a small model (`decide`, typed output, no generation), and the cluster/topic of each item from the vectors we already have.
   - *Written parts* (tens of seconds, cached, faithfulness-checked): the range overview, a one-line summary and a suggested next action per open PR. Same `llm.grounded` machinery as the digest and thread summaries, so a sentence the source does not support is dropped.
2. **Range is capped at 1 year**, default 30 days; the API refuses more (`since` older than 366 days) rather than quietly truncating. Presets: 7d, 30d, 90d, 1y, and custom within the cap. A year of scala2+scala3 is about 20k chunks of activity, still a one-query job; what the cap really protects is the model layers, which are bounded by *items in range*, not by range, so each has its own item cap (see below).
3. **Open PRs are always "now", not "in range".** Triage is about what is open today, whatever its age; the range only sets the activity side and highlights PRs touched within it. (An open PR from 2024 matters on a 30-day dashboard.)
4. **Triage needs state the index does not have** (CI, mergeability, draft, requested reviewers, review decisions, base, milestone, diff size). First version: **stored by the nightly refresh**, for open PRs only: `sources/ghprstate.py` fetches them with GraphQL (50 per request, about 4 calls for all five repos) into the PR chunk's metadata as `pr_state`, like `ghlinks` does for closing references, so the page reads only the indexes and works offline, at the cost of being as old as the last refresh (shown on the page, with the age of each PR's state). A live fetch at dashboard time (cached 15 minutes) is the likely next step and would be an addition, not a rewrite: the page already treats `pr_state` as optional per PR.
5. **Labels beyond GitHub's own** are a closed vocabulary assigned by `decide` (typed choice with probabilities, so each label has a confidence and the page can show only confident ones):
   - *Kind*: bugfix, feature, perf, refactor, docs, build/CI, deps bump, release/backport, experiment/WIP.
   - *Risk*: touches public API / binary compatibility, language semantics (typer, implicits), backend, or is local.
   - *Readiness*: ready to merge, needs author, needs review, needs decision (design question), stale/abandoned, close candidate.
   - *Reason* is the sentence that justifies the label, from the written layer.

   Readiness is the one that matters. It is a rule-first, model-second call: rules read the live state (CI failing, conflicts, approved, draft, days since last activity, who spoke last) and the model only breaks ties and reads the thread for things rules cannot see ("author says they will rebase", "maintainer asked to close"). Rule-derived labels are marked *auto*, as in the artifact.
5b. **Duplicates and neighbours for free**: the neighbours database already knows each PR's nearest issues and PRs. A PR with a close open twin (the duplicates tab) or that links an issue (the links database) gets a chip, and an issue-less perf PR shows the issue it probably fixes. No new model work.
6. **Cache by content hash, as everywhere else.** A PR's *summary* is keyed by `(title, body, head commit)` and its *readiness/next action* by `(activity hash: latest review/comment/CI/conflict state)`, so only PRs that changed are re-assessed; stored in `cache.db` (existing cache module) with the model name. A re-opened dashboard is instant; a morning refresh could pre-warm the open-PR layer (future).
7. **Budgets are explicit.** The page shows what is still running and what it cost. Item caps: summaries for the 40 most relevant open PRs first (touched in range, requested from `me`, or stalest-but-reviewable), the rest on a "summarise more" click; thread summaries for the 10 most-discussed threads in range. Model calls go through the gateway like the indexer's, one at a time, behind the queue the Ask tab uses.

## Surface

**API** (gateway, read-only over the project databases, same style as `/api/search/clusters`):

- `GET /api/search/dashboard?universe&projects&since&until` returns the facts layer as JSON, immediately: `{range, totals, series (per day or week), by_project, authors, releases, hot_threads, open_prs: [{repo, number, title, author, created, updated, labels, linked issues, neighbours, rule labels...}]}`, plus a `job` id for the layers that are still being computed.
- `GET /api/search/dashboard/live?repos=...` the live GitHub fields for open PRs (merge into the table client-side; separate so a slow `gh` never holds up the first paint).
- `GET /api/search/dashboard/job?id` returns what the model layers have produced so far (labels, summaries, overview) and whether more is coming; the page polls it every few seconds, like the Index status tab. Results are written into the cache as they complete, so a reload does not start over.

A module `pipelines/search/dashboard.py` does the work, and `gateway/searchinfo.py` exposes it, same split as `neighbours.py`/`searchinfo.clusters`.

**Page** (a new `Dashboard` tab, project picks shared with the other tabs):

1. Header: range presets, project multi-select, "me" selector (remembered, as in the artifact).
2. Activity strip: opened / merged / closed per day, release markers, and a one-paragraph overview that fills in when written (faithfulness-checked, otherwise omitted, as the digest does).
3. Lanes as filters (counts over the open PRs): ready to merge, needs review, needs author, needs decision, close candidates, CI failing, conflicts, drafts; "my move".
4. Staleness strip on a log time axis, one dot per open PR (the artifact's best idea; keep it).
5. The PR table: one dense row per PR with state, flags, size, age, labels, and the summary and suggested action filling in as they arrive (a quiet placeholder, not a spinner per row).
6. Below: hot threads in range with their thread summaries, topic clusters of the range's activity (from the vectors, labelled by representative titles), and authors.

## Steps

Each step compiles and is committed on its own.

1. **Facts layer**: `dashboard.py` (SQL only), API route, tab with totals, series, releases, hot threads, an open-PR table with staleness; range cap and tests on a fixture database.
2. **Live PR state**: GraphQL fetch with caching and graceful degradation; flags, lanes and the staleness strip; the rule-based readiness label.
3. **Cheap model layer**: typed labels via `decide`, neighbour/link chips; job endpoint and progressive fill.
4. **Written layer**: per-PR summary and next action, range overview, hot-thread summaries, all `llm.grounded` and cached by content hash.
5. **Evaluate**: label agreement against the maintainer-labelled PRs already used for the NLI triage work (the 300), and timing on a 30-day and 1-year range; record in this file.

## Open questions

- **Live fetch** (decided: not in the first version, see decision 4).
- **Who is "me"**: `search.json` already has `me` for author filters; the "my move" lane would reuse it.
- **Issues** (decided): facts only for the backlog (volume, most discussed, newest, duplicate flags). New issues, those opened within the range, get judgement: the same typed `decide` pass (kind: bug, feature request, question, regression, tracking; needs-triage: missing repro or version, likely duplicate from the neighbours, looks actionable), capped per run and cached by content hash. Old open issues get none.
- **Snapshot publishing** ([PLAN.md](../../PLAN.md) future work) would not carry the live PR layer; the dashboard degrades to facts plus cached labels.
