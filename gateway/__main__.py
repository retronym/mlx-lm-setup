"""Run the gateway:  python -m gateway [serve] [--catalog gateway.toml]
Helpers:           python -m gateway token   print the token needed by start/stop/policy tools (creates it on first use)
                   python -m gateway url     print the MCP endpoint URL
                   python -m gateway admin   open the admin site in your browser, authenticated (token in the URL fragment)"""
import argparse, asyncio, contextlib, fcntl, os, signal, sys, time

import uvicorn

from . import catalog as cat_mod
from .app import create_app
from .supervisor import Supervisor


GRACEFUL_SHUTDOWN_S = 5       # long-lived connections (SSE, MCP streams) never end by themselves; cut them after this


def acquire_instance_lock(state_dir) -> int:
    """One gateway per state directory. A second one would reap the first one's backends as 'orphans'. flock is released by the
    OS when the process dies, so a crash never leaves a stale lock. Returns the open fd (keep it open for the process lifetime)."""
    os.makedirs(state_dir, exist_ok=True)
    fd = os.open(os.path.join(state_dir, "gateway.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = os.read(fd, 32).decode().strip() or "unknown pid"
        os.close(fd)
        print(f"error: another gateway is already running for this state directory ({state_dir}; pid {holder}).", file=sys.stderr)
        raise SystemExit(2)
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


def admin_url(cat, token: str) -> str:
    """Admin page URL with the token in the fragment: browsers never send a fragment over the network or into logs."""
    return f"http://{cat.gateway.host}:{cat.gateway.port}/admin#token={token}"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m gateway")
    ap.add_argument("command", nargs="?", default="serve", choices=["serve", "token", "url", "admin"])
    ap.add_argument("--catalog", default=os.environ.get("GATEWAY_CATALOG", "gateway.toml"))
    ap.add_argument("--log-level", default="info")
    a = ap.parse_args(argv)
    cat = cat_mod.load(a.catalog)
    if a.command == "token":
        from . import auth
        print(auth.load_or_create(cat.state_path() / "token"))
        return
    if a.command == "admin":
        import webbrowser
        from . import auth
        webbrowser.open(admin_url(cat, auth.load_or_create(cat.state_path() / "token")))
        print(f"opened http://{cat.gateway.host}:{cat.gateway.port}/admin")
        return
    if a.command == "url":
        print(f"http://{cat.gateway.host}:{cat.gateway.port}/mcp")
        return

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
        config = uvicorn.Config(app, host=cat.gateway.host, port=cat.gateway.port, log_level=a.log_level, lifespan="on",
                                timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S)
        server = uvicorn.Server(config)
        # uvicorn >= 0.29 re-raises a captured SIGTERM as soon as serve() returns, killing the process BEFORE our cleanup below
        # runs (found the hard way: backends were left running). So we own signal handling: the first signal asks uvicorn to
        # exit gracefully, a second forces it, and then the finally block always stops every backend.
        server.capture_signals = contextlib.nullcontext
        loop = asyncio.get_running_loop()
        hits = []

        def on_signal():
            hits.append(1)
            app.stopping.set()                                    # SSE streams end within a second, before the timeout backstop
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

    lock_fd = acquire_instance_lock(cat.state_path())             # noqa: F841  (held until the process exits)
    print(f"gateway on http://{cat.gateway.host}:{cat.gateway.port}  backends: {', '.join(cat.backends)}", flush=True)
    asyncio.run(serve())


if __name__ == "__main__":
    main()
