"""Phase 0a, second corpus: Scala that Qwen3-Coder writes. Asks the gateway's qwen3-coder for Scala 3 code on varied prompts and
saves the code blocks as .scala files, for ceiling.py to measure.

Usage: python qwen_sample.py <out-dir>
"""
import json, os, re, sys, time, urllib.request

GATEWAY = "http://127.0.0.1:8090/v1/chat/completions"

TASKS = [
    "an LRU cache with get/put and a capacity, backed by a mutable.LinkedHashMap",
    "a JSON pretty-printer for a small JSON ADT (enum Json with JNull, JBool, JNum, JStr, JArr, JObj)",
    "a tokenizer and recursive-descent parser for arithmetic expressions with + - * / and parentheses, and an evaluator",
    "a word-frequency counter that reads a file with scala.io.Source and prints the top 20 words",
    "a simple in-memory key-value store with transactions (begin, commit, rollback) using immutable Maps",
    "Dijkstra's shortest path over a graph given as Map[String, List[(String, Int)]]",
    "a type class Show with given instances for Int, String, List[A] and Option[A], and extension method show",
    "a tree-walking interpreter for a tiny lambda calculus with Let, Lam, App, Var, and integer literals",
    "a CSV reader that parses quoted fields and returns List[Map[String, String]] keyed by the header",
    "a bank account simulation with case classes, a sealed trait of transactions, and a fold to compute balances",
    "a binary search tree with insert, contains, delete, in-order traversal and height",
    "a rate limiter (token bucket) with a clock abstraction so it can be tested",
    "matrix multiplication and transpose over Vector[Vector[Double]] with dimension checks",
    "an event sourcing example: commands, events, an aggregate, and replay from a list of events",
    "a small HTTP-like router that matches paths with parameters like /users/:id and dispatches to handlers",
    "a Future-based concurrent web crawler simulation using a fake fetch function and a visited set",
    "a priority queue based task scheduler with mutable.PriorityQueue and an Ordering on deadlines",
    "a parser combinator library (Parser[A] with map, flatMap, |, many) and a parser for a list of integers",
    "a game of life on a finite grid, with step and render to a String",
    "a library management system with books, members, loans, and queries using collections operations",
    "a generic Graph[A] class with BFS, DFS and topological sort",
    "a simple state machine for a vending machine using an enum of states and an enum of inputs",
    "a Scala 3 macro-free compile-time-safe units of measure using opaque types for Meters and Seconds",
    "an AST for a tiny statically-typed language and a type checker that reports errors with positions",
    "a text adventure command parser with a room map, inventory and look/go/take commands",
    "string utilities: camelCase to snake_case, palindrome check, Levenshtein distance, run-length encoding",
    "a mini dependency-injection container keyed by ClassTag",
    "a log file analyzer that parses lines with a regex and aggregates counts per level and per hour",
    "an immutable persistent stack and queue (two-list queue) with amortized O(1) operations",
    "a bowling score calculator with a sealed trait for frames",
    "a sudoku solver using backtracking over Array[Array[Int]]",
    "a validation library with Validated[E, A], mapN for tuples, and an example validating a user form",
    "a symbol table with nested scopes for a compiler, with enter, lookup and a scope stack",
    "a pretty-printer for a small Scala-like AST that indents nested blocks",
    "a Huffman coding encoder and decoder",
    "a trie for autocomplete with insert and prefix search returning the top-k words by frequency",
    "a simple actor-like mailbox using java.util.concurrent queues and a worker thread",
    "an inventory system using Map updates, Option handling, and Either for errors",
    "a JSON-like config loader that merges defaults with overrides recursively",
    "a stream processing pipeline over Iterator with windowed averages and filtering",
]


def ask(prompt: str) -> str:
    body = {"model": "qwen3-coder", "temperature": 0.2, "max_tokens": 1500,
            "messages": [{"role": "user", "content": f"Write idiomatic Scala 3 code for {prompt}. Include a small main method. Reply with one code block."}]}
    req = urllib.request.Request(GATEWAY, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def main():
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    for i, t in enumerate(TASKS):
        path = os.path.join(out, f"q{i:02d}.scala")
        if os.path.exists(path):
            continue
        text = ask(t)
        blocks = re.findall(r"```(?:scala)?\n(.*?)```", text, re.S) or [text]
        with open(path, "w") as fh:
            fh.write("\n\n".join(blocks))
        el = time.time() - t0
        print(f"[{i + 1}/{len(TASKS)}] {len(blocks[0])} chars  elapsed {el:.0f}s  eta {el / (i + 1) * (len(TASKS) - i - 1):.0f}s", flush=True)


if __name__ == "__main__":
    main()
