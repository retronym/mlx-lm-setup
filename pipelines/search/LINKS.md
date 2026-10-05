# Links between code, issues and PRs

Design. Steps 1 (extraction from what is indexed), 2 (GitHub-native edges, tag-based `shipped_in`), 3 (direct use in search) and 4 (related results) are built: see [Status](#status) below and `links.py` / `refs.py`.

## Problem

The index holds the pieces of a story as unrelated chunks: the `scala/bug` ticket that reports the problem, the PR that fixes it, the review comments that explain why the fix looks the way it does, the commit that landed it, the release note that shipped it, and the code that changed. Today the only trace of a connection is `refs` (bare `#123` numbers) in commit and release metadata, which nothing reads, and `neighbours`, which links by *textual similarity* only (and only issues/PRs).

Two consequences:

- A query that hits the code finds neither the issue that motivated it nor the PR discussion that justifies its odd shape. A query that hits an issue doesn't surface the fix. The user has to re-search by hand, using the ticket number as a keyword, which only works if the number happens to be in the text.
- "Which PR fixed this?", "what did this commit close?", "what issues did 2.13.12 ship?", "which tickets touched this file?" are not answerable at all.

## Key decisions

1. **Links are a first-class, typed, directed edge table, not chunk metadata.** `(src, dst, type, evidence, confidence)`. Metadata `refs` can't be queried backwards ("who mentions #123?") without a scan, and a ref in a commit can point at a different project's tracker. Edges are derived data: always rebuildable from the indexes, never the source of truth.
2. **Edges connect *documents* (an issue, a PR, a commit, a release, a file), not chunks.** Search already collapses to one hit per document, comments belong to their thread, and code chunks churn when a file is re-chunked. Node ids are stable and chunk-independent: `owner/repo#N` (an issue or PR: they share a number space, so the id carries no kind; the node's `kind` says which once it is indexed), `commit:owner/repo@sha`, `release:owner/repo@tag`, `file:owner/repo:path`. A node can be dangling (target not indexed): the edge is still recorded and resolves later when the item is backfilled, which matters because the backfill drains newest-first over many nights.
3. **Extraction is deterministic and cheap first, model-assisted later, and every edge says how it was found.** Explicit, high-precision links come from structured data and text patterns; fuzzy links (similarity) stay separate and clearly weaker. Never present a similarity guess as a fact.
4. **Links are used in two ways, with different trust requirements.** *Direct*: edges are searchable and queryable (a filter, a tool, a field on every hit). *Indirect*: edges expand the result set of a query from its top hits ("related"), kept visibly separate from the primary ranking and tunable, so a wrong edge can never displace a real hit.
5. **Same philosophy as `neighbours`**: a refresh phase, derived into its own SQLite file under `data/links/<universe>.db`, read-only from the gateway, so no model is needed at query time and no change to the per-project index schemas (no re-embedding, no migration of the per-project databases).

## Link types and where they come from

| Type | From -> to | Source of evidence | Confidence |
|---|---|---|---|
| `closes` | PR/commit -> issue | GitHub's own closing references (GraphQL `closingIssuesReferences` / timeline `cross-referenced` + `closed` events), or `fixes/closes/resolves #N` in the PR body or commit message | high |
| `mentions` | any -> issue/PR/commit | `#N`, `owner/repo#N`, `/issues/N`, `/pull/N` URLs, commit SHAs in text of an issue, PR, comment, review, commit message, release note | medium (a mention is not a fix) |
| `merged_as` | PR -> commit | PR's merge / squash commit sha (GitHub API) | high |
| `touches` | commit/PR -> file | commit's changed paths (already captured in `git_log` chunks), PR's file list | high, but noisy for mega-commits: cap and down-weight by files changed |
| `shipped_in` | issue/PR/commit -> release | release note refs, plus `git describe --contains` on the merge commit for the first containing tag | high |
| `defines` (code -> text) | file/symbol -> issue/PR/commit | code comments naming `SI-1234`, `scala/bug#123`, `#123`, `see https://github.com/...` in source; the Scala codebase has many `// scala/bug#1234` | high |
| `blame` | file region -> commit | (later) `git blame` / `git log -L` on a hit's line range | high, expensive: on demand only |
| `similar` | issue/PR <-> issue/PR | existing `neighbours` pairs | model-derived; never expanded transitively |

Reference resolution details worth deciding up front: bare `#N` resolves against the repo of the *containing* document (commit in `scala/scala` -> `scala/scala#N`, but scala commits that say `#N` often mean `scala/bug#N`; resolve ambiguity by trying the project's other trackers and keeping the one that exists, otherwise record both at lower confidence); legacy `SI-1234` (Trac) maps to `scala/bug#1234`; a project config lists its `tracker` repos and legacy prefixes so this is configuration, not code.

Because GitHub issues and PRs share a number space, a number resolves to whichever exists; the node kind comes from the indexed item.

## Storage

`data/links/<universe>.db`:

```
nodes(id PK, kind, project, repo, ref, title, state, created, url, indexed)   -- indexed = 0 for dangling targets
edges(src, dst, type, conf, evidence, UNIQUE(src, dst, type))      -- evidence: where the text was found, e.g. "pr body", "commit msg", "closing ref"
meta(k, v)                                                         -- stamp, version, counts
```

with indexes on `dst` and `(src, type)`. A node maps to its chunks through the per-project index (`doc` column already groups chunks by document), so the links DB does not duplicate text.

Extraction is incremental the same way `neighbours` is: a stamp of per-project newest `updated`, `--force` to rebuild, and a version in `meta` so extractor changes recompute. Each source contributes edges independently, so adding a new source type adds an extractor and nothing else.

Capturing the GitHub-native edges needs the sync to fetch two things it doesn't today: closing references / timeline events on PRs and issues, and PR file lists + merge commit sha. Both are additional fields on requests the GitHub adapter already makes (GraphQL batch of 100 PRs per request would be the cheap route) and are stored in chunk metadata like `refs` is now, so extraction reads only the indexes and the work stays in `sync`.

## Direct use: links are searchable

- **Hit fields**: every hit gets `links`: counts per type and a short list of the most important neighbours (closing PR/issue, merge commit, release). The `summary_line` gains e.g. `fixes #1234, in 2.13.12`.
- **Query syntax / filters**: `linked_to` (a ref or `#N`), `link_type`, and `has_link` (e.g. `kinds=issue&has_link=closed_by` "issues with a fix", or the reverse: open issues with no linked PR). Implemented as a restriction on the candidate doc set before ranking, like `kinds` and `open_only` are.
- **Link-text search**: a mention of `#1234` in a commit body is currently matched only if FTS tokenises `1234`; add the *resolved* canonical id of every outbound edge into the FTS body of the source document as a synthetic token (`ref_scala_bug_1234`), so "scala/bug#1234" as a query finds everything that mentions it. This is the cheapest way to make links "directly searchable" and works for BM25 with no new query path. Open question below: whether to add it at chunk-write time (invalidates hashes? no: FTS-only, not part of the hash) or in the links phase.
- **New MCP tools / endpoints**: `links(ref, direction, types, depth)` returns the neighbourhood of a document with evidence; a `thread` view stitches the story in time order (report -> discussion -> PR -> review -> commit -> release), which is what a human actually wants when they ask "what's the history of this".
- **Page**: a "Related" strip under each hit, and a story view for a ref.

## Indirect use: surfacing related results

For a query, after fusion (and before/after rerank, see open questions), take the top N documents as *seeds* and expand over edges:

```
related_score(d) = sum over seeds s linked to d of  seed_score(s) * w(type) * decay(depth) * conf
```

- Weights per type in `search.json` (`links.weights`): `closes` and `merged_as` high, `shipped_in` low (don't drag in a whole release), `touches` low and scaled by 1/log(files changed), `mentions` medium, `similar` lowest. Depth 1 by default; depth 2 only through `closes` / `merged_as` (issue -> PR -> commit). A hub guard: nodes with degree above a cap (release notes, umbrella issues, mass-refactor commits) are not expanded.
- Expanded documents enter the ranked list with their own `via` ("linked from #1234, closes") and are shown in a separate **Related** group under the primary results; they only join the main ranking if they *also* matched the query lexically/semantically, in which case a link adds a small boost to their RRF score (a third list in the fusion, `links`, with its own weight, so it is measured like the others). Default: related group on, boost off until the evaluation shows it helps.
- Per-kind quotas on related items so one huge thread does not fill the group; code files that an issue's PR touches are listed by file, not by every chunk.
- `explain` says which seed pulled a hit in and over which edge.

## Evaluation (before turning anything on by default)

This ties into the still-open evaluation step 7 in [PLAN.md](PLAN.md). Cheap ground truth exists in the data: for PRs that close issues, the issue text is the query and the PR (and its changed files) is the expected answer, and the reverse. Metrics: recall@k of the linked document with and without expansion, and a regression check that the primary ranking of the canaries and `eval_code.py` is unchanged when related results are shown separately. Edge quality itself: sample N edges per type and judge precision, in particular for bare-`#N` resolution and `touches`.

## Steps

1. **DONE** Extraction on what's already indexed: reference parser module (project `tracker` / `legacy_prefixes` config, bare `#N` resolution, SHAs, URLs, `SI-N`), links DB schema, `links.py` phase (stamp, version, `--force`), `mentions` / `shipped_in` / `touches` edges from existing metadata, `defines` edges from code comments. Tests with fixture chunks; a stats command (edges per type, dangling %, top hubs). No gateway change. Stop for review of edge counts and a precision sample.
2. **DONE** GitHub-native edges: closing references and merge commit sha in the adapter (additive, stored in meta; forward and backfill cursors unaffected), `closes` / `merged_as` extractors, `git describe --contains` for `shipped_in`.
3. **DONE** Direct: `links` on hits, `linked_to` / `link_type` / `has_link` filters, canonical ref tokens in FTS, MCP `links` tool and `POST /api/search/links`, README section.
4. **DONE** Indirect: seed expansion, weights and hub guard in `search.json`, Related group (API, MCP, page) with `via`, `explain`.
5. **TODO** Evaluation set from closing PR/issue pairs; decide the boost and depth defaults on numbers. Fold `similar` edges in.
6. **Future** `blame` / `log -L` edges for a code hit, on demand; NLI-confirmed `mentions` -> `closes` upgrade for prose like "this supersedes #123"; Discourse and SIP links once those sources exist; a graph tab (cluster the link graph, find orphan issues with no PR and orphan PRs with no issue).

## Status

**Step 1 done.** `links.py` is the refresh phase `links` (after `neighbours`; `links.enabled`), `refs.py` the parser, `test_links.py` the tests. Config (`search.json` `links`): `repo_aliases` (`lampepfl/dotty` is `scala/scala3`, one repo renamed), `legacy_prefixes` (`SI` is `scala/bug`), `bare_fallbacks` (a bare `#N` in a `scala/scala` document that is no item of `scala/scala` may be a `scala/bug` Trac ticket, a guess at half confidence; elsewhere a bare number is always the document's own repo), `max_refs_per_chunk`.

Decisions taken while building it:

- Text patterns give `closes` already (a closing keyword in a PR title or body or a commit message; in an issue or comment it is only a `mentions`); step 2 adds GitHub's own closing references. When both exist the better-evidenced edge wins, and `closes` replaces `mentions` for the same pair.
- A bare short sha only counts if it resolves to exactly one indexed commit; a bare 40-hex sha of an unindexed commit is dropped (its repo is unknowable), a commit URL keeps its repo. Shas in `/blob/<sha>/` URLs are not mentions.
- `touches` edges go only to files that are indexed (most paths of a commit, tests for one, are not): the rest are counted in `meta.skipped_touches`. Confidence is 1 up to five files and 5/n beyond.
- Noise filtered in the parser: bytecode listings (`invokevirtual #38`, `#29 = Utf8`), fenced code blocks (bare refs only), the text of a markdown or HTML link whose URL is read on its own, `scala/scala#2.13.x`, six-digit numbers.

On the real index (21k issues and PRs, 61k commits, 3.7k files; builds in 7 s): 125k edges, `closes` 7.9k, `mentions` 32k, `shipped_in` 12k, `touches` 72k, `defines` 1k; 15k dangling nodes, mostly scala3 issues before its backfill horizon and old Trac numbers. `links.py stats`, `links.py sample <type> [n]` (random edges with the words they were found in) and `links.py of <ref>` are for judging it.

**Step 2 done.**

- `sources/ghlinks.py` runs at the end of a GitHub repo's sync (when it has a `prs` source; `links.github.enabled`): GraphQL, 50 PRs per request (about 6 of the 5,000 points an hour; 300 zinc PRs cost about 30), `closingIssuesReferences` and `mergeCommit`, written into the PR chunk's metadata (`closes`, `merge_sha`, `gh_links`). The marker is the work queue: PRs without one are fetched newest first up to `links.github.max_prs_per_run` (3000) per run, so scala3's backlog drains over a few nights like the other backfills. A PR the sync rewrites (edited, closed, merged) loses the marker and is fetched again; the chunk hash is untouched, so nothing is re-embedded. A PR GitHub no longer has counts as done.
- `links.py` turns them into `closes` (conf 1.0 beats the 0.85 of a text keyword; `how` = "github closing ref") and `merged_as` (PR -> commit, dangling when the commit is not indexed).
- `shipped_in` also comes from git now: per managed clone, tags in date order, one `git rev-list` of each against the tags before it (4 s for scala/scala), so every commit is attributed to the first tag that contains it, and a merged PR to the tag of its merge commit. Only tags that have a release node count. On the real index this took `shipped_in` from 12k edges (release notes) to 59k.
- Not done: the PR's changed-file list (`touches` from PRs): it multiplies the GraphQL cost by the number of files and the commit edges already cover indexed files; worth revisiting only if the evaluation says PR-level file links matter.

**Step 3 done.**

- `linkdb.py` (standard library only, so the gateway loads it too) reads the links database: naming a document (`resolve`: a hit's `ref`, `scala/bug#123`, `#123`, `SI-123`, a commit url or sha), a document's relations seen from both ends (`closes` / `closed_by`, `merged_as` / `merge_of`, `mentions` / `mentioned_by`, `shipped_in` / `ships`, `touches` / `touched_by`, `defines` / `defined_by`), summaries, and `story`.
- **Hits** carry `links` (`counts` per relation, `top`: up to six that matter most, two per relation, `line`) and their one-line `line` ends with it: `closed by scala/scala#456 · shipped in release v2.13.12 · mentioned by 5`.
- **Restrictions**, applied before ranking in the keyword and the vector pass (the restriction is a temp table of (source, document) pairs on each store's connection, so a read-only database is fine): `linked_to` (documents linked to those named, through `link_type` relations), `has_link` (`["closed_by"]`: has a fix; `["no_closed_by"]`: has none, also `no_` for any relation; several are ANDed). With `kinds ["issue"]` and `open_only`, `no_closed_by` is "open issues nobody has fixed".
- **A reference in the query**: `scala/bug#1234`, a URL, `SI-1234` or a bare `#123` makes that document first and what links to it follow, as a ranking of its own (`pin`) in the fusion, respecting the other filters; `refs_in_query: false` turns it off.
- **Surfaces**: MCP `links` (neighbourhood or `story`) and the new `search` parameters, `POST /api/search/links`, the search page (a links row under a hit, "all n links ›" restricts to what is linked to that hit, a "links" selector for has a fix / no fix / shipped).
- **Changed from the plan**: no ref tokens in the FTS text. `unicode61` already tokenizes `scala/bug#1234` into `scala bug 1234`, and a bare `#1234` cannot be told apart from another repo's without the document's repo, which only the links know; resolving the reference in the query through the links database does both and is rebuildable. `link_type` takes relation names only (an edge type such as `closes` would be ambiguous with the outgoing relation of that name; name both ends for either direction). Links are only ever *added* to what search returns by the pin: related results from the top hits (indirect use) are step 4.
- Parser: `#N` near a JVM constant pool or bytecode listing (`invokestatic`, `Method arguments:`, `Lscala/...`) is no reference; it had given some old tickets hundreds of false links.

**Step 4 done.**

- `search.related` / `LinkDB.expand`: the first `related.seeds` (5) hits seed an expansion over the links; a document's score is the sum over seeds of each seed's *best* path, seed score (1/rank) x relation weight x confidence, so a PR that both closes and is mentioned by a hit is not twice as related, and two hits pointing at one document add up. The second step (x 0.5) only goes through `closes` / `closed_by` / `merged_as` / `merge_of`: an issue's fixing PR's merge commit, not a mention's mentions.
- **Hub guard**: a relation of a seed with more than `related.hub_degree` (150) edges is skipped (a file's `touched_by`, a release's `ships`), and so is a target with more than that many edges of any kind (an umbrella issue, a release). Weights per relation as seen from the hit (`related.weights` in `search.json`, defaults: closes / closed_by 1, merged_as / merge_of 0.9, defines / defined_by 0.6, mentions / mentioned_by 0.5, shipped_in 0.3, ships 0.2, touches / touched_by 0.15).
- **A group of its own**: the response's `related` (MCP `search` too; `related: false` or a number; `limit` 8, at most `per_kind` 4 of one kind, hits already shown left out, `kinds` / `projects` / `open_only` respected, not computed under an explicit `linked_to` / `has_link`). Each item has `ref` (for `get`), `via` (which hit, which relation from that hit's side, confidence, `through` for the second step), a `line`, and with `explain` every path and its score. The page shows it under the hits as "Related".
- **Boost is an experiment and off**: `related.boost` (or `link_boost` in a request) adds up to boost x the best fused score to a candidate that is linked to the top hits, before the rerank; only hits the query already retrieved move, nothing new enters. Whether to turn it on or expansion before the rerank is for the evaluation (step 5).
- Seen on the real index: for "Future firstCompletedOf memory leak" the top related item is the PR that fixed the second hit (`scala/scala#10927`, closes `scala/bug#13058`), then the issue that mentions it, and PRs and commits around the third and fourth hits.

## Open questions

(Answered: ref tokens in FTS at chunk time, expansion after rerank, links out of the universe stay dangling nodes shown as URLs, GraphQL batching for the GitHub budget. Kept for the record.)


1. **Where do ref tokens go in FTS**: written by sync at chunk time (self-contained, but changing the extractor means re-syncing), or injected by the links phase into the FTS table (derived, rebuildable, but then the per-project DB is written by a second process)? Leaning to chunk time with a small, stable extractor and keeping the richer typed graph in the links DB.
2. **Expansion before or after rerank?** Before means the reranker judges related docs against the query (good: it filters irrelevant links, costs more candidates); after means related docs are never reranked and stay in their own group. Leaning to after for step 4, before as an experiment in step 5.
3. **Cross-universe links** (a Zinc commit referencing a `scala/scala` PR when they are in the same universe is fine; a link out of the universe is kept as a dangling node and shown as a URL only). Agree?
4. **How much GitHub API budget** closing references and PR file lists may use: GraphQL batching makes it roughly one extra request per 100 items, but scala/scala3 is priority 8 and capped.
