"""Text-to-image backend on mflux (FLUX.2 klein, Z-Image-Turbo, ...) as a small server.  Runs in .venv-image.

  POST /generate  {"prompt", "seed"?, "width"?, "height"?, "steps"?, "name"?, "fresh"?}
                  -> {"path", "name", "width", "height", "seed", "steps", "model", "gen_s", "cached"}

Images are PNGs under --output-dir, content-addressed by every parameter that affects the picture, so the same request again is a
cache hit (``fresh: true`` forces a new take; an omitted ``seed`` draws a random one, which is never cached). Sizes are multiples of
16, 256..2048 on a side.
"""
import argparse, hashlib, json, os, random, re, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("--family", required=True, choices=["flux2", "z_image"], help="which mflux model class")
ap.add_argument("--config", required=True, help="mflux ModelConfig factory, e.g. flux2_klein_4b or z_image_turbo")
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--output-dir", required=True)
ap.add_argument("--quantize", type=int, default=None, choices=[3, 4, 5, 6, 8])
ap.add_argument("--steps", type=int, default=4, help="default inference steps")
ap.add_argument("--size", type=int, default=1024, help="default width and height")
a = ap.parse_args()

from concurrent.futures import ThreadPoolExecutor                   # noqa: E402

# MLX streams are per-thread: the model is loaded, warmed up and run on this one worker thread, never on the HTTP handler threads.
WORKER = ThreadPoolExecutor(max_workers=1)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _http import serve                                             # noqa: E402

t0 = time.time()
from mflux.models.common.config.model_config import ModelConfig    # noqa: E402
if a.family == "flux2":
    from mflux.models.flux2.variants.txt2img.flux2_klein import Flux2Klein as Model    # noqa: E402
else:
    from mflux.models.z_image.variants.z_image import ZImage as Model                  # noqa: E402
model = WORKER.submit(lambda: Model(quantize=a.quantize, model_config=getattr(ModelConfig, a.config)())).result()
print(f"model loaded in {time.time() - t0:.1f}s", flush=True)
os.makedirs(a.output_dir, exist_ok=True)

MODEL_ID = f"{a.config}-q{a.quantize}" if a.quantize else a.config
NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")


def _dim(req: dict, key: str) -> int:
    v = req.get(key, a.size)
    if isinstance(v, bool) or not isinstance(v, int) or not 256 <= v <= 2048 or v % 16:
        raise ValueError(f"{key} must be a multiple of 16 between 256 and 2048, got {v!r}")
    return v


def generate(req: dict) -> dict:
    prompt = (req.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is empty")
    if len(prompt) > 4000:
        raise ValueError("prompt is longer than 4000 characters")
    width, height = _dim(req, "width"), _dim(req, "height")
    steps = req.get("steps", a.steps)
    if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= 50:
        raise ValueError(f"steps must be an integer between 1 and 50, got {steps!r}")
    seed = req.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or seed < 0):
        raise ValueError("seed must be a non-negative integer")
    name = req.get("name")
    if name is not None and not NAME_OK.match(str(name)):
        raise ValueError("name may contain letters, digits, '.', '_' and '-' only")
    random_seed = seed is None
    if random_seed:
        seed = random.randrange(2 ** 31)
    key = hashlib.sha1(json.dumps([MODEL_ID, prompt, width, height, steps, seed], sort_keys=True).encode()).hexdigest()[:12]
    fname = f"{name or 'image'}-{key}.png"
    path, meta_path = os.path.join(a.output_dir, fname), os.path.join(a.output_dir, f"{name or 'image'}-{key}.json")
    if not random_seed and not req.get("fresh") and os.path.exists(path) and os.path.exists(meta_path):
        return {**json.load(open(meta_path)), "cached": True}
    t1 = time.time()
    def run():
        img = model.generate_image(seed=seed, prompt=prompt, num_inference_steps=steps, width=width, height=height)
        img.save(path=path, overwrite=True)
    WORKER.submit(run).result()
    meta = {"path": os.path.abspath(path), "name": fname, "width": width, "height": height, "seed": seed, "steps": steps,
            "model": MODEL_ID, "prompt": prompt, "gen_s": round(time.time() - t1, 2)}
    with open(meta_path, "w") as f:
        json.dump(meta, f)
    return {**meta, "cached": False}


try:                                            # first generation pays for graph compilation: do it before READY
    WORKER.submit(lambda: model.generate_image(seed=0, prompt="warm-up", num_inference_steps=1, width=256, height=256)).result()
except Exception as e:                          # noqa: BLE001
    print(f"warm-up skipped: {type(e).__name__}: {e}", flush=True)

serve(a.port, {"model": MODEL_ID, "load_s": round(time.time() - t0, 1)}, {"/generate": generate})
