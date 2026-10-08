"""Run a reproducer spec against one Scala version, inside a sandbox.

The code comes from bug reports, so it is untrusted, and compiling it already runs it (macros, plugins). Two steps per (version, JVM, deps):
  warm     outside the sandbox, compile a trivial file with the same version, JVM and dependencies, so scala-cli fetches everything it will need;
  run      `scala-cli compile|run --offline` of the real files under `sandbox-exec`: no network, writes only to the run's own directory, the
           caches and the temp dirs, a wall-clock limit. `sandbox-exec` is deprecated by Apple but works; the runner is one function, so a
           container can replace it.
Returns {version, jvm, cmd, exit, timed_out, secs, out}: `out` is stdout and stderr together, without scala-cli's own banner lines."""
import os, re, shutil, subprocess, sys, threading, time
from pathlib import Path

HOME = os.path.expanduser("~")
WARMED, _LOCK, _KEYLOCKS = set(), threading.Lock(), {}
LATEST = {"2.12": "2.12.21", "2.13": "2.13.18", "3": "3.9.0"}
TIMEOUT = 180
_NOISE = re.compile(r"^(The `--offline` option is experimental|Please bear in mind that non-ideal|If you encounter any bugs or have feedback|"
                    r"Compiling project \(|Compiled project \(|WARNING: (A terminally deprecated|sun\.misc\.Unsafe|Please consider reporting|"
                    r"Use --enable-native-access|Restricted methods will be blocked|A restricted method|java\.lang\.System::load|"
                    r"Please consider))")


def jvm_for(version):
    """The JVM a version is comfortable on: JDK 8 before JDK 17 support (2.12.15, 2.13.6), else 17."""
    if version.startswith("3"):
        return "17"
    major, patch = version.rsplit(".", 1)
    return "8" if (major == "2.12" and int(patch) < 15) or (major == "2.13" and int(patch) < 6) else "17"


def _profile(work):
    allow = [work, os.path.realpath(work), "/private/var/folders", "/private/tmp/scala-cli",
             f"{HOME}/Library/Caches/Coursier", f"{HOME}/Library/Caches/ScalaCli", f"{HOME}/Library/Application Support/ScalaCli"]
    paths = "\n  ".join(f'(subpath "{p}")' for p in allow)
    return f"""(version 1)
(allow default)
(deny network*)
(allow network* (local unix-socket))
(deny file-write*)
(allow file-write*
  {paths}
  (literal "/dev/null") (literal "/dev/zero") (regex #"^/dev/tty") (regex #"^/dev/fd/"))
"""


def _args(spec, version, jvm, offline):
    a = ["scala-cli", "--power", "run" if spec.get("mode") == "run" else "compile", "--server=false", "-S", version, "--jvm", jvm]
    a += ["--offline"] if offline else []
    for o in spec.get("scalac_options") or []:
        a += ["-O", o]
    for d in spec.get("deps") or []:
        a += ["--dep", d]
    return a


def warm(spec, version, jvm, scratch):
    key = (version, jvm, tuple(spec.get("deps") or ()))
    with _LOCK:
        lock = _KEYLOCKS.setdefault(key, threading.Lock())
    with lock:                                                        # parallel runs share one warm-up per key
        return None if key in WARMED else _warm(spec, version, jvm, scratch, key)


def _warm(spec, version, jvm, scratch, key):
    d = Path(scratch).resolve() / "warm" / f"{version}-{jvm}-{abs(hash(key)) % 10**8}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "W.scala").write_text("object W\n")
    r = subprocess.run(_args({"deps": spec.get("deps")}, version, jvm, False) + [str(d / "W.scala")], capture_output=True, text=True, timeout=900, cwd=d)
    WARMED.add(key)
    return None if r.returncode == 0 else (r.stdout + r.stderr)[-2000:]


def run(spec, version, work, timeout=TIMEOUT):
    work = Path(work).resolve()
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    jvm = jvm_for(version)
    err = warm(spec, version, jvm, work.parent.parent)
    if err:
        return {"version": version, "jvm": jvm, "exit": None, "timed_out": False, "secs": 0, "out": "", "infra": f"warm-up failed: {err}"}
    names = []
    for f in spec["files"]:
        name = Path(f["name"]).name                                   # no paths from the model
        (work / name).write_text(f["content"])
        names.append(name)
    prof = work / ".sandbox.sb"
    prof.write_text(_profile(str(work)))
    cmd = ["sandbox-exec", "-f", str(prof)] + _args(spec, version, jvm, True) + names
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=work, errors="replace")
        code, out, to = r.returncode, r.stdout + ("\n" if r.stdout and r.stderr else "") + r.stderr, False
    except subprocess.TimeoutExpired as e:
        code, to = None, True
        out = ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")) + \
              ((e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""))
    bad = re.findall(r"bad option: '([^']+)'|missing argument for (\S+)", out)
    gone = sorted({a or b for a, b in bad} & set(spec.get("scalac_options") or []))
    if gone and not spec.get("_retry"):                               # a flag this version does not know (removed, renamed): run once without it
        r2 = run({**spec, "_retry": True, "scalac_options": [o for o in spec["scalac_options"] if o not in gone]}, version, work, timeout)
        r2["dropped_flags"] = gone
        return r2
    out = "\n".join(l for l in out.splitlines() if not _NOISE.match(re.sub(r"\x1b\[[0-9;]*m", "", l).strip()))
    out = re.sub(r"\x1b\[[0-9;]*m", "", out)
    if len(out) > 20000:
        out = out[:10000] + "\n[... output cut ...]\n" + out[-10000:]
    return {"version": version, "jvm": jvm, "cmd": " ".join(cmd[3:]), "exit": code, "timed_out": to, "secs": round(time.time() - t0, 1), "out": out}


if __name__ == "__main__":
    spec = {"mode": "run", "files": [{"name": "A.scala", "content": "object A extends App { println(util.Properties.versionString) }"}]}
    print(run(spec, sys.argv[1] if len(sys.argv) > 1 else "2.13.18", "/tmp/claude-501/repro-selftest/runs/x"))
