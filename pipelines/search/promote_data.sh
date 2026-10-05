#!/usr/bin/env bash
# Copy the search index (per-project databases and managed git clones) from one checkout's data dir to another's, e.g. from a worktree
# to the main checkout after merging, so the running service finds what was built during development without re-indexing.
# Uses APFS clones (cp -c): instant and no extra disk until either side changes. Never deletes anything in the destination.
#
#   promote_data.sh <from-checkout> <to-checkout> [--force] [--only project[,project...]] [--no-repos]
#
# A project database that already exists in the destination is skipped unless --force (then the old one is kept as index.db.bak).
# Afterwards restart the service (service/service.sh restart) so the gateway picks up the new catalog entry and indexes.
set -euo pipefail
[ $# -ge 2 ] || { sed -n '2,10p' "$0"; exit 2; }
FROM="$(cd "$1" && pwd -P)/pipelines/search/data"; TO="$(cd "$2" && pwd -P)/pipelines/search/data"; shift 2
FORCE=0; ONLY=""; REPOS=1
while [ $# -gt 0 ]; do case "$1" in --force) FORCE=1;; --only) ONLY="$2"; shift;; --no-repos) REPOS=0;; *) echo "unknown option $1"; exit 2;; esac; shift; done
[ -d "$FROM/projects" ] || { echo "nothing to promote: $FROM/projects does not exist"; exit 1; }
mkdir -p "$TO/projects"
for p in "$FROM"/projects/*/; do
  id="$(basename "$p")"
  [ -z "$ONLY" ] || [[ ",$ONLY," == *",$id,"* ]] || continue
  dst="$TO/projects/$id"
  if [ -e "$dst/index.db" ]; then
    if [ "$FORCE" = 1 ]; then mv "$dst/index.db" "$dst/index.db.bak"; echo "$id: replacing (old kept as index.db.bak)"
    else echo "$id: exists in the destination, skipped (use --force)"; continue; fi
  fi
  mkdir -p "$dst"; cp -c "$p"index.db "$dst/index.db"
  echo "$id: copied ($(du -h "$dst/index.db" | cut -f1))"
done
if [ "$REPOS" = 1 ] && [ -d "$FROM/repos" ]; then
  mkdir -p "$TO/repos"
  for r in "$FROM"/repos/*.git; do
    [ -e "$TO/repos/$(basename "$r")" ] && continue
    cp -cR "$r" "$TO/repos/"; echo "clone $(basename "$r"): copied"
  done
fi
echo "done: $FROM -> $TO"
