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
| `git` | `repo`, `ref`, `paths`, `exclude` (globs), `chunkers` (suffix to `scala` / `java` / `markdown` / `plain`) | files of that ref from a managed bare clone under `data/repos/`; one chunk per definition or heading section |
| `github` | `repo`, `include` (`issues`, `prs`, `comments`, `reviews`), `since` | issues and PRs (state open / merged / closed), conversation comments, inline review comments with the diff hunk; bots and `/rebuild`-style comments skipped |
| `github_releases` | `repo`, `tag_messages` | release notes per heading, plus annotated tag messages for tags without a release (*not implemented yet*) |

Every source also takes `id`, `label`, `color`, `priority` (1 highest .. 9), `enabled`, `min_interval_hours`, `max_items_per_run`. Labels and colours are what the web page shows, so adding a project needs no code change. `python config.py check` validates everything (all problems at once, with file and key path) and prints the tree; `python config.py show <universe>`.

The shipped universe is `scala-zinc` (Scala 2, Scala 3, scala-dev, Zinc, scala-asm). A project belongs to as many universes as list it; all members of a universe must use the same embedding model.

```bash
PY=/path/to/.venv-jev/bin/python                  # numpy, torch, transformers
$PY pipelines/search/config.py check
$PY pipelines/search/sync.py                      # the default universe, by priority; or: sync.py zinc | zinc/issues | scala-zinc --max-priority 3
$PY pipelines/search/embed.py                     # fill missing/stale vectors in priority order (Qwen3-Embedding-0.6B, MPS, ~80 chunks/s)
$PY pipelines/search/search.py "where is eta expansion of by-name parameters handled"
$PY pipelines/search/search.py --rerank --project zinc --source issues "incremental compilation loops"
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
- Chunking is heuristic (indentation and keywords), not a parser; one-liners under 20 characters are dropped. Tree-sitter or Scalameta would give exact definition boundaries.
- The query CLI loads the embedder (and reranker) per call, 4 s hybrid and 7 s with `--rerank`. That disappears when this becomes a gateway backend (`/v1/embeddings`, `/v1/rerank` and a `search` MCP tool).
- sbt/zinc, the SIPs and Discourse are not indexed yet, so e.g. Zinc invalidation questions only find scala/bug issues.

## Reranking (`--rerank`)

`rerank.py` scores the top 30 documents (after fusion and one-hit-per-document) with Qwen3-Reranker-0.6B, a yes/no relevance judgement per (query, text) pair. Six hand-picked queries, judged by eye: it clearly helps on "where is X implemented" (for the invokedynamic query the top 5 changed from two issues and incidental hits to `Delambdafy.mkLambdaMetaFactoryCall`, `genInvokeDynamicLambda` and `addLambdaDeserialize`) and keeps code, docs and issues together in one list for concept queries (implicit shadowing: issue, issue, Scala 3 doc, `Implicits.LocalShadower`). On duplicate-issue queries it only reshuffles an already good top 3. It can also demote a good hit (`EtaExpansion.expand` fell out of the top 5 for the eta-expansion query), so a real evaluation set is the next step before making it the default.

## In the gateway

The `scala-search` backend (adapter `search`, `gateway/backends/search_server.py`) keeps the embedder and reranker warm and reads this index (never writes it). It is exposed as:

| Surface | What |
|---|---|
| `/search` | page: sample questions, source / method / rerank / open-issues-only controls, per-hit links, keyword and vector ranks, rerank scores |
| MCP `search` | `query`, `k`, `source`, `mode`, `rerank`, `open_only`, `text_chars`; also returns what is indexed and the commit or timestamp each source was last synced to |
| `POST /api/search` | the same as JSON |
| `POST /v1/embeddings` | OpenAI-compatible; `"kind": "query"` adds the retrieval instruction used for search queries |
| `POST /api/rerank` | `{"query", "documents": [...]}` -> relevance scores |
| `GET /api/search/status` | chunks and embedded chunks per source, last sync position; reads the SQLite file, starts nothing |

The index lives at `pipelines/search/data/search.db` unless the catalog sets `db`. Run `sync.py` and `embed.py` (they can run while the gateway is up; the backend reloads its vector matrix when the file changes), then no restart is needed.

## Progress dashboard

```bash
python3 pipelines/search/dashboard.py        # http://127.0.0.1:8767/  (stdlib only, read-only on the index)
```

One card per source with a "synced" bar (git sources: files indexed of files in the tree; GitHub sources: where each stream's cursor is between its start date and now, per stream) and an "embedded" bar (chunks that have a current vector), plus an overall bar with the embedding rate and ETA, running sync / embed indicators, and the latest log lines.

Backfilling older history is `sync.py bug --since 2000-01-01T00:00:00Z` (then `embed.py`); it is idempotent, so a re-run only costs the API requests.
