import asyncio, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path

import httpx

from gateway.catalog import parse
from gateway.supervisor import BackendUnavailable, StartFailed, State, Supervisor, UnknownBackend

FAKE = str(Path(__file__).with_name("fake_backend.py"))
_next_port = [19200]


def catalog(backends: dict, **gateway):
    base = _next_port[0]
    _next_port[0] += 10
    d = {"gateway": {"backend_port_base": base, "memory_budget_gb": 100, "pressure_eviction": False, **gateway}, "backends": {}}
    for name, opts in backends.items():
        opts = dict(opts)
        flags = opts.pop("flags", [])
        d["backends"][name] = {"adapter": "command", "command": [sys.executable, FAKE, "--port", "{port}", *flags], "est_mem_gb": 1, **opts}
    return parse(d, Path(tempfile.gettempdir()))


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def gone(pid: int, timeout: float = 3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not alive(pid):
            return True
        await asyncio.sleep(0.05)
    return not alive(pid)


async def until(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


def listening(port: int) -> bool:
    return subprocess.run(["pgrep", "-f", f"--port {port}"], capture_output=True).returncode == 0


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sups = []
        self.http = httpx.AsyncClient(trust_env=False)

    async def asyncTearDown(self):
        for s in self.sups:
            await s.shutdown()
        await self.http.aclose()
        self.tmp.cleanup()

    def make(self, backends, state_dir=None, **kw):
        kw = {"reap_interval": 0.05, "health_interval": 0.05, "grace_s": 0.5, **kw}
        sup = Supervisor(catalog(backends), state_dir=state_dir or Path(self.tmp.name) / "state", **kw)
        self.sups.append(sup)
        return sup

    async def info(self, sup, name):
        async with sup.lease(name) as url:
            return (await self.http.get(url + "/info")).json()

    # ------------------------------------------------------------------------------------------------------------
    async def test_requests_are_recorded_for_the_timeline(self):
        sup = self.make({"a": {"flags": ["--ready-delay", "0.2"]}})
        async with sup.lease("a", "/v1/chat/completions"):
            snap = sup.requests_snapshot()
            [act] = snap["active"]
            self.assertEqual((act["backend"], act["label"], act["status"], act["cold"]), ("a", "/v1/chat/completions", "running", True))
            self.assertGreaterEqual(act["t_run"] - act["t0"], 0.15)             # the cold start is the wait before running
        with self.assertRaises(RuntimeError):
            async with sup.lease("a", "/x"):
                raise RuntimeError("boom")

        async def hang():
            async with sup.lease("a", "/slow"):
                await asyncio.sleep(10)
        t = asyncio.create_task(hang())
        await asyncio.sleep(0.1)
        t.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await t
        snap = sup.requests_snapshot()
        self.assertEqual(snap["active"], [])
        self.assertEqual([(r["label"], r["status"], r["cold"]) for r in snap["recent"]],
                         [("/v1/chat/completions", "ok", True), ("/x", "error", False), ("/slow", "cancelled", False)])
        self.assertIn("boom", snap["recent"][1]["error"])
        self.assertEqual([e["kind"] for e in snap["events"]], ["starting", "ready"])
        self.assertEqual(sup.requests_snapshot(window_s=0.0001)["recent"], [])

    async def test_start_stop_and_events(self):
        sup = self.make({"a": {}})
        q = sup.subscribe()
        b = await sup.start("a")
        self.assertEqual(b.state, State.READY)
        pid = b.proc.pid
        self.assertEqual((await self.http.get(b.base_url + "/info")).json()["pid"], pid)
        await sup.stop("a")
        self.assertEqual(b.state, State.STOPPED)
        self.assertTrue(await gone(pid))
        await sup.stop("a")                                    # idempotent
        kinds = [e["kind"] for e in sup.events]
        self.assertEqual(kinds, ["starting", "ready", "passivating", "stopped"])
        self.assertEqual(q.qsize(), 4)

    async def test_single_flight_start(self):
        sup = self.make({"a": {"flags": ["--ready-delay", "0.4"]}})
        bs = await asyncio.gather(*[sup.ensure_running("a") for _ in range(8)])
        self.assertEqual({id(b) for b in bs}, {id(bs[0])})
        self.assertEqual(sup.rt["a"].starts, 1)
        self.assertEqual([e["kind"] for e in sup.events].count("starting"), 1)

    async def test_requests_are_serialized_at_concurrency_1_and_overlap_at_2(self):
        async def work(sup, name):
            async with sup.lease(name) as url:
                return (await self.http.post(url + "/work", json={"sleep": 0.25})).json()
        s1 = self.make({"a": {}})
        r = sorted(await asyncio.gather(work(s1, "a"), work(s1, "a")), key=lambda x: x["t0"])
        self.assertGreaterEqual(r[1]["t0"], r[0]["t1"] - 0.02)                       # no overlap
        s2 = self.make({"a": {"concurrency": 2}})
        r = sorted(await asyncio.gather(work(s2, "a"), work(s2, "a")), key=lambda x: x["t0"])
        self.assertLess(r[1]["t0"], r[0]["t1"])                                      # overlap
        self.assertEqual(s1.rt["a"].requests, 2)

    async def test_idle_passivation_then_transparent_depassivation(self):
        sup = self.make({"a": {}})
        await sup.start("a")
        pid1 = (await self.info(sup, "a"))["pid"]
        sup.set_policy("a", ttl_s=0.3)
        self.assertTrue(await until(lambda: sup.rt["a"].state is State.STOPPED, 3))
        self.assertTrue(await gone(pid1))
        self.assertEqual(sup.rt["a"].passivations, 1)
        self.assertIn("idle", [e for e in sup.events if e["kind"] == "passivating"][0]["reason"])
        pid2 = (await self.info(sup, "a"))["pid"]                                   # lease starts it again
        self.assertNotEqual(pid1, pid2)
        self.assertEqual(sup.rt["a"].starts, 2)

    async def test_never_passivated_while_leased_or_queued(self):
        sup = self.make({"a": {}})
        await sup.start("a")
        sup.set_policy("a", ttl_s=0.2)
        async def hold(sleep):
            async with sup.lease("a") as url:
                await self.http.post(url + "/work", json={"sleep": sleep})
        await asyncio.gather(hold(0.5), hold(0.3))                                   # second one queues behind the first
        self.assertEqual(sup.rt["a"].state, State.READY)
        self.assertTrue(await until(lambda: sup.rt["a"].state is State.STOPPED, 3))  # but idles out afterwards

    async def test_pinned_and_zero_ttl_are_never_passivated(self):
        sup = self.make({"p": {"pinned": True, "ttl_s": 1}, "z": {"ttl_s": 0}})
        await asyncio.gather(sup.start("p"), sup.start("z"))
        sup.set_policy("p", ttl_s=0.1)
        sup.rt["z"].ttl_s = 0
        await asyncio.sleep(0.6)
        self.assertEqual((sup.rt["p"].state, sup.rt["z"].state), (State.READY, State.READY))

    async def test_crash_after_ready_then_next_lease_restarts(self):
        sup = self.make({"a": {"flags": ["--crash-after", "0.3"]}})
        await sup.start("a")
        self.assertTrue(await until(lambda: sup.rt["a"].state is State.STOPPED, 3))
        self.assertIn("crashed", sup.rt["a"].last_error)
        self.assertIn("crashed", [e["kind"] for e in sup.events])
        info = await self.info(sup, "a")                                              # restarts transparently
        self.assertEqual(sup.rt["a"].starts, 2)
        self.assertTrue(info["pid"])

    async def test_failed_start_reports_log_and_backs_off(self):
        sup = self.make({"a": {"flags": ["--die-on-start"]}})
        with self.assertRaises(StartFailed) as cm:
            await sup.ensure_running("a")
        self.assertIn("exited with code 3", str(cm.exception))
        self.assertIn("dying on start", str(cm.exception))                           # log tail included
        self.assertEqual(sup.rt["a"].state, State.FAILED)
        with self.assertRaises(BackendUnavailable):                                   # lazy path respects backoff
            await sup.ensure_running("a")
        with self.assertRaises(StartFailed):                                          # explicit start retries
            await sup.start("a")
        self.assertEqual(sup.rt["a"].fail_count, 2)

    async def test_start_timeout_kills_the_process(self):
        sup = self.make({"a": {"flags": ["--ready-delay", "5"], "start_timeout_s": 0.5}})
        port = sup.rt["a"].spec.port
        with self.assertRaises(StartFailed) as cm:
            await sup.start("a")
        self.assertIn("not ready after", str(cm.exception))
        self.assertTrue(await until(lambda: not listening(port), 3))
        self.assertIsNone(sup.rt["a"].proc)

    async def test_sigterm_ignored_escalates_to_sigkill(self):
        sup = self.make({"a": {"flags": ["--ignore-term"]}}, grace_s=0.3)
        b = await sup.start("a")
        pid = b.proc.pid
        t0 = time.time()
        await sup.stop("a")
        self.assertGreaterEqual(time.time() - t0, 0.25)                               # waited the grace period
        self.assertTrue(await gone(pid))

    async def test_whole_process_group_is_killed(self):
        sup = self.make({"a": {"flags": ["--spawn-child"]}})
        await sup.start("a")
        info = (await self.http.get(sup.rt["a"].base_url + "/info")).json()
        self.assertTrue(alive(info["child"]))
        await sup.stop("a")
        self.assertTrue(await gone(info["pid"]))
        self.assertTrue(await gone(info["child"]))

    async def test_restarted_supervisor_reaps_orphans(self):
        sd = Path(self.tmp.name) / "shared"
        sup1 = self.make({"a": {}}, state_dir=sd)
        b = await sup1.start("a")
        pid = b.proc.pid
        sup2 = Supervisor(sup1.catalog, state_dir=sd)                                 # as if the gateway crashed and restarted
        self.sups.append(sup2)
        self.assertEqual(sup2.reap_orphans(), ["a"])
        self.assertTrue(await gone(pid))
        self.assertEqual(list((sd / "pids").glob("*.json")), [])

    async def test_reap_ignores_stale_pidfile_for_a_different_process(self):
        sd = Path(self.tmp.name) / "stale"
        sup = self.make({"a": {}}, state_dir=sd)
        victim = subprocess.Popen(["sleep", "30"])
        (sd / "pids" / "a.json").write_text(f'{{"pid": {victim.pid}, "port": 1, "cmd": ["{sys.executable}", "--port", "1"], "started": 0}}')
        try:
            self.assertEqual(sup.reap_orphans(), [])                                  # pid reused by an unrelated process: left alone
            self.assertTrue(alive(victim.pid))
        finally:
            victim.kill(); victim.wait()

    async def test_errors_policy_and_snapshot(self):
        sup = self.make({"a": {"ttl_s": 100}})
        with self.assertRaises(UnknownBackend):
            await sup.ensure_running("nope")
        with self.assertRaises(ValueError):
            sup.set_policy("a", ttl_s=-1)
        s0 = sup.snapshot()[0]
        self.assertEqual((s0["state"], s0["pid"], s0["ttl_left_s"]), ("stopped", None, None))
        await sup.start("a")
        s1 = sup.snapshot()[0]
        self.assertEqual((s1["state"], s1["ttl_s"], s1["starts"]), ("ready", 100, 1))
        self.assertGreater(s1["ttl_left_s"], 90)
        self.assertIsNotNone(s1["last_start_s"])

    async def test_shutdown_stops_everything(self):
        sup = self.make({"a": {}, "b": {}})
        pids = [b.proc.pid for b in await asyncio.gather(sup.start("a"), sup.start("b"))]
        await sup.shutdown()
        for pid in pids:
            self.assertTrue(await gone(pid))


if __name__ == "__main__":
    unittest.main()
