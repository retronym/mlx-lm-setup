"""Phase 0b: how many compile errors in model-written bodies would a member/scope mask address?

Holds out method bodies in scala3's compiler/src, asks Qwen3-Coder (via the gateway) for the body given the file prefix, splices
the answer back and typechecks the file with a stock 3.10.1-RC1 compiler (jvm/Tool.scala). The checkout is ahead of that
release, so some files have errors already; only errors inside the spliced body count, and only bodies whose original has no
error count at all.

Usage: python phase0b.py <workdir> [--n 100] [--seed 1]
Steps are cached in <workdir>: defs.jsonl, sample.jsonl, gen/, check.jsonl; rerun to resume.
"""
import argparse, collections, json, os, random, re, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = "/Users/jzaugg-virtus/code/scala/scala3/compiler/src"
JAVA_HOME = os.path.expanduser("~/.sdkman/candidates/java/21.0.12+1.1-tem")
GATEWAY = "http://127.0.0.1:8090/v1/chat/completions"
MASKABLE = {"NotAMemberID", "MissingIdentID"}          # "x is not a member of T", "Not found: x"


def tool(*args, out):
    cp = subprocess.run(["coursier", "fetch", "org.scala-lang:scala3-compiler_3:3.10.1-RC1", "--classpath"],
                        capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1]
    with open(out, "w") as fh:
        subprocess.run(["timeout", "3600", "scala-cli", "run", "-q", "--server=false", "--java-home", JAVA_HOME,
                        os.path.join(HERE, "jvm", "Tool.scala"), "--", *args],
                       env={**os.environ, "CP": cp}, stdout=fh, check=True)


def jsonl(path):
    with open(path) as fh:
        return [json.loads(l) for l in fh if l.startswith("{")]


def prefix_for(src: str, d) -> str:
    """File header (package, imports) plus the ~150 lines before the def, ending at its `=`."""
    upto = src[:d["eq"] + 1]
    lines = upto.split("\n")
    hdr = []
    for l in lines:
        if l.startswith(("package", "import")) or not l.strip() or l.lstrip().startswith(("//", "/*", "*")):
            hdr.append(l)
        else:
            break
    tail_start = max(len(hdr), len(lines) - 150)
    body = lines[tail_start:]
    if tail_start > len(hdr):
        return "\n".join(hdr + ["", "// ... (elided) ...", ""] + body)
    return "\n".join(lines)


def ask(prefix: str, name: str) -> tuple[str, dict]:
    msg = (f"```scala\n{prefix}\n```\n\nThis Scala 3 file from the Scala 3 compiler (dotty) stops right after the `=` of method `{name}`. "
           "Write only the body of that method (the expression after `=`), indented as it would be in the file. "
           "Reply with a single scala code block and nothing else.")
    body = {"model": "qwen3-coder", "temperature": 0.2, "max_tokens": 1200, "messages": [{"role": "user", "content": msg}]}
    req = urllib.request.Request(GATEWAY, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.load(r)
    text = d["choices"][0]["message"]["content"]
    m = re.search(r"```(?:scala)?\n(.*?)```", text, re.S)
    return (m.group(1) if m else text), d.get("usage", {})


def splice(src: str, d, gen: str) -> tuple[str, int, int]:
    """Put the generated body after the `=`, re-indented if the model dropped the indentation. Returns text, body start, end."""
    gen = gen.rstrip()
    orig_newline = "\n" in src[d["eq"] + 1:d["rhsStart"]]
    lines = gen.split("\n")
    nonblank = [l for l in lines if l.strip()]
    if not nonblank:
        gen = "???"
        lines = [gen]
    want = d["indent"] + 2
    if orig_newline or len(lines) > 1 and not lines[0].lstrip().startswith("{"):
        have = min(len(l) - len(l.lstrip()) for l in nonblank) if nonblank else 0
        shift = max(0, want - have)
        body = "\n" + "\n".join((" " * shift + l) if l.strip() else l for l in lines)
    else:
        first, rest = lines[0].strip(), lines[1:]
        have = min((len(l) - len(l.lstrip()) for l in rest if l.strip()), default=want)
        shift = max(0, want - have)
        body = " " + "\n".join([first] + [(" " * shift + l) if l.strip() else l for l in rest])
    start = d["eq"] + 1
    return src[:start] + body + src[d["rhsEnd"]:], start, start + len(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    W = a.work
    os.makedirs(os.path.join(W, "gen"), exist_ok=True)
    files = sorted(os.path.join(r, f) for r, _, fs in os.walk(SRC) for f in fs if f.endswith(".scala"))

    defs_path = os.path.join(W, "defs.jsonl")
    if not os.path.exists(defs_path):
        print(f"parsing {len(files)} files ...", flush=True)
        tool("defs", *files, out=defs_path)
    defs = jsonl(defs_path)
    cands = [d for d in defs if 3 <= d["rhsLines"] <= 25 and not d["name"].startswith("$") and d["name"] not in ("toString", "hashCode", "equals")]
    print(f"{len(defs)} defs, {len(cands)} with 3-25 line bodies", flush=True)

    sample_path = os.path.join(W, "sample.jsonl")
    if not os.path.exists(sample_path):
        rnd = random.Random(a.seed)
        rnd.shuffle(cands)
        per_file, sample = collections.Counter(), []
        for d in cands:
            if per_file[d["file"]] < 2:
                per_file[d["file"]] += 1
                sample.append(d)
            if len(sample) == int(a.n * 1.3):           # spare, for bodies whose original has errors
                break
        for i, d in enumerate(sample):
            d["id"] = f"b{i:03d}"
        with open(sample_path, "w") as fh:
            fh.writelines(json.dumps(d) + "\n" for d in sample)
    sample = jsonl(sample_path)

    base_path = os.path.join(W, "baseline.jsonl")
    if not os.path.exists(base_path):
        bfiles = sorted({d["file"] for d in sample})
        print(f"baseline typecheck of {len(bfiles)} files ...", flush=True)
        tool("check", *bfiles, out=base_path)
    base = {r["file"]: r for r in jsonl(base_path)}
    ok = []
    for d in sample:
        b = base.get(d["file"], {})
        if "crash" in b:
            continue
        if any(d["rhsStart"] <= e["offset"] < d["rhsEnd"] for e in b.get("errors", [])):
            continue
        ok.append(d)
    ok = ok[:a.n]
    print(f"{len(ok)} bodies whose original typechecks cleanly (of {len(sample)} sampled)", flush=True)

    t0, done = time.time(), 0
    for d in ok:
        out_dir = os.path.join(W, "gen", d["id"])
        meta = os.path.join(out_dir, "meta.json")
        if os.path.exists(meta):
            continue
        src = open(d["file"], encoding="utf-8").read()
        gen, usage = ask(prefix_for(src, d), d["name"])
        text, s, e = splice(src, d, gen)
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, os.path.basename(d["file"])), "w") as fh:
            fh.write(text)
        with open(meta, "w") as fh:
            json.dump({**d, "gen": gen, "bodyStart": s, "bodyEnd": e, "usage": usage, "orig": src[d["rhsStart"]:d["rhsEnd"]]}, fh)
        done += 1
        el = time.time() - t0
        print(f"[gen {done}/{len(ok)}] {d['id']} {d['name']} {usage.get('completion_tokens')} tok  elapsed {el:.0f}s  eta {el / done * (len(ok) - done):.0f}s", flush=True)

    check_path = os.path.join(W, "check.jsonl")
    metas = [json.load(open(os.path.join(W, "gen", d["id"], "meta.json"))) for d in ok]
    if not os.path.exists(check_path):
        print(f"typechecking {len(metas)} spliced files ...", flush=True)
        tool("check", *[os.path.join(W, "gen", m["id"], os.path.basename(m["file"])) for m in metas], out=check_path)
    checks = {os.path.basename(os.path.dirname(r["file"])): r for r in jsonl(check_path)}
    report(metas, checks, base)


def report(metas, checks, base):
    kinds, body_err, outside_delta, n_bad, n_all_mask, crashes = collections.Counter(), 0, 0, 0, 0, 0
    examples = collections.defaultdict(list)
    per_body = []
    for m in metas:
        c = checks.get(m["id"])
        if c is None or "crash" in c:
            crashes += 1
            continue
        inb = [e for e in c["errors"] if m["bodyStart"] <= e["offset"] < m["bodyEnd"]]
        outside = len(c["errors"]) - len(inb) - len(base[m["file"]]["errors"])
        outside_delta += max(0, outside)
        # the compiler often reports one error twice (same offset and id); count distinct
        inb = list({(e["id"], e["offset"]): e for e in inb}.values())
        for e in inb:
            kinds[e["id"]] += 1
            if len(examples[e["id"]]) < 4:
                examples[e["id"]].append(f"{m['id']} {m['name']}: {e['msg'].splitlines()[0][:140]}")
        body_err += len(inb)
        if inb:
            n_bad += 1
            if all(e["id"] in MASKABLE for e in inb):
                n_all_mask += 1
        per_body.append(len(inb))
    n = len(per_body)
    mask = sum(v for k, v in kinds.items() if k in MASKABLE)
    print(f"\nbodies typechecked: {n} (crashes {crashes})")
    print(f"bodies with >=1 error in the body: {n_bad} ({n_bad / max(1, n):.0%}); errors in bodies: {body_err}; new errors outside bodies: {outside_delta}")
    print(f"not-a-member / not-found: {mask} of {body_err} body errors ({mask / max(1, body_err):.0%})")
    print(f"bodies whose only errors are not-a-member / not-found: {n_all_mask} of {n_bad} bodies with errors")
    print("\nby error id:")
    for k, v in kinds.most_common():
        print(f"  {v:4}  {k}")
        for ex in examples[k]:
            print(f"          {ex}")


if __name__ == "__main__":
    main()
