"""Manual driver for the supervisor (the daemon comes in phase 3+). In-process: shows the lifecycle live.

  python -m gateway.cli catalog                        print backends and the exact commands the supervisor would run
  python -m gateway.cli demo NAME [--ttl S] [--cycles N] [--path /health]
        lease the backend (lazy start), wait past its TTL so it passivates, lease again (depassivation); events print live
"""
import argparse, asyncio, sys, time

from . import catalog as cat_mod
from .supervisor import Supervisor


def fmt_event(e, t0):
    extra = " ".join(f"{k}={v}" for k, v in e.items() if k not in ("ts", "kind", "backend"))
    return f"  +{e['ts'] - t0:6.1f}s  {e['kind']:<12} {e['backend']:<14} {extra}"


async def demo(args):
    cat = cat_mod.load(args.catalog)
    sup = Supervisor(cat, reap_interval=0.5)
    t0 = time.time()
    q = sup.subscribe()

    async def printer():
        while True:
            print(fmt_event(await q.get(), t0), flush=True)

    pt = asyncio.create_task(printer())
    try:
        sup.set_policy(args.name, ttl_s=args.ttl)
        for i in range(1, args.cycles + 1):
            print(f"\n[{i}/{args.cycles}] request (backend may need to start)...", flush=True)
            t = time.time()
            async with sup.lease(args.name) as url:
                import httpx
                r = await httpx.AsyncClient(trust_env=False).get(url + args.path)
                print(f"  -> {r.status_code} in {time.time() - t:.2f}s total (includes any start)", flush=True)
            s = sup.snapshot()[sup.snapshot().index(next(x for x in sup.snapshot() if x['name'] == args.name))]
            print(f"  state={s['state']} idle_s={s['idle_s']} ttl_left_s={s['ttl_left_s']} starts={s['starts']}", flush=True)
            if i < args.cycles:
                print(f"  waiting {args.ttl + 2:.0f}s for the idle reaper (ttl {args.ttl:.0f}s)...", flush=True)
                await asyncio.sleep(args.ttl + 2)
                s = next(x for x in sup.snapshot() if x["name"] == args.name)
                print(f"  state={s['state']} pid={s['pid']} passivations={s['passivations']}", flush=True)
    finally:
        await sup.shutdown()
        await asyncio.sleep(0.1)
        pt.cancel()


def discover(a) -> None:
    from pathlib import Path
    from . import discovery
    cat = cat_mod.load(a.catalog)
    dirs = discovery.default_dirs() + [("dir", Path(d).expanduser()) for d in a.dir]
    found = discovery.scan(cat, dirs, llm_only=not a.all)
    if a.snippet:
        hits = [f for f in found if f.id == a.snippet] or [f for f in found if a.snippet.lower() in f.id.lower()]
        if len(hits) != 1:
            sys.exit(f"{'no model' if not hits else 'ambiguous: ' + ', '.join(f.id for f in hits)} matches {a.snippet!r}; run `discover` to list")
        print(discovery.snippet(hits[0], context_tokens=a.context, kv_bits=a.kv_bits, python=discovery.catalog_python(cat) or "python3"), end="")
        return
    bits = f"{a.kv_bits}-bit" if a.kv_bits else "fp16"
    print(f"{len(found)} MLX model(s) found; KV sized for {a.context} tokens ({bits}). Read-only: nothing is changed or started.\n")
    print(f"{'model':56s} {'GB':>6s} {'type':14s} {'bits':>4s} {'mlx-lm':>6s} {'KiB/tok':>8s} {'KV':>6s}  in catalog")
    for f in found:
        kv = f.kv_gb(a.context, a.kv_bits)
        sup = {True: "yes", False: "NO", None: "?"}[f.supported]
        print(f"{f.id:56s} {f.weights_gb:6.1f} {str(f.model_type):14s} {str(f.quant_bits or '-'):>4s} {sup:>6s} "
              f"{f.kv_kib_per_token if f.kv_kib_per_token is not None else '-':>8} {('%.1fG' % kv) if kv is not None else '-':>6s}  {', '.join(f.in_catalog) or '-'}")
    print("\nTo add one: python -m gateway.cli discover --snippet <id> [--context N] [--kv-bits N]  (prints a table to paste into gateway.toml)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m gateway.cli")
    ap.add_argument("--catalog", default="gateway.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("catalog")
    d = sub.add_parser("demo")
    d.add_argument("name")
    d.add_argument("--ttl", type=float, default=5)
    d.add_argument("--cycles", type=int, default=2)
    d.add_argument("--path", default="/health")
    v = sub.add_parser("discover", help="list MLX models on disk (read-only); --snippet ID prints a catalog entry for one")
    v.add_argument("--dir", action="append", default=[], help="extra directory to scan (repeatable)")
    v.add_argument("--context", type=int, default=32768, help="context length (tokens) to size the KV cache for")
    v.add_argument("--kv-bits", type=int, choices=[2, 3, 4, 5, 6, 8], default=None, help="quantized KV cache (adds --kv-bits to the snippet)")
    v.add_argument("--all", action="store_true", help="include MLX models that are not generative LLMs")
    v.add_argument("--snippet", metavar="ID", help="print a [backends.*] table for this model (exact id or unique substring)")
    a = ap.parse_args(argv)
    if a.cmd == "discover":
        return discover(a)
    if a.cmd == "catalog":
        c = cat_mod.load(a.catalog)
        print(f"gateway {c.gateway.host}:{c.gateway.port}, budget {c.gateway.memory_budget_gb} GB")
        for s in c.backends.values():
            mem = f"~{s.est_mem_gb} GB" + (f" (weights {s.weights_gb} + KV {s.kv_gb} + overhead {s.overhead_gb}"
                                           + (f", sized for {s.context_tokens} tokens" if s.context_tokens else "") + ")" if s.weights_gb else "")
            print(f"- {s.name} [{s.kind}] :{s.port} {mem} ttl {s.ttl_s}s start-timeout {s.start_timeout_s}s concurrency {s.concurrency}\n    {' '.join(s.command())}")
        for p in c.profiles.values():
            print(f"- profile {p.name} -> {p.backend}" + (f" (aliases {', '.join(p.aliases)})" if p.aliases else "") + f": {p.defaults}"
                  + (" + system prompt" if p.system_prompt else "") + (f"  # {p.description}" if p.description else ""))
    else:
        asyncio.run(demo(a))


if __name__ == "__main__":
    main()
