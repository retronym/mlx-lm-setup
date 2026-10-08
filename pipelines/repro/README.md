# Reproducing scala/bug issues (G1 pilot)

## Problem

scala/bug has about 1,460 open issues, most reported between 2012 and 2018 against compilers nobody runs any more. Many have since been fixed by changes that never mentioned them; others still reproduce on 2.13 and some in Scala 3. Nobody can re-check them by hand, but most carry a reproducer in a code fence, so a machine can: extract it, run it on old and current compilers, and say which issues are probably fixed. The output is a list for a human to confirm and close, never automatic action on GitHub.

## Key decisions

- **Measure the judge before trusting it.** The links database knows which closed issues a merged PR closed and which release shipped that PR. Running such an issue's reproducer on the release before the fix and on the release with it gives a labelled pair for free: the bug should show before and not after. The calibration sample says how often "not reproduced" means "fixed" before that verdict is used on open issues.
- **The local LLM extracts, rules judge.** Turning a thread into a runnable file is reading comprehension (which of five snippets is the reproducer, REPL transcripts to a file, flags, dependencies); the local model does it, but mechanical gates check the answer against the thread: the code, the flags, the dependencies, the version and the quoted output must be copied from it, not invented. A failed gate is fed back and the model retries. Judging is rules over exit status and output per symptom (crash, spurious error, missing error, wrong output, hang), with an explicit `differs` verdict when something else happened, so a broken reproducer is not counted as a fix.
- **Untrusted code runs sandboxed and offline.** Compiling a bug report runs its code (macros, plugins). Dependencies, compilers and JVMs are fetched first by compiling a trivial file outside the sandbox; the real run is `scala-cli --offline` under `sandbox-exec` with no network and writes only to its own directory and the caches. `sandbox-exec` is deprecated but works; the runner is one function, so a container can replace it once Docker is set up.
- **scala-cli as the driver.** It resolves any 2.12, 2.13 and 3 release and the JVM it needs (JDK 8 before 2.12.15 and 2.13.6, else 17). It does not support 2.11 and earlier, so issues are run on current compilers and calibration on fixes shipped in 2.12.1 and later.

## Running it

```bash
python pipelines/repro/pilot.py all        # select, extract (LLM through the gateway), run (4 in parallel), report -> pipelines/repro/data/report.md
python pipelines/repro/pilot.py report     # or one step: select | extract | run [-j N] | report
python pipelines/repro/extract.py 9337     # the spec for one issue
```

Specs are cached by thread text in `data/specs/`, runs and their output are kept in `data/runs/<issue>/<version>/`, results in `data/results.jsonl`.

## Results

See [Pilot results](#pilot-results) once the first run is in.
