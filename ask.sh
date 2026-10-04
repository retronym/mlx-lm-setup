#!/usr/bin/env bash
# Send a one-shot prompt to the local gateway (starts the model on first use) and print the reply.
# Usage: ./ask.sh "prompt" [port]      (port 8090 = the gateway; 8080 = a standalone ./serve.sh)
#        cat file.scala | ./ask.sh "Summarize this file"
set -euo pipefail

PROMPT="${1:?usage: ask.sh \"prompt\" [port]}"
PORT="${2:-8090}"
MODEL="${MLX_MODEL:-mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit}"

# Append stdin (if piped) to the prompt.
if [ ! -t 0 ]; then
  PROMPT="$PROMPT"$'\n\n'"$(cat)"
fi

jq -n --arg model "$MODEL" --arg p "$PROMPT" \
  '{model: $model, messages: [{role: "user", content: $p}], max_tokens: 2048}' |
  curl -sS "http://127.0.0.1:$PORT/v1/chat/completions" \
    -H 'Content-Type: application/json' -d @- |
  jq -r '.choices[0].message.content'
