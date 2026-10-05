#!/usr/bin/env python3
"""Tree-sitter chunkers (needs tree-sitter, tree-sitter-scala and tree-sitter-java: run with the .venv-jev python): declarations are whole, local
definitions stay in their method, big classes become a header plus members, big methods are cut between statements with the signature repeated,
small siblings are packed, Scala 3 braceless syntax and Java work, and a file with syntax errors falls back to the heuristic chunker."""
import os, subprocess, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(__file__))
from sources import treesitter as ts
from sources.gitsrc import CHUNKERS, GitSource
from store import Store
import config

SCALA2 = '''/* Copyright (C) Lightbend. License: Apache. A license header is not a chunk of its own, however long it is. 0123456789 0123456789 0123456789 0123456789 0123456789 0123456789 0123456789 0123456789 0123456789 0123456789 */
package demo.pkg

import scala.collection.mutable
import scala.util.Try

/** Documents Foo. */
@deprecated("old", "1.0")
class Foo(a: Int) {
  /** Adds. */
  def add(x: Int): Int = {
    def helper(y: Int) = y + a      // a local definition: stays inside add
    val total = helper(x)
    total
  }
  val one = 1
  val two = 2
  type T = Int
}

object Foo { def apply() = new Foo(1) }
'''

SCALA3 = '''package three

enum Color:
  case Red, Green
  def next = this

trait Show[A]:
  def show(a: A): String

given Show[Int] with
  def show(a: Int) = a.toString

extension (s: String)
  def shout = s.toUpperCase
  def whisper = s.toLowerCase

object O:
  val q = 3
  inline def zed(x: Int) = x
'''

JAVA = '''package demo.pkg;

import java.util.List;

/** The outer class. */
public class Outer {
    private static final String BRACES = "{ not a block }";
    static { System.out.println("static init"); }

    /** Docs for the constructor. */
    public Outer(int x) { this.counter = x; }

    public <U> List<U> convert(List<? extends T> in) {
        Runnable r = new Runnable() { public void run() { if (in.isEmpty()) { return; } } };
        return null;
    }

    enum Palette { RED, GREEN; String label() { return name().toLowerCase(); } }
    interface Contract { int once(int v); }
}
'''


def big_method(n=60):
    stmts = "\n".join(f"    val v{i} = compute({i}) + helperFunction({i}) // statement number {i} of the long method" for i in range(n))
    return f"object Big {{\n  /** A long method. */\n  def run(x: Int): Int = {{\n{stmts}\n    x\n  }}\n}}\n"


def big_class(n=80):
    members = "\n".join(f"  def member{i}(x: Int): Int = x + {i} // padding padding padding padding padding padding padding padding" for i in range(n))
    return f"class Huge {{\n{members}\n}}\n"


def titles(chunks):
    return [c[1].split("  ")[-1] for c in chunks]


class ScalaTests(unittest.TestCase):
    def test_declarations_are_whole_and_locals_stay_inside(self):
        out = ts.chunk("scala", "A.scala", SCALA2)
        names = titles(out)
        self.assertNotIn("Foo.helper", names)                                  # the local def is not a member
        foo = next(c for c in out if c[0] == "Foo#1")
        self.assertIn("def helper", foo[2]); self.assertIn("val total", foo[2]); self.assertIn("/** Documents Foo. */", foo[2])
        self.assertIn("@deprecated", foo[2]); self.assertIn("type T = Int", foo[2])          # the whole small class: doc, annotation, all members
        self.assertEqual((foo[3], foo[4]["end"], foo[4]["sym"]), (7, 19, "class"))
        self.assertTrue(foo[1].startswith("A.scala  demo.pkg  Foo"))
        self.assertFalse(any("Lightbend" in c[2] for c in out))                  # the license header is dropped
        self.assertEqual(len({c[0] for c in out}), len(out))                      # ids are unique (Foo#1 the class, Foo#2 its companion)

    def test_imports_are_packed_and_named_for_what_follows(self):
        out = ts.chunk("scala", "A.scala", SCALA2)
        self.assertEqual(out[0][4]["sym"], "imports")
        self.assertIn("scala.util.Try", out[0][2])

    def test_scala3_braceless_syntax(self):
        out = ts.chunk("scala", "T.scala", SCALA3)
        body = "\n".join(c[2] for c in out)
        for needle in ("enum Color:", "trait Show[A]:", "given Show[Int] with", "extension (s: String)", "inline def zed"):
            self.assertIn(needle, body)

    def test_big_class_is_a_header_with_an_outline_plus_members(self):
        ts.configure(2400, 1000)
        out = ts.chunk("scala", "H.scala", big_class())
        head = out[0]
        self.assertEqual((head[0], head[4]["sym"]), ("Huge#1", "class"))
        self.assertIn("// members: member0, member1", head[2])
        self.assertTrue(all(c[1].split("  ")[-1].startswith("Huge.member") for c in out[1:]))   # members carry the enclosing class in their title
        self.assertTrue(all(len(c[2]) <= ts.MAX * ts.FIT * 1.5 for c in out))
        self.assertGreater(len(out), 2)

    def test_big_method_is_cut_between_statements_with_its_signature(self):
        ts.configure(2400, 1000)
        out = ts.chunk("scala", "B.scala", big_method())
        parts = [c for c in out if c[0].startswith("Big.run#")]
        self.assertGreater(len(parts), 1)
        self.assertIn("A long method", parts[0][2])
        for p in parts[1:]:
            self.assertTrue(p[2].startswith("def run(x: Int): Int ="), p[2][:60])             # continuation: the signature, then where it resumes
            self.assertIn("// ... continued", p[2])
        for p in parts:
            for ln in p[2].splitlines():
                if "statement number" in ln:
                    self.assertTrue(ln.strip().startswith("val v"), ln)                        # never cut inside a statement
        joined = "\n".join(p[2] for p in parts)
        self.assertTrue(all(f"val v{i} =" in joined for i in range(60)))                      # nothing lost

    def test_small_siblings_are_packed(self):
        src = "object S {\n" + "\n".join(f"  val x{i} = {i}" for i in range(12)) + "\n}\n" + "object Pad {\n" + "\n".join(f"  def m{i} = {i} // pad pad pad pad pad pad pad pad pad pad pad pad pad pad" for i in range(40)) + "\n}\n"
        out = ts.chunk("scala", "S.scala", src)
        packed = [c for c in out if c[4]["sym"] == "members"]
        self.assertTrue(packed)
        self.assertTrue(all(len(c[2]) <= ts.PACK + 100 for c in packed))
        self.assertTrue(any("(+" in c[1] for c in packed))

    def test_syntax_errors_fall_back_to_the_heuristic_chunker(self):
        bad = "package x\nobject A { def f( = }\n class {{{\n"
        self.assertIsNone(ts.chunk("scala", "bad.scala", bad))
        out = CHUNKERS["scala_ts"]("bad.scala", "package x\nclass Ok {\n  def fine = 1 // padding padding padding\n}\nobject A { def f( = }\n class {{{\n")
        self.assertTrue(out and all(c[4] == {"fallback": "heuristic"} for c in out))

    def test_editing_one_method_changes_one_chunk(self):
        ts.configure(2400, 1000)
        a = {c[0]: c[2] for c in ts.chunk("scala", "H.scala", big_class())}
        b = {c[0]: c[2] for c in ts.chunk("scala", "H.scala", big_class().replace("x + 7 ", "x + 70 "))}
        self.assertEqual(a.keys(), b.keys())
        self.assertEqual(sum(a[k] != b[k] for k in a), 1)


class JavaTests(unittest.TestCase):
    def test_java(self):
        out = ts.chunk("java", "Outer.java", JAVA)
        outer = next(c for c in out if c[0] == "Outer#1")
        self.assertIn("The outer class", outer[2]); self.assertIn("enum Palette", outer[2]); self.assertIn("new Runnable()", outer[2])
        self.assertTrue(outer[1].startswith("Outer.java  demo.pkg  Outer"))
        self.assertEqual(outer[4]["sym"], "class")

    def test_big_java_class_splits_into_members_with_nested_types(self):
        members = "\n".join(f"    /** Doc {i}. */\n    public int member{i}(int x) {{ return x + {i}; /* padding padding padding padding padding padding padding */ }}" for i in range(50))
        out = ts.chunk("java", "Huge.java", f"package p;\npublic class Huge {{\n{members}\n    static class Nested {{ void deep() {{}} }}\n}}\n")
        self.assertEqual(out[0][0], "Huge#1")
        self.assertIn("member0", out[0][2])
        self.assertTrue(any(c[1].endswith("Huge.Nested") or "Huge.Nested" in c[1] for c in out))
        self.assertTrue(any("Huge.member" in c[1] for c in out))


class SourceSwitchTests(unittest.TestCase):
    def test_switching_chunker_rechunks_once_and_replaces_the_chunks(self):
        def run(repo, *a): subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True)
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "r"); os.mkdir(repo)
            run(repo, "init", "-q"); run(repo, "config", "user.email", "t@t"); run(repo, "config", "user.name", "t")
            with open(os.path.join(repo, "A.scala"), "w") as f:
                f.write(SCALA2)
            run(repo, "add", "."); run(repo, "commit", "-qm", "1")
            st = Store(os.path.join(d, "t.db"))
            def src(chunker):
                cs = config.Source(project="t", id="t", type="git", label="t", color="#000000", priority=5, enabled=True, min_interval_hours=0, max_items_per_run=None,
                                   repo="o/r", ref="HEAD", paths=(".",), chunkers={".scala": chunker})
                return GitSource(cs, repo)
            def sync(s):
                out = []; s.sync(st, log=out.append); return out[-1].split("->")[1].strip()
            sync(src("scala")); old = st.db.execute("SELECT count(*) FROM chunks").fetchone()[0]
            self.assertTrue(sync(src("scala")).startswith("+0 ~0 -0"))                       # unchanged chunker: nothing re-chunked
            r = sync(src("scala_ts"))
            new = st.db.execute("SELECT count(*) FROM chunks").fetchone()[0]
            self.assertLess(new, old)                                                          # fewer, larger chunks
            self.assertEqual(st.db.execute("SELECT count(*) FROM chunks").fetchone()[0], st.db.execute("SELECT count(*) FROM fts").fetchone()[0])
            self.assertTrue(sync(src("scala_ts")).startswith("+0 ~0 -0"), r)                  # and the new state sticks
            m = st.db.execute("SELECT json_extract(meta, '$.sym'), json_extract(meta, '$.end') FROM chunks WHERE id LIKE '%Foo#1'").fetchone()
            self.assertEqual(m, ("class", 19))                                                 # the metadata the get tool can use


if __name__ == "__main__":
    unittest.main()
