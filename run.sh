#!/bin/bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <bedrock-model-id> [host] [port]" >&2
  echo "Example: $0 anthropic.claude-3-5-sonnet-20240620-v1:0" >&2
  exit 1
fi

export BEDROCK_MODEL_ID="$1"
HOST="${2:-0.0.0.0}"
PORT="${3:-8000}"

RELOAD_FLAG=""
if [ "${RELOAD:-0}" = "1" ]; then
  RELOAD_FLAG="--reload"
fi

if [ -t 0 ]; then
  ORIG_STTY=$(stty -g)
  stty intr ^G
  trap 'stty "$ORIG_STTY"' EXIT INT TERM
  echo ">> Press Ctrl+G to quit (Ctrl+C is disabled while this server runs)"
fi

uvicorn app.main:app --host "$HOST" --port "$PORT" --timeout-graceful-shutdown 2 $RELOAD_FLAG
