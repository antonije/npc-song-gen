#!/usr/bin/env bash
# Ensure ACE-Step 1.5 is installed and its API is listening on ACESTEP_API_PORT.
# Linux (CUDA): pins to ACESTEP_CUDA_VISIBLE_DEVICES (default 0).
# macOS Apple Silicon: uses MLX LM backend (ACESTEP_LM_BACKEND=mlx).
set -euo pipefail

NPC_SONG_GEN="$(cd "$(dirname "$0")/.." && pwd)"
ACESTEP_DIR="${ACESTEP_DIR:-$NPC_SONG_GEN/vendor/ACE-Step-1.5}"
ACESTEP_REPO="${ACESTEP_REPO:-https://github.com/ACE-Step/ACE-Step-1.5.git}"
ACESTEP_HOST="${ACESTEP_API_HOST:-127.0.0.1}"
ACESTEP_PORT="${ACESTEP_API_PORT:-8001}"
ACESTEP_BASE="${ACESTEP_API_BASE:-http://${ACESTEP_HOST}:${ACESTEP_PORT}}"
LOG_FILE="${ACESTEP_LOG:-$NPC_SONG_GEN/vendor/acestep-api.log}"
PID_FILE="${ACESTEP_PID_FILE:-$NPC_SONG_GEN/vendor/acestep-api.pid}"
HEALTH_URL="${ACESTEP_BASE%/}/health"
WAIT_SECS="${ACESTEP_WAIT_SECS:-180}"

OS="$(uname -s)"
ARCH="$(uname -m)"

# Platform defaults (env still wins).
if [[ "$OS" == "Darwin" && "$ARCH" == "arm64" ]]; then
  PLATFORM="macos-arm64"
  export ACESTEP_LM_BACKEND="${ACESTEP_LM_BACKEND:-mlx}"
  export TOKENIZERS_PARALLELISM="${TOKENIZERS_PARALLELISM:-false}"
  # Unified memory is large on 48GB Macs; still skip XL preload for a lean start.
  export ACESTEP_CONFIG_PATH="${ACESTEP_CONFIG_PATH:-acestep-v15-sft}"
  export ACESTEP_CONFIG_PATH2="${ACESTEP_CONFIG_PATH2:-}"
  export ACESTEP_CONFIG_PATH3="${ACESTEP_CONFIG_PATH3:-}"
  export ACESTEP_LM_MODEL_PATH="${ACESTEP_LM_MODEL_PATH:-acestep-5Hz-lm-0.6B}"
  export ACESTEP_INIT_LLM="${ACESTEP_INIT_LLM:-true}"
  ACESTEP_CUDA_VISIBLE_DEVICES=""
elif [[ "$OS" == "Darwin" ]]; then
  PLATFORM="macos-intel"
  echo "WARNING: Intel Mac detected — MLX unavailable; using PyTorch LM backend (slow)."
  export ACESTEP_LM_BACKEND="${ACESTEP_LM_BACKEND:-pt}"
  export ACESTEP_CONFIG_PATH="${ACESTEP_CONFIG_PATH:-acestep-v15-sft}"
  export ACESTEP_CONFIG_PATH2="${ACESTEP_CONFIG_PATH2:-}"
  export ACESTEP_CONFIG_PATH3="${ACESTEP_CONFIG_PATH3:-}"
  export ACESTEP_LM_MODEL_PATH="${ACESTEP_LM_MODEL_PATH:-acestep-5Hz-lm-0.6B}"
  export ACESTEP_INIT_LLM="${ACESTEP_INIT_LLM:-true}"
  ACESTEP_CUDA_VISIBLE_DEVICES=""
else
  PLATFORM="linux-cuda"
  # Pin ACE-Step to the primary GPU. Demucs can use another via --demucs-device cuda:1
  ACESTEP_CUDA_VISIBLE_DEVICES="${ACESTEP_CUDA_VISIBLE_DEVICES:-0}"
  export ACESTEP_CONFIG_PATH="${ACESTEP_CONFIG_PATH:-acestep-v15-sft}"
  # Optional XL slots (env empty string disables). Default off to avoid VRAM crush on re-init.
  export ACESTEP_CONFIG_PATH2="${ACESTEP_CONFIG_PATH2:-}"
  export ACESTEP_CONFIG_PATH3="${ACESTEP_CONFIG_PATH3:-}"
  export ACESTEP_LM_MODEL_PATH="${ACESTEP_LM_MODEL_PATH:-acestep-5Hz-lm-0.6B}"
  export ACESTEP_INIT_LLM="${ACESTEP_INIT_LLM:-true}"
  export ACESTEP_LM_BACKEND="${ACESTEP_LM_BACKEND:-vllm}"
fi

health_ok() {
  curl -sf --max-time 3 "$HEALTH_URL" >/dev/null 2>&1
}

if health_ok; then
  echo "ACE-Step already healthy at $HEALTH_URL"
  echo "  platform=$PLATFORM  CONFIG=$ACESTEP_CONFIG_PATH  LM=$ACESTEP_LM_MODEL_PATH  backend=${ACESTEP_LM_BACKEND:-default}"
  echo "  (startup env applies on next restart)"
  exit 0
fi

if [[ ! -d "$ACESTEP_DIR/.git" ]]; then
  echo "Cloning ACE-Step into $ACESTEP_DIR …"
  mkdir -p "$(dirname "$ACESTEP_DIR")"
  git clone --depth 1 "$ACESTEP_REPO" "$ACESTEP_DIR"
fi

echo "Syncing ACE-Step deps (uv sync) …"
(
  cd "$ACESTEP_DIR"
  uv sync
)

mkdir -p "$(dirname "$LOG_FILE")"
echo "Starting acestep-api on ${ACESTEP_HOST}:${ACESTEP_PORT}"
echo "  platform=$PLATFORM"
echo "  DiT=${ACESTEP_CONFIG_PATH}  LM=${ACESTEP_LM_MODEL_PATH}  INIT_LLM=${ACESTEP_INIT_LLM}"
echo "  LM_BACKEND=${ACESTEP_LM_BACKEND:-default}"
if [[ -n "${ACESTEP_CUDA_VISIBLE_DEVICES}" ]]; then
  echo "  CUDA_VISIBLE_DEVICES=${ACESTEP_CUDA_VISIBLE_DEVICES}"
fi
(
  cd "$ACESTEP_DIR"
  if [[ -n "${ACESTEP_CUDA_VISIBLE_DEVICES}" ]]; then
    export CUDA_VISIBLE_DEVICES="$ACESTEP_CUDA_VISIBLE_DEVICES"
  fi
  export ACESTEP_CONFIG_PATH ACESTEP_LM_MODEL_PATH ACESTEP_INIT_LLM
  export ACESTEP_CONFIG_PATH2 ACESTEP_CONFIG_PATH3
  [[ -n "${ACESTEP_LM_BACKEND:-}" ]] && export ACESTEP_LM_BACKEND
  [[ -n "${TOKENIZERS_PARALLELISM:-}" ]] && export TOKENIZERS_PARALLELISM
  # Background; models may download on first request
  nohup uv run acestep-api --host "$ACESTEP_HOST" --port "$ACESTEP_PORT" \
    >"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
)
echo "  pid=$(cat "$PID_FILE")  log=$LOG_FILE"

echo "Waiting up to ${WAIT_SECS}s for $HEALTH_URL …"
deadline=$((SECONDS + WAIT_SECS))
while (( SECONDS < deadline )); do
  if health_ok; then
    echo "ACE-Step healthy at $HEALTH_URL ($PLATFORM)"
    exit 0
  fi
  sleep 2
done

echo "Timed out waiting for ACE-Step. Last log lines:" >&2
tail -n 40 "$LOG_FILE" >&2 || true
if [[ "$PLATFORM" == "macos-arm64" ]]; then
  echo >&2
  echo "Mac tip: first boot downloads models (~10GB). Raise wait with ACESTEP_WAIT_SECS=600" >&2
  echo "  Or start foreground to watch progress:" >&2
  echo "  cd $ACESTEP_DIR && ACESTEP_LM_BACKEND=mlx uv run acestep-api --port $ACESTEP_PORT" >&2
fi
exit 1
