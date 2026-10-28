# Plan: Scala 3 compiler completions in a local model's decoding

## Problem

A local code model (Qwen3-Coder-30B-A3B on mlx-lm) spends one decode step per token, including the tokens of member names that a compiler could have told it. The multi-reader presentation compiler (retronym/scala3#84) answers completions in about 1 ms on a warm session, cheap enough to sit inside a decoding loop. Two uses:

- **Drafts (lossless).** Once the model's first token of a selected name is in the compiler's completion trie, draft the rest of the name and verify it in one forward pass, as in retrieval-style speculative decoding (REST, prompt lookup). New angle versus prior art: the drafts come from types, and chain (`a.b.c`) without re-typing.
- **Mask (lossy, validating).** Restrict logits to the trie at a selection, as in monitor-guided decoding (Agrawal et al., NeurIPS 2023) and type-constrained decoding (Mündler et al., PLDI 2025). Never where the compiler is guessing (error-typed, `Dynamic`, unresolved qualifier).

Lesson from elsewhere: measure the ceiling before building. Phase 0 is a go/no-go.

## Phases

| Phase | What | Status |
|---|---|---|
| 0a | Ceiling on drafts: share of Qwen3-Coder tokens inside a selected name after its first token, per hop of a chain; scala3 `compiler/src` and Qwen-written Scala | DONE: small (×1.046 oracle) |
| 0b | Value of masking: Qwen3-Coder writes held-out method bodies in scala3; share of compile errors that are not-a-member / not-found | DONE: large (71% of errors) |
| — | Jason's go: mask first, against a typecheck-and-retry baseline | DONE |
| 1 | Compiler service (JVM): `/complete` after a dot, `/members`, `/scope` at a bare identifier, `/check`; one `CompletionService` interface with a stock 3.10.1-RC1 back end and a retronym/scala3#84 back end (`MultiReaderService`, `readUpTo`, `-Yinteractive-body-edits`) | DONE |
| 2 | Decoding loop on mlx-lm: selection mask (chained hops too), linear trie drafts with KV rollback, bare identifiers checked at their end and taken back when unresolved; eval of 5 arms; demo page | DONE on 62 of 100 bodies (paused for memory) |
| — | Finish the eval on the remaining 38 bodies | TODO |
| — | #84 body-edit findings: `spliceAll` assertion; per-edit typing 75–145 ms instead of single-digit ms | TODO (investigating) |

## Phase 0 method

- `ceiling.py`: tokenises with Qwen3-Coder's tokenizer (offsets), masks comments and literals, finds `.name` lexically. A selection's drafted tokens = tokens overlapping the name minus the first one (the model's choice). Qwen's BPE usually glues the `.` to the start of the name (`.symbol`, `.is`+`Class`), so the first token is often the whole name. Step ceiling = N / (N − drafted), assuming a verify pass costs one decode step.
- `qwen_sample.py`: 40 varied Scala 3 prompts to the gateway's `qwen3-coder`, code blocks saved for `ceiling.py`.
- `phase0b.py` + `jvm/Tool.scala`: parse scala3 `compiler/src` with a stock 3.10.1-RC1 compiler, sample member defs with 3–25 line bodies (≤2 per file, seed 1), keep those whose original body typechecks against the 3.10.1-RC1 jar (the checkout is ahead of it), ask Qwen3-Coder (gateway chat, temperature 0.2) for the body given the file header plus the 150 lines before the `=`, splice, typecheck to typer with the compiler's own flags (`-Yexplicit-nulls` etc.), count distinct errors inside the body by `ErrorMessageID`. Maskable = `NotAMemberID`, `MissingIdentID`.

## Results

### 0a: draft ceiling — small

Qwen's BPE glues the `.` to the name (`.symbol`, `.owner`, `.is`+`Class`): in 92% of selections the first token carries the dot, and 43% of selected names are a single token, so there is nothing left to draft.

| Corpus | Tokens | Selections | Drafted (oracle) | Step ceiling |
|---|---|---|---|---|
| scala3 `compiler/src` (572 files) | 2.27 M | 120.7 k | 4.38% of tokens (7.05% of code tokens) | ×1.046 |
| — excluding import/package | | | 4.10% | ×1.043 |
| Qwen3-Coder-written Scala 3 (40 prompts) | 23.8 k | 1.1 k | 1.10% | ×1.011 |

Per hop (scala3, share of all tokens): hop 1 2.89%, hop 2 0.64%, hop 3 0.19%, hop 4+ 0.22%, first selection after `)`/`]` 0.17%. Chained hops are a quarter of the pool.

Realistic linear drafts (`realistic.py`, typed selections in 78 scala3 files, every 6th): drafting to the longest common prefix of the qualifier's member names that match the model's first token covers 0.71% of tokens (×1.007), 30% of the oracle on the same typed selections (2.36%) and about 19% of the lexical oracle on those files (3.76%; the gap is mostly extension methods and type selections, which are not `Select`s on a member). The first token leaves one candidate in 47% of selections, median 2 candidates.

For comparison, all identifiers (scope completions too, including definitions, which cannot be drafted) would be 16.5% of tokens, ×1.20 oracle.

Verdict: drafts from selections alone cannot pay for the machinery; a verify pass of k tokens on an MoE also costs more than one decode step, so the real gain would be below ×1.01.

### 0b: value of masking — large

100 held-out bodies (from 130 sampled; 30 dropped because the original body already has errors against 3.10.1-RC1). The chat model often repeats the signature, nests a fence or runs on into further members; `clean_gen` strips those before splicing.

| Group | Bodies | Note |
|---|---|---|
| Runaway | 16 | hit 1200 tokens, mostly repetition loops; an own decoding loop would stop at the dedent |
| Syntax | 5 | parse/indentation errors or new errors outside the body |
| Well-formed | 79 | 33 with errors in the body (42%), 72 distinct errors |

On the 79 well-formed bodies:

| Error | Errors | Bodies |
|---|---|---|
| `MissingIdentID` (Not found: x) | 27 | 9 |
| `NotAMemberID` (x is not a member of T) | 24 | 16 |
| `TypeMismatchID` | 5 | 5 |
| missing given `Context`, reassignment to val, missing argument, wrong unapply arity, others | 16 | 11 |

Not-a-member / not-found are 51 of 72 errors (71%), occur in 22 of the 33 bodies with errors, and are the only kind in 19 of them (24% of well-formed bodies). The selection mask alone (Phase 2b, `NotAMemberID`) touches 16 bodies, and is the only error in 12 (15% of well-formed bodies). `MissingIdentID` needs a scope mask at identifier starts (harder: new binding names must stay free), and is the only error in 4 bodies. Typical errors are plausible-sounding hallucinated members: `sym.denotation`, `sym.isTypeParameter`, `tp.nonEmpty`, `defTree.annotations`, `aliasType`.

These are upper bounds: a mask removes the bad name but cannot guarantee that the forced alternative typechecks or means the right thing.

### Go/no-go

- Drafts: no-go as a speed-up (oracle ×1.04 on scala3, ×1.01 on Qwen-written code; member-based linear drafts ×1.007).
- Mask: worth building. Phase 2 should lead with the selection mask (b) and measure errors per body, with drafts as a by-product of the same trie rather than the goal. The baseline it must beat is generate → typecheck → feed the error back → retry.

### Phases 1–2: errors per body (62 held-out bodies, all arms done)

Greedy decoding, same prompt in every arm (assistant turn pre-filled up to the def's `=`), arms interleaved per body; errors are distinct errors inside the body, typechecked with the stock compiler as in 0b. Compiler: the #84 back end.

| Arm | Bodies with errors | Errors | Not a member | Not found (term) | Not found (type) | Other | Tokens | Forward passes | Wall s |
|---|---|---|---|---|---|---|---|---|---|
| plain | 35 | 194 | 32 | 54 | 12 | 96 | 7708 | 7646 | 143 |
| mask (selections) | 30 | 172 | 6 | 57 | 10 | 99 | 7047 | 6985 | 163 |
| mask + drafts | 32 | 181 | 10 | | | | 6994 | 6743 | 161 |
| mask + identifiers | 30 | 151 | 6 | 21 | 12 | 112 | 7350 | 7345 | 204 |
| typecheck and retry (≤2) | 32 | 135 | 28 | 28 | 10 | 69 | 20271 | 20143 | 378 |

- The selection mask removes 81% of not-a-member errors (32 → 6) at 506 compiler calls (~0.4 s per body); identifier checks remove 61% of term not-found errors (54 → 21) with 1200 checks and 22 take-backs, none of them forward references. Neither reduces the number of bodies with errors much (35 → 30): what is left is type mismatches and wrong-but-existing names, and a forced name raises "other" errors (96 → 112).
- Retry fixes more of the other errors (96 → 69) and name errors less (not-a-member + term not-found 86 → 56, against 86 → 27 with mask + identifiers), and costs 2.6× the tokens and 2.6× the wall time. Mask + identifiers is the cheaper way to clean names; the combination (mask + identifiers, then retry) is the obvious next arm.
- Drafts: 189 of 223 drafted tokens accepted (85%), 3.5% fewer forward passes than mask, no wall-clock gain (verify passes of several tokens cost more on the MoE). Greedy mask + drafts should equal mask; it differs on some bodies because a batched verify pass is not bit-identical to sequential decoding (near-ties flip).
- Runaways (greedy repetition loops, cut by the loop detector) are ~9 bodies in every arm.

What the model sees: only the file header and the 150 lines before the def, no tools. Many name errors (`readBoolean` on a `PickleBuffer`, `Flags.Explicit`, `paramClauseOpt`) are what a model that can grep the code base would avoid; an agentic arm (grep/read tools over `compiler/src`) is the fair comparison for agents.

### Lessons from building it

- Close the half-written body's brackets in the compiler's buffer: otherwise the rest of the file mis-parses and typing costs ~3 s instead of ~0.4 s (stock) on a 1.2k-line file.
- "Resolves?" must be name lookup, not the typed tree's symbol: an identifier that fails its expected type becomes an error tree without a symbol. And no verdict where the typer never reached the identifier (an erroneous call drops its arguments).
- Qwen writes `.name` as one token, so the end of one name and the start of the next hop come in one token; the mask must re-trigger there.
- Binders are decidable from text with a little lookahead: lambda parameters, `case ... =>` patterns (also mid-line), definition names, named arguments, `for` binders, `s"..."` interpolators.

## Future work

- Tree drafts / tree attention; multi-threaded compiler access (out of scope for now).
- Scope completions (identifiers not after a `.`): a bigger pool than selections, see 0a.
