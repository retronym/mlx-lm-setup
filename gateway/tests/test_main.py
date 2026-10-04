"""End-to-end: run the real `python -m gateway` as a subprocess (fake backends), then SIGTERM it."""
import json, os, signal, socket, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
FAKE = Path(__file__).with_name("fake_backend.py")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class MainTests(unittest.TestCase):
    def test_sigterm_stops_gateway_and_every_backend(self):
        with tempfile.TemporaryDirectory() as td:
            gw, base = free_port(), free_port()
            cat = Path(td) / "gateway.toml"
            cat.write_text(f"""[gateway]
port = {gw}
backend_port_base = {base}
memory_budget_gb = 10
state_dir = "{td}/state"

[backends.llm]
adapter = "command"
command = ["{sys.executable}", "{FAKE}", "--port", "{{port}}", "--spawn-child"]
kind = "llm"
est_mem_gb = 1
""")
            p = subprocess.Popen([sys.executable, "-m", "gateway", "--catalog", str(cat), "--log-level", "warning"], cwd=ROOT,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                url = f"http://127.0.0.1:{gw}"
                for _ in range(100):
                    try:
                        urlopen(url + "/healthz", timeout=1).read()
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    self.fail("gateway did not come up")
                req = Request(url + "/v1/chat/completions", data=json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode(),
                              headers={"Content-Type": "application/json"})
                self.assertEqual(urlopen(req, timeout=30).status, 200)                     # lazily starts the backend
                snap = json.loads(urlopen(url + "/api/backends").read())["backends"][0]
                bpid = snap["pid"]
                child = json.loads(urlopen(f"http://127.0.0.1:{snap['port']}/info").read())["child"]   # grandchild too
                self.assertTrue(alive(bpid) and alive(child))
                self.assertEqual(len(list((Path(td) / "state" / "pids").glob("*.json"))), 1)

                p.send_signal(signal.SIGTERM)
                rc = p.wait(timeout=20)
                self.assertEqual(rc, 0, p.stdout.read())                                    # exited normally, not killed by the signal
                for _ in range(50):
                    if not alive(bpid) and not alive(child):
                        break
                    time.sleep(0.1)
                self.assertFalse(alive(bpid), "backend left running after gateway SIGTERM")
                self.assertFalse(alive(child), "backend's child left running")
                self.assertEqual(list((Path(td) / "state" / "pids").glob("*.json")), [])   # pidfiles cleaned up
            finally:
                if p.poll() is None:
                    p.kill()
                    p.wait()
                p.stdout.close()
                subprocess.run(["pkill", "-f", f"--port {base}"], capture_output=True)

    def start(self, td, gw, base, extra=""):
        cat = Path(td) / "gateway.toml"
        cat.write_text(f"""[gateway]
port = {gw}
backend_port_base = {base}
memory_budget_gb = 10
state_dir = "{td}/state"
{extra}
[backends.llm]
adapter = "command"
command = ["{sys.executable}", "{FAKE}", "--port", "{{port}}"]
kind = "llm"
est_mem_gb = 1
""")
        p = subprocess.Popen([sys.executable, "-m", "gateway", "--catalog", str(cat), "--log-level", "warning"], cwd=ROOT,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return p, f"http://127.0.0.1:{gw}"

    def wait_up(self, url, p):
        for _ in range(100):
            if p.poll() is not None:
                return False
            try:
                urlopen(url + "/healthz", timeout=1).read()
                return True
            except OSError:
                time.sleep(0.1)
        return False

    def test_sigterm_still_exits_promptly_with_a_long_lived_sse_connection_open(self):
        """Regression: an open SSE stream (admin page) or MCP stream made uvicorn wait forever for connections to close."""
        with tempfile.TemporaryDirectory() as td:
            gw, base = free_port(), free_port()
            p, url = self.start(td, gw, base)
            try:
                self.assertTrue(self.wait_up(url, p))
                stream = urlopen(url + "/api/events", timeout=10)
                self.assertIn(b"event: snapshot", stream.readline())                       # connected and streaming
                t0 = time.time()
                p.send_signal(signal.SIGTERM)
                rc = p.wait(timeout=20)
                err = p.stderr.read()
                self.assertEqual(rc, 0, err)
                self.assertLess(time.time() - t0, 4.5)                                     # SSE ends itself; the 5 s timeout is only a backstop
                self.assertNotIn("Traceback", err)                                         # and no noisy cancellation traces
                stream.close()
            finally:
                if p.poll() is None:
                    p.kill()
                    p.wait()
                p.stdout.close(); p.stderr.close()

    def test_a_second_gateway_for_the_same_state_dir_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as td:
            gw, base = free_port(), free_port()
            p1, url = self.start(td, gw, base)
            try:
                self.assertTrue(self.wait_up(url, p1))
                p2, _ = self.start(td, free_port(), free_port())
                try:
                    rc = p2.wait(timeout=15)
                    err = p2.stderr.read()
                finally:
                    if p2.poll() is None:
                        p2.kill(); p2.wait()
                    p2.stdout.close(); p2.stderr.close()
                self.assertEqual(rc, 2)
                self.assertIn("another gateway is already running", err)
                self.assertIn(str(p1.pid), err)                                            # names the holder
                self.assertEqual(urlopen(url + "/healthz", timeout=2).status, 200)         # the first one is unaffected
                p1.send_signal(signal.SIGTERM)
                self.assertEqual(p1.wait(timeout=20), 0)
                p3, url3 = self.start(td, gw, base)                                        # lock released on exit: starts fine
                try:
                    self.assertTrue(self.wait_up(url3, p3))
                finally:
                    p3.send_signal(signal.SIGTERM)
                    p3.wait(timeout=20)
                    p3.stdout.close(); p3.stderr.close()
            finally:
                if p1.poll() is None:
                    p1.kill(); p1.wait()
                p1.stdout.close(); p1.stderr.close()


if __name__ == "__main__":
    unittest.main()
