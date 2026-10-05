# Search: projects, universes, refresh

## Decisions

- **Project**: one logical project, indexed on its own into `data/projects/<id>/index.db`; has sources (git paths, GitHub issues/PRs/comments/reviews, release notes). **Universe**: a named list of projects searched together ("Scala / Zinc"). Projects know nothing about universes. All members of a universe must share the embedding model.
- All of it is JSON under `config/` (`search.json`, `projects/*.json`, `universes/*.json`), validated with every problem reported at once (`python config.py check`).
- Grouping: `scala2` = scala/scala code + spec + PRs + the scala/bug tracker + release notes. scala-asm indexes `main` (the default branch is a README stub). scala3 issues and PRs are in, at priority 8.
- **Priority** (1 highest .. 9): orders the refresh, the GitHub quota and the embedding queue; low-priority sources get per-run item caps and minimum intervals, and a two-cursor backfill (a forward cursor for new items, a backward one walking newest-first to the horizon), so big trackers drain over several nights without starving the rest.
- Release notes (`github_releases`): GitHub releases chunked per heading, plus annotated tag messages for tags without a release.
- Managed clones under `data/repos/` (fetched by the refresh), so indexing never depends on the branch checked out in a working copy.
- Local models throughout: embeddings through the gateway, reranker, rule-then-`decide` noise filter, thread summaries and the refresh digest via `iterate` with an NLI gate, LLM-written evaluation questions. Chunking, dedup and parsing stay plain code. LLM steps run before embedding so the big LLM and the embedder do not evict each other.
- Refresh nightly at 03:00 (launchd).

## Steps

1. **DONE** Config loader, schemas, `check`, shipped config for scala2, scala3, scala-dev, zinc, scala-asm and the `scala-zinc` universe.
2. **DONE** Per-project databases (`data/projects/<id>/index.db`), `migrate.py` (legacy index split without re-embedding, verified), managed bare clones (`repos.py`), config-driven `sync.py` / `embed.py` (priority order, `min_interval_hours`, `max_items_per_run`), federated search over a universe (`search.py`: scores merged across stores, per-project best hit guaranteed a place in the rerank), the GitHub adapter driven by `include`, and the gateway kept working on top (backend, `/api/search` with `universe` / `projects` / `sources`, `/api/search/status` from config + databases, MCP `search` and `search_universes`, the page's source filter, colours and index strip from config). Tests: `test_config`, `test_search`, `test_github`, `test_lifecycle`, 202 gateway tests.
   Interim, to be replaced in step 3: a repo with both an issues and a PRs source has its comment stream walked twice; `max_items_per_run` walks oldest-first (the two-cursor newest-first backfill comes in step 3).
3. **TODO** Java chunker, releases source (+ tag messages), two-cursor newest-first backfill with caps, one comment stream per repo shared by its sources; zinc, scala-asm, scala-dev backfilled.
4. **TODO** Gateway: universe-aware backend, `universe`/`projects` parameters, MCP, page selectors, labels and colours from config. Stop for review.
5. **TODO** Status integration: per-project status files, progress API, "Index status" tab, Home strip; remove the standalone dashboard.
6. **TODO** `refresh.py` with phases, locks, status, priority tiers, launchd job; embedding through the gateway. Stop for review.
7. **TODO** Evaluation set; measure rerank and the LLM steps; decide their defaults on numbers.
