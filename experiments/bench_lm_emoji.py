"""Pick emoji straight from an LLM's next-token distribution (no option lists, no generation).

Few-shot "Phrase: ... / Emoji: X" prompt; one forward pass; softmax over the tokens that decode to a single emoji.
Run with the brew mlx-lm python:  /opt/homebrew/opt/mlx-lm/libexec/bin/python bench_lm_emoji.py [model]
"""
import json, os, sys, time, unicodedata
import numpy as np
import mlx.core as mx
from mlx_lm import load

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # the repo root: data/ and models/ live there
sys.path[:0] = [os.path.join(HERE, "experiments"), os.path.join(HERE, "pipelines", "emoji_book")]
MODEL = sys.argv[1] if len(sys.argv) > 1 else "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit"
FEWSHOT = """Phrase: The cat knocked the glass off the table and stared at me.
Emoji: 😼

Phrase: We finally got the keys to our first house!
Emoji: 🏠

Phrase: I am literally dying, that was so funny.
Emoji: 💀

Phrase: Her heart sank when she read the letter.
Emoji: 💔

Phrase: """

def is_emoji(s):
    s = s.strip()
    if not s:
        return False
    cps = [c for c in s if c not in "️‍"]
    return len(cps) >= 1 and all(unicodedata.category(c) == "So" or 0x1F300 <= ord(c) <= 0x1FAFF for c in cps)

t = time.time(); model, tok = load(MODEL); print(f"load {time.time()-t:.1f}s", flush=True)
vocab = tok.get_vocab() if hasattr(tok, "get_vocab") else tok._tokenizer.get_vocab()
emoji_ids, emoji_txt = [], []
for text, i in vocab.items():
    s = tok.decode([i])
    if is_emoji(s) and len(s.strip()) <= 2:
        emoji_ids.append(i); emoji_txt.append(s.strip())
print("single-token emoji in vocab:", len(emoji_ids), flush=True)
ids_arr = mx.array(emoji_ids)

def top_emoji(phrase, k=5):
    prompt = FEWSHOT + phrase + "\nEmoji:"
    x = mx.array(tok.encode(prompt))[None]
    logits = model(x)[0, -1].astype(mx.float32)
    lp = logits - mx.logsumexp(logits)               # log-prob over the whole vocab
    sel = np.array(lp[ids_arr])
    order = np.argsort(-sel)[:k]
    return [(emoji_txt[i], float(np.exp(sel[i]))) for i in order]

phrases = json.load(open(os.path.join(HERE, "data", "bench_phrases.json")))
probes = [
    "That was a very flirty, suggestive joke with an innuendo.", "Her rear view in those jeans was a real showstopper.",
    "I am literally dying, that was so hilarious.", "He is the greatest of all time, the GOAT.", "That outfit is fire.",
    "Don't lie to me, that is total cap.", "She was sweating bullets, nervous and soaked.",
    "He kept it real, a hundred percent honest.", "He is such a clown for believing her.", "Everyone was ghosting him after that.",
]
top_emoji(phrases[0])
tt = []
for label, texts in (("BOOK ", phrases), ("PROBE", probes)):
    for p in texts:
        t = time.time(); r = top_emoji(p); tt.append(time.time() - t)
        print(f"{label} {p[:52]:52s} " + " ".join(f"{e}{pr:.2f}" for e, pr in r[:4]), flush=True)
print(f"mean {np.mean(tt):.2f}s per phrase (one forward pass)")
