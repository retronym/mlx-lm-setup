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
                        os.path.join(HERE, "jvm"), "--main-class", "Tool", "--", *args],
                       env={**os.environ, "CP": cp}, stdout=fh, check=True)
    if os.path.getsize(out) == 0:
        raise RuntimeError(f"jvm/Tool.scala {args[0]} printed nothing (did it compile?)")


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


MEMBER_START = re.compile(r"\s*(def|val|var|lazy|override|private|protected|final|inline|given|@|class|object|trait|case class|end\b|/\*\*|//)")


def clean_gen(gen: str, name: str) -> str:
    """The chat model often repeats the signature, nests a fence, or runs on into further members: keep only the body."""
    gen = re.sub(r"^\s*```\w*\n", "", gen)
    gen = gen.split("```")[0]
    m = re.match(r"\s*(?:(?:override|private|protected|final|inline)(?:\[\w+\])?\s+)*def\s+" + re.escape(name) + r"\b", gen)
    if m:                                                   # drop the repeated signature: up to its `=` at bracket depth 0
        depth, i = 0, m.end()
        while i < len(gen):
            c = gen[i]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == "=" and depth == 0 and gen[i + 1:i + 2] not in ("=", ">") and gen[i - 1] not in "=!<>":
                gen = gen[i + 1:]
                break
            i += 1
    lines = gen.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return ""
    if len(lines) > 1:                                      # cut where a member-level line follows the body
        base = min(len(l) - len(l.lstrip()) for l in lines[1:] if l.strip()) if any(l.strip() for l in lines[1:]) else 0
        for k in range(2, len(lines)):
            l = lines[k]
            if l.strip() and len(l) - len(l.lstrip()) < base and MEMBER_START.match(l):
                lines = lines[:k]
                break
    return "\n".join(lines)


def splice(src: str, d, gen: str) -> tuple[str, int, int]:
    """Put the generated body after the `=`, re-indented if the model dropped the indentation. Returns text, body start, end."""
    gen = clean_gen(gen, d["name"]).rstrip()
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
        os.makedirs(out_dir, exist_ok=True)
        with open(meta, "w") as fh:
            json.dump({**d, "gen": gen, "usage": usage, "orig": src[d["rhsStart"]:d["rhsEnd"]]}, fh)
        done += 1
        el = time.time() - t0
        print(f"[gen {done}/{len(ok)}] {d['id']} {d['name']} {usage.get('completion_tokens')} tok  elapsed {el:.0f}s  eta {el / done * (len(ok) - done):.0f}s", flush=True)

    check_path = os.path.join(W, "check.jsonl")
    metas = [json.load(open(os.path.join(W, "gen", d["id"], "meta.json"))) for d in ok]
    for m in metas:
        src = open(m["file"], encoding="utf-8").read()
        text, m["bodyStart"], m["bodyEnd"] = splice(src, m, m["gen"])
        path = os.path.join(W, "gen", m["id"], os.path.basename(m["file"]))
        if not os.path.exists(path) or open(path, encoding="utf-8").read() != text:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            if os.path.exists(check_path):
                os.remove(check_path)                       # the splice changed: typecheck again
    if not os.path.exists(check_path):
        print(f"typechecking {len(metas)} spliced files ...", flush=True)
        tool("check", *[os.path.join(W, "gen", m["id"], os.path.basename(m["file"])) for m in metas], out=check_path)
    checks = {os.path.basename(os.path.dirname(r["file"])): r for r in jsonl(check_path)}
    report(metas, checks, base)


SYNTAX = {"ExpectedStartOfTopLevelDefinitionID", "IllegalStartOfStatementID", "ExpectedTokenButFoundID", "NoExplanationID"}


def report(metas, checks, base):
    """Bodies fall in three groups: runaway (hit max_tokens, usually a repetition loop), syntax (parse or indentation errors, or
    new errors outside the body), well-formed. Name errors are counted on the well-formed ones, where they are not parse fallout."""
    groups = collections.defaultdict(list)
    for m in metas:
        c = checks[m["id"]]
        inb_all = [e for e in c["errors"] if m["bodyStart"] <= e["offset"] < m["bodyEnd"]]
        outside = len(c["errors"]) - len(inb_all) - len(base[m["file"]]["errors"])
        inb = list({(e["id"], e["offset"]): e for e in inb_all}.values())   # the compiler often reports one error twice
        if m["usage"].get("completion_tokens", 0) >= 1200:
            g = "runaway"
        elif outside > 0 or any(e["id"] in SYNTAX for e in inb):
            g = "syntax"
        else:
            g = "well-formed"
        groups[g].append((m, inb))
    n = len(metas)
    print(f"\nbodies: {n}; " + ", ".join(f"{g} {len(v)}" for g, v in groups.items()))
    for g in ("well-formed", "syntax"):
        rows = groups[g]
        kinds, examples = collections.Counter(), collections.defaultdict(list)
        has = collections.Counter()
        for m, inb in rows:
            for e in inb:
                kinds[e["id"]] += 1
                if len(examples[e["id"]]) < 3:
                    examples[e["id"]].append(f"{m['id']} {m['name']}: {e['msg'].splitlines()[0][:130]}")
            for k in {e["id"] for e in inb}:
                has[k] += 1
        bad = [r for r in rows if r[1]]
        mask_bodies = sum(1 for _, inb in bad if any(e["id"] in MASKABLE for e in inb))
        only_mask = sum(1 for _, inb in bad if all(e["id"] in MASKABLE for e in inb))
        tot = sum(kinds.values())
        mask = sum(v for k, v in kinds.items() if k in MASKABLE)
        print(f"\n[{g}] {len(rows)} bodies, {len(bad)} with errors, {tot} distinct errors")
        print(f"  not-a-member / not-found: {mask} of {tot} errors ({mask / max(1, tot):.0%}); "
              f"in {mask_bodies} of {len(bad)} bodies with errors; the only kind in {only_mask}")
        print("  by error id (errors, bodies):")
        for k, v in kinds.most_common():
            print(f"    {v:4} {has[k]:3}  {k}")
            for ex in examples[k]:
                print(f"               {ex}")


if __name__ == "__main__":
    main()
