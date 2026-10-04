"""Text-to-speech backend on mlx-audio (Kokoro, Qwen3-TTS, Dia, ...) as a small server.  Runs in .venv-audio.

  POST /speak             {"text", "voice"?, "speed"?, "lang_code"?, "instruct"?, "ref_audio"?, "ref_text"?, "name"?, "fresh"?, "save_as_voice"?}
                          -> {"path", "duration_s", "sample_rate", "segments": [{"text", "start_s", "end_s"}], "voice", "gen_s", "cached"}
  POST /v1/audio/speech   OpenAI-compatible {"input", "voice"?, "speed"?} -> audio/wav bytes

The text is split into sentence groups; each group is synthesised on its own (so any text length works whatever the model's
context) and the pieces are joined with short pauses. The per-group start/end times come back as ``segments``: they are what a
video renderer needs to time captions and cuts. Clips are written under --output-dir, content-addressed by every parameter that
affects the sound, so asking for the same clip again is a cache hit (``fresh: true`` forces a new take).

Reference clips for voice cloning are named, not passed by path: ``ref_audio: "narrator"`` means <refs-dir>/narrator.wav, with its
transcript in <refs-dir>/narrator.txt (``ref_text`` overrides it).
"""
import argparse, hashlib, json, os, re, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("--model", required=True)
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--output-dir", required=True)
ap.add_argument("--refs-dir", required=True)
ap.add_argument("--voice", default=None)
ap.add_argument("--lang-code", default=None)
ap.add_argument("--ref-audio", default=None, help="default reference clip name for cloning models")
ap.add_argument("--max-chars", type=int, default=300, help="longest group of sentences synthesised in one call")
ap.add_argument("--pause-s", type=float, default=0.15, help="silence between groups in a paragraph")
ap.add_argument("--paragraph-pause-s", type=float, default=0.45, help="silence at a blank line")
a = ap.parse_args()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np                                                  # noqa: E402
import soundfile as sf                                              # noqa: E402
from _http import Raw, serve                                        # noqa: E402
from speechtext import NAME_OK, groups                              # noqa: E402

t0 = time.time()
from mlx_audio.tts.utils import load_model                          # noqa: E402
model = load_model(a.model)
SR = int(model.sample_rate)
print(f"model loaded in {time.time() - t0:.1f}s, sample rate {SR}", flush=True)
os.makedirs(a.output_dir, exist_ok=True)

def ref_paths(name: str) -> tuple[str, str | None]:
    if not NAME_OK.match(name):
        raise ValueError(f"ref_audio must be a clip name like 'narrator' (a file in the voices directory), got {name!r}")
    wav = os.path.join(a.refs_dir, name + ".wav")
    if not os.path.exists(wav):
        raise ValueError(f"no reference clip {name!r} (expected {wav})")
    txt = os.path.join(a.refs_dir, name + ".txt")
    return wav, (open(txt, encoding="utf-8").read().strip() if os.path.exists(txt) else None)


def save_voice(meta: dict, name: str, text: str) -> dict:
    """Keep this clip as a named reference voice (<refs-dir>/<name>.wav + .txt). With a voice-design model this is how a designed
    voice becomes reusable: every later scene clones it, instead of drawing a new random voice."""
    import shutil
    os.makedirs(a.refs_dir, exist_ok=True)
    shutil.copyfile(meta["path"], os.path.join(a.refs_dir, name + ".wav"))
    with open(os.path.join(a.refs_dir, name + ".txt"), "w", encoding="utf-8") as f:
        f.write(text)
    return {**meta, "saved_voice": name}


def synth(req: dict) -> dict:
    text = (req.get("text") or req.get("input") or "").strip()
    if not text:
        raise ValueError("text is empty")
    if len(text) > 20000:
        raise ValueError("text is longer than 20000 characters; split it into scenes")
    voice = req.get("voice") or a.voice
    speed = float(req.get("speed", 1.0))
    if not 0.5 <= speed <= 2.0:
        raise ValueError("speed must be between 0.5 and 2.0")
    kw: dict = {"speed": speed}
    if voice:
        kw["voice"] = voice
    lang = req.get("lang_code") or a.lang_code
    if "kokoro" in a.model.lower() and voice and not req.get("lang_code"):
        lang = voice[0]                                   # Kokoro voice names start with their language code: af_heart -> "a", bm_george -> "b"
    if lang:
        kw["lang_code"] = lang
    if req.get("instruct"):
        kw["instruct"] = req["instruct"]
    ref = req.get("ref_audio") or a.ref_audio
    if ref:
        kw["ref_audio"], ref_text = ref_paths(str(ref))
        ref_text = req.get("ref_text") or ref_text
        if ref_text:
            kw["ref_text"] = ref_text
    key = hashlib.sha1(json.dumps([a.model, text, {k: v for k, v in kw.items() if k != "ref_audio"}, ref,
                                   os.path.getmtime(kw["ref_audio"]) if ref else None, a.max_chars, a.pause_s, a.paragraph_pause_s],
                                  sort_keys=True).encode()).hexdigest()[:12]
    save_as = req.get("save_as_voice")
    if save_as is not None and not NAME_OK.match(str(save_as)):
        raise ValueError("save_as_voice may contain letters, digits, '.', '_' and '-' only")
    name = req.get("name")
    if name is not None and not NAME_OK.match(str(name)):
        raise ValueError("name may contain letters, digits, '.', '_' and '-' only")
    base = os.path.join(a.output_dir, f"{name or 'speech'}-{key}")
    wav_path, meta_path = base + ".wav", base + ".json"
    if not req.get("fresh") and os.path.exists(wav_path) and os.path.exists(meta_path):
        meta = {**json.load(open(meta_path)), "cached": True}
        return save_voice(meta, save_as, text) if save_as else meta

    t1 = time.time()
    pieces, segments, pos = [], [], 0.0
    for text_g, new_para in groups(text, a.max_chars):
        if pieces:
            gap = a.paragraph_pause_s if new_para else a.pause_s
            pieces.append(np.zeros(int(gap * SR), dtype=np.float32)); pos += gap
        chunks = [np.asarray(r.audio, dtype=np.float32).reshape(-1) for r in model.generate(text=text_g, verbose=False, **kw)]
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        if audio.size == 0:
            raise RuntimeError(f"the model produced no audio for: {text_g[:80]!r}")
        segments.append({"text": text_g, "start_s": round(pos, 3), "end_s": round(pos + audio.size / SR, 3)})
        pos += audio.size / SR
        pieces.append(audio)
    audio = np.concatenate(pieces)
    sf.write(wav_path, audio, SR, subtype="PCM_16")
    gen_s = time.time() - t1
    meta = {"path": os.path.abspath(wav_path), "duration_s": round(audio.size / SR, 3), "sample_rate": SR, "segments": segments,
            "voice": voice, "model": a.model, "speed": speed, "gen_s": round(gen_s, 2), "rtf": round(gen_s / (audio.size / SR), 3)}
    with open(meta_path, "w") as f:
        json.dump(meta, f)
    return save_voice({**meta, "cached": False}, save_as, text) if save_as else {**meta, "cached": False}


def speech(req: dict) -> Raw:
    if req.get("response_format", "wav") != "wav":
        raise ValueError("only response_format 'wav' is supported")
    return Raw(open(synth(req)["path"], "rb").read(), "audio/wav")


try:                                            # first synthesis pays for graph compilation and the text front end: do it before READY
    list(model.generate(text="Warming up.", verbose=False, **({"voice": a.voice} if a.voice else {}), **({"lang_code": a.lang_code} if a.lang_code else {})))
except Exception as e:                          # noqa: BLE001  a model that needs a reference clip cannot warm up this way; that is fine
    print(f"warm-up skipped: {type(e).__name__}: {e}", flush=True)

serve(a.port, {"model": a.model, "sample_rate": SR, "load_s": round(time.time() - t0, 1)},
      {"/speak": synth, "/v1/audio/speech": speech})
