#!/usr/bin/env bash
# Wait for each checkpoint, benchmark it (one process each), then print the comparison report.
cd "$(dirname "$0")"
PY=.venv-jev/bin/python
until [ -f data/bench_qwen3.5-4b-nli-v5.json ]; do sleep 5; done
for sub in qwen3.5-2b-nli-v5 qwen3.5-0.8b-nli-v2s-long; do
  until [ -f models/openjev/$sub/model.safetensors ]; do sleep 5; done
  sleep 3
  JEV_SUBFOLDER=$sub $PY bench_emoji.py run > bench_$sub.log 2>&1
done
$PY bench_emoji.py report
