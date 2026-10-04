"""Run the gateway:  python -m gateway [--catalog gateway.toml]   (use the gateway venv, see README)"""
import argparse, asyncio, contextlib, os, signal, sys, time

import uvicorn

from . import catalog as cat_mod
from .app import create_app
from .supervisor import Supervisor


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m gateway")
    ap.add_argument("--catalog", default=os.environ.get("GATEWAY_CATALOG", "gateway.toml"))
    ap.add_argument("--log-level", default="info")
    a = ap.parse_args(argv)
    cat = cat_mod.load(a.catalog)

    sup_holder = {}

    async def serve():
        sup = Supervisor(cat)
        sup_holder["sup"] = sup
        q = sup.subscribe()

        async def log_events():                                   # lifecycle events to stdout (launchd captures this)
            t0 = time.time()
            while True:
                e = await q.get()
                extra = " ".join(f"{k}={v}" for k, v in e.items() if k not in ("ts", "kind", "backend"))
                print(f"[{time.strftime('%H:%M:%S', time.localtime(e['ts']))}] {e['kind']:<11} {e['backend']:<14} {extra}", flush=True)

        t = asyncio.create_task(log_events())
        app = create_app(cat, supervisor=sup)
        config = uvicorn.Config(app, host=cat.gateway.host, port=cat.gateway.port, log_level=a.log_level, lifespan="on")
        server = uvicorn.Server(config)
        # uvicorn >= 0.29 re-raises a captured SIGTERM as soon as serve() returns, killing the process BEFORE our cleanup below
        # runs (found the hard way: backends were left running). So we own signal handling: the first signal asks uvicorn to
        # exit gracefully, a second forces it, and then the finally block always stops every backend.
        server.capture_signals = contextlib.nullcontext
        loop = asyncio.get_running_loop()
        hits = []

        def on_signal():
            hits.append(1)
            server.should_exit = True
            if len(hits) > 1:
                server.force_exit = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, on_signal)
        try:
            await server.serve()
        finally:
            await sup.shutdown()                                  # stop every backend before exiting
            t.cancel()

    print(f"gateway on http://{cat.gateway.host}:{cat.gateway.port}  backends: {', '.join(cat.backends)}", flush=True)
    asyncio.run(serve())


if __name__ == "__main__":
    main()
