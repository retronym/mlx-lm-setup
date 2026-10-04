"""Supervisor: owns the lifecycle of the catalog's backends (subprocesses).

State machine per backend:   STOPPED -> STARTING -> READY -> PASSIVATING -> STOPPED      (FAILED on a failed start)

* Lazy start ("depassivation"): ``lease(name)`` starts the backend if needed. Concurrent callers share ONE start (single-flight).
* Passivation: a background reaper terminates READY backends that have been idle past their TTL (never while a request is
  pending or in flight, never if pinned or ttl == 0). Process exit is what actually returns Metal memory.
* Per-backend request queue: ``lease`` admits at most ``concurrency`` requests at a time (heavy models: 1).
* Safety: every backend runs in its own process group; termination is SIGTERM to the group, SIGKILL after a grace period, and a
  final group SIGKILL for stragglers. Pidfiles let a restarted supervisor reap orphans left by a crash.

Asyncio single-threaded: a check followed by a state flip with no ``await`` in between is atomic, which is how the reaper and
``lease`` avoid racing each other.
"""
from __future__ import annotations

import asyncio
import contextlib
import enum
import json
import os
import signal
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

import httpx

from .catalog import BackendSpec, Catalog
from .memory import macos_probe


class State(str, enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    PASSIVATING = "passivating"
    FAILED = "failed"


class GatewayError(Exception):
    pass


class UnknownBackend(GatewayError):
    pass


class StartFailed(GatewayError):
    pass


class BackendUnavailable(GatewayError):
    """The backend failed recently and is in its retry backoff window."""


class InsufficientMemory(GatewayError):
    """Starting the backend would exceed the memory budget and nothing can be evicted (busy or pinned backends hold it)."""


@dataclass
class Runtime:
    spec: BackendSpec
    ttl_s: float
    pinned: bool
    state: State = State.STOPPED
    proc: asyncio.subprocess.Process | None = None
    pgid: int | None = None
    pending: int = 0            # leases admitted or waiting (blocks passivation)
    in_flight: int = 0          # leases currently executing
    requests: int = 0
    starts: int = 0
    passivations: int = 0
    fail_count: int = 0
    retry_after: float = 0.0
    last_error: str | None = None
    last_used: float = 0.0      # monotonic
    started_wall: float | None = None
    start_secs: float | None = None
    transition: asyncio.Task | None = None
    admitted: bool = False      # a STARTING backend only holds budget once admitted
    sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.spec.port}"


class Supervisor:
    def __init__(self, catalog: Catalog, state_dir: str | Path | None = None, *, reap_interval: float = 1.0,
                 health_interval: float = 0.25, grace_s: float = 10.0, clock=time.monotonic, memory_probe=None):
        self.catalog = catalog
        sd = state_dir or catalog.gateway.state_dir or (catalog.base_dir / ".gateway")
        self.state_dir = Path(sd)
        (self.state_dir / "logs").mkdir(parents=True, exist_ok=True)
        (self.state_dir / "pids").mkdir(parents=True, exist_ok=True)
        self.reap_interval, self.health_interval, self.grace_s, self.clock = reap_interval, health_interval, grace_s, clock
        self.rt: dict[str, Runtime] = {}
        for n, s in catalog.backends.items():
            r = Runtime(spec=s, ttl_s=float(s.ttl_s), pinned=s.pinned)
            r.sem = asyncio.Semaphore(s.concurrency)
            self.rt[n] = r
        self.events: deque[dict] = deque(maxlen=500)
        self._subs: set[asyncio.Queue] = set()
        self._http: httpx.AsyncClient | None = None
        self._reaper: asyncio.Task | None = None
        self._watchers: set[asyncio.Task] = set()
        self._probe = memory_probe or macos_probe
        self._admission = asyncio.Lock()
        self._pressure: asyncio.Task | None = None
        self._swap_samples: deque[tuple[float, float]] = deque()
        self._last_pressure_evict = -1e9
        self.last_system: dict = {}

    # ---- events ---------------------------------------------------------------------------------------------------
    def emit(self, kind: str, backend: str, **kw) -> None:
        ev = {"ts": time.time(), "kind": kind, "backend": backend, **kw}
        self.events.append(ev)
        for q in list(self._subs):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(ev)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    # ---- lifecycle of the supervisor itself -----------------------------------------------------------------------
    async def __aenter__(self) -> "Supervisor":
        await self.start_background()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.shutdown()

    async def start_background(self) -> None:
        self.reap_orphans()
        self._ensure_reaper()

    def _ensure_reaper(self) -> None:
        """The idle reaper starts on first use, so a supervisor can never silently skip passivation."""
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_loop(), name="idle-reaper")
        if self.catalog.gateway.pressure_eviction and (self._pressure is None or self._pressure.done()):
            self._pressure = asyncio.create_task(self._pressure_loop(), name="pressure-monitor")

    async def shutdown(self) -> None:
        if self._reaper:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        if self._pressure:
            self._pressure.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pressure
            self._pressure = None
        for name in list(self.rt):
            with contextlib.suppress(Exception):
                await self.stop(name, force=True)
        for t in list(self._watchers):
            t.cancel()
        if self._http:
            await self._http.aclose()
            self._http = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(trust_env=False)
        return self._http

    def _get(self, name: str) -> Runtime:
        try:
            return self.rt[name]
        except KeyError:
            raise UnknownBackend(f"unknown backend {name!r}; catalog has {sorted(self.rt)}") from None

    # ---- public API -----------------------------------------------------------------------------------------------
    async def ensure_running(self, name: str, *, explicit: bool = False) -> Runtime:
        """Return once the backend is READY, starting it if needed. Concurrent callers share one start."""
        b = self._get(name)
        self._ensure_reaper()
        while True:
            if b.state is State.READY:
                return b
            t = b.transition
            if t is not None and not t.done():
                await asyncio.shield(t)                      # a failed start raises StartFailed in every waiter
                continue
            if b.state in (State.STOPPED, State.FAILED):
                if b.state is State.FAILED and not explicit and self.clock() < b.retry_after:
                    raise BackendUnavailable(f"{name} failed recently ({b.last_error}); retry in "
                                             f"{b.retry_after - self.clock():.0f}s")
                b.transition = asyncio.create_task(self._do_start(b), name=f"start-{name}")
                continue
            await asyncio.sleep(0)                           # transient state; let the owning task run

    @contextlib.asynccontextmanager
    async def lease(self, name: str) -> AsyncIterator[str]:
        """Admit one request: ensure the backend is up, wait for a slot, yield its base URL. Resets the idle clock on exit."""
        b = self._get(name)
        b.pending += 1                                       # blocks passivation from this moment on
        try:
            await self.ensure_running(name)
            async with b.sem:
                b.in_flight += 1
                b.requests += 1
                b.last_used = self.clock()
                try:
                    yield b.base_url
                finally:
                    b.in_flight -= 1
                    b.last_used = self.clock()
        finally:
            b.pending -= 1

    async def start(self, name: str) -> Runtime:
        return await self.ensure_running(name, explicit=True)

    async def stop(self, name: str, *, force: bool = False, drain_timeout: float = 30.0) -> None:
        """Stop a backend (idempotent). Unless ``force``, wait for pending requests to drain first."""
        b = self._get(name)
        t = b.transition
        if t is not None and not t.done():
            with contextlib.suppress(GatewayError):
                await asyncio.shield(t)                      # let a start/stop in progress settle
        if not force:
            deadline = self.clock() + drain_timeout
            while b.pending > 0 and self.clock() < deadline:
                await asyncio.sleep(0.05)
        if b.proc is not None and b.state is not State.PASSIVATING:
            self._begin_terminate(b, "stopped by request")
        t = b.transition
        if t is not None:
            await asyncio.shield(t)

    def set_policy(self, name: str, *, ttl_s: float | None = None, pinned: bool | None = None) -> None:
        b = self._get(name)
        if ttl_s is not None:
            if ttl_s < 0:
                raise ValueError("ttl_s must be >= 0")
            b.ttl_s = float(ttl_s)
        if pinned is not None:
            b.pinned = bool(pinned)
        self.emit("policy", name, ttl_s=b.ttl_s, pinned=b.pinned)

    def snapshot(self) -> list[dict]:
        now = self.clock()
        out = []
        for n, b in self.rt.items():
            idle = (now - b.last_used) if b.state is State.READY else None
            out.append({
                "name": n, "kind": b.spec.kind, "adapter": b.spec.adapter, "state": b.state.value, "busy": b.in_flight > 0,
                "pid": b.proc.pid if b.proc and b.proc.returncode is None else None, "port": b.spec.port,
                "est_mem_gb": b.spec.est_mem_gb, "ttl_s": b.ttl_s, "pinned": b.pinned,
                "idle_s": None if idle is None else round(idle, 1),
                "ttl_left_s": (None if idle is None or b.ttl_s == 0 or b.pinned else max(0.0, round(b.ttl_s - idle, 1))),
                "pending": b.pending, "in_flight": b.in_flight, "requests": b.requests, "starts": b.starts,
                "passivations": b.passivations, "holds_memory": self._holds(b), "last_start_s": b.start_secs, "last_error": b.last_error,
                "uptime_s": round(time.time() - b.started_wall, 1) if b.started_wall and b.state is State.READY else None,
            })
        return out

    def log_tail(self, name: str, n: int = 20) -> str:
        p = self.state_dir / "logs" / f"{name}.log"
        try:
            return "\n".join(p.read_text(errors="replace").splitlines()[-n:])
        except OSError:
            return ""

    # ---- start ----------------------------------------------------------------------------------------------------
    async def _do_start(self, b: Runtime) -> None:
        name = b.spec.name
        b.state = State.STARTING
        b.last_error = None
        self.emit("starting", name, port=b.spec.port)
        t0 = self.clock()
        proc = None
        try:
            await self._admit(b)                              # memory budget: may evict idle backends or wait for room
            t0 = self.clock()
            log = open(self.state_dir / "logs" / f"{name}.log", "ab")
            log.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
            log.flush()
            cmd = b.spec.command()
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                                        start_new_session=True, env={**os.environ, **b.spec.env})
            log.close()
            b.proc, b.pgid = proc, proc.pid
            self._write_pidfile(b, cmd)
            deadline = t0 + b.spec.start_timeout_s
            while True:
                if proc.returncode is not None:
                    raise StartFailed(f"{name} exited with code {proc.returncode} before becoming ready\n{self.log_tail(name, 8)}")
                if await self._healthy(b):
                    break
                if self.clock() > deadline:
                    raise StartFailed(f"{name} not ready after {b.spec.start_timeout_s}s")
                await asyncio.sleep(self.health_interval)
            b.state = State.READY
            b.starts += 1
            b.fail_count = 0
            b.start_secs = round(self.clock() - t0, 2)
            b.started_wall = time.time()
            b.last_used = self.clock()
            self.emit("ready", name, pid=proc.pid, secs=b.start_secs)
            w = asyncio.create_task(self._watch(b, proc), name=f"watch-{name}")
            self._watchers.add(w)
            w.add_done_callback(self._watchers.discard)
        except BaseException as e:                           # includes cancellation: never leave a half-started process behind
            if proc is not None:
                await self._kill_group(proc, b.pgid)
            self._remove_pidfile(b)
            b.proc = b.pgid = None
            b.admitted = False
            if isinstance(e, asyncio.CancelledError):
                b.state = State.STOPPED
                raise
            if isinstance(e, InsufficientMemory):             # not the backend's fault: no FAILED state, no backoff
                b.state = State.STOPPED
                self.emit("rejected", name, reason=str(e).splitlines()[0])
                raise
            b.state = State.FAILED
            b.fail_count += 1
            b.last_error = str(e).splitlines()[0] if str(e) else type(e).__name__
            b.retry_after = self.clock() + min(30.0, 2.0 ** b.fail_count)
            self.emit("failed", name, error=b.last_error, retry_in_s=round(b.retry_after - self.clock(), 1))
            if isinstance(e, StartFailed):
                raise
            raise StartFailed(f"{name}: {e}") from e

    async def _healthy(self, b: Runtime) -> bool:
        try:
            r = await self._client().get(f"{b.base_url}{b.spec.health}", timeout=1.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def _watch(self, b: Runtime, proc: asyncio.subprocess.Process) -> None:
        """A READY backend that exits on its own has crashed: mark it STOPPED so the next lease restarts it."""
        rc = await proc.wait()
        if b.proc is proc and b.state is State.READY:
            b.state = State.STOPPED
            b.admitted = False
            b.last_error = f"crashed (exit code {rc})"
            self.emit("crashed", b.spec.name, code=rc)
            self._remove_pidfile(b)
            b.proc = b.pgid = None
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)         # reap any grandchildren

    # ---- stop / passivate -----------------------------------------------------------------------------------------
    def _begin_terminate(self, b: Runtime, reason: str) -> None:
        """Flip to PASSIVATING synchronously (no await), then terminate in a task other callers can await."""
        t = b.transition
        if t is not None and not t.done() and b.state is State.PASSIVATING:
            return
        b.state = State.PASSIVATING
        b.transition = asyncio.create_task(self._do_terminate(b, reason), name=f"stop-{b.spec.name}")
        b.transition.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)   # retrieve, avoid warnings

    async def _do_terminate(self, b: Runtime, reason: str) -> None:
        name = b.spec.name
        self.emit("passivating", name, reason=reason)
        if b.proc is not None:
            await self._kill_group(b.proc, b.pgid)
        self._remove_pidfile(b)
        b.proc = b.pgid = None
        b.admitted = False
        b.state = State.STOPPED
        b.passivations += 1
        self.emit("stopped", name, reason=reason)

    async def _kill_group(self, proc: asyncio.subprocess.Process, pgid: int | None) -> None:
        pgid = pgid or proc.pid
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), self.grace_s)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pgid, signal.SIGKILL)
                await proc.wait()
        with contextlib.suppress(ProcessLookupError, PermissionError):   # stragglers (grandchildren) that outlived the leader
            os.killpg(pgid, signal.SIGKILL)

    # ---- memory budget: admission control + LRU eviction ----------------------------------------------------------
    def _holds(self, r: Runtime) -> bool:
        """Does this backend occupy budget right now? A PASSIVATING backend still does until its process has exited."""
        return r.state in (State.READY, State.PASSIVATING) or (r.state is State.STARTING and r.admitted)

    def resident_gb(self, exclude: Runtime | None = None) -> float:
        return sum(r.spec.est_mem_gb for r in self.rt.values() if r is not exclude and self._holds(r))

    def _evictable(self, exclude: Runtime | None) -> list[Runtime]:
        """Idle, unpinned, READY backends, least recently used first."""
        return sorted((r for r in self.rt.values() if r is not exclude and r.state is State.READY and not r.pinned
                       and r.pending == 0 and r.in_flight == 0), key=lambda r: r.last_used)

    def memory_status(self) -> dict:
        g = self.catalog.gateway
        res = self.resident_gb()
        return {"budget_gb": g.memory_budget_gb, "resident_gb": res, "free_gb": max(0.0, g.memory_budget_gb - res)}

    async def system_status(self) -> dict:
        """Fresh reading of what macOS says (free-memory percentage, swap)."""
        try:
            return {**await asyncio.to_thread(self._probe), "ts": time.time()}
        except Exception:                                    # noqa: BLE001
            return {}

    async def _admit(self, b: Runtime) -> None:
        """Reserve budget for ``b`` before it is spawned. Evicts idle backends (LRU) until it fits; waits for passivating
        ones to really exit; fails with InsufficientMemory after ``room_timeout_s`` if busy or pinned backends hold the room.
        The check-and-reserve step is serialized so two concurrent starts cannot both claim the same free memory."""
        g = self.catalog.gateway
        need, budget = b.spec.est_mem_gb, g.memory_budget_gb
        deadline = self.clock() + g.room_timeout_s
        announced = False
        while True:
            victim = None
            async with self._admission:
                resident = self.resident_gb(exclude=b)
                if resident + need <= budget + 1e-9:
                    b.admitted = True
                    return
                ev = self._evictable(b)
                if ev:
                    victim = ev[0]
                    self._begin_terminate(victim, f"evicted: making room for {b.spec.name} "
                                                  f"({need:g} GB needed, {resident:g} of {budget:g} GB in use)")
            if victim is not None:
                with contextlib.suppress(GatewayError):
                    await asyncio.shield(victim.transition)  # wait until the memory is really returned
                continue
            if not announced:
                announced = True
                self.emit("waiting_for_memory", b.spec.name, need_gb=need, in_use_gb=resident, budget_gb=budget)
            if self.clock() >= deadline:
                holders = ", ".join(f"{r.spec.name} ({'pinned' if r.pinned else 'busy' if r.pending or r.in_flight else r.state.value})"
                                    for r in self.rt.values() if r is not b and self._holds(r)) or "nothing"
                raise InsufficientMemory(f"cannot start {b.spec.name} ({need:g} GB): {resident:g} of {budget:g} GB budget is held "
                                         f"by {holders}, and none of it can be evicted right now")
            await asyncio.sleep(0.05)

    async def _pressure_loop(self) -> None:
        """If macOS reports memory pressure (low free percentage, or swap growing) while backends are idle, evict the LRU one."""
        g = self.catalog.gateway
        while True:
            await asyncio.sleep(g.pressure_interval_s)
            p = await self.system_status()
            if not p:
                continue
            self.last_system = p
            now = self.clock()
            used = p.get("swap_used_gb")
            if used is not None:
                self._swap_samples.append((now, used))
                while self._swap_samples and now - self._swap_samples[0][0] > g.pressure_window_s:
                    self._swap_samples.popleft()
            reason = None
            free = p.get("free_pct")
            if g.min_free_pct and free is not None and free < g.min_free_pct:
                reason = f"only {free}% memory free (limit {g.min_free_pct}%)"
            elif used is not None and len(self._swap_samples) >= 2:
                growth = used - self._swap_samples[0][1]
                if growth >= g.swap_growth_gb:
                    reason = f"swap grew {growth:.1f} GB in {now - self._swap_samples[0][0]:.0f}s"
            if reason and now - self._last_pressure_evict >= 2 * g.pressure_interval_s:     # cooldown: let memory settle
                victims = self._evictable(None)
                if victims:
                    self._begin_terminate(victims[0], f"pressure: {reason}")
                    self._last_pressure_evict = now
                    self._swap_samples.clear()
                    self.emit("pressure", victims[0].spec.name, reason=reason)

    # ---- idle reaper ----------------------------------------------------------------------------------------------
    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(self.reap_interval)
            now = self.clock()
            for b in self.rt.values():
                if (b.state is State.READY and not b.pinned and b.ttl_s > 0 and b.pending == 0 and b.in_flight == 0
                        and now - b.last_used >= b.ttl_s):
                    self._begin_terminate(b, f"idle for {now - b.last_used:.0f}s (ttl {b.ttl_s:.0f}s)")

    # ---- orphan reaping -------------------------------------------------------------------------------------------
    def _pidfile(self, b: Runtime) -> Path:
        return self.state_dir / "pids" / f"{b.spec.name}.json"

    def _write_pidfile(self, b: Runtime, cmd: list[str]) -> None:
        self._pidfile(b).write_text(json.dumps({"pid": b.pgid, "port": b.spec.port, "cmd": cmd, "started": time.time()}))

    def _remove_pidfile(self, b: Runtime) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._pidfile(b).unlink()

    def reap_orphans(self) -> list[str]:
        """Kill backends left running by a previous supervisor (crash / kill -9). Verifies the pid still runs OUR command."""
        reaped = []
        for pf in (self.state_dir / "pids").glob("*.json"):
            try:
                info = json.loads(pf.read_text())
                pid = int(info["pid"])
                out = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout.strip()
                # Identity check: match the distinctive ARGUMENTS, not argv[0] (Homebrew's python re-execs with another path).
                needle = " ".join(info["cmd"][1:]) or info["cmd"][0]
                if out and needle in out:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(pid, signal.SIGKILL)
                    reaped.append(pf.stem)
                    self.emit("reaped", pf.stem, pid=pid)
            except (ValueError, KeyError, OSError):
                pass
            with contextlib.suppress(FileNotFoundError):
                pf.unlink()
        return reaped
