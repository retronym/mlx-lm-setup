"""Phase 0a: the ceiling on what a perfect compiler draft could save.

Tokenises Scala sources with Qwen3-Coder's tokenizer and counts the tokens that sit inside a selected name (`qual.<name>`) after
the first token that touches it. Those are the tokens a perfect draft (the model has committed to a member, the compiler knows
the rest of its name) would get accepted without a decode step each. The first token is the model's choice and is never
drafted; Qwen's BPE usually merges the `.` with the start of the name (`.symbol`), so that first token often is the whole name.

Selections are found lexically (comments, strings and char literals are masked); a hop counts the selections in a chain
`a.b.c` (b is hop 1, c is hop 2). A selection after `)`, `]` or `}` starts a new chain, numbered as "after-apply" hops.
Imports and package clauses are reported apart, because their drafts come from packages, not from a type.

Usage: python ceiling.py <dir-or-file>... [--json out.json]
"""
import argparse, bisect, collections, json, os, re, sys

from transformers import AutoTokenizer

MODEL = "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit"
IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*|`[^`\n]+`")


def mask_non_code(src: str) -> str:
    """Blank out comments and string/char literals (same length, newlines kept), so selections inside them are not counted."""
    out = list(src)
    i, n = 0, len(src)

    def blank(a, b):
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(i, j); i = j
        elif src.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if src.startswith("/*", j): depth += 1; j += 2
                elif src.startswith("*/", j): depth -= 1; j += 2
                else: j += 1
            blank(i, j); i = j
        elif src.startswith('"""', i):
            j = src.find('"""', i + 3)
            j = n if j < 0 else j + 3
            while j < n and src[j] == '"': j += 1
            blank(i, j); i = j
        elif c == '"':
            j = i + 1
            while j < n and src[j] not in '"\n':
                j += 2 if src[j] == "\\" else 1
            blank(i, min(j + 1, n)); i = j + 1
        elif c == "'" and i + 2 < n and (src[i + 2] == "'" or (src[i + 1] == "\\" and "'" in src[i + 2:i + 8])):
            j = src.find("'", i + 2 if src[i + 1] != "\\" else i + 3)
            blank(i, j + 1); i = j + 1
        else:
            i += 1
    return "".join(out)


def selections(code: str):
    """Yield (name_start, name_end, kind, hop) for each `.name` in masked code. kind: path | after-apply | import | package."""
    line_kind = {}
    for m in re.finditer(r"(?m)^[ \t]*(import|export|package)\b[^\n]*", code):
        line_kind[m.start()] = m.group(1)
    for m in re.finditer(r"\.(?=([A-Za-z_$`]))", code):
        dot = m.start()
        if dot > 0 and code[dot - 1] == ".":
            continue
        if dot > 0 and code[dot - 1].isdigit() and not re.search(r"[A-Za-z_$][A-Za-z0-9_$]*$", code[max(0, dot - 40):dot]):
            continue                                       # 1.0e3 style numbers
        im = IDENT.match(code, dot + 1)
        if not im:
            continue
        # what precedes the dot: walk back over `ident(.ident)*`
        j, hop = dot, 1
        k = j - 1
        while k >= 0 and code[k] in " \t\r\n":           # leading-dot chains continue on the next line
            k -= 1
        prev = code[k] if k >= 0 else ""
        if prev in ")]}":
            kind = "after-apply"
            hop = 1
            # hops after an apply: count `.ident` segments between the bracket and this dot
            seg = re.search(r"[)\]}]((?:\s*\.(?:[A-Za-z_$][A-Za-z0-9_$]*|`[^`\n]+`))*)$", code[max(0, dot - 400):k + 1])
            if seg:
                hop = seg.group(1).count(".") + 1
        else:
            seg = re.search(r"(?:[A-Za-z_$][A-Za-z0-9_$]*|`[^`\n]+`)((?:\s*\.(?:[A-Za-z_$][A-Za-z0-9_$]*|`[^`\n]+`))*)$", code[max(0, dot - 400):k + 1])
            if not seg:
                continue
            hop = seg.group(1).count(".") + 1
            kind = "path"
        ls = code.rfind("\n", 0, dot) + 1
        lk = line_kind.get(ls)
        if lk is None:
            # indentation before import keyword
            stripped = code[ls:dot].lstrip()
            if stripped.startswith(("import ", "export ", "package ")):
                lk = stripped.split()[0]
        if lk in ("import", "export"):
            kind = "import"
        elif lk == "package":
            kind = "package"
        yield im.start(), im.end(), kind, hop


def analyse(tok, src: str, acc):
    code = mask_non_code(src)
    enc = tok(src, return_offsets_mapping=True, add_special_tokens=False)
    offs = enc["offset_mapping"]
    acc["tokens"] += len(offs)
    acc["chars"] += len(src)
    # code tokens: tokens with any non-blank char in the masked text
    code_tok = sum(1 for a, b in offs if code[a:b].strip())
    acc["code_tokens"] += code_tok
    starts = [a for a, _ in offs]
    for s, e, kind, hop in selections(code):
        # tokens overlapping [s, e): the first one is the model's choice
        i = bisect.bisect_right(starts, s) - 1
        idx = []
        while i < len(offs) and offs[i][0] < e:
            if offs[i][1] > s:
                idx.append(i)
            i += 1
        n_over = len(idx)
        drafted = max(0, n_over - 1)
        hb = min(hop, 4)
        key = (kind, hb)
        acc["sel"][key] += 1
        acc["sel_tokens"][key] += n_over
        acc["drafted"][key] += drafted
        acc["hist"][kind, min(n_over, 6)] += 1
        first = offs[idx[0]] if idx else None
        if first and first[0] < s:
            acc["first_has_dot"] += 1
        acc["sels"] += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--json")
    a = ap.parse_args()
    tok = AutoTokenizer.from_pretrained(MODEL)
    acc = {"tokens": 0, "chars": 0, "code_tokens": 0, "sels": 0, "first_has_dot": 0, "files": 0,
           "sel": collections.Counter(), "sel_tokens": collections.Counter(), "drafted": collections.Counter(),
           "hist": collections.Counter()}
    files = []
    for p in a.paths:
        if os.path.isdir(p):
            for root, _, fs in os.walk(p):
                files += [os.path.join(root, f) for f in fs if f.endswith(".scala")]
        else:
            files.append(p)
    for f in sorted(files):
        with open(f, encoding="utf-8", errors="replace") as fh:
            analyse(tok, fh.read(), acc)
        acc["files"] += 1
    report(acc)
    if a.json:
        with open(a.json, "w") as fh:
            json.dump({k: ({"|".join(map(str, kk)): vv for kk, vv in v.items()} if isinstance(v, collections.Counter) else v)
                       for k, v in acc.items()}, fh, indent=1)


def report(acc):
    T, D = acc["tokens"], sum(acc["drafted"].values())
    print(f"files {acc['files']}  chars {acc['chars']}  tokens {T}  code tokens {acc['code_tokens']}  selections {acc['sels']}")
    print(f"first token of the name includes the '.': {acc['first_has_dot'] / max(1, acc['sels']):.1%}")
    print(f"\n{'kind':12} {'hop':>4} {'sels':>8} {'name toks':>10} {'drafted':>8} {'%all tok':>9} {'drafted/sel':>11}")
    for kind in ("path", "after-apply", "import", "package"):
        for hop in (1, 2, 3, 4):
            k = (kind, hop)
            if acc["sel"][k]:
                print(f"{kind:12} {('%d' % hop) + ('+' if hop == 4 else ''):>4} {acc['sel'][k]:8} {acc['sel_tokens'][k]:10} {acc['drafted'][k]:8} "
                      f"{acc['drafted'][k] / T:9.2%} {acc['drafted'][k] / acc['sel'][k]:11.2f}")
    no_imp = sum(v for (kind, _), v in acc["drafted"].items() if kind in ("path", "after-apply"))
    print(f"\ndrafted, all selections:        {D:8} = {D / T:.2%} of all tokens, {D / acc['code_tokens']:.2%} of code tokens -> step ceiling x{T / (T - D):.3f}")
    print(f"drafted, excl. import/package:  {no_imp:8} = {no_imp / T:.2%} of all tokens -> x{T / (T - no_imp):.3f}")
    print("\ntokens per selected name (path + after-apply):",
          {n: sum(acc['hist'][kk, n] for kk in ('path', 'after-apply')) for n in range(0, 7)})


if __name__ == "__main__":
    main()
