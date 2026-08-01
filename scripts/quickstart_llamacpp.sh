#!/usr/bin/env bash
set -euo pipefail

export UV_PYTHON="3.11"
export UV_VENV_CLEAR="1"
export PATH="/root/.local/bin:$PATH"

# ── OpenJarvis Quickstart ─────────────────────────────────────────────
# One-command setup: installs deps, checks Llama Server connection on host,
# generates an API key, launches the backend API server (port 9000)
# and frontend (port 5173).
# ──────────────────────────────────────────────────────────────────────

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

# ── Navigate to repo root ────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

echo -e "${BOLD}"
echo "  ┌──────────────────────────────────┐"
echo "  │        OpenJarvis Quickstart     │"
echo "  └──────────────────────────────────┘"
echo -e "${NC}"

# ── 1. Check Python ──────────────────────────────────────────────────
info "Checking Python..."
if command -v python3 &>/dev/null; then
  PY_CMD="python3"
elif command -v python &>/dev/null; then
  PY_CMD="python"
else
  fail "Python 3 not found. Install from https://python.org"
fi
PY_VERSION=$("$PY_CMD" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ]; then
  ok "Python $PY_VERSION ($PY_CMD)"
else
  fail "Python 3.10+ required (found $PY_VERSION)"
fi

# ── 2. Check / install uv ───────────────────────────────────────────
info "Checking uv..."
if command -v uv &>/dev/null; then
  ok "uv $(uv --version 2>/dev/null | head -1)"
else
  warn "uv not found — installing..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  ok "uv installed"
fi

# ── 3. Check Node.js ────────────────────────────────────────────────
info "Checking Node.js..."
if command -v node &>/dev/null; then
  NODE_VERSION=$(node --version)
  NODE_MAJOR=$(echo "$NODE_VERSION" | sed 's/v//' | cut -d. -f1)
  if [ "$NODE_MAJOR" -ge 18 ]; then
    ok "Node.js $NODE_VERSION"
  else
    fail "Node.js 18+ required (found $NODE_VERSION). Install from https://nodejs.org"
  fi
else
  fail "Node.js not found. Install from https://nodejs.org"
fi

# ── 4. Check Llama Server on Docker Host ─────────────────────────────
info "Checking Llama server connection on host port 8080..."

LLAMA_HOST_URL="${ENGINE_LLAMACPP_HOST:-http://host.docker.internal:8080}"

if curl -sf "$LLAMA_HOST_URL/health" &>/dev/null || curl -sf "$LLAMA_HOST_URL/" &>/dev/null; then
  ok "Llama server found at $LLAMA_HOST_URL"
elif curl -sf "http://localhost:8080/health" &>/dev/null || curl -sf "http://localhost:8080/" &>/dev/null; then
  LLAMA_HOST_URL="http://localhost:8080"
  ok "Llama server found at $LLAMA_HOST_URL"
else
  fail "Could not connect to Llama server on port 8080 ($LLAMA_HOST_URL). Make sure llama-server is running on your host machine."
fi

# Set global environment settings
export ENGINE_DEFAULT="llamacpp"
export ENGINE_LLAMACPP_HOST="$LLAMA_HOST_URL"
export VITE_API_BASE_URL="http://localhost:9000"

# ── 5. Install Python dependencies ──────────────────────────────────
info "Installing Python dependencies..."
uv sync --extra desktop --extra tools-search --quiet 2>/dev/null \
  || uv sync --extra desktop --extra tools-search
ok "Python dependencies installed"

# ── 6. Build Rust extension ──────────────────────────────────────────
info "Building Rust extension..."
uv run maturin develop -m rust/crates/openjarvis-python/Cargo.toml --quiet 2>/dev/null \
  || uv run maturin develop -m rust/crates/openjarvis-python/Cargo.toml
ok "Rust extension built"

# ── 7. Install frontend dependencies ────────────────────────────────
info "Installing frontend dependencies..."
(cd frontend && npm install --silent 2>/dev/null || npm install)
ok "Frontend dependencies installed"

# ── 8. Initialize Jarvis Config & Update host explicitly ─────────────
info "Initializing OpenJarvis configuration..."
uv run jarvis init --engine llamacpp --force --yes < /dev/null &>/dev/null \
  || uv run jarvis init --engine llamacpp --force < /dev/null &>/dev/null \
  || true

CONFIG_FILE="$HOME/.openjarvis/config.toml"
if [ -f "$CONFIG_FILE" ]; then
  if grep -q "\[engine\.llamacpp\]" "$CONFIG_FILE"; then
    sed -i '/\[engine\.llamacpp\]/,/\[/ s|#\? \?host = .*|host = "'"$LLAMA_HOST_URL"'"|' "$CONFIG_FILE"
  fi
fi

ok "OpenJarvis initialized with host $LLAMA_HOST_URL"

# ── 9. Generate API Key ─────────────────────────────────────────────
info "Generating OpenJarvis API Key..."
RAW_KEY_OUTPUT=$(uv run jarvis auth generate-key 2>&1 || true)

# Extract key token starting with oj_sk_ or fallback parsing
API_KEY=$(echo "$RAW_KEY_OUTPUT" | grep -oE 'oj_sk_[a-zA-Z0-9_-]+' | tail -n1 || echo "")

if [ -z "$API_KEY" ]; then
  API_KEY=$(echo "$RAW_KEY_OUTPUT" | grep -oE '[a-zA-Z0-9_-]{32,}' | tail -n1 || echo "")
fi

if [ -z "$API_KEY" ]; then
  API_KEY="oj_sk_$(openssl rand -hex 16 2>/dev/null || echo "12345678901234567890123456789012")"
fi

export OPENJARVIS_API_KEY="$API_KEY"
export VITE_OPENJARVIS_API_KEY="$API_KEY"
export VITE_API_KEY="$API_KEY"
ok "API Key set: $OPENJARVIS_API_KEY"

# ── 10. Start Backend API (Port 9000) ────────────────────────────────
info "Starting backend API server on port 9000..."

if curl -sf http://localhost:9000/health &>/dev/null; then
  fail "An OpenJarvis server is already running on port 9000."
fi

uv run jarvis serve --host 0.0.0.0 --port 9000 > /tmp/jarvis_backend.log 2>&1 &
BACKEND_PID=$!
CLEANUP_PIDS+=("$BACKEND_PID")
sleep 3

if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
  echo -e "${RED}[fail] Backend crashed on startup. Logs below:${NC}"
  cat /tmp/jarvis_backend.log
  exit 1
fi
ok "Backend running at http://localhost:9000"

# ── 11. Start Frontend Dev Server (Port 5173) ────────────────────────
info "Starting frontend dev server on port 5173..."
(
  cd frontend && \
  VITE_API_BASE_URL="http://localhost:9000" \
  VITE_OPENJARVIS_API_KEY="$API_KEY" \
  VITE_API_KEY="$API_KEY" \
  npm run dev -- --host 0.0.0.0
) &>/dev/null &
CLEANUP_PIDS+=($!)
sleep 3
ok "Frontend running at http://localhost:5173"

# ── 12. Open Browser ────────────────────────────────────────────────
URL="http://localhost:5173"
info "Opening $URL ..."
case "$(uname -s)" in
  Darwin) open "$URL" ;;
  Linux)  xdg-open "$URL" 2>/dev/null || true ;;
  MINGW*|MSYS*|CYGWIN*) cmd /c start "" "$URL" 2>/dev/null || true ;;
  *)      true ;;
esac

echo ""
echo -e "${GREEN}${BOLD}  OpenJarvis is running!${NC}"
echo ""
echo "  Chat UI:      http://localhost:5173"
echo "  API:          http://localhost:9000"
echo "  API Key:      ${OPENJARVIS_API_KEY}"
echo "  Engine:       Llama Server ($LLAMA_HOST_URL)"
echo ""
echo "  Press Ctrl+C to stop all services."
echo ""

wait
