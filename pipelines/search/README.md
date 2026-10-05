# Semantic search over Scala compiler and build sources

Projects are indexed independently, one SQLite file each (chunks, an FTS5 keyword index, embeddings), and composed into **universes** that are searched together: BM25 and vector hits from every member database are merged by score, fused by reciprocal rank, and optionally reranked. Everything runs locally; the only network use is `git` and `gh api` for syncing and the one-time model download. See [PLAN.md](PLAN.md) for the design decisions and what is still to do.

## Configuration (JSON)

```
config/search.json          global: data dirs, models, chunking, GitHub politeness, refresh tiers, local-model steps
config/projects/<id>.json   a project: title and sources
config/universes/<id>.json  a universe: a named list of projects
```

| Source type | Fields | What it indexes |
|---|---|---|
| `git` | `repo`, `ref`, `paths`, `exclude` (globs), `chunkers` (suffix to `scala_ts` / `java_ts` (tree-sitter, [below](#chunking-code)), `scala` / `java` (heuristic, kept for fallback and comparison) or `markdown`; `plain` not yet) | files of that ref from a managed bare clone under `data/repos/`; one chunk per definition or heading section |
| `git_log` | `repo`, `ref`, `paths` (optional), `since`, `merges` (default false), `skip_authors` (default: dependency-bump bots) | commit messages, one chunk per commit: the full message plus the paths it changed, with `#123` / `scala/bug#123` references in the metadata. History is walked newest-first from the ref under `max_items_per_run` (a forward walk first picks up new commits), like the GitHub sources; merges and bots are left out; a rewritten branch is re-walked |
| `github` | `repo`, `include` (`issues`, `prs`, `comments`, `reviews`), `since` | issues and PRs (state open / merged / closed), conversation comments, inline review comments with the diff hunk; bots and `/rebuild`-style comments skipped. All the sources of a project on one repo share one pass over its streams; two sources may not index the same kind of item (the config says so) |
| `github_releases` | `repo`, `tag_messages` | GitHub release notes (a header chunk, then one chunk per heading for long notes; `#123` and `/pull/123` references go into the chunk metadata) plus annotated tag messages for tags without a release |

Every source also takes `id`, `label`, `color`, `priority` (1 highest .. 9), `enabled`, `min_interval_hours`, `max_items_per_run`. Labels and colours are what the web page shows, so adding a project needs no code change. `python config.py check` validates everything (all problems at once, with file and key path) and prints the tree; `python config.py show <universe>`.

The shipped universe is `scala-zinc` (Scala 2, Scala 3, scala-dev, Zinc, scala-asm). A project belongs to as many universes as list it; all members of a universe must use the same embedding model.

```bash
PY=/path/to/.venv-jev/bin/python                  # numpy, torch, transformers
$PY pipelines/search/config.py check
$PY pipelines/search/sync.py                      # the default universe, by priority; or: sync.py zinc | zinc/issues | scala-zinc --max-priority 3
$PY pipelines/search/embed.py                     # fill missing/stale vectors in priority order (Qwen3-Embedding-0.6B, MPS, ~80 chunks/s)
$PY pipelines/search/search.py "where is eta expansion of by-name parameters handled"
$PY pipelines/search/search.py --rerank --project zinc --source issues "incremental compilation loops"
$PY pipelines/search/search.py --kind commit --kind release "trait extraHash"   # only commit messages and release notes, from every source
$PY pipelines/search/migrate.py --old data/search.db   # one-off: split the legacy single index into per-project databases, no re-embedding
$PY pipelines/search/test_config.py; $PY pipelines/search/test_search.py; $PY pipelines/search/test_github.py; $PY pipelines/search/test_lifecycle.py
```

`sync.py` skips disabled sources and sources synced less than `min_interval_hours` ago (`--force` overrides), runs a failing source's error to the end of the run, and takes `--since`, `--limit`, `--no-fetch`, `--reconcile` (drop issues and PRs deleted upstream).

## Lifecycle: why re-indexing is cheap

- **Sync and embed are separate passes.** Sync diffs chunk content hashes (`store.apply`): new → insert, changed → update, missing → delete, same → skip. Embedding fills whatever has no vector for the current model or whose hash moved on. Keyword search works before any model exists; a model change is just a refill (vectors carry their model name).
- **Chunk identity is stable under edits elsewhere**: `path:Enclosing.name#n`, `issue:N`, `comment:ID`. Editing one method changes one chunk; adding a method leaves the others' ids alone.
- Measured on this machine: initial sync of scala/scala (37k chunks) 18 s, scala/bug since 2020 (2.8k issues, 8k comments) 75 s, an incremental issue sync 1.5 s; embedding is the only slow step (~17 min for everything, once).

## Known gaps (PoC)

- A rename re-embeds the moved file's chunks (vectors are keyed by chunk id, not content hash; keying by hash would reuse them).
- Issue deletions and transfers are only caught by `sync.py reconcile`. Comment deletions are not caught at all.
- The issue backfill starts at 2023-01-01 unless `--since` says otherwise; GitHub PRs, Discourse and the SIPs repository are not wired in (each is an adapter that yields chunks and is written like `ghissues.py`).
- Markdown chunking is heuristic (headings), and the `plain` chunker does not exist. Code is chunked by tree-sitter ([below](#chunking-code)).
- The query CLI loads the embedder (and reranker) per call, 4 s hybrid and 7 s with `--rerank`. That disappears when this becomes a gateway backend (`/v1/embeddings`, `/v1/rerank` and a `search` MCP tool).
- sbt/zinc, the SIPs and Discourse are not indexed yet, so e.g. Zinc invalidation questions only find scala/bug issues.

## Chunking code

`scala_ts` and `java_ts` (`sources/treesitter.py`) cut code along the parse tree. The heuristic chunkers they replace started a chunk at any line that looked like a definition at indent <= 4: half the chunks were under 100 characters (a lone `val`, a one-line `def`), 14-21% were *local* definitions split out of their method under a wrong parent title, members indented deeper were swallowed into their neighbour, and a long method was cut at blank lines into continuations that no longer said what they belonged to. Now:

- a declaration with its doc comment and annotations is one unit; a class, object, trait, enum, given, extension or interface that fits `chunking.max_chars` (2400) stays whole, whatever its members;
- one that does not becomes a **header** chunk (doc, signature, a `// members:` outline) plus its members, recursively, each titled `path  package  Enclosing.name`; local definitions are never split out;
- a method that does not fit (up to 1.5x `max_chars` it stays whole) is cut **between statements** (or `case` clauses), and each continuation starts with the method's signature;
- runs of small siblings (vals, one-liners, imports) are **packed** into one chunk up to `chunking.pack_chars` (1000), titled with the first member and `(+n more)`;
- metadata gains `end` (last line) and `sym` (class, function, imports, members...); a file the parser reports errors for falls back to the heuristic chunker and says `fallback` in its metadata (none of the ~3,400 files of the five code sources does).

Measured on the real sources: chunks 2.4-3x fewer (scala/scala 36.7k -> 12.8k, Scala 3 45.8k -> 17.1k), median 123 -> 720 characters, under 100 characters 41% -> 6%, the same text overall.

**Changing a source's chunker is a controlled move**, because it re-embeds that source (chunks get new ids and hashes):

```bash
$PY pipelines/search/rechunk.py zinc/code --sample 8      # the plan, touching nothing: chunk counts and sizes both ways, parse fallbacks, embedding time
$PY pipelines/search/eval_code.py build zinc/code          # once: 150 queries "first sentence of a definition's doc comment -> that definition"
$PY pipelines/search/eval_code.py run data/eval/zinc-code.json --label before [--mask]    # --mask removes the definition's name from the query
# set "chunkers": {".scala": "scala_ts", ".java": "java_ts"} in config/projects/zinc.json, then
$PY pipelines/search/sync.py zinc/code --no-fetch && $PY pipelines/search/embed.py zinc/code
$PY pipelines/search/eval_code.py run data/eval/zinc-code.json --label after [--mask]    # compare recall@1/5/10 and MRR with the stored before run
```

Sync re-chunks each file once (the file's state carries the chunker's version) and replaces its chunks; keyword search works throughout and the old vectors go with the old chunks. Embedding is the only slow step (`rechunk.py` prints the estimate). The eval is a regression guard more than a measure of gain: a doc comment is part of the chunk, so it is a lexical-ish known-item test. Results of the move on this machine (150 queries per source, hybrid, no rerank; plain / name masked):

| Source | Chunks | recall@1 | recall@5 | MRR |
|---|---|---|---|---|
| zinc | 6.8k -> 2.3k | .807 -> .787 / .780 -> .760 | .880 -> .900 / .880 -> .887 | .841 -> .836 / .827 -> .817 |
| scala-asm | 3.1k -> 1.0k | .887 -> .873 / .853 -> .833 | .920 -> .933 / .900 -> .907 | .902 -> .902 / .875 -> .867 |
| scala3 | 45.8k -> 17.0k | .847 -> .813 / .800 -> .773 | .920 -> .927 / .880 -> .893 | .879 -> .863 / .837 -> .826 |
| scala2 | 36.7k -> 12.7k | .867 -> .813 / .833 -> .793 | .920 -> .907 / .900 -> .893 | .891 -> .857 / .862 -> .838 |

Recall@5 is level (-0.013 to +0.020); recall@1 gives up 0.014-0.054, because a chunk now holds a whole class or a run of members, so a query for one method competes with its neighbours' words. scala2 is the one source that fell outside the 0.03 gate (MRR -0.034). The test favours small chunks (the query is text inside the chunk), and it was run without the reranker, which reorders the top 30 and is on by default.

## Duplicates, clusters and outliers

`neighbours.py` (the refresh's `neighbours` phase, or `neighbours.py [universe] [--force]` on its own) turns the issue and PR vectors that are already in the indexes into two things, written to `data/neighbours/<universe>.db` and read by the gateway: each item's closest matches (`neighbours`, default 5, kept at cosine >= `min_similarity`, default 0.8), spherical k-means topic clusters (`clusters`, default 80, at most one per ten items) named by their most distinctive title words, and two scores per item for the outliers view: `iso`, the cosine to the nearest other item, and `ctr`, the cosine to the centre of its own cluster. Nothing here is a model: the vectors are the search index's own, so a duplicate view costs a file read. One run serves every filter, because filters are applied when reading.

The **Duplicates**, **Clusters** and **Outliers** tabs on `/search` (and `GET /api/search/duplicates`, `/api/search/clusters`, `/api/search/outliers`) filter by state, kind, creation date and repo:

| Filter | Duplicates (pairs) | Clusters (items) |
|---|---|---|
| `state` | `open`: at least one of the pair is open; `closed`: neither is | `open`; `closed` (closed or merged) |
| `kind` | `issue` or `pr` (both are), `mixed` (an issue and a PR: the fix probably exists), `any` | `issue`, `pr`, `any` |
| `since`, `until` (`YYYY[-MM[-DD]]`) | kept if EITHER item was created in range | per item |
| `projects` | kept if EITHER item is in one | per item |
| `min_sim` | 0.95: nearly always the same text; 0.90: real duplicates and follow-ups; below 0.90: related, not duplicate | |
| `adjacent` (default 1), `templated` | below | |

Two kinds of pair are hidden by default because they are artifacts, not duplicates. An old import created copies of tickets under adjacent numbers, so a same-repo pair whose numbers differ by at most `adjacent` is dropped when at least one of them is closed (open and open stays: it may be a real duplicate; `adjacent=0` shows them all). Release procedures, `Release 2.13.x`, dependency bumps, dummy tickets and `(Issue was deleted)` placeholders look alike by construction (`templated=1` keeps them).

For clusters, `trend` is the share of a cluster's items created in the last two years divided by the same share over everything matching the filters, so above 1 the topic is heating up (shown from 20 items). Expanding a cluster lists its items newest first.

**Outliers** lists the issues and PRs that are far from everything else, lowest score first, ordered by `iso` (most isolated) or `ctr` (furthest from its own cluster; `by=iso|ctr`), with the same per-item filters as clusters. Dependency-bump PRs and release procedures are hidden by default (`templated=1` keeps them): they are unlike anything else and uninteresting. What it finds in practice: off-topic questions that are not compiler bugs (already closed), leftovers from the old Trac tracker, and, among the open items, real but unusual tickets and design questions. A database written before the scores existed is recomputed on the next run (`version` in its `meta` table).

## How hits are ranked, and why

BM25 and vector lists (each merged across projects by score) are fused by **reciprocal rank fusion** with a **top-rank bonus**: being first in any list adds 0.05, second or third 0.02 (`fusion.top_bonus`; a first place is only worth 0.016 by RRF alone), so an exact keyword hit such as `trait extraHash` is not diluted by fuzzy vector neighbours. When reranking, the order is a **position-aware blend**, not the reranker alone: `final = w * fused + (1 - w) * rerank`, with the fused score scaled so the best is 1 and `w` by fused rank, 0.75 for ranks 1-3, 0.6 for 4-10, 0.4 beyond (`reranker.blend`: `[[rank limit, w], ...]`; `null` = reranker alone). The 0.6B reranker's P(relevant) saturates near 1 for anything on topic, so on its own it reshuffles good hits (it demoted `EtaExpansion.expand` out of the top 5); blended, the retrieval order can only be overturned by a large reranker margin, and a deep hit with a clear reranker win still rises.

`--explain` (CLI), `"explain": true` (`POST /api/search`, MCP `search`; the page always asks and shows it as a tooltip on the scores) adds to each hit `explain` = `{fused_rank, rrf, top_bonus, retrieval, weight, final}`.

**Caches** (`search.json` `cache`): the search backend memoises reranker scores in `data/cache.db`, keyed by model, instruction, query and the document text (so an edited chunk misses by itself; least recently used entries are dropped beyond `max_entries`), and query embeddings in a small in-memory LRU. Repeated queries, the canaries and a page reload cost no model call; the response's `cache` says how many documents were served from it.

## Reading what a hit belongs to (`get`)

Every hit has a `ref` (`project/chunk id`). MCP `get` / `POST /api/search/get` take up to 20 refs and return the document: `scope` `doc` (default) is all chunks of the hit's document in reading order (an issue or PR with every comment and review, a doc file by section, a commit), `chunk` just the hit, `file` the whole source file at the indexed commit from the managed clone (`lines` `"120-180"` for a range). Text is paged by `max_chars` / `offset` (`truncated` and `next_offset` say there is more). It reads the SQLite files and the clone, so it starts no model.

## Reranking (`--rerank`)

`rerank.py` scores the top 30 documents (after fusion and one-hit-per-document) with Qwen3-Reranker-0.6B, a yes/no relevance judgement per (query, text) pair. Six hand-picked queries, judged by eye: it clearly helps on "where is X implemented" (for the invokedynamic query the top 5 changed from two issues and incidental hits to `Delambdafy.mkLambdaMetaFactoryCall`, `genInvokeDynamicLambda` and `addLambdaDeserialize`) and keeps code, docs and issues together in one list for concept queries (implicit shadowing: issue, issue, Scala 3 doc, `Implicits.LocalShadower`). On duplicate-issue queries it only reshuffles an already good top 3. It can also demote a good hit (`EtaExpansion.expand` fell out of the top 5 for the eta-expansion query), so a real evaluation set is the next step before making it the default.

## In the gateway

The `scala-search` backend (adapter `search`, `gateway/backends/search_server.py`) keeps the embedder and reranker warm and reads this index (never writes it). It is exposed as:

| Surface | What |
|---|---|
| `/search` | page: sample questions, source / kind / method / rerank / open-issues-only controls, per-hit links, keyword and vector ranks, rerank scores |
| MCP `get`, `POST /api/search/get` | the document behind a hit's `ref`: thread, file or lines of it (below) |
| MCP `search` | `query`, `k` (default 20), `universe`, `projects`, `sources`, `kinds`, `mode`, `rerank`, `open_only`, `explain`, `text_chars`; also returns what is indexed and the commit or timestamp each source was last synced to |
| `POST /api/search` | the same as JSON |
| MCP `search_universes` | universes, their projects and sources, and the kinds of hit each source holds |
| `POST /v1/embeddings` | OpenAI-compatible; `"kind": "query"` adds the retrieval instruction used for search queries |
| `POST /api/rerank` | `{"query", "documents": [...]}` -> relevance scores |
| `GET /api/search/duplicates`, `/api/search/clusters`, `/api/search/outliers` | the Duplicates, Clusters and Outliers tabs: filtered reads of the neighbours database (below) |
| `GET /api/search/status` | chunks and embedded chunks per source, last sync position; reads the SQLite file, starts nothing |

The index lives at `pipelines/search/data/search.db` unless the catalog sets `db`. Run `sync.py` and `embed.py` (they can run while the gateway is up; the backend reloads its vector matrix when the file changes), then no restart is needed.

## Kinds

A hit's `kind` is finer than its source and cuts across sources: `file` (a chunk of a file in a git tree: code, docs, spec), `issue`, `pr`, `comment`, `review` (an inline review comment with its diff hunk), `summary` (an LLM summary of a long thread), `commit`, `release`, `tag` (an annotated tag's message). `kinds` filters on it before ranking, in both the keyword and vector passes, so the top-k is the best k of those kinds rather than whatever survived a cut. Each source declares the kinds it writes (`search_universes`), and the page offers only the kinds the universe holds.

## Who and when

Search results carry the author's GitHub handle (`author`, a login; commits also have `author_name`, and the handle is taken from a `users.noreply.github.com` email when there is one), `created` and `updated` times, and, for a comment or review, the `thread` it belongs to and who opened it. The page shows them as "opened by @x 2 y ago · updated 11 mo ago", "@y commented 2.9 y ago · on PR #12 opened by @x 3 y ago", "committed by @z 5 mo ago", "released 3 mo ago" (exact dates in the tooltips). Older chunks fall back sensibly (a comment's author from its title, a commit's git name).

Chunks indexed before these fields existed get them with `sync.py <targets> --meta-only`: it re-reads the part of history that is indexed (the backfill frontier up to now), refreshes the metadata of chunks that already exist and ingests nothing new, moves no cursor and re-embeds nothing (a text edit made upstream since the last sync is also picked up and embedded next time). GitHub sources cost one API request per 100 items; commits are local.

## Watching progress

The **Index status** tab on `/search` (`#status`) shows, per project and source, how much is synced (git sources: files indexed of files in the tree; GitHub sources: how far back the newest-first backfill has come) and how much is embedded, plus the indexer's current phase and source, rate and ETA, the last refresh phase by phase, and the latest digest (rendered as markdown: the digest is model-written text containing GitHub titles, so it goes through marked and DOMPurify, never straight into the page). The Home page's Search panel carries a one-line version (chunks, projects, % embedded, what the indexer is doing). Both read the index files and `data/run.json` read-only through `GET /api/search/status`, so they cost no model. From a worktree, `draft.sh` serves them on their own port.

Backfilling older history is `sync.py bug --since 2000-01-01T00:00:00Z` (then `embed.py`); it is idempotent, so a re-run only costs the API requests.

## GitHub sync: priorities, caps and the two cursors

Each stream (issues and PRs, comments, review comments) of a repo has a **forward cursor** (newest item ingested: every run first walks from there, so new and edited items arrive at once) and a **backfill frontier** that fills history newest-first in time windows until the source's `since` horizon. `max_items_per_run` caps the backfill per run (the current window always finishes), so a huge low-priority tracker such as scala/scala3 (priority 8) drains over several runs with the recent past first and never starves the rest. Widening a source's `since` re-opens its backfill. The page's "synced" bar for a GitHub source is how far back the frontier has come. Pages are fetched politely: the rate limit is checked every 25 pages (sleeping to the reset below `github.min_remaining`), a 403/429 is waited out, and long lists are re-anchored before GitHub's 10,000-item `page=` cap.

## Where data lives, and moving it between checkouts

Everything generated is under `pipelines/search/data/` (git-ignored) **of the checkout you run in**: `projects/<id>/index.db`, `repos/` (managed bare clones) and `run.json` (what the indexer is doing). A worktree therefore has its own index and never touches the one a running service reads from the main checkout. `SEARCH_DATA_DIR=/some/dir` overrides the location (relative paths are relative to `pipelines/search`).

```bash
pipelines/search/draft.sh start          # a draft gateway for THIS checkout on :8091 (search backend only; venvs come from the main checkout): http://127.0.0.1:8091/search#status
pipelines/search/promote_data.sh <worktree> <main-checkout>     # copy indexes and clones across with APFS clones (instant, no extra disk); --only zinc,scala-asm; --force replaces (old kept as .bak)
```

After merging to `main`: `promote_data.sh` from the worktree into the main checkout, then `service/service.sh restart`. The main checkout's legacy `data/search.db` is no longer read once the service runs the new code and can be deleted after you have checked the new indexes.

## Refresh: one command, local models throughout

```bash
pipelines/search/refresh.sh                 # everything, in priority order (needs the gateway: embeddings, LLM and NLI come from it)
pipelines/search/refresh.sh --tier high     # only sources with priority <= 3 (tiers are in search.json refresh.tiers)
pipelines/search/refresh.sh --dry-run       # the plan: which phases run and why, which sources in what order, caps and intervals
pipelines/search/refresh.sh --only embed,verify --budget-hours 2 --local     # pick phases, stop starting new work after 2 h, embed in-process
service/search-refresh.sh install           # a nightly launchd job (search.json refresh.at, default 03:00; low CPU and I/O priority; caffeinate)
```

| Phase | What | Local model |
|---|---|---|
| `sync` | fetch the managed clones and sync every source in priority order; GitHub: forward walk, then the capped newest-first backfill | none |
| `reconcile` | drop issues and PRs deleted upstream (weekly, `refresh.reconcile_every_days`) | none |
| `enrich` | for long threads (>= `min_comments` comments), a summary chunk written by `llm.thread_summaries.model`, **checked sentence by sentence against the thread by the NLI model** and retried with the problems; unfaithful summaries are not indexed. Newest threads first, `max_per_run` per run, re-done when a thread has grown by half. Off by default | Qwen3-Coder + OpenJev NLI |
| `digest` | what changed since the last refresh: exact bullets rendered from the indexes, plus a short LLM overview checked against them (unsupported lines dropped, the overview left out if nothing faithful remains). Shown on the Index status tab | Qwen3-Coder + OpenJev NLI |
| `embed` | vectors for everything new, through the gateway's `/v1/embeddings` (one managed copy of the model, started and stopped by the gateway's memory manager; `--local` runs it in-process) | Qwen3-Embedding |
| `neighbours` | duplicate candidates and topic clusters over issues and PRs, from the vectors already in the indexes (numpy, no model, about 10 s for 20k items; skipped when nothing changed; `neighbours.enabled`). See [Duplicates and clusters](#duplicates-and-clusters) | none |
| `verify` | database integrity, nothing left without a vector, and the canary queries (`config/canaries.json`: a query passes when an expected string is in the title or URL of the top `k` results) through the gateway, which also proves the search backend end to end | the search backend |

The LLM phases run before `embed` so the large LLM and the embedder do not evict each other from the gateway's memory budget. `--budget-hours` (or `refresh.budget_hours`) stops starting new sources, LLM items and embedding work after that long; the rest comes first next time. One indexer run at a time (a lock shared with `sync.py` and `embed.py`; a second one exits with status 3). Progress is `data/run.json` (what the Index status tab shows live: phase chips, current source, rate), history is `data/refresh.json`, digests are `data/digest.json` and `data/digests/<universe>/`. Exit status: 0 ok, 1 something failed, 3 busy.

Why the NLI model checks prose but not the digest bullets: it is good at "is this sentence supported by that text" and weak on lists of identifiers and numbers, so the bullets are rendered from the data (exact by construction) and only the overview is model-written. `noise_filter` (rules, then the decision model for borderline comments) is configured but not wired yet: it waits for the evaluation set to show it helps.
