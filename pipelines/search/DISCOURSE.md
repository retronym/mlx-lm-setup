# Discourse: the Scala Contributors forum

## Why

Much of the *why* behind Scala language and library changes never reaches GitHub: Pre-SIP and SIP discussions, language design threads, the Core team's meeting notes and decisions, and announcements happen on [contributors.scala-lang.org](https://contributors.scala-lang.org/). A PR that implements a SIP usually links its forum thread, and the thread links back to prototypes, issues and PRs. Without the forum, search finds the implementation but not the debate that shaped it, and the links graph has a hole exactly where design decisions live.

## Pilot (before writing the adapter)

A pilot crawl (the full topic listing plus 67 topics stratified by category and size, 162 requests in under 3 minutes, no rate-limit responses) settled how to use the API:

- **Size**: 1,784 public topics and about 27k posts (the site's own count of 35k includes private categories and deleted posts), from August 2016, in 13 categories (language-design is half of all posts, then SIP, announcements, compiler, tooling). Posts per topic: median 7, 90th percentile 33, maximum 578; 332 topics have more than 20 posts. No tags are used.
- **Fetching**: `/t/{id}.json` returns the first 20 posts and the ids of all of them; `/t/{id}/posts.json?post_ids[]=...` returns up to 100 more per request. `?print=true` (every post at once) is rate-limited to 5 an hour per address, so it is not used. `include_raw=true` adds the markdown the author wrote next to Discourse's HTML (`cooked`) at no extra cost. A full crawl is about 2,000 requests.
- **Text**: from `raw`. A post's median length is 520 characters (90th percentile 1,900; 6% are longer than a chunk and get split). A third of all posts quote another post (`[quote="user, post:N, topic:T"]`), so quotes are cut to who and their first 160 characters: the chunk says what it answers without indexing the quoted text twice. Images become their alt text. Small-action posts ("closed this topic", "split this topic") are not text anyone wrote and are skipped.
- **Links**: from `cooked`, where Discourse has already made relative links and auto-linked bare URLs absolute. Of the links in the sample, the most common targets are other forum topics, then `github.com/scala/*`, `lampepfl/dotty` (now `scala/scala3`), docs.scala-lang.org, scalacenter, the users forum and Scastie.
- **Incremental sync**: `/latest.json?order=activity` lists topics by `bumped_at`, which a new reply moves forward and an edit does not.

## Key decisions

- **A topic is a document, a post is a chunk.** The opening post has kind `topic`, replies kind `post`, so a search can ask for forum discussions only, and one hit per document means one hit per thread. A reply's title carries the topic's title (`Pre SIP: Named tuples  (reply #12 by odersky)`), so it embeds with its context. The category goes into `labels`. Authors are forum usernames (`author`) and display names (`author_name`), so `authors: ["me"]` finds a person's posts when the forum username or display name is in `search.json` `me`.
- **The same two cursors as the GitHub sources**: a forward cursor (newest `bumped_at` indexed: new replies arrive on the next run, uncapped) and a newest-first backfill under `max_items_per_run` down to `since`. A topic seen again re-reads its first 20 posts and fetches only posts it does not have, so an active 500-post thread costs two requests, not six; a renamed topic is re-read whole.
- **Politeness over speed**: one request a second (Discourse's defaults allow 200 a minute per address), a User-Agent that says what is asking, `Retry-After` honoured on 429 and 503, and the rest of the run slowed by half again after one. The full backfill takes about 40 minutes and runs once; a nightly run is a listing page plus the bumped topics.
- **The weekly reconcile** walks the whole listing (about 60 requests) and drops topics that are no longer public. Edits below a topic's first 20 posts are only seen when the topic is renamed: an accepted gap.
- **The forum is a project of its own** (`scala-contributors`, priority 4) in the `scala-zinc` universe, keyed by its host in place of a repo (`site`), so another Discourse (users.scala-lang.org) is a config file, not code.

## Links

Forum topics are nodes in the links graph (`topic:<host>/<id>`):

- a topic **mentions** what its posts link to: GitHub issues, PRs and commits by URL, and other topics (confidence 0.7 from the opening post, 0.5 from a reply). A bare `#123` in a forum post names no repo and is not a reference.
- anything whose text links to a topic **mentions** it: a PR that cites its Pre-SIP thread, an issue that points at a discussion. This is the existing reference parser with one more URL form, so it applies to every source.
- topic URLs of the hosts in `links.forums` that are not indexed (users.scala-lang.org by default) are dangling nodes, as unindexed GitHub items are.

So `links` on a SIP implementation PR shows its forum thread, `linked_to` a topic URL restricts a search to what that discussion touched, and the related group can bring a thread in next to the PR that a query found.

## Results

The full backfill (2026-10-06): 1,784 topics, 27,178 posts read, 28,657 chunks (2,374 of them opening posts or their continuations), 2,217 requests in 38 minutes (about 47 topics a minute; most of the time is the one-second spacing), no 429 or 503. Embedding took about 12 minutes in-process. A nightly run with nothing new costs one listing page.

Links (built over the whole `scala-zinc` universe, 15 s): 4,324 edges touch a forum topic, all `mentions`.

| | edges |
|---|---|
| topic -> indexed issue / PR / commit | 554 / 576 / 15 |
| topic -> topic (indexed) | 745 |
| topic -> GitHub item or commit not indexed (sbt/sbt, scala-native, coursier, scala/improvement-proposals, Kotlin KEEPs...) | 1,868 |
| topic -> users.scala-lang.org topic (dangling) | 152 |
| issue / PR / commit / release -> topic | 515, to 392 topics (127 of them on users.scala-lang.org) |

Sampled by eye, every edge in a random 16 (8 each way) was a real reference: forum posts citing the scala3 issue they discuss, scala3 PRs saying "as reported on contributors", scala-dev release tracking issues linking their release thread, scala/bug comments pointing at a users-forum question. The most-linked topics are the design threads one would hope for (Pre-SIP: ThisFunction, Proposed Changes and Restrictions For Implicit Conversions, Principles for Implicits in Scala 3, Pre-SIP: export, Let's drop auto-tupling). A release note that links its forum announcement mentions the topic (a first build had it as `shipped_in`, which reads backwards).

## Future work

- users.scala-lang.org as a second forum project (a config file).
- Thread summaries for long topics: the `enrich` phase reads GitHub threads only; a 578-post design thread is where a summary pays most.
- Forum topics in Duplicates (clusters and outliers have them; duplicate pairs are issues and PRs only).
- Edits deep in long topics, if they turn out to matter: re-read topics whose listing `posts_count` differs from what is stored.
