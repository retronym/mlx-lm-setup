# Emoji book: Alice annotated phrase by phrase

Run from the repo root (paths below are relative to it); the gateway must be running. The page is served by [`pipelines/dashboard_server.py`](../dashboard_server.py).

Deterministic code around scorers: spaCy cuts the text into phrases, a decision model and an LLM score every emoji, the scores are fused, and the page renders them as ruby text above each phrase, tinted by the "vibe colour" when the model is confident.

```mermaid
flowchart LR
  TXT["corpus/alice.txt<br/>Project Gutenberg"] --> WORDS["data/book_words.json<br/>4,280 words, chapters I–II"]
  WORDS --> CH["chunk_book.py<br/>spaCy chunker, 4–12 words<br/>0.1 s per phrase"]
  CH --> CHUNKS["data/book_chunks.json<br/>748 phrases"]
  CHUNKS --> JW["score_worker.py jev<br/>/api/decide · Jev-Style 2B · ~1 s per phrase"]
  CHUNKS --> LW["score_worker.py lm<br/>/api/score · Qwen3-Coder log P(emoji) · ~4.5 s per phrase"]
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

The workers call the gateway (`pipelines/gateway_client.py`, stdlib only), so they run under any `python3` and never load a model; run them one at a time, since they share the GPU. The chunker and the fusion step need spaCy and numpy from `.venv-jev`.

```bash
.venv-jev/bin/python pipelines/emoji_book/chunk_book.py        # phrase boundaries -> data/book_chunks.json
python3 pipelines/emoji_book/score_worker.py jev               # resumable; --fresh to restart
python3 pipelines/emoji_book/score_worker.py lm                # optional: the gateway evicts idle backends to fit the 18 GB scorer
.venv-jev/bin/python pipelines/emoji_book/emoji_book_fused.py --fresh     # combine -> data/book_events.jsonl (follows the workers live)
python3 pipelines/dashboard_server.py &                        # http://127.0.0.1:8766/book
```

`recolor.py` re-answers only the colour question for phrases already scored, so prompt wording can be iterated quickly. Questions and the context window live in `jev_attrs.py`, the LLM's few-shot prompt in `lm_prompt.py`, the emoji list in `emoji_vocab.py`.
