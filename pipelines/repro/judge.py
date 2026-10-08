"""Does one run show the reported bug? Rules, by symptom, over the run's exit status and output:

  reproduces       the bug is there (an evidence string from the thread is in the output, or the symptom's signature: a compiler stack trace
                   for a crash, an error for a spurious error, a clean compile for a missing error, a timeout for a hang)
  not_reproduced   the run is clean in the way the symptom says a fixed compiler would be
  differs          something else happened (another error than the one quoted, plain errors where a crash was reported): the reproducer, the
                   version or the bug may be at fault, so it is not counted either way
  infra            the run itself failed (a flag this version does not know, a dependency, scala-cli)
  inconclusive     nothing to decide on (wrong output with no quoted output to compare)"""
import difflib, re

CRASH = re.compile(r"Exception in thread|java\.lang\.[\w.$]*(Error|Exception)\b|AssertionError|FatalError|error while (typing|emitting|compiling)|"
                   r"Unexpected tree|## Exception when compiling|uncaught exception|while compiling:|"
                   r"scala\.reflect\.internal\.(Types\$)?\w*(Error|Exception)|Symbols\$CyclicReference|exception during macro expansion", re.I)
ERROR = re.compile(r"(^|\n)\s*(\[error\]|error:|-- (\[E\d+\] )?[\w ]*Error:|.*\.scala:\d+:(\d+:)? error)", re.I)
INFRA = re.compile(r"bad option|Unrecognized option|not a valid choice for|Error downloading|Could not find|not found: https?://|"
                   r"Error while fetching|is not supported by Scala CLI|Scala CLI does not support|No such file or directory", re.I)


def _norm(s):
    return re.sub(r"\s+", " ", s).strip()


_REPL = re.compile(r"^(?:val )?res\d+: .+? = (.+)$")
_PATH = re.compile(r"(/[\w.@~-]+)+\.scala:\d+(:\d+)?:|<console>:\d+:|<synthetic>:\d+:")


def _quote(q):
    """A quoted line as the program or compiler would print it now: the value of a REPL result, a diagnostic without its position."""
    q = _norm(q)
    m = _REPL.match(q)
    return _norm(m.group(1)) if m else _norm(_PATH.sub("", q))


def found(q, out):
    """Is quoted text `q` in `out`, allowing for REPL formatting, file positions and small wording changes between versions?"""
    if _REPL.match(_norm(q)):                                         # a REPL value: the program printed it on a line of its own
        return _quote(q) in {_norm(l) for l in out.splitlines()}
    q, o = _quote(q), _norm(_PATH.sub("", out))
    if len(q) < 3:
        return False
    if q in o:
        return True
    if len(q) < 12:                                                   # short values ("1", "true") must match exactly
        return False
    for line in _PATH.sub("", out).splitlines():                     # "match is not exhaustive" vs "match may not be exhaustive"
        line = _norm(line)
        if line and difflib.SequenceMatcher(None, q, line[:len(q) + 20]).ratio() >= 0.85:
            return True
    return False


def judge(spec, r):
    """(verdict, why) for run `r` of `spec`."""
    if r.get("infra"):
        return "infra", r["infra"][:200]
    out, sym = r.get("out") or "", spec.get("symptom")
    if r.get("timed_out"):
        return ("reproduces", "timed out, as reported") if sym == "hang" else ("differs", f"timed out after {r['secs']} s")
    ok = r.get("exit") == 0
    crashed, errored = bool(CRASH.search(out)), bool(ERROR.search(out)) or not ok
    seen = lambda k: [e for e in spec.get(k) or [] if isinstance(e, str) and found(e, out)]
    fixed, ev = seen("fixed_output"), seen("bug_output")
    if fixed and not ev:
        return "not_reproduced", f"output has the fixed {fixed[0][:80]!r}"
    if ev:
        return "reproduces", f"output has {ev[0][:80]!r}"
    if spec.get("fixed_output") and sym in ("missing_error", "wrong_warning") and r.get("exit") == 0:
        return "reproduces", "the expected diagnostic is missing"
    if not ok and INFRA.search(out) and not crashed:
        return "infra", INFRA.search(out).group(0)
    has_ev = bool(spec.get("bug_output"))
    if sym == "hang":
        return "not_reproduced", "finished"
    if sym == "crash":
        if crashed:
            return ("differs", "a crash, but not with the quoted output") if has_ev else ("reproduces", "compiler crash")
        return ("not_reproduced", "compiles cleanly") if ok else ("differs", "errors, but no crash")
    if sym == "spurious_error":
        if ok:
            return "not_reproduced", "compiles"
        if crashed:
            return "differs", "a crash where an error was reported"
        return ("differs", "an error, but not the quoted one") if has_ev else ("reproduces", "rejected")
    if sym == "missing_error":
        if ok:
            return "reproduces", "accepted"
        return ("differs", "a crash") if crashed else ("not_reproduced", "rejected now")
    if sym == "runtime_exception":
        if spec.get("mode") != "run":
            return "inconclusive", "runtime exception reported but the spec only compiles"
        if ok:
            return "not_reproduced", "ran cleanly"
        return ("differs", "failed, but not with the quoted output") if has_ev else ("reproduces", "failed at run time")
    if errored:
        return "differs", "does not compile" if not crashed else "crash"
    if has_ev and spec.get("fixed_output"):
        return "not_reproduced", "the quoted output is gone"
    return "inconclusive", "the quoted output is gone, but nothing confirms a fix" if has_ev else "no quoted output to compare"
