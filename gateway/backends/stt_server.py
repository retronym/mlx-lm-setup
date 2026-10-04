"""Speech-to-text backend on mlx-audio (Whisper) as a small server.  Runs in .venv-audio.

  POST /transcribe   {"path": "<file under --audio-dir or --refs-dir>", "language"?: "en", "words"?: true}
                     -> {"text", "language", "duration_s", "segments": [{"start_s","end_s","text"}], "words": [{"word","start_s","end_s"}]}

Word timestamps are what a renderer needs for captions and word-synced highlights. Only files under the allowed directories can be
read, so the endpoint cannot be used to probe the rest of the disk.
"""
import argparse, os, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("--model", required=True)
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--audio-dir", required=True)
ap.add_argument("--refs-dir", required=True)
a = ap.parse_args()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _http import serve                                             # noqa: E402

t0 = time.time()
from mlx_audio.stt import load                                      # noqa: E402
model = load(a.model)
print(f"model loaded in {time.time() - t0:.1f}s", flush=True)
ALLOWED = [os.path.realpath(d) for d in (a.audio_dir, a.refs_dir)]


def transcribe(req: dict) -> dict:
    path = os.path.realpath(str(req.get("path") or ""))
    if not any(path == d or path.startswith(d + os.sep) for d in ALLOWED):
        raise ValueError(f"path must be inside {ALLOWED}")
    if not os.path.isfile(path):
        raise ValueError(f"no such file: {path}")
    words = bool(req.get("words", True))
    kw = {"word_timestamps": words}
    if req.get("language"):
        kw["language"] = req["language"]
    t1 = time.time()
    r = model.generate(path, verbose=False, **kw)
    segs, wds = [], []
    for s in r.segments or []:
        segs.append({"start_s": round(s["start"], 3), "end_s": round(s["end"], 3), "text": s["text"].strip()})
        for w in s.get("words") or []:
            wds.append({"word": w["word"].strip(), "start_s": round(w["start"], 3), "end_s": round(w["end"], 3)})
    return {"text": r.text.strip(), "language": getattr(r, "language", None), "duration_s": segs[-1]["end_s"] if segs else 0.0,
            "segments": segs, "words": wds, "secs": round(time.time() - t1, 2)}


serve(a.port, {"model": a.model, "load_s": round(time.time() - t0, 1)}, {"/transcribe": transcribe})
