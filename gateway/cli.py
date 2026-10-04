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
    a = ap.parse_args(argv)
    if a.cmd == "catalog":
        c = cat_mod.load(a.catalog)
        print(f"gateway {c.gateway.host}:{c.gateway.port}, budget {c.gateway.memory_budget_gb} GB")
        for s in c.backends.values():
            print(f"- {s.name} [{s.kind}] :{s.port} ~{s.est_mem_gb} GB ttl {s.ttl_s}s start-timeout {s.start_timeout_s}s concurrency {s.concurrency}\n    {' '.join(s.command())}")
    else:
        asyncio.run(demo(a))


if __name__ == "__main__":
    main()
