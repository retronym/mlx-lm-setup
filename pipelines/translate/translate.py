#!/usr/bin/env python3
"""Translate text or a screenshot into English through the gateway's /api/translate; prints markdown (translation + summary).

Stdlib only (runs under macOS's /usr/bin/python3), so the Translate shortcut can call it from "Run Shell Script":
  translate.py "Guten Tag"            text from the arguments
  pbpaste | translate.py               text, or image bytes (PNG, JPEG, TIFF, HEIC, ...), on stdin
  translate.py --image shot.png        an image file
  translate.py                         nothing on stdin: pick a screen region (screencapture -i)
"""
import argparse, os, subprocess, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from gateway_client import post  # noqa: E402

MAGIC = {b"\x89PNG": "png", b"\xff\xd8\xff": "jpg", b"GIF8": "gif", b"II*\0": "tiff", b"MM\0*": "tiff", b"%PDF": "pdf"}


def image_kind(data):
    for m, kind in MAGIC.items():
        if data.startswith(m):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp":                                          # HEIC / HEIF / AVIF
        return "heic"
    return None


def as_gateway_image(data, kind, tmp):
    """A path the gateway accepts: PNG/JPEG/GIF/WebP/PDF as is, anything else converted to PNG with sips."""
    path = os.path.join(tmp, "in." + kind)
    with open(path, "wb") as f:
        f.write(data)
    if kind in ("png", "jpg", "gif", "webp", "pdf"):
        return path
    out = os.path.join(tmp, "in.png")
    subprocess.run(["/usr/bin/sips", "-s", "format", "png", path, "--out", out], check=True, capture_output=True)
    return out


CAPTURE_APP = os.path.expanduser("~/Applications/TranslateCapture.app")       # built by make_capture_app.sh
CAPTURE_DIR = os.path.expanduser("~/Library/Caches/mlx-translate")
NO_PERMISSION = "could not create image"                      # what screencapture says without Screen Recording permission


def capture(tmp):
    """Let the user pick a screen region -> (png path or None if cancelled, error message or None).

    Through TranslateCapture.app when it is installed: launched by LaunchServices it is its own responsible process, so its
    Screen Recording grant applies whatever app is in front (a Quick Action's own children are charged to the front app)."""
    if os.path.isdir(CAPTURE_APP):
        shot, errf = os.path.join(CAPTURE_DIR, "shot.png"), os.path.join(CAPTURE_DIR, "shot.err")
        for f in (shot, errf):
            if os.path.exists(f):
                os.remove(f)
        subprocess.run(["/usr/bin/open", "-W", "-n", CAPTURE_APP], capture_output=True)
        stderr = open(errf).read() if os.path.exists(errf) else ""
        who = "TranslateCapture (in ~/Applications)"
    else:
        shot = os.path.join(tmp, "shot.png")
        stderr = subprocess.run(["/usr/sbin/screencapture", "-i", "-x", shot], capture_output=True, text=True).stderr
        who = "the app running this (your terminal, or the app in front when run as a Quick Action; build TranslateCapture.app to avoid that)"
    if NO_PERMISSION in stderr:
        return None, (f"Screen capture failed: {who} needs Screen Recording permission in System Settings → Privacy & Security → "
                      "Screen & System Audio Recording.")
    return (shot if os.path.exists(shot) else None), None


def notify(msg):
    subprocess.run(["/usr/bin/osascript", "-e", f'display notification "{msg}" with title "Translate"'], capture_output=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="*", help="text to translate (else stdin, else a screenshot)")
    ap.add_argument("--image", help="an image file to translate")
    ap.add_argument("--mode", choices=("auto", "ocr", "vision"), default="auto", help="for images: OCR first (auto), OCR only, or the vision model")
    ap.add_argument("--notify", action="store_true", help="show a 'Translating...' notification while waiting")
    ap.add_argument("--json", action="store_true", help="print the gateway's JSON instead of markdown")
    a = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        body = None
        if a.text:
            body = {"text": " ".join(a.text)}
        elif a.image:
            with open(a.image, "rb") as f:
                data = f.read()
            body = {"image": as_gateway_image(data, image_kind(data) or "png", tmp)}
        elif not sys.stdin.isatty():
            data = sys.stdin.buffer.read()
            kind = image_kind(data)
            if kind:
                body = {"image": as_gateway_image(data, kind, tmp)}
            elif data.strip():
                body = {"text": data.decode("utf-8", errors="replace").strip()}
        if body is None:                                              # nothing given: let the user pick a region
            shot, err = capture(tmp)
            if err:
                print(err, file=sys.stderr)
                return 1
            if shot is None:                                          # Escape pressed
                print("_Cancelled._")
                return 0
            body = {"image": shot}
        if "image" in body:
            body["mode"] = a.mode
        if a.notify:
            notify("Translating…")
        try:
            res = post("/api/translate", body, timeout=300, retries=6)
        except RuntimeError as e:
            print(f"Translation failed: {e}", file=sys.stderr)
            return 1
    if a.json:
        import json
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(res["markdown"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
