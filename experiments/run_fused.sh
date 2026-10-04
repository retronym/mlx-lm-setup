#!/usr/bin/env bash
# Run the two scoring workers ONE AT A TIME through the gateway (they contend for the GPU when concurrent), resumable.
cd "$(dirname "$0")/.."
python3 pipelines/emoji_book/score_worker.py jev > data/logs/worker_jev.log 2>&1
python3 pipelines/emoji_book/score_worker.py lm  > data/logs/worker_lm.log 2>&1
