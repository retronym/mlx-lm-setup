"""Shared question definitions + state construction for the Jev-Style scorer (used by score_worker.py and recolor.py)."""
BEFORE, AFTER = 15, 5                                   # context window (words) around the marked chunk
PTR = "the passage marked ⟦ ⟧ in the text"
COLORS = ["red", "orange", "yellow", "green", "blue", "purple", "pink", "brown", "black", "white", "grey", "gold"]

COLOR_Q = {"t": "choice",
           "ins": f"Imagine {PTR} as a mood board. Which single colour best captures its vibe, feeling and atmosphere?",
           "crit": {n: None for n in COLORS}}           # no abstain option: the confidence threshold decides what gets tinted
ATTRS = {
    "color": COLOR_Q,
    "mood": {"t": "choice", "ins": f"What is the dominant mood of {PTR}?",
             "crit": {n: None for n in ["joyful", "sad", "afraid", "angry", "curious", "calm", "surprised", "tired", "confused", "neutral"]}},
    "sentiment": {"t": "score", "ins": f"How positive is the tone of {PTR}?",
                  "crit": ["very negative", "negative", "neutral", "positive", "very positive"]},
}


def emoji_question(names):
    return {"t": "choice", "ins": f"Which emoji best expresses the meaning or mood of {PTR}?", "crit": {n: None for n in names}}


def make_state(words, w0, w1):
    chunk = " ".join(words[w0:w1])
    return (" ".join(words[max(0, w0 - BEFORE):w0]) + " ⟦" + chunk + "⟧ " + " ".join(words[w1:w1 + AFTER])).strip()


def attr_result(name, r):
    pr = r["probabilities"]
    if name == "sentiment":
        return {"score": round(sum(int(k) * v for k, v in pr.items()) - 2, 3), "probs": {k: round(v, 3) for k, v in pr.items()}}
    return {"top": r["answer"], "p": round(r["top_probability"], 3),
            "probs": {k: round(v, 3) for k, v in sorted(pr.items(), key=lambda kv: -kv[1])[:4]}}
