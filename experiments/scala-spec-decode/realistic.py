"""Phase 0a, realistic variant: drafts from the qualifier type's real member names instead of an oracle.

For each typed selection `qual.name` (jvm/Tool.scala `selects`: widened qualifier type, its term member names), the model's first
token fixes a prefix of the name; a linear draft can then extend only to the longest common prefix of the members that start
with it. Counts the tokens of the name that such a draft covers (after the first), against the oracle count from ceiling.py.
Names that are not members of the widened type (extension methods, implicit conversions) get no draft here.

Usage: python realistic.py <selects.jsonl>
"""
import bisect, collections, json, os, sys

from transformers import AutoTokenizer

from ceiling import MODEL


def lcp(names):
    s, e = min(names), max(names)
    i = 0
    while i < len(s) and s[i] == e[i]:
        i += 1
    return s[:i]


def main():
    tok = AutoTokenizer.from_pretrained(MODEL)
    members, sels = {}, collections.defaultdict(list)
    for line in open(sys.argv[1]):
        if not line.startswith("{"):
            continue
        r = json.loads(line)
        if "members" in r:
            members[r["type"]] = r["members"]
        else:
            sels[r["file"]].append(r)
    T = oracle = real = n = notmember = unique_after_first = 0
    cand_sizes = []
    for f, rs in sels.items():
        src = open(f, encoding="utf-8").read()
        offs = tok(src, return_offsets_mapping=True, add_special_tokens=False)["offset_mapping"]
        T += len(offs)
        starts = [a for a, _ in offs]
        for r in rs:
            s, name = r["start"], r["name"]
            e = s + len(name)
            i = bisect.bisect_right(starts, s) - 1
            idx = []
            while i < len(offs) and offs[i][0] < e:
                if offs[i][1] > s:
                    idx.append(i)
                i += 1
            if not idx:
                continue
            n += 1
            oracle += len(idx) - 1
            mem = members[r["type"]]
            if name not in mem:
                notmember += 1
                continue
            p = name[:min(offs[idx[0]][1], e) - s]          # the part of the name the model's first token chose
            cands = [m for m in mem if m.startswith(p)]
            cand_sizes.append(len(cands))
            ext = lcp(cands)                                  # a linear draft reaches this far
            if len(cands) == 1:
                unique_after_first += 1
                real += len(idx) - 1
            else:
                real += sum(1 for j in idx[1:] if offs[j][1] <= s + len(ext))
    print(f"files {len(sels)}  tokens {T}  typed selections {n}  (name not a member of the widened type: {notmember})")
    print(f"oracle drafted   {oracle:7} = {oracle / T:.2%} of tokens -> x{T / (T - oracle):.3f}")
    print(f"members drafted  {real:7} = {real / T:.2%} of tokens -> x{T / (T - real):.3f}   ({real / max(1, oracle):.0%} of oracle)")
    print(f"first token leaves one candidate: {unique_after_first / max(1, n):.0%} of selections; "
          f"median candidates {sorted(cand_sizes)[len(cand_sizes) // 2] if cand_sizes else 0}")


if __name__ == "__main__":
    main()
