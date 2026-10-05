#!/usr/bin/env python3
"""Chunker tests: the Java chunker on tricky inputs (braces in strings and comments, nested and anonymous classes, enums, annotations with
arguments, initializer blocks, array initializers, text blocks, generics), and a sanity run over real Java and Scala files if checkouts exist."""
import os, subprocess, sys, unittest
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
from sources.gitsrc import chunk_java, chunk_scala

JAVA = '''package demo.pkg;

import java.util.List;

/** The outer class. */
@SuppressWarnings({"unchecked", "rawtypes"})
public class Outer<T extends Comparable<T>> implements Runnable {
    private static final String BRACES = "{ not a block } and a quote \\" still in the string";
    private final int[] table = {1, 2, 3};   // array initializer: braces at member level
    static { System.out.println("static init { with brace }"); }
    { counter = computeInitialValue(); }

    /** Docs for the constructor. */
    public Outer(int x) { this.counter = x; }

    // a comment with a brace { and another }
    public <U> List<U> convert(List<? extends T> in, java.util.function.Function<T, U> f) throws Exception {
        Runnable r = new Runnable() { public void run() { if (in.isEmpty()) { return; } } };
        return in.stream().map(v -> { return f.apply(v); }).collect(java.util.stream.Collectors.toList());
    }

    abstract int declaredOnly(String s);

    public String block() { return """
        text block with { braces }
        """; }

    interface InnerContract { default int twice(int v) { return v * 2; } int onceOnly(int value); }

    enum Palette { RED, GREEN { @Override String label() { return "g"; } }, BLUE; String label() { return name().toLowerCase(); } }

    public static class NestedHolder { void deep() { class Local { void m() {} } } }

    @Override public void run() { }
}
'''


class JavaChunkerTests(unittest.TestCase):
    def chunks(self):
        return {cid.rsplit("#", 1)[0]: (title, body, line) for cid, title, body, line in chunk_java("src/Outer.java", JAVA)}

    def test_members_types_and_scopes(self):
        c = self.chunks()
        for name in ("Outer", "Outer.BRACES", "Outer.table", "Outer.<static>", "Outer.<init>", "Outer.Outer", "Outer.convert", "Outer.declaredOnly",
                     "Outer.block", "Outer.InnerContract", "Outer.InnerContract.twice", "Outer.InnerContract.onceOnly", "Outer.Palette.RED", "Outer.Palette.label",
                     "Outer.NestedHolder", "Outer.NestedHolder.deep", "Outer.run"):
            self.assertIn(name, c, f"missing {name}; have {sorted(c)}")
        self.assertTrue(c["Outer.convert"][0].endswith("demo.pkg  Outer.convert"))
        self.assertEqual(c["Outer.<init>"][1].strip(), "{ counter = computeInitialValue(); }")
        self.assertTrue(c["Outer.convert"][0].startswith("src/Outer.java  "))

    def test_braces_in_strings_comments_and_lambdas_do_not_split(self):
        c = self.chunks()
        self.assertIn("collect(java.util.stream", c["Outer.convert"][1])                 # the whole body, lambdas and anonymous class included
        self.assertIn("new Runnable()", c["Outer.convert"][1])
        self.assertIn("text block with { braces }", c["Outer.block"][1])
        self.assertIn("{1, 2, 3};", c["Outer.table"][1])                                  # the array initializer stayed in its field
        self.assertFalse([n for n in c if n.startswith("Outer.Outer.")])                  # nothing leaked into a phantom scope

    def test_javadoc_and_annotations_stay_with_the_member(self):
        c = self.chunks()
        self.assertTrue(c["Outer.Outer"][1].lstrip().startswith("/** Docs for the constructor. */"))
        self.assertIn("The outer class.", c["Outer"][1])                                  # the type header carries its javadoc and annotation
        self.assertIn("@SuppressWarnings", c["Outer"][1])
        self.assertNotIn("convert", c["Outer"][1])                                        # ... but not its members

    def test_scope_pops_after_a_type_closes(self):
        names = [cid for cid, *_ in chunk_java("A.java", "package p;\nclass A { void a() { int x = 1; } }\nclass B { void b() { int y = 2; } }\n")]
        self.assertEqual(names, ["A.a#1", "B.b#1"])                                       # the one-line headers are too short to be chunks, the scopes still pop

    def test_lines_point_at_the_start_and_trailing_comments_stay_put(self):
        c = self.chunks()
        self.assertTrue(JAVA.splitlines()[c["Outer.convert"][2] - 1].lstrip().startswith("// a comment with a brace"))      # its leading comment is part of it
        self.assertNotIn("array initializer", c["Outer.<static>"][1])                       # a comment on the previous member's line is not

    def test_scala_chunker_still_handles_braceless_scala3(self):
        src = "package p\n\nobject A:\n  def one(x: Int): Int =\n    x + 1\n\n  def two = 2\n\nenum Color:\n  case Red, Green\n"
        names = [t.split("  ")[-1] for _, t, *_ in chunk_scala("A.scala", src)]
        self.assertTrue(any(n.endswith("one") for n in names), names)


class RealFilesSanity(unittest.TestCase):
    """Runs the chunkers over real checkouts when present: no exceptions, sane sizes, every file yields chunks."""

    def run_over(self, repo, suffix, fn, limit=300):
        repo = Path(os.path.expanduser(repo))
        if not (repo / ".git").exists():
            self.skipTest(f"{repo} not checked out")
        files = subprocess.run(["git", "-C", str(repo), "ls-files", f"*{suffix}"], capture_output=True, text=True).stdout.split()[:limit]
        total = 0
        for f in files:
            text = (repo / f).read_text(errors="replace")
            ch = fn(f, text)
            total += len(ch)
            self.assertTrue(all(len(b) < 6000 for _, _, b, _ in ch), f)
            if len(text) > 400:
                self.assertTrue(ch, f"no chunks for {f}")
        return total

    def test_zinc_java(self):
        self.assertGreater(self.run_over("~/code/sbt/zinc", ".java", chunk_java), 100)

    def test_asm_java(self):
        self.assertGreater(self.run_over("~/code/scala/scala-asm", ".java", chunk_java), 0) if (Path.home() / "code/scala/scala-asm/src").exists() else self.skipTest("main branch not checked out")


if __name__ == "__main__":
    unittest.main()
