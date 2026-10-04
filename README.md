# mlx-lm-setup

A local model workbench for an M5 Pro Mac (48 GB unified memory): a generative coding LLM, two "decision"-style models, and the scripts, pipelines and web pages built around them. Everything runs on localhost; prompts and code never leave the machine.

## Why

Offload cheap, bounded, verifiable work (summaries, extraction, classification, boilerplate, first-pass triage) to local models so the large hosted model is reserved for design and hard reasoning. The recurring lesson: **models that score a closed set of options in one forward pass (decision models) are far cheaper and more deterministic than generating text**, so most of the interesting pipelines here are deterministic code that calls a scorer, with no text generation in the inner loop. The binding constraint is memory, not speed: the models together do not fit comfortably, which is what [PLAN.md](PLAN.md) addresses.

## The models

| Role | Model | Runtime / env | Memory | Used for |
|---|---|---|---|---|
| Generative LLM | Qwen3-Coder-30B-A3B-Instruct, 4-bit (MoE, ~3B active) | `mlx-lm` 0.32 (Homebrew, python 3.14) | ~17 GB | chat, MCP sub-agent, emoji next-token scoring (`lm_emoji.py`) |
| Generative LLM | Qwen3.6-35B-A3B, 4-bit (MoE, 3B active; thinks by default) | `mlx-lm` 0.32 | ~21.5 GB peak | general sub-agent work at speed, long context (small KV cache) |
| Generative LLM | Gemma-4-26B-A4B QAT, 4-bit (MoE, 4B active) | `mlx-lm` 0.32 | ~16 GB peak | safest general default: passed all six benchmark tasks, smallest memory |
| NLI cross-encoder | OpenJev 4B v5 (Qwen3.5-4B fine-tune; also 2B, 0.8B) | PyTorch on MPS, `.venv-jev` (python 3.12) | ~9 GB | claim verification, PR triage. Slow: no fast kernels for Qwen3.5's linear-attention layers on MPS |
| Decision model | Jev-Style 2B v3 (Qwen3.5-2B fine-tune), 8-bit | MLX, `.venv-mlxjev` (versions pinned, the runtime refuses others) | ~2 GB weights | emoji, colour, mood, sentiment: one pass scores hundreds of options |
| Encoder (alternative) | open-jev DeBERTa-v3-large | PyTorch on MPS, `.venv-jev` | 1.7 GB | fast baseline; 512-token cap |

Which LLM to pick for which job, with measurements: **[docs/MODEL_GUIDE.md](docs/MODEL_GUIDE.md)**. Weights live under `models/` (git-ignored) or the Hugging Face cache. The 4B NLI and the DeBERTa model are not currently used by any pipeline, only by benchmarks and the triage demo.

## Architecture

The gateway is the front door for Claude Code (MCP over HTTP) and starts, stops and passivates the model processes on demand within a memory budget. The older standalone scripts still run models directly; the dashed box marks what bypasses the gateway and so sits outside its memory management (migrating the workers is phase 7 in [PLAN.md](PLAN.md)).

```mermaid
flowchart TB
  CC["Claude Code"] -- "MCP over HTTP + token" --> GW
  BR["Browser"] -- "chat site / and admin site /admin<br/>(SSE live status, token via URL fragment)" --> GW
  subgraph gw ["Gateway · python -m gateway · :8090 · .venv"]
    GW["chat and admin sites · MCP /mcp · OpenAI API /v1 · /api/decide · /api/entail · /api/backends<br/>supervisor: lazy start, idle passivation, memory budget with LRU eviction, pressure monitor"]
  end
  GW -- "spawn / stop / proxy" --> Q["qwen3-coder :18101<br/>mlx_lm.server · Homebrew python · ~17.5 GB"]
  GW -- "spawn / stop / proxy" --> O["openjev-4b :18102<br/>OpenJev NLI · .venv-jev · ~9.5 GB"]
  GW -- "spawn / stop / proxy" --> J["jevstyle-2b :18103<br/>Jev-Style decision · .venv-mlxjev · ~3 GB"]
  subgraph direct ["Standalone scripts, outside the gateway's memory management"]
    SRV["serve.sh: mlx_lm.server :8080<br/>~17 GB"]
    TRI["jev_triage.py: OpenJev 4B directly<br/>~9 GB"]
    JW["score_worker.py jev: Jev-Style directly"]
    LW["score_worker.py lm: its OWN in-process<br/>Qwen3-Coder copy, ~17 GB"]
  end
  DASH["dashboard_server.py :8766<br/>whitelisted pages + data files"]
  TRI -- "results.jsonl" --> DASH
  JW -- "scores_jev.jsonl" --> FUS["emoji_book_fused.py"]
  LW -- "scores_lm.jsonl" --> FUS
  FUS -- "book_events.jsonl" --> DASH
  DASH --> PAGES["dashboard.html · book.html"]
  style direct stroke-dasharray: 5 5
  classDef warn stroke:#d95926,stroke-width:2px;
  class LW warn;
```

The orange box is the one to watch: the LLM emoji worker does not use any server; it loads a second copy of the 17 GB model. Running it beside the gateway's Qwen is how the machine once reached 6 GB of swap.

## Pipelines

### Emoji book: Alice annotated phrase by phrase

Deterministic code around scorers: spaCy cuts the text into phrases, a decision model and an LLM score every emoji, the scores are fused, and the page renders them as ruby text above each phrase, tinted by the "vibe colour" when the model is confident.

```mermaid
flowchart LR
  TXT["corpus/alice.txt<br/>Project Gutenberg"] --> WORDS["data/book_words.json<br/>4,280 words, chapters I–II"]
  WORDS --> CH["chunk_book.py<br/>spaCy chunker, 4–12 words<br/>0.1 s per phrase"]
  CH --> CHUNKS["data/book_chunks.json<br/>748 phrases"]
  CHUNKS --> JW["score_worker.py jev<br/>Jev-Style 2B · ~1 s per phrase"]
  CHUNKS --> LW["score_worker.py lm<br/>Qwen3-Coder log P(emoji) · ~1.5 s per phrase"]
  JW -- "emoji scores + colour, mood,<br/>sentiment" --> SJ["scores_jev.jsonl"]
  LW -- "log-probs, 346 emoji" --> SL["scores_lm.jsonl"]
  SJ --> FU["emoji_book_fused.py<br/>z-score fusion, w_lm = 0.6<br/>Jev-only where LLM not yet scored"]
  SL --> FU
  FU --> EV["book_events.jsonl<br/>book_status.json"]
  EV --> PG["book.html<br/>emoji ruby text · colour tint · hover popup"]
```

What one decision-model call looks like (this is why attributes are nearly free: the state is computed once and every extra question only adds its own options):

```mermaid
flowchart LR
  subgraph state ["state: about 25 tokens"]
    direction LR
    B["15 words before"] --- C["⟦ phrase ⟧"] --- A["5 words after"]
  end
  state --> M["Jev-Style 2B<br/>one call, state computed once"]
  Q1["emoji? · 346 options"] --> M
  Q2["vibe colour? · 12 options"] --> M
  Q3["mood? · 10 options"] --> M
  Q4["sentiment · 5 ordered levels"] --> M
  M --> S["score of option k =<br/>logit(yes) − logit(no) at its slot"]
  S --> R["raw scores + softmax probabilities<br/>→ top-3 emoji, colour tint, popup"]
```

```bash
.venv-jev/bin/python chunk_book.py                                    # phrase boundaries -> data/book_chunks.json
.venv-mlxjev/bin/python score_worker.py jev                           # resumable; --fresh to restart
/opt/homebrew/opt/mlx-lm/libexec/bin/python score_worker.py lm        # optional, 17 GB: run ALONE, not beside other heavy models
.venv-jev/bin/python emoji_book_fused.py --fresh                      # combine -> data/book_events.jsonl
python3 dashboard_server.py &                                         # http://127.0.0.1:8766/book
```

`recolor.py` re-answers only the colour question for phrases already scored (170 phrases in 9 s), so prompt wording can be iterated quickly. Questions and the context window live in `jev_attrs.py`; the emoji list in `emoji_vocab.py`.

### PR triage: OpenJev on scala/scala

Typed yes/no questions about each merged PR (title, changed files, start of the description; no diff), scored against the labels maintainers actually applied. 300 PRs, 1.24 s each, AUROC 0.80 to 0.99 but badly calibrated at the default threshold; see the findings log.

```mermaid
flowchart LR
  GH["gh pr list<br/>(read-only)"] --> PRS["data/prs.json"] --> TRI["jev_triage.py<br/>6 hypotheses per PR<br/>OpenJev 4B NLI"] --> RES["data/results.jsonl"] --> DASH["dashboard.html<br/>precision, recall, AUROC<br/>live, threshold slider"]
```

```bash
gh pr list -R scala/scala --state merged --limit 300 --json number,title,body,labels,files,mergedAt,author > data/prs.json
python3 dashboard_server.py &                      # http://127.0.0.1:8766/
.venv-jev/bin/python jev_triage.py --fresh
```

### Chat and MCP

```bash
brew install mlx-lm                  # one-time
.venv/bin/python -m gateway &        # the gateway: chat site, admin site, MCP, OpenAI-compatible API on 127.0.0.1:8090
open http://127.0.0.1:8090/          # chat site: streaming, tok/s, shows "starting model..." on a cold start
python -m gateway admin              # admin site, authenticated (opens your browser; token travels in the URL fragment only)
./ask.sh "Summarize" < Foo.scala     # one-shot through the gateway
./serve.sh &                         # optional, independent of the gateway: a plain mlx_lm.server on :8080 (use ./ask.sh "..." 8080)
```

The standalone server above is independent of the gateway. Claude Code now talks to the **gateway's own MCP server** (the third-party `mlx-mcp-server` was retired: unregistered, uninstalled; its vetting notes remain in [docs/FINDINGS.md](docs/FINDINGS.md)).

### Gateway MCP tools

```bash
.venv/bin/python -m gateway &          # must be running for Claude Code to connect (launchd service: phase 8)
./register_mcp.sh                      # claude mcp add --transport http ... with the token header; restart Claude Code after
```

| Tool | What | Token |
|---|---|---|
| `chat` | local LLM (default Qwen3-Coder); `message` or `messages`, optional `system` | no |
| `decide` | typed decisions with probabilities from the decision model: one `question` + `options`, or several `questions` about one state | no |
| `entail` | NLI: is each hypothesis entailed by / contradicted by / neutral to a premise | no |
| `iterate` | `chat` with retries until the answer passes gates: JSON (+schema), regex, contains, length, NLI faithfulness to a source text; optional escalation model for the last try; no shell gate | no |
| `backends_status` | state, memory, idle timers, budget, system free memory and swap; starts nothing | no |
| `start_backend`, `stop_backend`, `set_backend_policy` | change what is running; TTL and pin | **yes** |

State-changing tools need the token in the `Authorization: Bearer` header (the registration script adds it; the token lives in `.gateway/token`, mode 0600, created on first run; `python -m gateway token` prints it). Inference and status are open on localhost. `.venv/bin/python -m gateway.live_check` exercises every tool against a running gateway with the real models.

## What we measured (headlines)

| Question | Answer |
|---|---|
| Qwen3-Coder-30B speed and memory | ~103 tok/s generation, 17.2 GB peak. Good at gated extraction, boilerplate, long-file summaries; unreliable at open-ended review (hallucinated a bug). |
| Why was OpenJev 4B NLI slow (33 s per 343 emoji)? | Qwen3.5's linear-attention layers fall back to pure PyTorch on MPS: 96% of time in copies, cost independent of model size. |
| What fixed it? | A model that scores all options in one pass on native MLX: Jev-Style 2B, 0.9 s for all 346 emoji (37x faster). |
| Group pruning of emoji candidates | Bad idea: loses half of the good answers. |
| Whole-chapter context | Worse (collapses onto chapter gist, 2x slower). A 15-before / 5-after word window is free and better. |
| Glyph-only emoji options vs names | Not better for the 2B model; some glyph knowledge exists (🍆 rank 203 → 5). |
| LLM next-token emoji scoring | Knows slang (💀 🐐 🔥 🤡 👻); noisier on narrative. Fusion with the decision model: slang top-1 13/22 (Jev alone) and 15/22 (LLM alone) → 17–18/22 fused. |
| Memory | LLM + decision model + browsers → 6 of 7 GB swap and a thrashing machine. Run heavy models one at a time. |

Details, tables and dead ends: [docs/FINDINGS.md](docs/FINDINGS.md).

## Files

| Path | What |
|---|---|
| `serve.sh`, `ask.sh` | standalone LLM server, one-shot client (gateway by default) |
| `dashboard_server.py`, `dashboard.html`, `book.html` | whitelist-only localhost server; triage dashboard; emoji book |
| `score_worker.py`, `jev_attrs.py`, `lm_emoji.py`, `emoji_book_fused.py`, `emoji_vocab.py`, `chunker_spacy.py`, `chunk_book.py`, `recolor.py` | emoji book pipeline (current) |
| `emoji_book.py`, `emoji_book_mlx.py` | earlier pipeline iterations, kept for reference |
| `jev_check.py`, `jev_triage.py` | OpenJev NLI wrapper and PR triage |
| `bench_*.py`, `fuse_*.py`, `bench_all.sh`, `run_fused.sh` | benchmarks and fusion experiments (`bench_llm_compare.py`: head-to-head of the catalog LLMs, one model per process) |
| `data/`, `models/`, `corpus/`, `.venv*/`, `*.log` | generated or downloaded; git-ignored |
| `docs/FINDINGS.md` | long-form log of what was tried |
| `PLAN.md` | design for the model gateway (lifecycle, passivation, MCP, web) and its phase status |
| `gateway.toml`, `gateway/` (`web/` holds the chat and admin sites) | gateway: catalog (`python -m gateway.catalog`), supervisor, HTTP app (`python -m gateway`), thin backend servers for the decision and NLI models; supervisor in `gateway/supervisor.py`, driver: `python -m gateway.cli demo jevstyle-2b --ttl 5`; all tests: `.venv/bin/python -m unittest discover -s gateway/tests -t .` (phases 1-6 done: catalog, supervisor, HTTP front door, MCP tools, chat and admin sites, memory budget) |

Environments: `.venv` (the gateway: `mcp[cli]<2`, uvicorn, httpx), `.venv-jev` (torch, transformers, spaCy), `.venv-mlxjev` (pinned `mlx==0.32.2 mlx-lm==0.31.3 transformers==5.17.0 tokenizers==0.23.2 numpy==2.5.3`). Two of these exist because dependency pins conflict, which is one reason the planned gateway runs each model as a separate process.

## Gateway (in progress)

A localhost gateway that starts models on demand, passivates idle ones, and fronts them with one API. Phases 1-6, 4b and 8 are built (catalog, supervisor, HTTP proxy, MCP tools, gated `iterate`, chat and admin sites, memory budget, launchd service via `service/service.sh install`); migrating the workers comes next (see [PLAN.md](PLAN.md)). **Memory is managed**: a 28 GB budget with least-recently-used eviction of idle backends (busy and pinned ones are never evicted), plus a pressure monitor that evicts an idle backend when macOS reports low free memory or growing swap.

```bash
.venv/bin/python -m gateway                      # http://127.0.0.1:8090, backends start lazily, stop on idle or SIGTERM
curl -N localhost:8090/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"stream":true,"messages":[{"role":"user","content":"hello"}]}'          # starts Qwen3-Coder on first use
curl localhost:8090/api/backends                 # state, idle countdown, memory estimate per backend
```

Add a model by adding a `[backends.<name>]` table to `gateway.toml` (adapters: `mlx_lm`, `jevstyle`, `openjev_nli`, `command`). Tests (no models needed, they use a fake backend): `.venv/bin/python -m unittest discover -s gateway/tests -t .`

## Next

[PLAN.md](PLAN.md): a long-lived local gateway that starts, stops and passivates (unloads when idle, reloads on demand) the models, extends the MCP interface with lifecycle tools, and serves the chat site and an admin site.
