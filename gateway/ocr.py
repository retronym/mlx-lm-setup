"""On-device OCR with macOS Vision (VNRecognizeTextRequest, the engine behind Live Text): no model to load, well under a second.

Lines that the screen layout wrapped mid-sentence are joined back into paragraphs (`paragraphs`), so a translator does not
carry the screen's line breaks into its output. Used by `translate` so a screenshot does not pay a vision model's cold start; the vision model stays the fallback for images
where Vision finds little or no text (stylised or vertical text, handwriting). Needs pyobjc-framework-Vision in the gateway venv;
`available()` is False without it (or off macOS) and callers fall back to the vision model.
"""
from __future__ import annotations

import re
import time

try:
    import objc
    import Vision
    from Foundation import NSData
except ImportError:                                                  # not macOS, or pyobjc not installed
    Vision = None


def available() -> bool:
    return Vision is not None


def recognize(data: bytes, languages: list[str] | None = None) -> dict:
    """Image bytes (PNG, JPEG, TIFF, HEIC, ...) -> {"text", "lines": [{"text", "confidence", "box"}], "confidence", "secs"}.

    Lines come in Vision's order (top to bottom), each with its box [x, y, w, h] as fractions of the image, origin top left.
    `text` is the lines with layout wraps joined into paragraphs; `confidence` is the character-weighted mean (0 if no text)."""
    if Vision is None:
        raise RuntimeError("macOS Vision is not available (pip install pyobjc-framework-Vision)")
    t0 = time.monotonic()
    with objc.autorelease_pool():
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        req.setUsesLanguageCorrection_(True)
        if languages:
            req.setRecognitionLanguages_(languages)
        else:
            req.setAutomaticallyDetectsLanguage_(True)
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(NSData.dataWithBytes_length_(data, len(data)), None)
        ok, err = handler.performRequests_error_([req], None)
        if not ok:
            raise RuntimeError(f"Vision OCR failed: {err}")
        lines = []
        for obs in req.results() or []:
            cand = obs.topCandidates_(1)
            if cand:
                b = obs.boundingBox()                                # normalised, origin bottom left
                lines.append({"text": str(cand[0].string()), "confidence": round(float(cand[0].confidence()), 3),
                              "box": [round(b.origin.x, 4), round(1 - b.origin.y - b.size.height, 4), round(b.size.width, 4), round(b.size.height, 4)]})
    chars = sum(len(l["text"]) for l in lines)
    conf = sum(len(l["text"]) * l["confidence"] for l in lines) / chars if chars else 0.0
    return {"text": paragraphs(lines), "lines": lines, "confidence": round(conf, 3), "secs": round(time.monotonic() - t0, 3)}


# ---- joining layout wraps -----------------------------------------------------------------------------------------------
_CJK = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")   # scripts written without spaces between words
_SENTENCE_END = re.compile(r"[.!?:;。！？：；]$")


def _same_size(h1: float, h2: float) -> bool:
    """Box heights as a font-size proxy. Loose: Vision's boxes follow ascenders and descenders, so lines in one font differ by up
    to ~30% (Cyrillic, Polish diacritics), and a bold heading only a little more."""
    return 0.7 <= h2 / h1 <= 1.43


def _first_word(text: str) -> str:
    return text[0] if _CJK.match(text) else text.split(" ", 1)[0]


def _wrapped(prev: dict, nxt: dict, right_edge: float) -> bool:
    """Did the layout wrap `prev` onto `nxt`? Same font size, `nxt` just below and aligned left, and `prev` reaches so close to
    the paragraph's right edge that `nxt`'s first word would not have fitted after it. Plus a text cue, because a short line in
    a banner can pass the geometry: a wrapped line is prose (3+ words), continues with a letter or digit, and not after a sentence end."""
    (px, py, pw, ph), (nx, ny, nw, nh) = prev["box"], nxt["box"]
    pt, nt = prev["text"].rstrip(), nxt["text"].lstrip()
    if not pt or not nt or not _same_size(ph, nh):
        return False
    if (len(pt) < 8) if _CJK.search(pt) else (len(pt.split()) < 3):
        return False                                                 # not prose: a column of labels or numbers also "fills" its width
    char_w = nw / max(len(nt), 1)
    if not -0.4 * ph <= ny - (py + ph) <= 0.8 * ph or abs(nx - px) > 2 * char_w:
        return False
    if px + pw + char_w * (len(_first_word(nt)) + 1) <= right_edge:
        return False                                                 # the next word would have fitted: a real line break
    if not nt[0].isalnum() or _SENTENCE_END.search(pt):
        return False
    # A capital may start a new line ("elektryczne / Raty 0%" in a banner) or continue one ("Kraków / Główny"): only join it
    # when the sizes match closely too; lower case, digits and caseless scripts keep the loose size check.
    return not nt[0].isupper() or 0.85 <= nh / ph <= 1.18


def paragraphs(lines: list[dict]) -> str:
    """OCR lines (with boxes, in reading order) -> text with layout wraps joined: a space between words, nothing between CJK
    characters, a trailing hyphen dropped when a word was split. Lines without a box are kept as they are."""
    out: list[str] = []
    for i, line in enumerate(lines):
        prev = lines[i - 1] if i else None
        if prev and "box" in prev and "box" in line:
            px, _, pw, ph = prev["box"]
            # the paragraph's right edge: the furthest any line of this font size reaches that overlaps it horizontally
            right_edge = max(b["box"][0] + b["box"][2] for b in lines if "box" in b and _same_size(ph, b["box"][3])
                             and b["box"][0] < px + pw and b["box"][0] + b["box"][2] > px)
            if _wrapped(prev, line, right_edge):
                head, tail = out[-1].rstrip(), line["text"].lstrip()
                if _CJK.match(head[-1]) and _CJK.match(tail[0]):
                    out[-1] = head + tail
                elif re.search(r"\w-$", head) and tail[0].islower():
                    out[-1] = head[:-1] + tail
                else:
                    out[-1] = f"{head} {tail}"
                continue
        out.append(line["text"])
    return "\n".join(out)
