"""Find references to issues, PRs and commits in text: the pure parsing half of `links.py` (no database, no configuration beyond the legacy tracker prefixes).

  #123, (#123)                      bare: resolved by the caller against the repo of the document that says it
  scala/bug#123, Fixes sbt/zinc#9   qualified
  https://github.com/o/r/issues/1, .../pull/1, .../commit/<sha>
  SI-1234                           a legacy tracker prefix (Trac), mapped to a repo by the caller's `legacy_prefixes`
  <40 hex>, `commit abc1234`, `@abc1234`   commit shas; short ones only after a cue word, and the caller drops those that match no indexed commit

A reference is *closing* when a closing keyword leads it (`fixes`, `closes`, `resolves`, in any tense, also a list: `Fixes #1, #2 and #3`), which is how GitHub reads
a PR body or commit message. Whether a closing keyword counts is up to the caller (in an issue or a comment it is only a mention)."""
import re
from dataclasses import dataclass

REPO = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
_URL = re.compile(rf"https?://github\.com/({REPO})/(?:issues|pull)/(\d{{1,7}})\b")
_URL_COMMIT = re.compile(rf"https?://github\.com/({REPO})/commit/([0-9a-f]{{7,40}})\b")
_QUAL = re.compile(rf"(?<![\w./-])({REPO})#(\d{{1,7}})(?!\w|\.\w)")        # not scala/scala#2.13.x
_BARE = re.compile(r"(?<![\w/&#.-])#(\d{1,5})(?!\w|\.\w|\]\(|</a>)")   # a link's text (`[#1](url)`) takes its repo from the URL, which is found separately                  # not &#123; or C#1 or a/b#1 (qualified handles those)
# javap output and constant pools are full of `invokevirtual #38` and `#29 = Utf8`: not references
_BYTECODE = re.compile(r"\b(?:invoke\w+|get(?:static|field)|put(?:static|field)|ldc\w*|checkcast|instanceof|anewarray|new)\s+#\d|(?:#|SI-)\d+\s*(?:=\s*(?:Utf8|Class|Methodref|Fieldref|NameAndType|String|InterfaceMethodref|MethodHandle|MethodType|InvokeDynamic|Integer|Long|Double|Float)\b|;?\s*//)"
                       r"|\bMethod arguments:|\binvoke(?:static|virtual|special|dynamic|interface)\b|\bBootstrapMethods\b|\bREF_invoke|\(L[\w/$;]*\)|\bL(?:java|scala)/")
_SHA40 = re.compile(r"(?<![0-9A-Za-z/])[0-9a-f]{40}(?![0-9A-Za-z])")
_SHA_CUE = re.compile(r"(?:\b(?:commit|sha|revert(?:s|ed)?|cherry-picked from commit)\s+|@)([0-9a-f]{7,40})\b")
_CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[:\s]*(?:\S+\s*(?:,|and|&)\s*)*$", re.I)
_CLOSING_ONE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[:\s]*$", re.I)       # GitHub's rule: the keyword goes with each reference
_LINE_COMMENT = re.compile(r"(?<!:)//(.*)")


@dataclass(frozen=True)
class Ref:
    kind: str                # "issue" (an issue or PR: one number space) or "commit"
    repo: str | None         # None: the repo of the document that mentions it
    key: str                 # the number, or the sha
    via: str                 # bare | qual | url | legacy | sha
    closing: bool
    snip: str                # the words around it, for judging precision


def _snip(text, a, b):
    return " ".join(text[max(0, a - 36):b + 36].split())[:100]


def make_parser(legacy_prefixes=None):
    """parse(text) -> [Ref], each distinct target once (closing wins), in order of first appearance."""
    legacy = {k.upper(): v for k, v in (legacy_prefixes or {}).items()}
    legacy_rx = re.compile(rf"(?<![\w-])({'|'.join(map(re.escape, legacy))})-(\d{{1,6}})\b") if legacy else None

    def parse(text, lists=True):
        """`lists`: `Fixes #1, #2 and #3` closes all three (how commit messages of the Trac era meant it); False is GitHub's reading, where only #1 closes."""
        if not text:
            return []
        found = []                                                         # (pos, Ref)
        closing_rx = _CLOSING if lists else _CLOSING_ONE
        def add(m, kind, repo, key, via):
            closing = bool(closing_rx.search(text[max(0, m.start() - 80):m.start()]))
            found.append((m.start(), Ref(kind, repo, key, via, closing, _snip(text, m.start(), m.end()))))
        for m in _URL.finditer(text):
            add(m, "issue", m.group(1), m.group(2), "url")
        for m in _URL_COMMIT.finditer(text):
            add(m, "commit", m.group(1), m.group(2), "url")
        for m in _QUAL.finditer(text):
            add(m, "issue", m.group(1), m.group(2), "qual")
        fences = [m.start() for m in re.finditer(r"^\s*```", text, re.M)]
        for m in _BARE.finditer(text):
            if sum(f < m.start() for f in fences) % 2:                    # inside a fenced code block
                continue
            if _BYTECODE.search(text[max(0, m.start() - 120):m.end() + 120]):          # constant pools run over many lines: look around, not at the line
                continue
            add(m, "issue", None, m.group(1), "bare")
        if legacy_rx:
            for m in legacy_rx.finditer(text):
                if not _BYTECODE.search(text[text.rfind("\n", 0, m.start()) + 1:(text.find("\n", m.end()) + 1 or len(text))]):
                    add(m, "issue", legacy[m.group(1).upper()], m.group(2), "legacy")
        for m in _SHA40.finditer(text):
            add(m, "commit", None, m.group(0), "sha")
        for m in _SHA_CUE.finditer(text):
            add(m, "commit", None, m.group(1), "sha")
        out = {}
        for _, r in sorted(found, key=lambda x: x[0]):
            k = (r.kind, r.repo, r.key)
            if k not in out:
                out[k] = r
            elif r.closing and not out[k].closing:
                out[k] = Ref(r.kind, r.repo, r.key, out[k].via, True, out[k].snip)
        return list(out.values())
    return parse


def code_comments(text):
    """The comment parts of Scala / Java source (line comments, block and doc comments, also when the chunk starts inside a block), one per line."""
    out, block = [], False
    for line in text.splitlines():
        s = line.strip()
        if block:
            out.append(s)
            block = "*/" not in s
        elif s.startswith("/*"):
            out.append(s)
            block = "*/" not in s
        elif s.startswith("*"):
            out.append(s)
        else:
            m = _LINE_COMMENT.search(line)
            if m:
                out.append(m.group(1))
    return "\n".join(out)
