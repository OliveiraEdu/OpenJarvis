# ─────────────────────────────────────────────────────────────────────────────
# OpenJarvis — developer + deployment Makefile
#
#   llama-*   manage the host llama-server  (scripts/llamaserver.sh, ctx-8192)
#   jarvis-*  manage the Jarvis container   (compose, openjarvis:lean, :9000)
#   setup/build/test/lint/format  dev workflow (unchanged)
#
# The llama-server runs on the HOST (reached by the container via
# host.docker.internal:8080). Jarvis runs in the compose container with the
# API + SPA on :9000 and the host workspace mounted at /workspace.

LLAMA := scripts/llamaserver.sh
COMPOSE := docker compose -f deploy/docker/docker-compose.gpu.nvidia.yml

.DEFAULT_GOAL := help

.PHONY: help
.PHONY: setup build test lint format
.PHONY: llama-start llama-stop llama-restart llama-status llama-health \
        llama-logs llama-logs-follow llama-config
.PHONY: jarvis-up jarvis-down jarvis-stop jarvis-start jarvis-restart \
        jarvis-rebuild jarvis-ps jarvis-logs jarvis-logs-follow \
        jarvis-shell jarvis-shell-root jarvis-exec jarvis-health
.PHONY: boot status

help: ## Show this help
	@echo "OpenJarvis targets:"
	@echo ""
	@echo "  dev        make setup | build | test | lint | format"
	@echo "  llama      make llama-{start,stop,restart,status,health,logs,logs-follow,config}"
	@echo "  jarvis     make jarvis-{up,down,stop,start,restart,rebuild,ps,logs,logs-follow,shell,shell-root,exec,health}"
	@echo "  combined   make boot   (llama-start + jarvis-up)"
	@echo "             make status (llama-status + jarvis-ps)"
	@echo "  exec       make jarvis-exec CMD='jarvis telemetry stats'"

# ── llama-server (host engine) ───────────────────────────────────────────────

llama-start: ## Start the host llama-server (ctx-8192), wait for health + warmup
	$(LLAMA) start

llama-stop: ## Stop the host llama-server
	$(LLAMA) stop

llama-restart: ## Restart the host llama-server
	$(LLAMA) restart

llama-status: ## llama-server state + Jarvis container connectivity
	$(LLAMA) status

llama-health: ## Print ok/down for llama-server (exit 0/1, scriptable)
	$(LLAMA) health

llama-logs: ## Tail last 50 lines of the llama-server log
	$(LLAMA) logs

llama-logs-follow: ## Follow the llama-server log
	$(LLAMA) logs -f

llama-config: ## Show the effective llama-server config and server flags
	$(LLAMA) config

# ── Jarvis container (compose, openjarvis:lean) ─────────────────────────────

jarvis-up: ## Create/start the Jarvis container (detached)
	$(COMPOSE) up -d

jarvis-down: ## Stop and remove the Jarvis container (workspace volume kept)
	$(COMPOSE) down

jarvis-stop: ## Stop the Jarvis container
	$(COMPOSE) stop

jarvis-start: ## Start an already-created Jarvis container
	$(COMPOSE) start

jarvis-restart: ## Restart the Jarvis container
	$(COMPOSE) restart

jarvis-rebuild: ## Rebuild the lean image and recreate the container
	$(COMPOSE) up -d --build

jarvis-ps: ## Container status
	$(COMPOSE) ps

jarvis-logs: ## Tail last 100 lines of container logs
	$(COMPOSE) logs --tail=100

jarvis-logs-follow: ## Follow container logs
	$(COMPOSE) logs -f --tail=100

jarvis-shell: ## Interactive shell in the container (openjarvis user)
	$(COMPOSE) exec jarvis bash

jarvis-shell-root: ## Interactive root shell in the container (debugging)
	$(COMPOSE) exec --user 0 jarvis bash

jarvis-exec: ## Run a command:  make jarvis-exec CMD='jarvis telemetry stats'
	@$(COMPOSE) exec -T jarvis $(CMD)

jarvis-health: ## Container API health check on :9000
	@curl -sf http://localhost:9000/health && echo "Jarvis :9000 OK" \
		|| { echo "Jarvis :9000 DOWN" >&2; exit 1; }

# ── combined ─────────────────────────────────────────────────────────────────

boot: llama-start jarvis-up ## Bring up the whole stack (llama-server + Jarvis)

status: ## Full status: llama-server + Jarvis container
	$(LLAMA) status
	$(COMPOSE) ps

# ── dev workflow (unchanged) ─────────────────────────────────────────────────

setup:
	uv sync --extra dev --extra framework-comparison --extra server

build:
	uv run maturin develop --manifest-path rust/crates/openjarvis-python/Cargo.toml

test: build
	uv run pytest tests/ -n auto -q --tb=short -m "not live and not cloud and not hub"

lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/

format:
	uv run ruff format src/ tests/
