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


if __name__ == "__main__":
    unittest.main()
