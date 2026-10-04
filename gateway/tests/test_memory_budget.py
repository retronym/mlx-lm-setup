import asyncio, tempfile, time, unittest
from pathlib import Path

from gateway.supervisor import InsufficientMemory, State, Supervisor
from gateway.tests.test_supervisor import catalog, gone, until


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sups = []

    async def asyncTearDown(self):
        for s in self.sups:
            await s.shutdown()
        self.tmp.cleanup()

    def make(self, backends, budget=10, room=0.6, probe=None, grace=0.5, **gw):
        cat = catalog(backends, memory_budget_gb=budget, room_timeout_s=room, **gw)
        sup = Supervisor(cat, state_dir=Path(self.tmp.name) / "state", reap_interval=0.05, health_interval=0.05, grace_s=grace, memory_probe=probe)
        self.sups.append(sup)
        return sup

    def evictions(self, sup):
        return [e for e in sup.events if e["kind"] == "passivating" and e["reason"].startswith("evicted")]

    async def touch(self, sup, name, sleep=0.0):
        async with sup.lease(name) as url:
            if sleep:
                import httpx
                async with httpx.AsyncClient(trust_env=False) as c:
                    await c.post(url + "/work", json={"sleep": sleep})

    # ------------------------------------------------------------------------------------------------------------
    async def test_fits_without_eviction(self):
        sup = self.make({"a": {"est_mem_gb": 4}, "b": {"est_mem_gb": 4}})
        await asyncio.gather(sup.start("a"), sup.start("b"))
        self.assertEqual((sup.rt["a"].state, sup.rt["b"].state), (State.READY, State.READY))
        self.assertEqual(sup.memory_status(), {"budget_gb": 10, "resident_gb": 8.0, "free_gb": 2.0})
        self.assertEqual(self.evictions(sup), [])

    async def test_evicts_the_lru_idle_backend_to_make_room_and_swaps_back(self):
        sup = self.make({"a": {"est_mem_gb": 6}, "b": {"est_mem_gb": 6}})
        await sup.start("a")
        await self.touch(sup, "b")                                                   # lazily starts b: a must go
        self.assertEqual((sup.rt["a"].state, sup.rt["b"].state), (State.STOPPED, State.READY))
        self.assertIn("making room for b", self.evictions(sup)[0]["reason"])
        self.assertEqual(sup.resident_gb(), 6.0)
        await self.touch(sup, "a")                                                   # and back again: b is now the LRU
        self.assertEqual((sup.rt["a"].state, sup.rt["b"].state), (State.READY, State.STOPPED))
        self.assertEqual(len(self.evictions(sup)), 2)

    async def test_eviction_order_is_least_recently_used(self):
        sup = self.make({"a": {"est_mem_gb": 4}, "b": {"est_mem_gb": 4}, "c": {"est_mem_gb": 4}})
        await sup.start("a")
        await sup.start("b")
        await asyncio.sleep(0.05)
        await self.touch(sup, "a")                                                   # a is now more recently used than b
        await sup.start("c")
        self.assertEqual({n: r.state for n, r in sup.rt.items()}, {"a": State.READY, "b": State.STOPPED, "c": State.READY})

    async def test_pinned_backend_is_never_evicted_and_rejection_is_not_a_failure(self):
        sup = self.make({"p": {"est_mem_gb": 6, "pinned": True}, "x": {"est_mem_gb": 6}}, room=0.4)
        await sup.start("p")
        with self.assertRaises(InsufficientMemory) as cm:
            await sup.ensure_running("x")
        self.assertIn("p (pinned)", str(cm.exception))
        self.assertEqual(sup.rt["p"].state, State.READY)
        self.assertEqual((sup.rt["x"].state, sup.rt["x"].fail_count), (State.STOPPED, 0))      # no FAILED state, no backoff
        with self.assertRaises(InsufficientMemory):                                  # retried immediately, not "backoff"
            await sup.ensure_running("x")
        self.assertIn("rejected", [e["kind"] for e in sup.events])

    async def test_busy_backend_is_not_evicted_but_waited_for(self):
        sup = self.make({"a": {"est_mem_gb": 6}, "b": {"est_mem_gb": 6}}, room=5)
        await sup.start("a")
        busy = asyncio.create_task(self.touch(sup, "a", sleep=0.6))
        await asyncio.sleep(0.15)
        t0 = time.monotonic()
        starter = asyncio.create_task(sup.start("b"))
        await asyncio.sleep(0.3)
        self.assertEqual(sup.rt["a"].state, State.READY)                             # still serving: not evicted
        self.assertFalse(starter.done())
        await starter
        await busy
        self.assertGreater(time.monotonic() - t0, 0.25)                              # b waited for a to become idle
        self.assertEqual((sup.rt["a"].state, sup.rt["b"].state), (State.STOPPED, State.READY))
        self.assertIn("waiting_for_memory", [e["kind"] for e in sup.events])

    async def test_busy_beyond_the_timeout_rejects_and_leaves_the_busy_backend_alone(self):
        sup = self.make({"a": {"est_mem_gb": 6}, "b": {"est_mem_gb": 6}}, room=0.3)
        await sup.start("a")
        busy = asyncio.create_task(self.touch(sup, "a", sleep=1.2))
        await asyncio.sleep(0.1)
        with self.assertRaises(InsufficientMemory) as cm:
            await sup.start("b")
        self.assertIn("a (busy)", str(cm.exception))
        await busy
        self.assertEqual(sup.rt["a"].state, State.READY)

    async def test_concurrent_starts_never_overcommit_the_budget(self):
        sup = self.make({"a": {"est_mem_gb": 4}, "b": {"est_mem_gb": 4}, "c": {"est_mem_gb": 4}})
        await sup.start("a")
        peak = [0.0]

        async def sample():
            while True:
                peak[0] = max(peak[0], sup.resident_gb())
                await asyncio.sleep(0.005)
        sampler = asyncio.create_task(sample())
        await asyncio.gather(sup.start("b"), sup.start("c"))                         # only room for one extra: a must be evicted
        sampler.cancel()
        self.assertLessEqual(peak[0], 10.0)
        self.assertEqual({n: r.state for n, r in sup.rt.items()}, {"a": State.STOPPED, "b": State.READY, "c": State.READY})

    async def test_a_passivating_backend_still_holds_its_budget_until_it_exits(self):
        sup = self.make({"a": {"est_mem_gb": 6, "flags": ["--ignore-term"]}, "b": {"est_mem_gb": 6}}, grace=0.5, room=5)
        await sup.start("a")
        await sup.start("b")                                                         # evicts a, which ignores SIGTERM for 0.5 s
        kinds = [(e["kind"], e["backend"]) for e in sup.events]
        self.assertLess(kinds.index(("stopped", "a")), kinds.index(("ready", "b")))  # b only started after a really exited

    async def test_stop_releases_budget(self):
        sup = self.make({"a": {"est_mem_gb": 6}, "b": {"est_mem_gb": 6}})
        await sup.start("a")
        await sup.stop("a")
        self.assertEqual(sup.resident_gb(), 0.0)
        await sup.start("b")
        self.assertEqual(self.evictions(sup), [])                                    # fit without evicting anything

    # ---- pressure monitor -------------------------------------------------------------------------------------------
    PRESSURE = dict(pressure_eviction=True, pressure_interval_s=0.05, pressure_window_s=5, swap_growth_gb=1.0, min_free_pct=12)

    async def test_low_free_memory_evicts_lru_idle_but_never_a_busy_backend(self):
        free = [90]                                                                  # healthy until the scenario is set up
        sup = self.make({"a": {"est_mem_gb": 1}, "b": {"est_mem_gb": 1}}, probe=lambda: {"free_pct": free[0], "swap_used_gb": 0.0, "swap_total_gb": 8.0}, **self.PRESSURE)
        await sup.start("a")
        await sup.start("b")
        busy = asyncio.create_task(self.touch(sup, "a", sleep=1.0))                  # a is busy, so b (idle) must be the victim
        await asyncio.sleep(0.1)
        free[0] = 5                                                                  # now the system reports pressure
        self.assertTrue(await until(lambda: sup.rt["b"].state is State.STOPPED, 3))
        self.assertEqual(sup.rt["a"].state, State.READY)
        ev = [e for e in sup.events if e["kind"] == "passivating" and e["backend"] == "b"][0]
        self.assertIn("pressure: only 5% memory free", ev["reason"])
        await busy

    async def test_swap_growth_triggers_eviction(self):
        readings = iter([1.0, 1.0, 1.0] + [3.0] * 1000)                              # swap jumps by 2 GB
        sup = self.make({"a": {"est_mem_gb": 1}}, probe=lambda: {"free_pct": 80, "swap_used_gb": next(readings), "swap_total_gb": 8.0}, **self.PRESSURE)
        await sup.start("a")
        self.assertTrue(await until(lambda: sup.rt["a"].state is State.STOPPED, 3))
        self.assertIn("swap grew", [e for e in sup.events if e["kind"] == "pressure"][0]["reason"])

    async def test_pressure_eviction_can_be_disabled(self):
        sup = self.make({"a": {"est_mem_gb": 1}}, probe=lambda: {"free_pct": 1, "swap_used_gb": 0, "swap_total_gb": 8}, **{**self.PRESSURE, "pressure_eviction": False})
        await sup.start("a")
        await asyncio.sleep(0.4)
        self.assertEqual(sup.rt["a"].state, State.READY)

    async def test_healthy_system_causes_no_eviction(self):
        sup = self.make({"a": {"est_mem_gb": 1}}, probe=lambda: {"free_pct": 60, "swap_used_gb": 2.0, "swap_total_gb": 8.0}, **self.PRESSURE)
        await sup.start("a")
        await asyncio.sleep(0.4)
        self.assertEqual(sup.rt["a"].state, State.READY)
        self.assertEqual(sup.last_system["free_pct"], 60)

    async def test_snapshot_reports_holds_memory(self):
        sup = self.make({"a": {"est_mem_gb": 3}})
        self.assertFalse(sup.snapshot()[0]["holds_memory"])
        await sup.start("a")
        self.assertTrue(sup.snapshot()[0]["holds_memory"])


if __name__ == "__main__":
    unittest.main()
