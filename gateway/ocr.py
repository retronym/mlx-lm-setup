"""On-device OCR with macOS Vision (VNRecognizeTextRequest, the engine behind Live Text): no model to load, well under a second.

Used by `translate` so a screenshot does not pay a vision model's cold start; the vision model stays the fallback for images
where Vision finds little or no text (stylised or vertical text, handwriting). Needs pyobjc-framework-Vision in the gateway venv;
`available()` is False without it (or off macOS) and callers fall back to the vision model.
"""
from __future__ import annotations

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
    """Image bytes (PNG, JPEG, TIFF, HEIC, ...) -> {"text", "lines": [{"text", "confidence"}], "confidence", "secs"}.

    Lines come in Vision's order (top to bottom); `confidence` is the character-weighted mean (0 when nothing was found)."""
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
                lines.append({"text": str(cand[0].string()), "confidence": round(float(cand[0].confidence()), 3)})
    chars = sum(len(l["text"]) for l in lines)
    conf = sum(len(l["text"]) * l["confidence"] for l in lines) / chars if chars else 0.0
    return {"text": "\n".join(l["text"] for l in lines), "lines": lines, "confidence": round(conf, 3), "secs": round(time.monotonic() - t0, 3)}
