"""Model catalog: parse + validate gateway.toml and turn a backend spec into a launch command.

Adding a model is configuration only: pick a built-in adapter and fill in its keys. Specs are static (read from the file,
never built from a request), so the gateway can only ever launch what the catalog declares.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

BACKENDS_DIR = Path(__file__).parent / "backends"


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class GatewaySettings:
    host: str = "127.0.0.1"
    port: int = 8090
    backend_port_base: int = 18101
    memory_budget_gb: float = 28.0
    default_llm: str | None = None
    state_dir: str | None = None      # logs + pidfiles; default <catalog dir>/.gateway
    room_timeout_s: float = 20.0      # how long a start may wait for memory (busy or pinned backends) before 503
    pressure_eviction: bool = True    # evict the LRU idle backend when macOS reports memory pressure
    pressure_interval_s: float = 5.0  # how often to sample system memory
    pressure_window_s: float = 30.0   # swap growth is measured over this window
    swap_growth_gb: float = 1.0       # evict if swap grew by at least this much within the window
    min_free_pct: int = 12            # evict if macOS free-memory percentage drops below this (0 disables)


@dataclass(frozen=True)
class BackendSpec:
    name: str
    adapter: str
    kind: str                      # llm | decision | nli | custom
    python: str | None
    est_mem_gb: float
    ttl_s: int
    pinned: bool
    health: str                    # HTTP path that returns 200 when the backend is ready
    port: int
    options: dict[str, Any] = field(default_factory=dict)   # adapter-specific keys
    env: dict[str, str] = field(default_factory=dict)
    start_timeout_s: int = 180     # give up if /health is not 200 by then
    concurrency: int = 1           # simultaneous requests the backend may serve (heavy models: 1)
    aliases: tuple[str, ...] = ()  # extra names clients may use as `model` (an mlx_lm model id is an implicit alias)

    def command(self) -> list[str]:
        return ADAPTERS[self.adapter].build(self)


@dataclass(frozen=True)
class Catalog:
    gateway: GatewaySettings
    backends: dict[str, BackendSpec]
    base_dir: Path

    def state_path(self) -> Path:
        """Where logs, pidfiles and the token live."""
        return Path(self.gateway.state_dir).expanduser() if self.gateway.state_dir else self.base_dir / ".gateway"

    def total_ports(self) -> list[int]:
        return [b.port for b in self.backends.values()]

    def find(self, model: str | None, kind: str) -> BackendSpec:
        """Resolve a client-supplied model name (backend name or alias) to a backend of the given kind."""
        if not model:
            if kind == "llm" and self.gateway.default_llm:
                return self.backends[self.gateway.default_llm]
            for s in self.backends.values():
                if s.kind == kind:
                    return s
            raise KeyError(f"no {kind} backend in the catalog")
        for s in self.backends.values():
            if model == s.name or model in s.aliases:
                if s.kind != kind:
                    raise ValueError(f"{model!r} is a {s.kind} backend, not {kind}")
                return s
        raise KeyError(f"unknown model {model!r}")


@dataclass(frozen=True)
class Adapter:
    kind: str
    required: tuple[str, ...]
    optional: tuple[str, ...]
    health: str
    build: Callable[[BackendSpec], list[str]]
    path_keys: tuple[str, ...] = ()        # option keys that are filesystem paths (resolved against the catalog dir)


def _mlx_lm(s: BackendSpec) -> list[str]:
    return [s.python, "-m", "mlx_lm.server", "--model", s.options["model"], "--host", "127.0.0.1", "--port", str(s.port)]


def _jevstyle(s: BackendSpec) -> list[str]:
    return [s.python, str(BACKENDS_DIR / "jevstyle_server.py"), "--model-dir", s.options["model_dir"],
            "--precision", s.options.get("precision", "8bit"), "--port", str(s.port)]


def _openjev_nli(s: BackendSpec) -> list[str]:
    return [s.python, str(BACKENDS_DIR / "openjev_server.py"), "--root", s.options["root"],
            "--subfolder", s.options["subfolder"], "--port", str(s.port)]


def _command(s: BackendSpec) -> list[str]:
    return [str(a).replace("{port}", str(s.port)) for a in s.options["command"]]


ADAPTERS: dict[str, Adapter] = {
    "mlx_lm": Adapter("llm", ("python", "model"), (), "/v1/models", _mlx_lm),
    "jevstyle": Adapter("decision", ("python", "model_dir"), ("precision",), "/health", _jevstyle, ("model_dir",)),
    "openjev_nli": Adapter("nli", ("python", "root", "subfolder"), (), "/health", _openjev_nli, ("root",)),
    "command": Adapter("custom", ("command",), ("health", "kind"), "/health", _command),
}
COMMON = {"adapter", "est_mem_gb", "ttl_s", "pinned", "env", "start_timeout_s", "concurrency", "aliases"}


def _check_type(name: str, key: str, value: Any, types: tuple[type, ...]) -> None:
    if isinstance(value, bool) and bool not in types or not isinstance(value, types):
        raise CatalogError(f"backends.{name}.{key}: expected {'/'.join(t.__name__ for t in types)}, got {type(value).__name__}")


def parse(data: dict, base_dir: Path) -> Catalog:
    g = data.get("gateway", {})
    unknown = set(g) - {"host", "port", "backend_port_base", "memory_budget_gb", "default_llm", "state_dir", "room_timeout_s",
                        "pressure_eviction", "pressure_interval_s", "pressure_window_s", "swap_growth_gb", "min_free_pct"}
    if unknown:
        raise CatalogError(f"[gateway]: unknown keys {sorted(unknown)}")
    gs = GatewaySettings(**g)
    for key in ("memory_budget_gb", "room_timeout_s", "pressure_interval_s", "pressure_window_s", "swap_growth_gb"):
        v = getattr(gs, key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise CatalogError(f"[gateway].{key} must be a positive number")
    if isinstance(gs.min_free_pct, bool) or not isinstance(gs.min_free_pct, int) or not 0 <= gs.min_free_pct < 100:
        raise CatalogError("[gateway].min_free_pct must be an integer in 0..99")
    if not isinstance(gs.pressure_eviction, bool):
        raise CatalogError("[gateway].pressure_eviction must be true or false")
    if gs.host not in ("127.0.0.1", "localhost", "::1"):
        raise CatalogError("[gateway].host must be a loopback address (the gateway is localhost-only)")
    raw = data.get("backends", {})
    if not raw:
        raise CatalogError("no [backends.*] defined")
    specs: dict[str, BackendSpec] = {}
    for i, (name, b) in enumerate(raw.items()):
        if not name.replace("-", "").replace("_", "").replace(".", "").isalnum():
            raise CatalogError(f"backend name {name!r}: use letters, digits, '-', '_' or '.'")
        adapter = b.get("adapter")
        if adapter not in ADAPTERS:
            raise CatalogError(f"backends.{name}.adapter: {adapter!r} is not one of {sorted(ADAPTERS)}")
        ad = ADAPTERS[adapter]
        missing = [k for k in ad.required if k not in b]
        if missing:
            raise CatalogError(f"backends.{name} ({adapter}): missing required keys {missing}")
        extra = set(b) - COMMON - set(ad.required) - set(ad.optional)
        if extra:
            raise CatalogError(f"backends.{name} ({adapter}): unknown keys {sorted(extra)}")
        est = b.get("est_mem_gb")
        if est is None:
            raise CatalogError(f"backends.{name}: est_mem_gb is required (the memory budget needs it)")
        _check_type(name, "est_mem_gb", est, (int, float))
        if est <= 0:
            raise CatalogError(f"backends.{name}.est_mem_gb must be > 0")
        ttl = b.get("ttl_s", 600)
        _check_type(name, "ttl_s", ttl, (int,))
        if ttl < 0:
            raise CatalogError(f"backends.{name}.ttl_s must be >= 0")
        pinned = b.get("pinned", False)
        _check_type(name, "pinned", pinned, (bool,))
        if est > gs.memory_budget_gb:
            raise CatalogError(f"backends.{name}.est_mem_gb ({est}) exceeds the whole memory budget ({gs.memory_budget_gb})")
        opts = {k: v for k, v in b.items() if k not in COMMON and k not in ("python",)}
        python = b.get("python")
        if python is not None:
            _check_type(name, "python", python, (str,))
            python = str(_resolve(base_dir, python))
        for k in ad.path_keys:
            opts[k] = str(_resolve(base_dir, opts[k]))
        if adapter == "command":
            cmd = opts["command"]
            if not (isinstance(cmd, list) and cmd and all(isinstance(a, str) for a in cmd)):
                raise CatalogError(f"backends.{name}.command: expected a non-empty list of strings")
        start_timeout = b.get("start_timeout_s", 180)
        _check_type(name, "start_timeout_s", start_timeout, (int, float))
        if start_timeout <= 0:
            raise CatalogError(f"backends.{name}.start_timeout_s must be > 0")
        conc = b.get("concurrency", 1)
        _check_type(name, "concurrency", conc, (int,))
        if conc < 1:
            raise CatalogError(f"backends.{name}.concurrency must be >= 1")
        aliases = b.get("aliases", [])
        if not (isinstance(aliases, list) and all(isinstance(a, str) and a for a in aliases)):
            raise CatalogError(f"backends.{name}.aliases: expected a list of non-empty strings")
        if adapter == "mlx_lm":
            aliases = [*aliases, b["model"]]
        env = b.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise CatalogError(f"backends.{name}.env: expected a table of strings")
        specs[name] = BackendSpec(
            name=name, adapter=adapter, kind=opts.pop("kind", ad.kind) if adapter == "command" else ad.kind,
            python=python, est_mem_gb=float(est), ttl_s=ttl, pinned=pinned,
            health=opts.pop("health", ad.health) if adapter == "command" else ad.health,
            port=gs.backend_port_base + i, options=opts, env=env, start_timeout_s=start_timeout, concurrency=conc, aliases=tuple(dict.fromkeys(aliases)))
    pinned_gb = sum(sp.est_mem_gb for sp in specs.values() if sp.pinned)
    if pinned_gb > gs.memory_budget_gb:
        raise CatalogError(f"pinned backends need {pinned_gb} GB, more than the whole memory budget ({gs.memory_budget_gb} GB)")
    seen: dict[str, str] = {}
    for n, sp in specs.items():                       # every name and alias must be unique across the catalog
        for key in (n, *sp.aliases):
            if key in seen and seen[key] != n:
                raise CatalogError(f"name/alias {key!r} is used by both {seen[key]!r} and {n!r}")
            seen[key] = n
    if gs.default_llm is not None and (gs.default_llm not in specs or specs[gs.default_llm].kind != "llm"):
        raise CatalogError(f"[gateway].default_llm {gs.default_llm!r} must name an llm backend")
    return Catalog(gs, specs, base_dir)


def _resolve(base: Path, p: str) -> Path:
    path = Path(p).expanduser()
    return path if path.is_absolute() else (base / path)


def load(path: str | Path) -> Catalog:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise CatalogError(f"{path}: invalid TOML: {e}") from e
    return parse(data, path.parent.resolve())


if __name__ == "__main__":      # python -m gateway.catalog [gateway.toml]: print what would be launched
    import sys
    cat = load(sys.argv[1] if len(sys.argv) > 1 else "gateway.toml")
    print(f"gateway {cat.gateway.host}:{cat.gateway.port}, budget {cat.gateway.memory_budget_gb} GB")
    for s in cat.backends.values():
        print(f"- {s.name} [{s.kind}] :{s.port} ~{s.est_mem_gb} GB ttl {s.ttl_s}s health {s.health}\n    {' '.join(s.command())}")
