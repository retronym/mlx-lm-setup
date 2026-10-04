"""Read-only discovery of MLX models already on disk, and generation of a catalog snippet for one of them.

Never launches, downloads or writes anything: it lists what it finds (Hugging Face cache, LM Studio's model folder, plus any extra
directories the caller passes) and, on request, prints a ``[backends.*]`` table you can paste into gateway.toml. Sizes come from
``stat`` on the safetensors files (``du`` is unreliable on the HF cache), MLX format from the safetensors header metadata, and
support from the installed mlx-lm (asked in a subprocess using the catalog's own ``python``, since the gateway venv has no mlx).
"""
from __future__ import annotations

import json
import os
import re
import struct
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import kv as kvmod
from .catalog import Catalog

MAX_HEADER = 64 * 1024 * 1024
LLM_ARCH = ("ForCausalLM", "ForConditionalGeneration", "LMHeadModel")
_supported_cache: dict[str, set[str] | None] = {}


@dataclass
class Found:
    id: str                         # "org/name" for HF cache entries, "publisher/name" for LM Studio, else the directory name
    source: str                     # hf-cache | lm-studio | dir
    path: str
    weights_gb: float
    model_type: str | None
    architectures: list[str] = field(default_factory=list)
    quant_bits: int | None = None
    is_mlx: bool = False
    kind_guess: str = "other"       # llm | other
    supported: bool | None = None   # None = could not ask mlx-lm
    max_context: int | None = None
    kv_kib_per_token: float | None = None    # long-context slope at fp16, KiB per token
    kv_error: str | None = None
    in_catalog: list[str] = field(default_factory=list)

    def kv_gb(self, context_tokens: int, kv_bits: int | None = None) -> float | None:
        try:
            return kvmod.kv_gb(_read_json(Path(self.path) / "config.json"), context_tokens, kv_bits)
        except (kvmod.KVEstimateError, OSError, ValueError):
            return None


def _read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def hf_cache_dir() -> Path:
    for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(var):
            return Path(os.environ[var]).expanduser()
    home = os.environ.get("HF_HOME")
    return (Path(home).expanduser() / "hub") if home else Path.home() / ".cache" / "huggingface" / "hub"


def default_dirs() -> list[tuple[str, Path]]:
    return [("hf-cache", hf_cache_dir()), ("lm-studio", Path.home() / ".lmstudio" / "models"),
            ("lm-studio", Path.home() / ".cache" / "lm-studio" / "models")]


def _safetensors_meta(path: Path) -> dict | None:
    """The ``__metadata__`` object from a safetensors header, or None if unreadable."""
    try:
        with open(path, "rb") as f:
            n = struct.unpack("<Q", f.read(8))[0]
            if n <= 0 or n > MAX_HEADER:
                return None
            return json.loads(f.read(n)).get("__metadata__", {}) or {}
    except (OSError, ValueError, struct.error):
        return None


def _snapshot(model_dir: Path) -> Path | None:
    """The snapshot directory of an HF cache entry: refs/main if present, else the newest."""
    snaps = model_dir / "snapshots"
    if not snaps.is_dir():
        return None
    ref = model_dir / "refs" / "main"
    if ref.is_file():
        cand = snaps / ref.read_text().strip()
        if cand.is_dir():
            return cand
    dirs = [d for d in snaps.iterdir() if d.is_dir()]
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def _candidates(source: str, root: Path):
    """Yield (id, directory) for every model directory under ``root``."""
    if not root.is_dir():
        return
    if source == "hf-cache":
        for d in sorted(root.glob("models--*")):
            snap = _snapshot(d)
            if snap is not None:
                yield d.name[len("models--"):].replace("--", "/"), snap
        return
    for cfg in sorted(root.rglob("config.json")):                    # LM Studio: <publisher>/<model>/..., or any directory
        d = cfg.parent
        rel = d.relative_to(root).parts
        yield "/".join(rel[:2]) if rel else d.name, d


def _inspect(mid: str, source: str, d: Path) -> Found | None:
    cfg_path = d / "config.json"
    shards = sorted(d.glob("*.safetensors"))
    if not cfg_path.is_file() or not shards:
        return None
    try:
        cfg = _read_json(cfg_path)
    except (OSError, ValueError):
        return None
    t = cfg.get("text_config") or cfg
    size = 0
    for s in shards:
        try:
            size += os.stat(s).st_size                              # follows symlinks into HF blobs
        except OSError:
            return None
    q = cfg.get("quantization") or cfg.get("quantization_config") or {}
    meta = _safetensors_meta(shards[0])
    archs = list(cfg.get("architectures") or [])
    f = Found(id=mid, source=source, path=str(d), weights_gb=round(size / 1e9, 2), model_type=cfg.get("model_type"),
              architectures=archs, quant_bits=q.get("bits") if isinstance(q, dict) else None,
              is_mlx=bool(meta and meta.get("format") == "mlx") or (isinstance(q, dict) and "bits" in q and "group_size" in q),
              kind_guess="llm" if any(a.endswith(LLM_ARCH) for a in archs) else "other",
              max_context=kvmod.max_context(cfg))
    try:
        f.kv_kib_per_token = round(kvmod.kv_bytes_per_token(cfg) / 1024, 1)
    except kvmod.KVEstimateError as e:
        f.kv_error = str(e)
    return f


def mlx_lm_supported_types(python: str | None) -> set[str] | None:
    """model_type values the installed mlx-lm can load (module names plus its remapping table), or None if it can't be asked."""
    if not python:
        return None
    if python in _supported_cache:
        return _supported_cache[python]
    code = ("import pkgutil, mlx_lm.models as m\n"
            "names = {x.name for x in pkgutil.iter_modules(m.__path__)}\n"
            "try:\n    from mlx_lm.utils import MODEL_REMAPPING as R\n    names |= set(R)\nexcept Exception:\n    pass\n"
            "print(' '.join(sorted(names)))")
    try:
        out = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=30)
        res = set(out.stdout.split()) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        res = None
    _supported_cache[python] = res
    return res


def catalog_python(catalog: Catalog | None) -> str | None:
    if catalog is None:
        return None
    for s in catalog.backends.values():
        if s.adapter == "mlx_lm" and s.python:
            return s.python
    return None


def scan(catalog: Catalog | None = None, dirs: list[tuple[str, Path]] | None = None, *,
         supported_types: set[str] | None = None, llm_only: bool = False) -> list[Found]:
    """List MLX-format models found on disk, cross-referenced with the catalog. Read-only."""
    sup = supported_types if supported_types is not None else mlx_lm_supported_types(catalog_python(catalog))
    in_cat: dict[str, list[str]] = {}
    if catalog is not None:
        for s in catalog.backends.values():
            if s.adapter == "mlx_lm":
                in_cat.setdefault(s.options["model"], []).append(s.name)
    found: dict[str, Found] = {}
    for source, root in (dirs if dirs is not None else default_dirs()):
        for mid, d in _candidates(source, Path(root)):
            f = _inspect(mid, source, d)
            if f is None or not f.is_mlx or (llm_only and f.kind_guess != "llm"):
                continue
            f.supported = None if sup is None else (f.model_type in sup)
            f.in_catalog = in_cat.get(mid, [])
            found.setdefault(mid, f)                                  # first source wins (HF cache before LM Studio)
    return sorted(found.values(), key=lambda f: f.id.lower())


def _slug(mid: str) -> str:
    base = mid.split("/")[-1].lower()
    base = re.sub(r"(-?mlx)?-?\d+bit$", "", base)                    # drop a trailing quantization suffix
    return re.sub(r"[^a-z0-9]+", "-", base).strip("-") or "model"


def _ceil1(x: float) -> float:
    return float(-(-int(round(x * 100)) // 10)) / 10


def snippet(f: Found, *, name: str | None = None, context_tokens: int = 32768, kv_bits: int | None = None, ttl_s: int = 600,
            python: str = "/opt/homebrew/opt/mlx-lm/libexec/bin/python", overhead_gb: float = 1.0) -> str:
    """A ``[backends.*]`` table for gateway.toml, sized for ``context_tokens`` (weights + KV + overhead). Text only: never applied."""
    name = name or _slug(f.id)
    kvg = f.kv_gb(context_tokens, kv_bits)
    lines = [f"[backends.{name}]            # {f.model_type}, {f.weights_gb} GB weights"
             + (f" at {f.quant_bits}-bit" if f.quant_bits else "")
             + (f"; KV about {kvg:.1f} GB at {context_tokens} tokens ({'fp16' if not kv_bits else f'{kv_bits}-bit'})" if kvg is not None else
                f"; KV could not be estimated ({f.kv_error or 'unreadable config'}), set kv_gb yourself"),
             'adapter = "mlx_lm"', f'python = "{python}"', f'model = "{f.id}"', f"weights_gb = {_ceil1(f.weights_gb)}",
             f"kv_gb = {_ceil1(kvg) if kvg is not None else 0.0}", f"overhead_gb = {overhead_gb}", f"context_tokens = {context_tokens}",
             f"ttl_s = {ttl_s}"]
    if kv_bits:
        lines.append(f'args = ["--kv-bits", "{kv_bits}"]')
    if f.supported is False:
        lines.insert(0, f"# WARNING: the installed mlx-lm does not list model_type {f.model_type!r}; this may not load.")
    return "\n".join(lines) + "\n"


def to_dict(f: Found, context_tokens: int = 32768, kv_bits: int | None = None) -> dict:
    d = asdict(f)
    d["kv_gb_at_context"] = None if (g := f.kv_gb(context_tokens, kv_bits)) is None else round(g, 2)
    d["context_tokens"], d["kv_bits"] = context_tokens, kv_bits
    return d
