"""Narrate script.json through the gateway and write the timeline the Remotion project renders against.

    python3 examples/showcase/narrate.py [--model clone] [--voice narrator] [--out data/showcase]

Per scene: /api/speak (wav + duration), then /api/transcribe (word timestamps). `[[cue]]` markers in the narration are stripped
before synthesis and resolved to the start time of the first spoken word after the marker, by aligning the script's words with
Whisper's. Animations key off cue names, so re-recording a line re-times its scene. Also extracts the real data shown on screen
(the Alice phrase, the triage ROC curves, model sizes) into data.json. Standard library only.
"""
import argparse, array, difflib, json, math, re, shutil, tomllib, urllib.request, wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CUE = re.compile(r"\[\[(\w+)\]\]")


def post(base, path, body):
    req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.load(r)


def norm(w):
    return re.sub(r"[^a-z0-9]", "", w.lower())


def split_cues(narration):
    """-> (text without markers, {cue: index of the script word that follows it})"""
    words, cues = [], {}
    for tok in narration.split():
        m = CUE.fullmatch(tok)
        if m:
            cues[m.group(1)] = len(words)
        else:
            words.append(tok)
    return " ".join(words), words, cues


def resolve(script_words, spoken, cues, duration):
    """Map script word indices to spoken word start times via a sequence alignment of normalised words."""
    a, b = [norm(w) for w in script_words], [norm(w["word"]) for w in spoken]
    to_spoken = {}
    for blk in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(blk.size):
            to_spoken[blk.a + k] = blk.b + k
    out = {}
    for name, i in cues.items():
        j = next((to_spoken[k] for k in range(i, len(a)) if k in to_spoken), None)
        if j is None:                                   # nothing aligned after the marker: interpolate by word position
            out[name] = round(duration * i / max(len(a), 1), 3)
        else:
            out[name] = round(spoken[j]["start_s"], 3)
    return out


def peaks(path, fps=30):
    """RMS level per video frame, normalised to the loudest frame (PCM16 mono wav), for drawing the waveform."""
    with wave.open(str(path)) as w:
        n, sr = w.getnframes(), w.getframerate()
        a = array.array("h", w.readframes(n))
    step = sr // fps
    rms = [math.sqrt(sum(x * x for x in a[i:i + step]) / max(len(a[i:i + step]), 1)) for i in range(0, len(a), step)]
    top = max(rms) or 1
    return [round(r / top, 3) for r in rms]


def roc(results, label):
    pts = sorted(((r["p"][label][1], r["truth"][label]) for r in results), reverse=True)     # p = [contradiction, entailment, neutral]
    P = sum(t for _, t in pts); N = len(pts) - P
    tp = fp = 0; curve = [(0.0, 0.0)]; auc = 0.0
    for s, t in pts:
        if t: tp += 1
        else:
            fp += 1; auc += tp / P
        curve.append((fp / N, tp / P))
    at_half = sum(1 for s, t in pts if s >= 0.5)
    prec = sum(1 for s, t in pts if s >= 0.5 and t) / max(at_half, 1)
    tp_half = sum(1 for s, t in pts if s >= 0.5 and t)
    return {"label": label, "auc": round(auc / N, 3), "positives": P, "n": len(pts),
            "at_half": [round((at_half - tp_half) / N, 4), round(tp_half / P, 4)],
            "curve": [[round(x, 4), round(y, 4)] for x, y in curve], "precision_at_half": round(prec, 3), "flagged_at_half": at_half}


def extract_data(out):
    cat = tomllib.load(open(ROOT / "gateway.toml", "rb"))
    sizes = {k: v.get("est_mem_gb") or round(v["weights_gb"] + v.get("kv_gb", 0) + v.get("overhead_gb", 0), 1) for k, v in cat["backends"].items()}
    events = [json.loads(l) for l in open(ROOT / "data/book_events.jsonl")]
    words = [w["w"] for w in json.load(open(ROOT / "data/book_words.json"))]
    e = next(x for x in events if x["i"] == 17)                       # "when suddenly a White Rabbit"
    phrase = {"text": e["text"], "before": " ".join(words[max(0, e["w0"] - 15):e["w0"]]), "after": " ".join(words[e["w1"]:e["w1"] + 5]),
              "emoji": e["alts"][:8], "top": e["top"], "attrs": e["attrs"]}
    sample = [{"text": x["text"], "top": [t["e"] for t in x["top"]], "color": x["attrs"]["color"]["top"]} for x in events[:60]]
    results = [json.loads(l) for l in open(ROOT / "data/results.jsonl")]
    prs = [{"number": r["number"], "title": r["title"]} for r in results[:40]]
    rocs = [roc(results, l) for l in ["internal", "docs", "collections", "repl", "perf", "release_notes"]]
    (out / "data.json").write_text(json.dumps({"sizes": sizes, "budget_gb": cat["gateway"]["memory_budget_gb"], "phrase": phrase,
                                               "book_sample": sample, "book_phrases": len(events), "prs": prs, "rocs": rocs}, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", default=str(Path(__file__).with_name("script.json")))
    ap.add_argument("--gateway", default="http://127.0.0.1:8090")
    ap.add_argument("--model", default="clone")
    ap.add_argument("--voice", default=None, help="reference clip name for clone (default: narrator), or a Kokoro preset")
    ap.add_argument("--out", default=str(ROOT / "data/showcase"))
    ap.add_argument("--only", help="comma-separated scene ids to (re)narrate; others keep their previous timeline entry")
    a = ap.parse_args()
    out = Path(a.out); (out / "audio").mkdir(parents=True, exist_ok=True)
    script = json.load(open(a.script))
    old = {s["id"]: s for s in json.load(open(out / "timeline.json"))["scenes"]} if (out / "timeline.json").exists() else {}
    only = set(a.only.split(",")) if a.only else None

    scenes = []
    for sc in script["scenes"]:
        if only is not None and sc["id"] not in only and sc["id"] in old:
            scenes.append(old[sc["id"]]); continue
        text, script_words, cues = split_cues(sc["narration"])
        body = {"model": a.model, "text": text, "name": f"showcase-{sc['id']}"}
        if a.voice:
            body["voice" if a.model == "kokoro" else "ref_audio"] = a.voice
        clip = post(a.gateway, "/api/speak", body)
        words = post(a.gateway, "/api/transcribe", {"path": clip["path"], "words": True})["words"]
        shutil.copy(clip["path"], out / "audio" / f"{sc['id']}.wav")
        times = resolve(script_words, words, cues, clip["duration_s"])
        missing = [c for c in cues if c not in times]
        print(f"{sc['id']:<10} {clip['duration_s']:6.2f}s {'(cached)' if clip.get('cached') else ''}  cues: {times}" + (f"  MISSING {missing}" if missing else ""), flush=True)
        scenes.append({"id": sc["id"], "audio": f"audio/{sc['id']}.wav", "duration_s": clip["duration_s"], "lead_s": sc.get("lead", 0.3),
                       "tail_s": sc.get("tail", 0.6), "text": text, "cues": times, "peaks": peaks(out / "audio" / f"{sc['id']}.wav"),
                       "words": [{"w": w["word"], "s": round(w["start_s"], 3), "e": round(w["end_s"], 3)} for w in words]})
    (out / "timeline.json").write_text(json.dumps({"fps": 30, "scenes": scenes}, ensure_ascii=False, indent=1))
    extract_data(out)
    total = sum(s["lead_s"] + s["duration_s"] + s["tail_s"] for s in scenes)
    print(f"\n{out / 'timeline.json'}: {len(scenes)} scenes, {total:.1f}s")


if __name__ == "__main__":
    main()
