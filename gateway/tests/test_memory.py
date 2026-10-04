import os, sys, unittest
from unittest import mock
from gateway.memory import macos_probe, parse_swapusage


class MemoryTests(unittest.TestCase):
    def test_parse_swapusage(self):
        used, total = parse_swapusage("total = 7168.00M  used = 6376.75M  free = 791.25M  (encrypted)")
        self.assertAlmostEqual(total, 7.0)
        self.assertAlmostEqual(used, 6.2276, places=3)
        used, total = parse_swapusage("total = 2.00G  used = 512.00M  free = 1.50G")
        self.assertEqual((used, total), (0.5, 2.0))
        self.assertIsNone(parse_swapusage("garbage"))

    def test_live_probe_returns_plausible_values(self):             # runs on the Mac this project targets
        p = macos_probe()
        self.assertEqual(set(p), {"free_pct", "swap_used_gb", "swap_total_gb"})
        if p["free_pct"] is not None:
            self.assertTrue(0 <= p["free_pct"] <= 100)
        if p["swap_total_gb"] is not None:
            self.assertGreaterEqual(p["swap_total_gb"], p["swap_used_gb"] - 0.01)

    @unittest.skipUnless(sys.platform == "darwin", "macOS probe")
    def test_probe_works_with_the_launchd_path(self):              # the service's PATH has no /usr/sbin, where sysctl lives
        with mock.patch.dict(os.environ, {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}):
            p = macos_probe()
        self.assertIsNotNone(p["free_pct"])
        self.assertIsNotNone(p["swap_used_gb"])


if __name__ == "__main__":
    unittest.main()
