#!/usr/bin/env bash
# =============================================================================
# start.sh: start the whole local proof of concept with ONE command.
#
#     ./start.sh            start everything, open the browser
#     ./start.sh --no-open  start everything, do not open the browser
#     ./start.sh --reload   restart the API automatically when you edit code in app/
#
# What it starts:
#
#     ┌────────────── your machine ──────────────────────────────────────────┐
#     │  Browser  ──►  FastAPI server :8000  ──►  Ollama :11434 (AI model)   │
#     │                 ├─ /ui   the web page (frontend)                     │
#     │                 └─ /v1   the API (backend)                           │
#     │                 saves to ./data/jobs/<job_id>/                       │
#     └──────────────────────────────────────────────────────────────────────┘
#
# The frontend is served BY the backend, so "both servers" are one process. The only
# other thing to start is Ollama, which runs the AI model in Docker.
#
# Steps (each one prints what it does):
#   1. check the tools exist (uv, docker, curl)
#   2. create .env from .env.example if missing
#   3. install Python dependencies (uv sync; quick when nothing changed)
#   4. start Ollama, unless something already answers on port 11434
#   5. wait until Ollama is ready
#   6. download the model if it is not there yet (once, 3.3 GB)
#   7. start the FastAPI server in the foreground and open http://localhost:8000
#
# Stop: press Ctrl+C. That stops the API. Ollama keeps running so the next start is fast;
# stop it with:  docker compose stop ollama
# =============================================================================

set -euo pipefail                   # stop on the first error, on unset variables, in pipes
cd "$(dirname "$0")"                # always run from the repository root

API_PORT=8000
OLLAMA_URL="http://localhost:11434"
OPEN_BROWSER=1
RELOAD_ARGS=()

for arg in "$@"; do
  case "$arg" in
    --no-open) OPEN_BROWSER=0 ;;
    --reload)  RELOAD_ARGS=(--reload --reload-dir app) ;;
    -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)"; exit 1 ;;
  esac
done

# Small helpers for readable output.
step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
fail() { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---- 1. Tools --------------------------------------------------------------------------
step "Checking tools"
command -v uv   >/dev/null || fail "uv is not installed. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
command -v curl >/dev/null || fail "curl is not installed. Install it: sudo apt-get install -y curl"
ok "uv and curl found"

# ---- 2. Settings -----------------------------------------------------------------------
step "Settings (.env)"
if [[ ! -f .env ]]; then
  cp .env.example .env
  ok "created .env from .env.example"
else
  ok ".env exists"
fi
# Read the model name from .env so the script pulls the model the server will use.
MODEL_NAME="$(grep -E '^SLM_MODEL_NAME=' .env | tail -1 | cut -d= -f2- || true)"
MODEL_NAME="${MODEL_NAME:-qwen3-vl:4b}"
MODEL_PROVIDER="$(grep -E '^SLM_MODEL_PROVIDER=' .env | tail -1 | cut -d= -f2- || true)"

# ---- 3. Python dependencies --------------------------------------------------------------
step "Python dependencies"
uv sync --quiet
ok "installed in .venv"

# ---- 4-6. The AI model -------------------------------------------------------------------
ollama_ready() { curl -fsS --max-time 2 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; }

if [[ "$MODEL_PROVIDER" == "fake" ]]; then
  step "AI model"
  ok "SLM_MODEL_PROVIDER=fake in .env: no model needed (results will have empty fields)"
else
  step "AI model (Ollama)"
  if ollama_ready; then
    ok "Ollama is already running on $OLLAMA_URL"
  else
    command -v docker >/dev/null || fail "Docker is not installed. See docs/local-setup.md section 2.1"
    docker info >/dev/null 2>&1 || fail "Docker is not running. Start it with:  sudo systemctl start docker   then run ./start.sh again"
    # The first time, Docker downloads the Ollama image (a few GB): progress is shown below.
    echo "    starting the ollama container (the first time this downloads the Ollama image)..."
    docker compose up -d ollama
    ok "started the ollama container"
    printf '    waiting for Ollama'
    for _ in $(seq 1 60); do
      ollama_ready && break
      printf '.'; sleep 1
    done
    echo
    ollama_ready || fail "Ollama did not start within 60 s. Check: docker compose logs ollama"
    ok "Ollama is ready"
  fi

  # Is the model downloaded? /api/tags lists the models Ollama has.
  if curl -fsS "$OLLAMA_URL/api/tags" | grep -q "\"$MODEL_NAME\""; then
    ok "model $MODEL_NAME is downloaded"
  else
    echo "    downloading $MODEL_NAME (about 3.3 GB, only the first time)..."
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'slm-fintech-ollama-1'; then
      docker compose exec ollama ollama pull "$MODEL_NAME"
    elif docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'slm-ollama'; then
      docker exec slm-ollama ollama pull "$MODEL_NAME"
    else
      # Ollama runs some other way: ask it to pull through its API.
      curl -fsS "$OLLAMA_URL/api/pull" -d "{\"name\": \"$MODEL_NAME\", \"stream\": false}" >/dev/null
    fi
    ok "model $MODEL_NAME downloaded"
  fi
fi

# ---- 7. The API + web page ---------------------------------------------------------------
step "Starting the server"
if curl -fsS --max-time 2 "http://localhost:$API_PORT/healthz" >/dev/null 2>&1; then
  fail "Something is already running on port $API_PORT. Stop it first (or it is this server from another terminal)."
fi

URL="http://localhost:$API_PORT"
cat <<EOF

    Web page   $URL           (upload a document here)
    API docs   $URL/docs      (try the API endpoints)
    Results    ./data/jobs/<job_id>/

    Press Ctrl+C to stop the server.

EOF

# Open the browser once the server answers (in the background, so the server can start).
if [[ "$OPEN_BROWSER" == 1 ]] && command -v xdg-open >/dev/null; then
  (
    for _ in $(seq 1 30); do
      curl -fsS --max-time 1 "$URL/healthz" >/dev/null 2>&1 && { xdg-open "$URL" >/dev/null 2>&1 || true; exit 0; }
      sleep 1
    done
  ) &
fi

# exec: replace this script with the server, so Ctrl+C goes straight to it.
# --host 127.0.0.1: only this machine can reach the server (it has no login).
exec uv run uvicorn app.asgi:app --host 127.0.0.1 --port "$API_PORT" "${RELOAD_ARGS[@]}"
