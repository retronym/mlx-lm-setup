import os, stat, tempfile, unittest
from pathlib import Path

from gateway import auth


class AuthTests(unittest.TestCase):
    def test_token_is_created_once_private_and_stable(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state" / "token"
            t1 = auth.load_or_create(p)
            self.assertGreaterEqual(len(t1), 32)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)                  # owner-only
            self.assertEqual(auth.load_or_create(p), t1)                                # stable across restarts

    def test_bearer_parsing_and_validation(self):
        self.assertEqual(auth.bearer("Bearer abc"), "abc")
        self.assertEqual(auth.bearer("bearer  abc "), "abc")                            # scheme is case-insensitive
        for bad in (None, "", "Basic abc", "Bearer", "Bearer   ", "abc"):
            self.assertIsNone(auth.bearer(bad))
        self.assertTrue(auth.valid("s3cret", "Bearer s3cret"))
        self.assertFalse(auth.valid("s3cret", "Bearer s3cre"))
        self.assertFalse(auth.valid("s3cret", "Bearer s3cret2"))
        self.assertFalse(auth.valid("s3cret", None))


if __name__ == "__main__":
    unittest.main()
