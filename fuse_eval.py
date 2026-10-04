import json, numpy as np
D = "data"
T = json.load(open(f"{D}/fuse_texts.json"))
LM = json.load(open(f"{D}/fuse_lm.json")); JV = json.load(open(f"{D}/fuse_jev.json"))
G = LM["glyphs"]; assert G == JV["glyphs"]
lm = np.array(LM["scores"]); jv = np.array(JV["scores"])

def z(x, floor=None):
    x = np.array(x, float)
    if floor is not None: x = np.maximum(x, x.max() - floor)       # LM logprobs have a huge negative tail: clip so z isn't tail-dominated
    return (x - x.mean()) / (x.std() + 1e-9)
def rrf(x, k=10):
    r = np.empty(len(x)); r[np.argsort(-x)] = np.arange(1, len(x) + 1); return 1.0 / (k + r)

def fuse(i, w, how, floor):
    if how == "z": return w * z(lm[i], floor) + (1 - w) * z(jv[i])
    return w * rrf(lm[i]) + (1 - w) * rrf(jv[i])

for t in T: t["want"] = [g for g in t["want"] if g in G]
slang = [i for i, t in enumerate(T) if t["kind"] == "slang" and t["want"]]
def eval_cfg(w, how, floor):
    h1 = h3 = 0; ranks = []
    for i in slang:
        s = fuse(i, w, how, floor); order = [G[j] for j in np.argsort(-s)]
        r = min(order.index(g) + 1 for g in T[i]["want"]); ranks.append(r); h1 += r == 1; h3 += r <= 3
    return h1, h3, float(np.mean(np.log2(ranks) + 1))   # geometric-ish mean rank
print(f"{len(slang)} slang probes. columns: top-1 hits, top-3 hits, mean log-rank (lower better)\n")
print(f"{'method':26s} " + " ".join(f"w_lm={w:<4}" for w in (0, .25, .5, .75, 1)))
for how, floor in (("z", 12), ("z", 20), ("z", None), ("rrf", None)):
    name = f"{how}" + (f" floor {floor}" if floor else "")
    cells = []
    for w in (0, .25, .5, .75, 1):
        h1, h3, lr = eval_cfg(w, how, floor); cells.append(f"{h1:2d}/{h3:2d}/{lr:4.1f}")
    print(f"{name:26s} " + "  ".join(cells))

print("\nbook phrases, top-3 by method:  JEV | LM | FUSED(z floor 12, w_lm=.5)")
for i, t in enumerate(T):
    if t["kind"] != "book": continue
    tops = [ "".join(G[j] for j in np.argsort(-s)[:3]) for s in (z(jv[i]), z(lm[i], 12), fuse(i, .5, "z", 12))]
    print(f"{t['text'][:44]:44s} {tops[0]:8s} {tops[1]:8s} {tops[2]:8s}")
print("\nslang probes: want | JEV | LM | FUSED")
for i in slang:
    tops = ["".join(G[j] for j in np.argsort(-s)[:3]) for s in (z(jv[i]), z(lm[i], 12), fuse(i, .5, "z", 12))]
    print(f"{T[i]['text'][:40]:40s} {''.join(T[i]['want']):5s} {tops[0]:8s} {tops[1]:8s} {tops[2]:8s}")
