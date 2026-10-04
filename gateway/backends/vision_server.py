"""Vision-language backend on mlx-vlm (Gemma 4, Qwen3.6: the same checkpoints as the text LLMs, vision tower included).  Runs in .venv-vlm.

  POST /v1/chat/completions   OpenAI chat body; user content may mix {"type":"text"} and {"type":"image_url","image_url":{"url":...}}
                              parts. Extra keys: "image_tokens" (detail per image), "chat_template_kwargs" ({"enable_thinking": true}).
                              -> an OpenAI chat.completion (no streaming)

Images arrive inline only, as data URIs (image/png, jpeg, webp, gif; or application/pdf with "#page=N", 1-based, rendered here).
The gateway turns local paths into data URIs. Anything else, URLs included, is refused: mlx-vlm's loader would fetch a URL, and a
backend must never reach the network on a request's say-so.
"""
import argparse, base64, io, os, sys, time, uuid

ap = argparse.ArgumentParser()
ap.add_argument("--model", required=True)
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--image-tokens", type=int, default=0, help="default visual tokens per image (0 = the model's own default)")
ap.add_argument("--max-tokens", type=int, default=1024)
a = ap.parse_args()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _http import serve                                             # noqa: E402

t0 = time.time()
import mlx.core as mx                                               # noqa: E402
from PIL import Image                                               # noqa: E402
from mlx_vlm import generate, load                                  # noqa: E402
from mlx_vlm.prompt_utils import apply_chat_template                # noqa: E402

model, processor = load(a.model)
IP = getattr(processor, "image_processor", None)
print(f"model loaded in {time.time() - t0:.1f}s", flush=True)

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_SIDE = 4096          # larger images are scaled down first (the processor would anyway; this bounds decode memory)
MAX_IMAGES = 16
PDF_SCALE = 2.0          # 144 dpi: a figure's small print stays legible


def _decode(url: str) -> Image.Image:
    if not isinstance(url, str) or not url.startswith("data:"):
        raise ValueError("image_url must be a data: URI (the gateway converts local paths; URLs are not fetched)")
    head, _, rest = url[5:].partition(",")
    mime, _, enc = head.partition(";")
    if enc != "base64":
        raise ValueError("image data URI must be base64")
    page = None
    if mime == "application/pdf" and "#" in rest:
        rest, _, frag = rest.partition("#")
        if not frag.startswith("page=") or not frag[5:].isdigit() or int(frag[5:]) < 1:
            raise ValueError("a PDF data URI takes '#page=N' with N >= 1")
        page = int(frag[5:])
    try:
        raw = base64.b64decode(rest, validate=True)
    except ValueError:
        raise ValueError("image data URI is not valid base64") from None
    if mime == "application/pdf":
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(raw)
        n = page or 1
        if n > len(doc):
            raise ValueError(f"PDF has {len(doc)} pages, asked for page {n}")
        img = doc[n - 1].render(scale=PDF_SCALE).to_pil()
    elif mime in IMAGE_TYPES:
        img = Image.open(io.BytesIO(raw))
        img.load()
    else:
        raise ValueError(f"unsupported image type {mime!r} (png, jpeg, webp, gif, or application/pdf#page=N)")
    img = img.convert("RGB")
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    return img


def _messages(messages) -> tuple[list[dict], list[Image.Image]]:
    """OpenAI messages -> mlx-vlm messages with {"type":"image"} placeholders, plus the decoded images in order."""
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    out, images = [], []
    for m in messages:
        role, content = m.get("role", "user"), m.get("content", "")
        if role not in ("system", "user", "assistant"):
            raise ValueError(f"unsupported role {role!r}")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        parts = []
        for p in content:
            kind = p.get("type")
            if kind == "text":
                parts.append({"type": "text", "text": str(p.get("text", ""))})
            elif kind == "image_url":
                iu = p.get("image_url")
                images.append(_decode(iu.get("url") if isinstance(iu, dict) else iu))
                parts.append({"type": "image"})
            else:
                raise ValueError(f"unsupported content part {kind!r} (text, image_url)")
        out.append({"role": role, "content": parts})
    if len(images) > MAX_IMAGES:
        raise ValueError(f"at most {MAX_IMAGES} images per request")
    return out, images


def _set_image_tokens(n: int) -> None:
    """Visual tokens per image: Gemma 4 takes a soft-token budget (70..1120); Qwen-VL processors take a pixel cap (32x32 px per token)."""
    if IP is None or n <= 0:
        return
    if hasattr(IP, "max_soft_tokens"):
        IP.max_soft_tokens = min((70, 140, 280, 560, 1120), key=lambda s: abs(s - n))
    elif hasattr(IP, "max_pixels"):
        IP.max_pixels = n * 32 * 32


DEFAULT_DETAIL = (getattr(IP, "max_soft_tokens", None), getattr(IP, "max_pixels", None))


def chat(req: dict) -> dict:
    if req.get("stream"):
        raise ValueError("streaming is not supported by the vision backend; send stream: false")
    msgs, images = _messages(req.get("messages"))
    kw = dict(req.get("chat_template_kwargs") or {})
    kw.setdefault("enable_thinking", False)
    if hasattr(IP, "max_soft_tokens"):
        IP.max_soft_tokens = DEFAULT_DETAIL[0]
    if hasattr(IP, "max_pixels"):
        IP.max_pixels = DEFAULT_DETAIL[1]
    _set_image_tokens(int(req.get("image_tokens") or a.image_tokens or 0))
    prompt = apply_chat_template(processor, model.config, msgs, num_images=len(images), **kw)
    t1 = time.time()
    r = generate(model, processor, prompt, image=images or None, max_tokens=int(req.get("max_tokens") or a.max_tokens),
                 temperature=float(req.get("temperature", 0.0)), verbose=False)
    finish = "length" if r.generation_tokens >= int(req.get("max_tokens") or a.max_tokens) else "stop"
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "created": int(time.time()), "model": a.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": r.text}, "finish_reason": finish}],
            "usage": {"prompt_tokens": r.prompt_tokens, "completion_tokens": r.generation_tokens,
                      "total_tokens": r.prompt_tokens + r.generation_tokens},
            "x_timing": {"secs": round(time.time() - t1, 2), "images": len(images), "generation_tps": round(r.generation_tps, 1),
                         "peak_gb": round(mx.get_peak_memory() / 1e9, 2)}}


serve(a.port, {"model": a.model, "load_s": round(time.time() - t0, 1), "image_tokens": a.image_tokens},
      {"/v1/chat/completions": chat}, max_body=96 * 1024 * 1024)
