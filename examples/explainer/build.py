"""Build a narrated, captioned explainer video from script.json, entirely on this Mac.

    .venv-audio/bin/python examples/explainer/build.py [--model clone] [--voice af_heart] [--out data/explainer]

Per scene: the gateway synthesises the narration (/api/speak: wav + duration), Whisper transcribes it back for word timestamps
(/api/transcribe), slides are drawn with Pillow, and ffmpeg (the binary bundled with imageio-ffmpeg) assembles the video.
The timing comes from the audio, never from guesses: a frame changes exactly when a caption line or a bullet is due.

Needs the gateway running on :8090 (it starts the speech models on demand). Uses only the HTTP API, so this is also what a script
written by an LLM would call.
"""
import argparse, json, os, subprocess, sys, urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1280, 720, 30
BG, PANEL, INK, MUTED, ACCENT = (15, 23, 42), (30, 41, 59), (241, 245, 249), (148, 163, 184), (45, 212, 191)
FONT = "/System/Library/Fonts/HelveticaNeue.ttc"
TAIL_S = 0.6                                                    # breathing room after each scene's narration


def font(size, bold=False):
    return ImageFont.truetype(FONT, size, index=1 if bold else 0)


def post(base, path, body):
    req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.load(r)


def caption_lines(words, max_words=9, max_gap=0.6):
    """Group timed words into caption lines: break at sentence ends, long pauses, or max_words."""
    lines, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w["start_s"] - cur[-1]["end_s"] > max_gap):
            lines.append(cur); cur = []
        cur.append(w)
        if w["word"].endswith((".", "?", "!")):
            lines.append(cur); cur = []
    if cur:
        lines.append(cur)
    caps = [{"start": l[0]["start_s"], "end": l[-1]["end_s"], "text": " ".join(x["word"] for x in l).replace(" -", "-")} for l in lines]
    for c, nxt in zip(caps, caps[1:]):                     # hold a caption through a short pause instead of flickering to blank
        if nxt["start"] - c["end"] < 1.2:
            c["end"] = nxt["start"]
    return caps


def wrap(draw, text, fnt, width):
    out, cur = [], ""
    for word in text.split():
        t = f"{cur} {word}".strip()
        if draw.textlength(t, font=fnt) > width and cur:
            out.append(cur); cur = word
        else:
            cur = t
    return out + [cur] if cur else out


def draw_slide(scene, shown_bullets, caption):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 12, H], fill=ACCENT)
    d.text((70, 60), scene["heading"], font=font(54, True), fill=INK)
    if "bullets" in scene:
        y = 190
        for b in scene["bullets"][:shown_bullets]:
            d.ellipse([78, y + 16, 94, y + 32], fill=ACCENT)
            d.text((116, y), b, font=font(38), fill=INK)
            y += 78
    if "diagram" in scene:
        boxes = {b["id"]: b for b in scene["diagram"]["boxes"]}
        visible = {b["id"] for i, b in enumerate(boxes.values()) if shown_bullets >= 99 or i < shown_bullets}      # boxes appear in order
        for a, b in scene["diagram"]["arrows"]:
            if a not in visible or b not in visible:
                continue
            A, B = boxes[a], boxes[b]
            p, q = (A["x"] + A["w"], A["y"] + 60 + A["h"] // 2), (B["x"], B["y"] + 60 + B["h"] // 2)
            d.line([p, q], fill=MUTED, width=3)
            d.polygon([q, (q[0] - 14, q[1] - 8), (q[0] - 14, q[1] + 8)], fill=MUTED)
        for b in boxes.values():
            if b["id"] not in visible:
                continue
            d.rounded_rectangle([b["x"], b["y"] + 60, b["x"] + b["w"], b["y"] + b["h"] + 60], 14, fill=PANEL, outline=ACCENT if b.get("accent") else MUTED, width=3)
            f = font(30, True)
            tw = d.textlength(b["label"], font=f)
            d.text((b["x"] + (b["w"] - tw) / 2, b["y"] + 60 + (b["h"] - 30) / 2 - 4), b["label"], font=f, fill=INK)
    if caption:
        f = font(34)
        lines = wrap(d, caption, f, W - 200)
        top = H - 70 - 46 * len(lines)
        d.rounded_rectangle([60, top - 18, W - 60, H - 40], 14, fill=(2, 6, 23))
        for i, l in enumerate(lines):
            d.text((W / 2 - d.textlength(l, font=f) / 2, top + 46 * i), l, font=f, fill=INK)
    return img


def srt_time(t):
    ms = int(round(t * 1000)); return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", default=str(Path(__file__).with_name("script.json")))
    ap.add_argument("--gateway", default="http://127.0.0.1:8090")
    ap.add_argument("--model", default="clone", help="speech model: kokoro (preset voices) or clone (a reference clip in data/voices)")
    ap.add_argument("--voice", default=None)
    ap.add_argument("--out", default="data/explainer")
    a = ap.parse_args()
    script = json.load(open(a.script))
    out = Path(a.out).resolve(); (out / "frames").mkdir(parents=True, exist_ok=True)

    timeline, audio_parts, t_scene, srt = [], [], 0.0, []
    for scene in script["scenes"]:
        body = {"model": a.model, "text": scene["narration"], "name": f"scene-{scene['id']}"}
        if a.voice:
            body["voice" if a.model == "kokoro" else "ref_audio"] = a.voice
        clip = post(a.gateway, "/api/speak", body)
        words = post(a.gateway, "/api/transcribe", {"path": clip["path"], "words": True})["words"]
        dur = clip["duration_s"] + TAIL_S
        print(f"{scene['id']:<14} {clip['duration_s']:6.2f}s  gen {clip['gen_s']}s  {'(cached)' if clip['cached'] else ''}", flush=True)
        lines = caption_lines(words)
        n_items = len(scene.get("bullets") or scene.get("diagram", {}).get("boxes") or [])
        reveal = [0.4 + i * (clip["duration_s"] - 0.8) / max(n_items, 1) * 0.9 for i in range(n_items)]   # items appear through the narration
        cuts = sorted({0.0, *reveal, *(l["start"] for l in lines), *(l["end"] for l in lines), dur})
        for s, e in zip(cuts, cuts[1:]):
            if e - s < 1e-3:
                continue
            mid = (s + e) / 2
            shown = sum(1 for r in reveal if r <= mid)
            cap = next((l["text"] for l in lines if l["start"] <= mid < l["end"]), "")
            timeline.append((scene, shown, cap, e - s))
        for l in lines:
            srt.append((t_scene + l["start"], t_scene + l["end"], l["text"]))
        audio, sr = sf.read(clip["path"], dtype="float32")
        audio_parts += [audio, np.zeros(int(TAIL_S * sr), dtype=np.float32)]
        t_scene += dur

    listing, cache = [], {}
    for i, (scene, shown, cap, d) in enumerate(timeline):
        key = (scene["id"], shown, cap)
        if key not in cache:
            cache[key] = out / "frames" / f"f{len(cache):04}.png"
            draw_slide(scene, shown, cap).save(cache[key])
        listing += [f"file '{cache[key]}'", f"duration {d:.4f}"]
    listing.append(f"file '{cache[(timeline[-1][0]['id'], timeline[-1][1], timeline[-1][2])]}'")     # concat demuxer quirk: repeat the last frame
    (out / "frames.txt").write_text("\n".join(listing) + "\n")
    sf.write(out / "narration.wav", np.concatenate(audio_parts), sr, subtype="PCM_16")
    (out / "captions.srt").write_text("".join(f"{i}\n{srt_time(s)} --> {srt_time(e)}\n{t}\n\n" for i, (s, e, t) in enumerate(srt, 1)))

    import imageio_ffmpeg
    mp4 = out / "explainer.mp4"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(out / "frames.txt"),
                    "-i", str(out / "narration.wav"), "-vf", f"fps={FPS},format=yuv420p", "-c:v", "libx264", "-c:a", "aac", "-b:a", "160k",
                    "-shortest", "-movflags", "+faststart", str(mp4)], check=True)
    print(f"\n{mp4}  ({t_scene:.1f}s, {len(cache)} distinct frames); captions: {out / 'captions.srt'}")


if __name__ == "__main__":
    main()
