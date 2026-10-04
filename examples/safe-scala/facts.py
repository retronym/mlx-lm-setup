"""Gather the facts the film puts on screen, and fail if any of them is not true today.

    python3 examples/safe-scala/facts.py [--out data/safe-scala]

1. Compiles each agent snippet in snippets/ against snippets/Api.scala (stubs with the signatures of TACIT's capability API)
   with today's Scala 3 nightly on this machine, and checks it compiles or fails as the film says. The compiler output, the
   nightly version and the wall time are kept so the film shows the real text.
2. Fetches the paper's arXiv HTML and checks that every table row in paper.json appears in it verbatim.
Writes facts.json. Standard library only (plus scala-cli on PATH).
"""
import argparse, datetime, html, json, platform, re, shutil, subprocess, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ANSI = re.compile(r"\x1b\[[0-9;]*m")
NOISE = re.compile(r"^(Check|Download|Compiling|Failed to download|Warning: setting)")

# snippet -> substring the compiler must print (None: must compile)
EXPECT = {
    "ok": None,
    "pure": None,
    "escape": "Capability `fs` outlives its scope",
    "leak": "Reference `fs` is not included in the allowed capture set {any.rd}",
    "cast": "Cannot use asInstanceOf in safe mode",
    "bypass": "Cannot refer to method writeString in object Files from safe code",
}


def scala_cli(*args, cwd):
    t = time.time()
    p = subprocess.run(["scala-cli", *args], cwd=cwd, capture_output=True, text=True)
    return p, time.time() - t


def compile_snippets(work):
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(HERE / "snippets", work)
    cp, _ = scala_cli("compile", "--server=false", "--print-class-path", "Api.scala", "ok/Agent.scala", cwd=work)
    version = re.search(r"scala3-library_3-([^/]+?)\.jar", cp.stdout).group(1)
    out = {}
    for name, want in EXPECT.items():
        p, secs = scala_cli("compile", "--server=false", "Api.scala", f"{name}/Agent.scala", cwd=work)
        text = "\n".join(l for l in ANSI.sub("", p.stdout + p.stderr).splitlines() if not NOISE.match(l))
        text = text.replace(str(work) + "/", "")
        ok = p.returncode == 0
        if want is None and not ok:
            raise SystemExit(f"{name}: expected to compile, but:\n{text}")
        if want is not None and (ok or want not in text):
            raise SystemExit(f"{name}: expected an error containing {want!r}, got (exit {p.returncode}):\n{text}")
        out[name] = {"code": (HERE / "snippets" / name / "Agent.scala").read_text(), "compiles": ok, "output": text, "secs": round(secs, 1)}
        print(f"{name:<7} {'compiles' if ok else 'rejected'}  {secs:5.1f}s")
    return version, out


def check_paper(out):
    paper = json.load(open(HERE / "paper.json"))
    page = out / "arxiv.html"
    if not page.exists():
        urllib.request.urlretrieve(paper["source"], page)
    txt = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page.read_text())))
    missing = [c for c in paper["checks"] if c not in txt]
    if missing:
        raise SystemExit("not found verbatim in the paper:\n" + "\n".join(missing))
    s = paper["security"]
    for m in ("Claude Sonnet 4.6", "MiniMax M2.5"):          # the checked strings must say what the structured numbers say
        u = s[m]["unclassified"]
        row = f"Malicious ( n = 11 n\\!=\\!11 ) 100% {u['malicious']}%"
        assert row in txt, (m, row)
    print(f"paper: {len(paper['checks'])} passages found verbatim")
    return paper


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data/safe-scala"))
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    paper = check_paper(out)
    version, snippets = compile_snippets(out / "snippets")
    facts = {"paper": paper, "scala": version, "api": (HERE / "snippets/Api.scala").read_text(), "snippets": snippets,
             "measured": {"date": datetime.date.today().isoformat(), "host": platform.node(), "machine": platform.machine()}}
    (out / "facts.json").write_text(json.dumps(facts, ensure_ascii=False, indent=1))
    print(f"{out / 'facts.json'}: Scala {version}")


if __name__ == "__main__":
    main()
