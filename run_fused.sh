#!/usr/bin/env bash
# Run the two scoring workers ONE AT A TIME (they contend badly for the GPU when concurrent), resumable.
cd "$(dirname "$0")"
.venv-mlxjev/bin/python score_worker.py jev      > worker_jev.log 2>&1
/opt/homebrew/opt/mlx-lm/libexec/bin/python score_worker.py lm > worker_lm.log 2>&1
