#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-serve}" != "serve" ]]; then
  exec "$@"
fi

if [[ -f /config/backend/.env ]]; then
  cp /config/backend/.env /app/backend/.env
fi

configured_model=""
if [[ -f /app/backend/.env ]]; then
  configured_model="$(sed -n 's/^OLLAMA_MODEL=//p' /app/backend/.env | tail -n 1 | tr -d '\r')"
fi
model="${OLLAMA_MODEL:-${configured_model:-qwen2.5:7b-instruct}}"

ollama serve \
  > >(sed -u 's/^/[ollama] /') \
  2> >(sed -u 's/^/[ollama] /' >&2) &

ready=0
for _ in $(seq 1 120); do
  if curl -fsS http://127.0.0.1:11434 >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != "1" ]]; then
  echo "Ollama did not become ready within 120 seconds." >&2
  exit 1
fi

if ! ollama list | awk 'NR > 1 {print $1}' | grep -Fxq "$model"; then
  echo "First run: downloading $model (~5GB), this can take a while..."
  ollama pull "$model"
else
  echo "Using cached Ollama model: $model"
fi

echo "Starting Resume Tailor backend on http://0.0.0.0:8765"
exec uvicorn backend.app.main:app --host 0.0.0.0 --port 8765
