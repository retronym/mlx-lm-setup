# Findings log

Chronological notes from building and testing this setup (2026-10-03/04). The README has the current architecture and how to run things; this file keeps the evidence and the dead ends. Sections are verbatim from earlier README revisions, so some wording is dated (e.g. 'current setup').

Scripts named here have since moved: pipelines to `pipelines/` (now gateway clients that load no model), benchmarks and earlier iterations to `experiments/` (see README, Files).

## MCP bridge vetting (`mlx-mcp-server` 0.7.0)

**Vetting notes (reviewed the 0.7.0 wheel source, ~2.3k lines):**

- Only network traffic is to `MLX_BASE_URL`; `trust_env=False`. No telemetry, no hidden endpoints.
- The GitHub repo linked from PyPI (`deresolution20/mlx-mcp-server`) returned 404 at review time, so provenance is PyPI-only. Version is pinned.
- Codex-first project (README is about Codex); its Claude Code installer writes `~/.claude/settings.json`, so **don't use `mlx-mcp-server install`**; use `claude mcp add` as above. It also copies legacy slash commands to `~/.claude/commands/`.
- `iterate` has an executable gate that runs a caller-supplied command with `shell=True` (the *calling agent* supplies it, not the local model). Be deliberate about approving it.
- Appends content-free call metrics to `~/.omlx/mlx-call-log.jsonl`.
- Incompatible with `mcp` 2.x (FastMCP renamed): pin `mcp<2`.
- Some features (`set_model`, work-hours guard) are written for the oMLX server and may be limited with plain `mlx_lm.server`, which serves one model per process.

## Measured (smoke test, 2026-10-03)

- Download: ~6 min, 15 files.
- Generation: **~103 tok/s**; peak memory **17.2 GB**.
- Quality caveat: the sample answer's "Method 2" had a bug (`reverseList(tail) +: head` is wrong). Verify local-model output; keep it to checkable tasks.

## Kicking the tyres (via MCP, 2026-10-03)

| Test | Result |
|---|---|
| `health_check`, `list_models` | OK |
| `chat`: 2-sentence explainer | ~1 s, correct but generic |
| `iterate` + `require_json`/`schema_keys` (extract method names from Scala) | Passed on first round |
| `iterate` + executable gate (`python3 $CANDIDATE_FILE`, codegen with assert) | Passed on first round |
| `chat`: spot the bug in a moving average | **Wrong**: invented an IndexError (slicing doesn't raise) and missed the real bug (partial trailing windows divided by `n`). Don't trust local review without a gate. |
| 7.7k-token prompt (summarize a 32 KB file) | 6.1 s total; ~1.5k tok/s prefill (estimated); accurate summary |

Takeaway: good for gated extraction/boilerplate and long-file summarization; unreliable for open-ended review, so keep that on the big model.

## OpenJev (NLI verifier) experiment

[`AlexWortega/openjev`](https://huggingface.co/AlexWortega/openjev) (MIT, Qwen3.5-4B fine-tuned as a 3-way NLI cross-encoder: premise + hypothesis → contradiction / entailment / neutral). Not a chat model; the idea is to use it as a cheap **verifier** of local-LLM output against its source.

- Checkpoint: `qwen3.5-4b-nli-v5` (9 GB bf16), downloaded to `models/openjev/` (git-ignored). Run with transformers + torch on MPS in a separate `.venv-jev/` (no MLX build exists for this lineage).
- Do **not** confuse with `openjev/openjev*` on HF (different project, CC BY-NC, 27 GB).
- `jev_check.py`: `Jev().check(premise, [claims])`; running it directly is the demo below.
- Caveats from its model card: not hardened against prompt injection, weak as a security/shell-command guard, and v5 trained on the test splits of several public benchmarks (so those numbers are meaningless).

**Demo** (premise = the `moving_avg` / `first_dup` code Qwen got wrong earlier). Load 13 s, 5 claims scored in 4.1 s (includes warm-up; falls back to slow reference kernels for the linear-attention layers):

| Claim | Truth | OpenJev |
|---|---|---|
| `moving_avg` raises IndexError when n > len(xs) (Qwen's hallucination) | false | **entailment 0.72 (wrong)** |
| `first_dup` returns None when no duplicate (Qwen's claim) | false | contradiction 0.92 (right) |
| trailing windows divided by n give wrong averages (the real bug) | true | entailment 0.98 (right) |
| `first_dup` returns -1 when no duplicate | true | entailment 1.00 (right) |
| `moving_avg` output has same length as input | true | entailment 0.98 (right) |

4/5. It missed the claim that requires knowing Python slicing semantics, which is exactly the kind of "plausible but false" claim we wanted to catch. Treat it as a weak extra signal, not a gate on its own.

## Demo: OpenJev zero-shot triage of scala/scala PRs (live dashboard)

Typed yes/no questions about each PR (premise = title + changed-file paths + first 1.5k chars of body; **no diff**), answered by OpenJev 4B v5 locally, scored against the labels maintainers actually applied.

```bash
gh pr list -R scala/scala --state merged --limit 300 \
  --json number,title,body,labels,files,mergedAt,author > data/prs.json   # read-only
python3 dashboard_server.py &                      # http://127.0.0.1:8766/ (whitelist-only, localhost)
.venv-jev/bin/python jev_triage.py --fresh         # appends data/results.jsonl, updates data/status.json
```

Files: `jev_triage.py` (questions + runner), `dashboard.html` (live polling dashboard: threshold slider, click a card to list its mistakes), `dashboard_server.py` (serves only the dashboard + 2 data files), `data/` (git-ignored).

**Result, 300 merged PRs (2024-11 → 2026-09), 373 s inference = 1.24 s/PR on MPS:**

| Question (truth label) | Positives | P@0.5 | R@0.5 | AUROC | Best-F1 thr → P / R / F1 |
|---|---|---|---|---|---|
| Internal housekeeping (`internal`) | 131 | 0.81 | 0.62 | 0.86 | 0.15 → 0.72 / 0.87 / 0.79 |
| Needs release notes (`release-notes`) | 38 | 0.39 | 0.50 | 0.82 | 0.70 → 0.67 / 0.42 / 0.52 |
| Documentation (`documentation`) | 22 | 0.70 | 0.64 | 0.94 | 0.25 → 0.70 / 0.73 / 0.71 |
| Library: collections (`library:collections`) | 19 | 0.49 | 1.00 | 0.98 | 0.90 → 0.56 / 0.95 / 0.71 |
| REPL (`tool:REPL`) | 5 | 0.23 | 0.60 | 0.80 | n too small |
| Performance (`performance*`) | 6 | 0.29 | 1.00 | 0.99 | n too small |

Notes: ranking quality (AUROC) is good but the default 0.5 threshold is badly calibrated per question, so tune it per question. Best-F1 thresholds are chosen on the same data (optimistic). Labels are applied inconsistently, so many "false positives" are PRs that arguably deserved the label; precision is a lower bound. REPL/perf have too few positives to conclude anything.

## Emoji book (live): Alice annotated phrase by phrase

`book.html` (served by `dashboard_server.py` at `/book`) shows the text like a book page; each phrase gets its top-3 emoji above it (hover: top-8 with probabilities). Data flow is deterministic code around a scorer; no text generation anywhere.

```bash
.venv-jev/bin/python chunk_book.py                       # spaCy phrase boundaries -> data/book_chunks.json (4 s for 4280 words)
.venv-mlxjev/bin/python emoji_book_mlx.py --fresh        # live scorer; writes data/book_events.jsonl + book_status.json
python3 dashboard_server.py &                            # http://127.0.0.1:8766/book
```

Pipeline: `chunker_spacy.py` (phrase breaks before prepositions/conjunctions, after punctuation, 4-12 words) -> `emoji_book_mlx.py` (Jev-Style-2B-Decision-v3, MLX 8-bit, ONE forward pass scores all 343 emoji from `emoji_vocab.py` as options of a single `choice` question; running per-emoji z-score calibration; abstain below p=0.04) -> `book.html`.

### What we learned getting here (iteration log)

1. **First version** used OpenJev 4B NLI (AlexWortega) for both phrase segmentation and emoji scoring (one hypothesis per emoji): **33 s per phrase**. Segmentation needed rework: the "natural phrase ends after X" wording was flat; scoring the prefix against "this is a complete phrase" worked, plus a function-word guard.
2. **Why slow** (profiled): Qwen3.5's Gated-DeltaNet layers fall back to a pure-PyTorch path on MPS (no CUDA kernels): 96% of time in `aten::copy_`, cost ~linear in batch, and **independent of model size** (0.8B = 2B = 13 s/phrase, 4B = 33 s).
3. **Group pruning** (score 9 category hypotheses first) was a bad idea: only 26% of the 4B's top-3 emoji sit in the top group; top-3 overlap 0.26-0.60.
4. **Cascade** (small model scores all, 4B re-ranks top-K) agrees well (0.90-0.95 top-3) but is still runtime-bound.
5. **Off-the-shelf fix:** two Apache-2.0 "Jev-shaped" models score all options in a single pass:

| Scorer | s/phrase (343 emoji) | Notes |
|---|---|---|
| OpenJev 4B NLI, PyTorch/MPS | 33.4 | 343 hypotheses, slow fallback kernels |
| OpenJev 2B / 0.8B NLI | 13.3 / 13.1 | same overhead-bound |
| `com-kotobalabs/open-jev-deberta-v3-large` | 0.52 | 435M encoder, 512-token cap so ~5 chunks; weaker on abstract text |
| **`chaoliangUNSW/Jev-Style-2B-Decision-v3-MLX` (8-bit)** | **0.89** | one 5.8k-token pass, native MLX Metal kernels. Used. |

Agreement with the 4B NLI top-3 is only ~0.45 for both fast models; that is a style difference, not worse quality (4B NLI over-picks 👩 because "she/Alice" entails "a woman"; the decision models pick mood/meaning). No ground truth exists here: judge by eye.

Setup notes: `.venv-mlxjev` pins `mlx==0.32.2 mlx-lm==0.31.3 transformers==5.17.0 tokenizers==0.23.2 numpy==2.5.3` (the runtime refuses other mlx-lm versions). Weights in `models/` (git-ignored): `jevstyle-2b-mlx/8bit` (2 GB), `deberta-openjev` (1.7 GB), `openjev/*` (4B 9 GB, 2B 4.4 GB, 0.8B 1.7 GB). I audited the DeBERTa package and the MLX runtime script (no network use besides the Hub download, no `trust_remote_code`, no subprocess) and did not install the `jev-style` pip package.

Files: `bench_emoji.py` (NLI checkpoint + group/cascade benchmark), `bench_alt.py` (DeBERTa + MLX 2B + comparison), `bench_all.sh`, `chunker_spacy.py`, `chunk_book.py`, `emoji_book.py` (original all-Jev loop, kept for reference), `emoji_book_mlx.py` (current).

### Letting the model pick the emoji itself (vs my text names)

Hypothesis: models know emoji double meanings (🍆, 💀, 🐐), so don't render emoji as my literal names.

1. **Jev-Style 2B, glyph-only options** (`bench_glyph.py`, `bench_glyph2.py`): not better. Overall worse than names on book text (attractor on 🕸️; ZWJ/variation-selector glyphs tokenise badly); on 10 slang probes top-3 hits were names 5, glyph-only 4, glyph+name 5, fused names+glyph 4. Glyph-only did lift 🍆 from rank 203 to 5, so some glyph knowledge exists, but a 2B is too small to rely on it.
2. **LLM next-token scoring** (`lm_emoji.py`, `bench_lm_probe.py`; Qwen3-Coder-30B-A3B 4-bit via brew mlx-lm): `log P(emoji tokens | few-shot "Phrase: ... Emoji:")` for every candidate, one shared prompt prefill, candidates batched against a broadcast KV cache (verified against naive scoring). Deterministic, real probabilities, 1.3-2.7 s per phrase for ~346 emoji. Slang probes 6/10 at rank 1 (💀 0.93, 🐐 0.93, 🔥 0.96, 💦, 🤡, 👻 all correct; missed 🍆 innuendo and 🧢 "cap"). Book narrative: noisier than the Jev-style decision model (good: 🌸🌹🌻 for "daisy-chain", 🕳️ for "the well"; poor: 🤷🎵🐾 for "I fell"). Note: Qwen's byte-level tokenizer splits most emoji over 2-4 tokens, so you must score whole token sequences, not "single emoji tokens".
3. PMI-style prior correction (subtract 0.7 x unconditional score) did not help.

Takeaway: the LLM adds genuine slang/idiom knowledge; the Jev-style model is better at narrative meaning. Untested next step: fuse the two (z-scored sum), ~3.5 s/phrase.

### Windowed state + typed attributes + colour tinting (current setup)

- **Whole-chapter context was worse** for the Jev-Style 2B (all chunks collapse onto the chapter gist: ⬇️🐭🏊🤔; 1.8 s). The recurrent (gated delta-net) layers can't be masked per question, so I tested the equivalent physical windows: 60/20 and 30/10 words around the chunk drift toward chapter themes; **15 words before + 5 after, chunk marked ⟦ ⟧**, costs the same as per-phrase (~1 s) and improves context-dependent chunks ("it was labelled" -> 🍊🍯🍬 vs 🐞). This is what `score_worker.py jev` now uses.
- **Typed attributes** are extra questions answered from the same state in one `score_many` call (~free): `color` (12 colours + "no particular colour" as the abstain option), `mood` (10 options), `sentiment` (5 ordered levels -> expected score -2..+2). Stored per phrase in `scores_jev.jsonl`, passed through as `attrs` in `book_events.jsonl`. Add more by adding entries to `ATTRS` in `score_worker.py`.
- **book.html**: phrases are tinted by the predicted colour when its probability >= the slider (default 0.5) and it isn't "no particular colour"; style toggle: highlight / text colour. Hover popup shows colour, mood, sentiment.
- Bug note: an early version of the Jev worker called the model once per emoji (346 passes/phrase) — fixed; both workers can run concurrently (the LLM worker slows from ~1.5 s to ~5 s while the Jev worker runs).

- **Colour = "vibe" colour, no abstain option** (`jev_attrs.py: COLOR_Q`): "Imagine the passage as a mood board. Which single colour best captures its vibe, feeling and atmosphere?" over 12 colours. With no abstain, top probabilities are naturally lower (median 0.27, p75 0.35, p90 0.51 over the first 170 phrases), so the page's confidence slider (default 0.40) decides what gets tinted. Observed bias: ~64% of answers are achromatic (grey/white/black). `recolor.py` re-answers only the colour question for already-scored phrases (170 phrases in 9 s), so prompt wording can be iterated cheaply.
- **Memory:** the 17 GB LLM worker plus everything else pushed swap to 6 of 7 GB and the machine thrashed. Run the LLM worker alone (or not at all). `emoji_book_fused.py` now falls back to Jev-only picks past the LLM's position; rerun `emoji_book_fused.py --fresh` once the LLM worker has caught up to refuse everything.

