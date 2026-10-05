"""Managed git clones: one bare clone per repository under <data dir>/repos, fetched by the refresh, so indexing reads exactly the ref the
config names and never depends on which branch is checked out in a working copy.

The first clone borrows objects from a local checkout when one exists (~/code/<owner>/<name>, or the dirs in search.json
"local_clone_dirs") and then detaches from it (--dissociate), which makes it fast and keeps the managed clone independent."""
import os, subprocess
from pathlib import Path

LOCAL_DIRS = ["~/code"]


def path_for(cfg, repo):
    return cfg.data_path(cfg.search["repos_dir"], repo.replace("/", "__") + ".git")


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _local_checkout(cfg, repo):
    for d in cfg.search.get("local_clone_dirs") or LOCAL_DIRS:
        p = Path(os.path.expanduser(d)) / repo
        if (p / ".git").exists():
            return p
    return None


def ensure(cfg, repo, fetch=True, log=print):
    """Path of the managed bare clone of `repo`, cloning it first if needed and fetching all branches and tags (forced) when `fetch`."""
    dest = path_for(cfg, repo)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        ref = _local_checkout(cfg, repo)
        cmd = ["clone", "--bare", "--quiet"] + (["--reference-if-able", str(ref), "--dissociate"] if ref else []) + [f"https://github.com/{repo}.git", str(dest)]
        log(f"  cloning {repo}" + (f" (borrowing objects from {ref})" if ref else ""))
        _git(*cmd)
        fetch = False                                              # a fresh clone is current
    if fetch:
        _git("fetch", "--quiet", "--force", "--prune", "origin", "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*", cwd=dest)
    return dest
