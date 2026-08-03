#!/usr/bin/env bash
set -euo pipefail

# ── OpenJarvis Docker Entrypoint ─────────────────────────────────────
# Single-pass runtime startup script with zero-configuration auth & proxy
# ─────────────────────────────────────────────────────────────────────

BLUE='\033[0;34m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'
BOLD='\033[1m'

info()  { echo -e "${BLUE}[info]${NC}   $*"; }
ok()    { echo -e "${GREEN}[ok]${NC}     $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}   $*"; }
fail()  { echo -e "${RED}[fail]${NC}   $*"; exit 1; }

CLEANUP_PIDS=()
cleanup() {
  echo ""
  info "Shutting down..."
  for pid in "${CLEANUP_PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  ok "Done."
}
trap cleanup EXIT INT TERM

# Ensure virtual environment is active and in PATH
export VIRTUAL_ENV="${VIRTUAL_ENV:-/app/.venv}"
export PATH="$VIRTUAL_ENV/bin:$PATH"

# Resolve repo root directory cleanly (/app)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -d "$SCRIPT_DIR/.." ] && [ -d "$SCRIPT_DIR/../frontend" ]; then
  REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
elif [ -d "/app/frontend" ]; then
  REPO_ROOT="/app"
else
  REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
fi

# Prefer the mounted workspace volume (host projects) as the default CWD so
# tools (shell_exec, code_interpreter, file_read) create/modify project files
# there. Fall back to the repo root when no workspace is mounted.
WORKSPACE_DIR="${WORKSPACE_DIR:-/workspace}"
if [ -d "$WORKSPACE_DIR" ]; then
  cd "$WORKSPACE_DIR"
  ok "Working directory set to workspace: $WORKSPACE_DIR"
else
  cd "$REPO_ROOT"
  ok "Working directory set to repo root: $REPO_ROOT"
fi

echo -e "${BOLD}"
echo "  ┌──────────────────────────────────┐"
echo "  │    OpenJarvis Container Boot     │"
echo "  └──────────────────────────────────┘"
echo -e "${NC}"

# Helper function to execute jarvis commands safely
run_jarvis() {
  if command -v jarvis &>/dev/null; then
    jarvis "$@"
  elif [ -f "$VIRTUAL_ENV/bin/jarvis" ]; then
    "$VIRTUAL_ENV/bin/jarvis" "$@"
  else
    python3 -m openjarvis "$@"
  fi
}

# ── 1. Check Llama Server on Docker Host ─────────────────────────────
info "Checking Llama server connection..."
LLAMA_HOST_URL="${ENGINE_LLAMACPP_HOST:-http://host.docker.internal:8080}"

if curl -sf "$LLAMA_HOST_URL/health" &>/dev/null || curl -sf "$LLAMA_HOST_URL/" &>/dev/null; then
  ok "Llama server found at $LLAMA_HOST_URL"
elif curl -sf "http://localhost:8080/health" &>/dev/null || curl -sf "http://localhost:8080/" &>/dev/null; then
  LLAMA_HOST_URL="http://localhost:8080"
  ok "Llama server found at $LLAMA_HOST_URL"
else
  warn "Could not reach Llama server at $LLAMA_HOST_URL. Continuing startup..."
fi

export ENGINE_DEFAULT="llamacpp"
export ENGINE_LLAMACPP_HOST="$LLAMA_HOST_URL"

# ── 2. Initialize Jarvis Config & Configure CORS ──────────────────────
# Initialize only on first boot (when config.toml is missing). The state dir
# is bind-mounted and user edits to ~/.openjarvis/config.toml (e.g. the
# [tools.web_search] provider toggle) must survive container restarts —
# `jarvis init --force` regenerates the file from defaults and would silently
# destroy them on every boot.
CONFIG_FILE="$HOME/.openjarvis/config.toml"
info "Initializing OpenJarvis configuration..."
if [ ! -f "$CONFIG_FILE" ]; then
  run_jarvis init --engine llamacpp --force --yes < /dev/null &>/dev/null \
    || run_jarvis init --engine llamacpp --force < /dev/null &>/dev/null \
    || true
fi

if [ -f "$CONFIG_FILE" ]; then
  if grep -q "\[engine\.llamacpp\]" "$CONFIG_FILE"; then
    sed -i '/\[engine\.llamacpp\]/,/\[/ s|#\? \?host = .*|host = "'"$LLAMA_HOST_URL"'"|' "$CONFIG_FILE"
  fi

  if grep -q "\[server\]" "$CONFIG_FILE"; then
    sed -i 's|cors_origins = .*|cors_origins = ["*"]|g' "$CONFIG_FILE"
  else
    echo -e "\n[server]\ncors_origins = [\"*\"]" >> "$CONFIG_FILE"
  fi
fi
ok "OpenJarvis initialized with host $LLAMA_HOST_URL and CORS enabled"

# Pin the default model to whatever llama-server actually serves (the id
# exposed by /v1/models). `jarvis init` writes a generic default (e.g.
# qwen3.5:2b) that never matches the host engine — without this, every command
# that relies on the default model fails with "model not found". Auto-adapts
# if the served model changes; falls back to the deployment model when the
# endpoint is unreachable.
MODEL_ID="$(curl -sf -m 5 "$LLAMA_HOST_URL/v1/models" 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null || true)"
MODEL_ID="${MODEL_ID:-Qwen3-8B-Q3_K_M.gguf}"
if grep -q "default_model" "$CONFIG_FILE"; then
  sed -i "s|default_model = .*|default_model = \"$MODEL_ID\"|" "$CONFIG_FILE"
elif grep -q "\[intelligence\]" "$CONFIG_FILE"; then
  sed -i '/\[intelligence\]/a default_model = "'"$MODEL_ID"'"' "$CONFIG_FILE"
else
  printf '\n[intelligence]\ndefault_model = "%s"\n' "$MODEL_ID" >> "$CONFIG_FILE"
fi
ok "Default model pinned to $MODEL_ID"

# Doctor-recommended security profile for a personal deployment.
if ! grep -q "profile =" "$CONFIG_FILE"; then
  printf '\n[security]\nprofile = "personal"\n' >> "$CONFIG_FILE"
fi

# ── 3. Establish Fixed Container API Key ─────────────────────────────
info "Configuring OpenJarvis API Key..."
API_KEY="${OPENJARVIS_API_KEY:-oj_sk_container_default_key_0000000000000000}"

# Register this key in backend storage
run_jarvis auth add-key "$API_KEY" &>/dev/null || true

export OPENJARVIS_API_KEY="$API_KEY"

# Bake the key into the frontend env file only when the source frontend exists
# (dev images). Lean images serve the pre-built SPA from the backend, so there
# is nothing to configure here.
if [ -d "$REPO_ROOT/frontend" ]; then
  export VITE_OPENJARVIS_API_KEY="$API_KEY"
  export VITE_API_KEY="$API_KEY"
  cat <<EOF > "$REPO_ROOT/frontend/.env.local"
VITE_API_BASE_URL=
VITE_OPENJARVIS_API_KEY=$API_KEY
VITE_API_KEY=$API_KEY
EOF
fi

ok "API Key configured: $OPENJARVIS_API_KEY"

# ── 4. Start Backend API Server (Port 9000) ──────────────────────────
# The backend refuses to start while the engine is unreachable (llama-server
# may be mid-restart, e.g. after a host reboot). Retry with backoff so a
# transient engine outage doesn't crash-loop the container (restart:
# unless-stopped) — otherwise every docker exec dies within seconds.
info "Starting backend API server on port 9000..."
BACKEND_OK=0
for attempt in 1 2 3 4 5; do
  run_jarvis serve --host 0.0.0.0 --port 9000 > /tmp/jarvis_backend.log 2>&1 &
  BACKEND_PID=$!
  CLEANUP_PIDS+=("$BACKEND_PID")
  # Poll health for up to ~15s per attempt
  for _ in 1 2 3 4 5; do
    sleep 3
    if kill -0 "$BACKEND_PID" 2>/dev/null && curl -sf "http://localhost:9000/health" >/dev/null 2>&1; then
      BACKEND_OK=1
      break 2
    fi
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
      break  # backend exited; try again with a fresh instance
    fi
  done
  if [ "$BACKEND_OK" = "1" ]; then
    break
  fi
  warn "Backend not healthy yet (attempt $attempt/5) — engine may still be starting"
  kill "$BACKEND_PID" 2>/dev/null || true
  sleep 2
done

if [ "$BACKEND_OK" != "1" ]; then
  echo -e "${RED}[fail] Backend failed to become healthy after 5 attempts. Logs below:${NC}"
  cat /tmp/jarvis_backend.log
  exit 1
fi
ok "Backend running at http://0.0.0.0:9000"

# ── 5. Start Frontend Server (Port 5173) ─────────────────────────────
# Only dev images ship node_modules + a frontend source tree. Lean images
# serve the pre-built SPA from the backend at :9000, so vite is skipped.
if command -v npm &>/dev/null && [ -d "$REPO_ROOT/frontend/node_modules" ]; then
  info "Starting frontend server on port 5173..."
  (
    cd "$REPO_ROOT/frontend" && \
    npm run dev -- --host 0.0.0.0 > /tmp/vite_frontend.log 2>&1
  ) &
  FRONTEND_PID=$!
  CLEANUP_PIDS+=("$FRONTEND_PID")
  sleep 3

  if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
    echo -e "${RED}[fail] Frontend crashed on startup. Logs below:${NC}"
    cat /tmp/vite_frontend.log
    exit 1
  fi
  ok "Frontend running at http://0.0.0.0:5173"
else
  info "No frontend dev tree (node_modules) — SPA is served by the backend at :9000. Skipping vite."
fi

echo ""
echo -e "${GREEN}${BOLD}  OpenJarvis Container Ready!${NC}"
echo ""
if command -v npm &>/dev/null && [ -d "$REPO_ROOT/frontend/node_modules" ]; then
  echo "  Chat UI:      http://localhost:5173"
else
  echo "  Chat UI:      http://localhost:9000 (SPA served by backend)"
fi
echo "  API:          http://localhost:9000"
echo "  API Key:      ${OPENJARVIS_API_KEY}"
echo "  Engine:       Llama Server ($LLAMA_HOST_URL)"
echo ""

wait
