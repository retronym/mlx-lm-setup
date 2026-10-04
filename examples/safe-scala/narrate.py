"""Narrate script.json through the gateway and write the timeline the Remotion project renders against.

    python3 examples/safe-scala/narrate.py [--only cold,end] [--out data/safe-scala]

The gateway's /api/narrate (the `narrate` MCP tool) does the work: it speaks each scene, transcribes it, and resolves the `[[cue]]`
markers to the start time of the word after each one. This script adds what only a renderer needs: copies of the wavs, waveform
peaks per video frame, the scene padding from script.json, and the facts shown on screen (facts.json from facts.py) as
data.json. The voice is the saved reference named in script.json ("voice"), spoken by the clone model. Standard library only.
"""
import argparse, array, json, math, shutil, urllib.request, wave
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


def extract_data(out):
    """data.json is facts.json (from facts.py: the paper's numbers, checked against arXiv, and today's compiler output)."""
    facts = out / "facts.json"
    if not facts.exists():
        raise SystemExit(f"{facts} missing: run facts.py first")
    shutil.copy(facts, out / "data.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", default=str(Path(__file__).with_name("script.json")))
    ap.add_argument("--gateway", default="http://127.0.0.1:8090")
    ap.add_argument("--model", default="qwen3-tts-clone")
    ap.add_argument("--voice", default=None, help="reference voice (default: script.json's voice)")
    ap.add_argument("--out", default=str(ROOT / "data/safe-scala"))
    ap.add_argument("--only", help="comma-separated scene ids to (re)narrate; others keep their previous timeline entry")
    a = ap.parse_args()
    out = Path(a.out); (out / "audio").mkdir(parents=True, exist_ok=True)
    script = json.load(open(a.script))
    old = {s["id"]: s for s in json.load(open(out / "timeline.json"))["scenes"]} if (out / "timeline.json").exists() else {}
    only = set(a.only.split(",")) if a.only else None

    todo = [sc for sc in script["scenes"] if only is None or sc["id"] in only or sc["id"] not in old]
    body = {"model": a.model, "scenes": [{"id": sc["id"], "text": sc["narration"]} for sc in todo]}
    body["ref_audio"] = a.voice or script["voice"]
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
                       "words": [{"w": w["word"], "s": round(w["start_s"], 3), "e": round(w["end_s"], 3)} for w in r["words"]]})
    (out / "timeline.json").write_text(json.dumps({"fps": 30, "scenes": scenes}, ensure_ascii=False, indent=1))
    extract_data(out)
    total = sum(s["lead_s"] + s["duration_s"] + s["tail_s"] for s in scenes)
    print(f"\n{out / 'timeline.json'}: {len(scenes)} scenes, {total:.1f}s")


if __name__ == "__main__":
    main()
