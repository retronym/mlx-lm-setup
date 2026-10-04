# Semantic search over Scala compiler and build sources (PoC)

One SQLite file (`data/search.db`, git-ignored) holding chunks, an FTS5 keyword index and embeddings; hybrid retrieval (BM25 + vector, reciprocal rank fusion). Everything runs locally; the only network use is `gh api` for issues and the one-time model download.

| Source | What | Change detection |
|---|---|---|
| `scalac` | scala/scala `src/{compiler,reflect,library}` (one chunk per member-level definition, prefixed with path, package and enclosing definition) and `spec/` (per heading section) | git blob sha per file; only changed files are re-chunked, vanished files are deleted |
| `scala3docs` | Scala 3 `docs/_docs/{reference,internals}` (per heading section) | same |
| `bug` | scala/bug issues, one chunk group per issue, one chunk per comment | `since=<max updated_at>` on the issues and the repo-wide comments stream; state and labels are metadata, so closing an issue updates a row but re-embeds nothing |

```bash
PY=/path/to/.venv-jev/bin/python           # numpy, torch, transformers
$PY pipelines/search/sync.py               # scalac scala3docs bug; add `reconcile` to drop deleted issues; --since ISO8601 widens the issue backfill
$PY pipelines/search/embed.py              # fill missing/stale vectors (Qwen3-Embedding-0.6B, MPS, ~50 chunks/s); resumable
$PY pipelines/search/search.py "where is eta expansion of by-name parameters handled"
$PY pipelines/search/search.py --rerank "where does the backend decide to emit invokedynamic for lambdas"   # + Qwen3-Reranker-0.6B over the top 30 (~3 s more)
$PY pipelines/search/search.py --source bug --open "Await.result leaks callbacks"     # --bm25 / --vec to see each half
$PY pipelines/search/test_lifecycle.py     # add / edit / rename / delete on a scratch repo: only the changed chunks are touched
```

## Lifecycle: why re-indexing is cheap

- **Sync and embed are separate passes.** Sync diffs chunk content hashes (`store.apply`): new → insert, changed → update, missing → delete, same → skip. Embedding fills whatever has no vector for the current model or whose hash moved on. Keyword search works before any model exists; a model change is just a refill (vectors carry their model name).
- **Chunk identity is stable under edits elsewhere**: `path:Enclosing.name#n`, `issue:N`, `comment:ID`. Editing one method changes one chunk; adding a method leaves the others' ids alone.
- Measured on this machine: initial sync of scalac (37k chunks) 18 s, scala/bug since 2020 (2.8k issues, 8k comments) 75 s, an incremental issue sync 1.5 s; embedding is the only slow step (~17 min for everything, once).

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
