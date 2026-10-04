"""Timed narration for video explainers: scenes of text with [[cue]] markers in, per-scene clips with word timings and resolved cues out.

The cue markers are stripped before synthesis. Each cue resolves to the start time of the first spoken word after it, found by
aligning the script's words with Whisper's transcript, so a renderer can key animations to named moments in the narration instead
of hand-typed seconds. Stdlib only; the speech calls are injected, so the logic is testable without models.
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Awaitable, Callable

from .backends.speechtext import NAME_OK

CUE = re.compile(r"\[\[([A-Za-z0-9_-]+)\]\]")
MAX_SCENES = 60
MAX_SCENE_CHARS = 6000


class NarrateError(ValueError):
    pass


def parse_scenes(scenes) -> list[dict]:
    if not isinstance(scenes, list) or not scenes:
        raise NarrateError("`scenes` must be a non-empty list of {id, text}")
    if len(scenes) > MAX_SCENES:
        raise NarrateError(f"at most {MAX_SCENES} scenes per call")
    out, seen = [], set()
    for i, s in enumerate(scenes):
        if not isinstance(s, dict) or not isinstance(s.get("text"), str):
            raise NarrateError(f"scene {i}: expected {{id, text}}")
        sid = str(s.get("id") or f"scene{i + 1}")
        if not NAME_OK.match(sid):
            raise NarrateError(f"scene {i}: id {sid!r} must be letters, digits, '.', '_' or '-'")
        if sid in seen:
            raise NarrateError(f"duplicate scene id {sid!r}")
        seen.add(sid)
        if len(s["text"]) > MAX_SCENE_CHARS:
            raise NarrateError(f"scene {sid}: text longer than {MAX_SCENE_CHARS} characters; split it")
        text, words, cues = split_cues(s["text"])
        if not words:
            raise NarrateError(f"scene {sid}: no text to speak")
        out.append({"id": sid, "text": text, "script_words": words, "cue_index": cues})
    return out


def split_cues(narration: str) -> tuple[str, list[str], dict[str, int]]:
    """-> (text without markers, its words, {cue: index of the word that follows the marker})."""
    words: list[str] = []
    cues: dict[str, int] = {}
    for tok in CUE.sub(lambda m: f" {m.group(0)} ", narration).split():
        m = CUE.fullmatch(tok)
        if m:
            if m.group(1) in cues:
                raise NarrateError(f"cue {m.group(1)!r} appears twice")
            cues[m.group(1)] = len(words)
        else:
            words.append(tok)
    text = CUE.sub("", narration)
    return re.sub(r"[ \t]+", " ", re.sub(r" +([,.;:!?])", r"\1", text)).strip(), words, cues


def norm(w: str) -> str:
    return re.sub(r"[^a-z0-9]", "", w.lower())


def script_timings(script_words: list[str], spoken: list[dict], to_spoken: dict[int, int], duration_s: float) -> list[dict]:
    """The script's own words (its spelling and punctuation, for captions) with the times of the spoken words they align to.
    Words Whisper heard differently ("thirty" as "30") get times interpolated between their aligned neighbours."""
    n = len(script_words)
    known = {i: (spoken[j]["start_s"], spoken[j]["end_s"]) for i, j in to_spoken.items()}
    out = []
    for i, w in enumerate(script_words):
        if i in known:
            s0, e0 = known[i]
        else:
            lo = max((k for k in known if k < i), default=None)
            hi = min((k for k in known if k > i), default=None)
            a = known[lo][1] if lo is not None else 0.0
            b = known[hi][0] if hi is not None else duration_s
            first, last = (lo + 1 if lo is not None else 0), (hi - 1 if hi is not None else n - 1)
            step = (b - a) / max(last - first + 1, 1)
            s0, e0 = a + (i - first) * step, a + (i - first + 1) * step
        out.append({"word": w, "start_s": round(s0, 3), "end_s": round(e0, 3)})
    return out


def align(script_words: list[str], spoken: list[dict], cues: dict[str, int], duration_s: float) -> tuple[dict[str, float], list[dict], list[dict]]:
    """Resolve cue times, list where the transcript differs from the script (expected for numbers: "thirty" vs "30"), and time the
    script's own words."""
    a, b = [norm(w) for w in script_words], [norm(w["word"]) for w in spoken]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    to_spoken: dict[int, int] = {}
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            to_spoken[blk.a + k] = blk.b + k
    times = {}
    for name, i in cues.items():
        j = next((to_spoken[k] for k in range(i, len(a)) if k in to_spoken), None)
        times[name] = round(spoken[j]["start_s"] if j is not None else duration_s * i / max(len(a), 1), 3)
    diffs = [{"script": " ".join(script_words[i1:i2]), "heard": " ".join(w["word"] for w in spoken[j1:j2])}
             for op, i1, i2, j1, j2 in sm.get_opcodes()
             if op != "equal" and "".join(a[i1:i2]) != "".join(b[j1:j2])]          # "M C P" vs "MCP", "back end" vs "backend": same words
    return times, diffs, script_timings(script_words, spoken, to_spoken, duration_s)


Speak = Callable[[dict], Awaitable[dict]]
Transcribe = Callable[[dict], Awaitable[dict]]


async def narrate(scenes: list[dict], speak: Speak, transcribe: Transcribe, voice_args: dict) -> dict:
    """Synthesise and time each scene in order (one GPU: scenes run one after another, and cached clips cost nothing)."""
    out, t = [], 0.0
    for sc in scenes:
        clip = await speak({**voice_args, "text": sc["text"], "name": f"narrate-{sc['id']}"})
        heard = await transcribe({"path": clip["path"], "words": True})
        words = heard.get("words") or []
        cues, diffs, script_words = align(sc["script_words"], words, sc["cue_index"], clip["duration_s"])
        out.append({"id": sc["id"], "path": clip["path"], "file": Path(clip["path"]).name, "duration_s": clip["duration_s"],
                    "start_s": round(t, 3), "cached": bool(clip.get("cached")), "text": sc["text"], "cues": cues,
                    "words": [{"word": w["word"], "start_s": w["start_s"], "end_s": w["end_s"]} for w in words],
                    "script_words": script_words,
                    "segments": clip.get("segments", []), "transcript_differs": diffs})
        t += clip["duration_s"]
    return {"scenes": out, "total_s": round(t, 3), "voice": voice_args}
