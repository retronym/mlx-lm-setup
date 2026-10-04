"""Narrate script.json through the gateway and write the timeline the Remotion project renders against.

    python3 examples/showcase/narrate.py [--model clone] [--voice narrator] [--out data/showcase]

The gateway's /api/narrate (the `narrate` MCP tool) does the work: it speaks each scene, transcribes it, and resolves the `[[cue]]`
markers to the start time of the word after each one. This script adds what only a renderer needs: copies of the wavs, waveform
peaks per video frame, the scene padding from script.json, and the real data shown on screen (the Alice phrase, the triage ROC
curves, model sizes) in data.json. Standard library only.
"""
import argparse, array, json, math, shutil, tomllib, urllib.request, wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def post(base, path, body):
    req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.load(r)


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
    """The real numbers shown on screen: catalog sizes, triage ROC curves, the live card sort and PR decision (cards.py), and the
    production counts for the timesheet (draft.py, cards.py, this script)."""
    cat = tomllib.load(open(ROOT / "gateway.toml", "rb"))
    sizes = {k: v.get("est_mem_gb") or round(v["weights_gb"] + v.get("kv_gb", 0) + v.get("overhead_gb", 0), 1) for k, v in cat["backends"].items()}
    results = [json.loads(l) for l in open(ROOT / "data/results.jsonl")]
    prs = [{"number": r["number"], "title": r["title"]} for r in results[:40]]
    rocs = [roc(results, l) for l in ["internal", "docs", "collections", "repl", "perf", "release_notes"]]
    load = lambda name: json.load(open(out / name)) if (out / name).exists() else None
    (out / "data.json").write_text(json.dumps({"sizes": sizes, "budget_gb": cat["gateway"]["memory_budget_gb"], "prs": prs, "rocs": rocs,
                                               "cards": load("cards.json"), "drafts": load("draft_stats.json"), "script_check": load("script_check.json")},
                                              ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", default=str(Path(__file__).with_name("script.json")))
    ap.add_argument("--gateway", default="http://127.0.0.1:8090")
    ap.add_argument("--model", default=None, help="speech model (default: the gateway's cloned narrator)")
    ap.add_argument("--voice", default=None, help="reference clip name for clone (default: narrator), or a Kokoro preset")
    ap.add_argument("--out", default=str(ROOT / "data/showcase"))
    ap.add_argument("--only", help="comma-separated scene ids to (re)narrate; others keep their previous timeline entry")
    a = ap.parse_args()
    out = Path(a.out); (out / "audio").mkdir(parents=True, exist_ok=True)
    script = json.load(open(a.script))
    old = {s["id"]: s for s in json.load(open(out / "timeline.json"))["scenes"]} if (out / "timeline.json").exists() else {}
    only = set(a.only.split(",")) if a.only else None

    todo = [sc for sc in script["scenes"] if only is None or sc["id"] in only or sc["id"] not in old]
    body = {"model": a.model, "scenes": [{"id": sc["id"], "text": sc["narration"]} for sc in todo]}
    if a.voice:
        body["voice" if a.model == "kokoro" else "ref_audio"] = a.voice
    fresh = {r["id"]: r for r in post(a.gateway, "/api/narrate", body)["scenes"]} if todo else {}
    scenes = []
    for sc in script["scenes"]:
        r = fresh.get(sc["id"])
        if r is None:
            scenes.append(old[sc["id"]]); continue
        wav = out / "audio" / f"{sc['id']}.wav"
        shutil.copy(r["path"], wav)
        odd = [d for d in r["transcript_differs"] if not any(c.isdigit() for c in d["heard"])]
        print(f"{sc['id']:<10} {r['duration_s']:6.2f}s {'(cached)' if r['cached'] else ''}  cues: {r['cues']}" + (f"  HEARD DIFFERENTLY: {odd}" if odd else ""), flush=True)
        scenes.append({"id": sc["id"], "audio": f"audio/{sc['id']}.wav", "duration_s": r["duration_s"], "lead_s": sc.get("lead", 0.3),
                       "tail_s": sc.get("tail", 0.6), "text": r["text"], "cues": r["cues"], "peaks": peaks(wav),
                       "words": [{"w": w["word"], "s": round(w["start_s"], 3), "e": round(w["end_s"], 3)} for w in r["script_words"]]})
    (out / "timeline.json").write_text(json.dumps({"fps": 30, "scenes": scenes}, ensure_ascii=False, indent=1))
    extract_data(out)
    total = sum(s["lead_s"] + s["duration_s"] + s["tail_s"] for s in scenes)
    print(f"\n{out / 'timeline.json'}: {len(scenes)} scenes, {total:.1f}s")


if __name__ == "__main__":
    main()
