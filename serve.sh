#!/usr/bin/env bash
# Start the local OpenAI-compatible MLX server (localhost only).
# Usage: ./serve.sh [model] [port]
set -euo pipefail

MODEL="${1:-mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit}"
PORT="${2:-8080}"

exec mlx_lm.server --model "$MODEL" --host 127.0.0.1 --port "$PORT"
