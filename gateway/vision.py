"""Vision: turn image references into inline data, prompt presets, and `look` (shared by /api/look and the MCP tool).

Clients name images the way that is natural to them: a local path (the MCP tool, scripts), a data URI (the browser, OpenAI clients),
or a PDF page as "paper.pdf#page=3". The gateway inlines local files so the backend never touches the disk or the network. A local
path is read with the gateway's own permissions, like any local tool; only image and PDF files are accepted (sniffed by content,
not by name) and URLs are refused.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from pathlib import Path
from typing import Awaitable, Callable

from .core import ApiError

MAX_FILE_BYTES = 40 * 1024 * 1024
MAX_EACH = 200                      # images in one `each` batch (a directory of stills)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_MAGIC = [(b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"),
          (b"%PDF-", "application/pdf")]


def sniff(raw: bytes) -> str | None:
    for magic, mime in _MAGIC:
        if raw.startswith(magic):
            return mime
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def inline(ref: str) -> str:
    """A local path, "file.pdf#page=N" or a data URI -> a data URI the backend accepts."""
    if not isinstance(ref, str) or not ref:
        raise ApiError(400, "invalid_arguments", "an image reference must be a non-empty string")
    if ref.startswith("data:"):
        mime = ref[5:].split(";", 1)[0]
        if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf"):
            raise ApiError(400, "invalid_arguments", f"unsupported data URI type {mime!r}")
        return ref
    if re.match(r"^[a-z][a-z0-9+.-]*://", ref, re.I) and not ref.startswith("file://"):
        raise ApiError(400, "invalid_arguments", "URLs are not fetched; download the image and pass its path or a data URI")
    path_s, _, frag = ref.removeprefix("file://").partition("#")
    path = Path(path_s).expanduser()
    if not path.is_absolute():
        raise ApiError(400, "invalid_arguments", f"image path must be absolute: {ref!r}")
    if not path.is_file():
        raise ApiError(400, "invalid_arguments", f"no such file: {path}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ApiError(400, "invalid_arguments", f"{path} is larger than {MAX_FILE_BYTES >> 20} MB")
    raw = path.read_bytes()
    mime = sniff(raw)
    if mime is None:
        raise ApiError(400, "invalid_arguments", f"{path} is not a PNG, JPEG, WebP, GIF or PDF file")
    if frag and not (mime == "application/pdf" and re.fullmatch(r"page=[1-9]\d*", frag)):
        raise ApiError(400, "invalid_arguments", f"only a PDF takes a fragment, and it must be '#page=N': {ref!r}")
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}" + (f"#{frag}" if frag else "")


def expand(refs: list[str]) -> list[str]:
    """Directories become their image files (sorted); everything else passes through."""
    out: list[str] = []
    for r in refs:
        p = Path(r).expanduser() if isinstance(r, str) and not r.startswith("data:") else None
        if p is not None and p.is_absolute() and p.is_dir():
            out += [str(f) for f in sorted(p.iterdir()) if f.suffix.lower() in IMAGE_SUFFIXES and f.is_file()]
        else:
            out.append(r)
    return out


def label(ref: str) -> str:
    return "inline image" if ref.startswith("data:") else ref


def inline_messages(messages: list[dict]) -> list[dict]:
    """Inline every image_url part of an OpenAI messages list (for /v1/chat/completions on a vision model)."""
    if not isinstance(messages, list):
        raise ApiError(400, "invalid_arguments", "messages must be a list")
    out = []
    for m in messages:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            parts = []
            for p in c:
                if isinstance(p, dict) and p.get("type") == "image_url":
                    iu = p.get("image_url")
                    url = iu.get("url") if isinstance(iu, dict) else iu
                    p = {"type": "image_url", "image_url": {"url": inline(url)}}
                parts.append(p)
            m = {**m, "content": parts}
        out.append(m)
    return out


def user_message(prompt: str, images: list[str]) -> dict:
    return {"role": "user", "content": [*({"type": "image_url", "image_url": {"url": inline(i)}} for i in images), {"type": "text", "text": prompt}]}


# ---- presets ------------------------------------------------------------------------------------------------------------
LAYOUT_SCHEMA = {"type": "object", "required": ["ok", "issues"], "properties": {
    "ok": {"type": "boolean"},
    "issues": {"type": "array", "items": {"type": "object", "required": ["kind", "where"], "properties": {
        "kind": {"enum": ["overlap", "clipped", "wrap"]}, "where": {"type": "string"}, "detail": {"type": "string"}}}}}}

PRESETS: dict[str, dict] = {
    "layout": {
        "title": "Layout check (film stills, slides, screenshots)",
        "prompt": """You are checking one still frame of an explainer video or a screenshot for layout defects before release.
Look only for these defects:
- overlap: text that overlaps other text, a chart, a line, or a panel border
- clipped: text or a panel cut off by the frame edge or by its container (including a trailing "..." that hides content)
- wrap: a short label, caption, heading or header that wraps onto extra lines when it obviously should fit on one (e.g. one or two words per line in a narrow column)
Do not report style or content opinions, deliberate dimming, a caption that is a sentence in progress, or text that is merely small.
Answer with JSON only: {"ok": true|false, "issues": [{"kind": "overlap|clipped|wrap", "where": "<which element, quoting its visible text>", "detail": "<one sentence>"}]}""",
        "gates": [{"type": "json", "schema": LAYOUT_SCHEMA}], "image_tokens": 1120, "each": True},
    "storyboard": {
        "title": "Against the storyboard (put the scene's storyboard text in Context)",
        "prompt": """Here is what the storyboard says this frame should show:
---
{context}
---
Compare the frame with it. List each storyboard element that is missing or different, and anything on screen that the storyboard does not mention.
Quote on-screen text exactly. Answer with JSON only: {"matches": true|false, "missing": ["..."], "different": ["..."], "extra": ["..."]}""",
        "gates": [{"type": "json", "schema": {"type": "object", "required": ["matches"], "properties": {"matches": {"type": "boolean"}}}}],
        "image_tokens": 1120, "each": True},
    "table": {
        "title": "Read a table or chart (papers, dashboards)",
        "prompt": """Transcribe the table or chart in this image as JSON: {"caption": "...", "columns": ["..."], "rows": [["..."]], "notes": "..."}.
Copy numbers exactly as printed, including units and signs; use null for an empty cell. For a chart, give one row per plotted point or bar you can read, and say in notes which values are estimated from the axis.{context}""",
        "gates": [{"type": "json", "schema": {"type": "object", "required": ["rows"], "properties": {"rows": {"type": "array"}}}}],
        "image_tokens": 1120, "each": False},
    "describe": {
        "title": "Describe",
        "prompt": "Describe this image precisely: layout, every piece of visible text (quoted), and anything that looks broken.{context}",
        "gates": [], "image_tokens": 560, "each": False},
}


def preset_prompt(name: str, context: str | None) -> str:
    p = PRESETS[name]["prompt"]
    if "{context}" not in p:
        return p
    if name == "storyboard":
        if not context:
            raise ApiError(400, "invalid_arguments", "the storyboard preset needs `context` (the storyboard text for this frame)")
        return p.replace("{context}", context)
    return p.replace("{context}", f"\n\nContext: {context}" if context else "")


def parse_json(text: str):
    """The first JSON object in a model answer (bare, fenced or embedded), or None."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# ---- look ---------------------------------------------------------------------------------------------------------------
# (model override, messages, image_tokens) -> (text, info with model/secs/usage)
VisionChat = Callable[[list[dict], int | None], Awaitable[tuple[str, dict]]]


async def look(chat: VisionChat, iterate, *, images: list[str], prompt: str | None = None, preset: str | None = None,
               context: str | None = None, system: str | None = None, each: bool | None = None, gates: list[dict] | None = None,
               image_tokens: int | None = None, max_attempts: int = 2) -> dict:
    """Ask the vision model about `images`. `each` asks once per image (a batch of stills); otherwise all images go in one request.
    `gates` (as for iterate) make the model retry until the answer passes; a preset brings its own prompt, gates and detail."""
    if preset is not None and preset not in PRESETS:
        raise ApiError(400, "invalid_arguments", f"unknown preset {preset!r}; one of {sorted(PRESETS)}")
    if (prompt is None) == (preset is None):
        raise ApiError(400, "invalid_arguments", "pass exactly one of `prompt` or `preset`")
    pr = PRESETS.get(preset or "", {})
    text = preset_prompt(preset, context) if preset else (prompt if not context else f"{prompt}\n\nContext: {context}")
    gates = pr.get("gates", []) if gates is None else gates
    image_tokens = image_tokens or pr.get("image_tokens")
    each = pr.get("each", False) if each is None else each
    refs = expand(list(images or []))
    if not refs:
        raise ApiError(400, "invalid_arguments", "`images` is empty (or the directory has no images)")
    if len(refs) > (MAX_EACH if each else 16):
        raise ApiError(400, "invalid_arguments", f"too many images ({len(refs)}; at most {MAX_EACH if each else 16})")
    for r in refs:                                                   # fail fast on a bad path before any model runs
        if not r.startswith("data:"):
            await asyncio.to_thread(inline, r)

    async def one(batch: list[str]) -> dict:
        msgs = ([{"role": "system", "content": system}] if system else []) + [await asyncio.to_thread(user_message, text, batch)]
        t0 = time.monotonic()
        if gates:
            res = await iterate(gates, msgs, image_tokens, max_attempts)
            out = {"text": res["text"], "passed": res["passed"], "attempts": res["attempts"]}
        else:
            ans, info = await chat(msgs, image_tokens)
            out = {"text": ans, **{k: v for k, v in info.items() if k in ("usage", "model")}}
        out["secs"] = round(time.monotonic() - t0, 2)
        j = parse_json(out["text"])
        if j is not None:
            out["json"] = j
        return out

    t0 = time.monotonic()
    if each:
        results = []
        for r in refs:
            results.append({"image": label(r), **await one([r])})
        flagged = [r["image"] for r in results if isinstance(r.get("json"), dict) and
                   (r["json"].get("ok") is False or r["json"].get("matches") is False)]
        return {"results": results, "flagged": flagged, "secs": round(time.monotonic() - t0, 2), "preset": preset}
    return {**await one(refs), "images": [label(r) for r in refs], "preset": preset}
