# GPU Host Stack Runbook (llama-server on host + lean container)

Runbook for this deployment: a lean Docker container running the OpenJarvis API
and SPA, with the inference engine (`llama-server`) running **on the host**
because it needs direct GPU/CUDA access. Everything is driven from the repo
root via the Makefile.

## Architecture

```
┌────────────────────────── HOST ──────────────────────────┐
│  llama-server  (Qwen3-8B, ctx-8192)   :8080   ← engine   │
│    managed by scripts/llamaserver.sh (Makefile llama-*)  │
│                                                          │
│  ~/Git/openjarvis-workspace  ←── mounted as ──→  /workspace
└──────────────────────────────────────────────────────────┘
                        │ host.docker.internal:host-gateway
                        ▼
┌──────────────────── CONTAINER (openjarvis:lean) ─────────┐
│  Jarvis API + SPA   :9000   ← what you talk to           │
│  compose: deploy/docker/docker-compose.gpu.nvidia.yml    │
└──────────────────────────────────────────────────────────┘
```

**The one rule to remember:** the **engine lives on the host** (it needs the GPU
and CUDA directly), the **app lives in the container** (pure Python, no CUDA).
The container reaches the engine at `host.docker.internal:8080`. "The system is
up" always means **both** are running.

Key decisions baked into this stack:

- Engine on the host (NOT in the image) — the image stays ~600 MB instead of
  13 GB, and CUDA/driver updates never force an image rebuild.
- `--add-host=host.docker.internal:host-gateway` is **required** — without it
  the container cannot reach the host engine and `jarvis serve` exits on startup.
- ctx-8192 is the adopted production config (ctx-8k experiment, 2026-08-01:
  −20% latency / +21% throughput vs 20480; offloads 32/37 layers).
- `--perf`/`--metrics` are kept so server-side throughput cross-checks and
  monitoring keep working.
- The deprecated bge embedding server (`:8081`) is intentionally NOT managed.
- Container runs non-root (uid/gid 1001 = host user), workspace bind-mounted.
- No autostart after reboot — start manually with `make boot`.

## Quick path (everything already set up)

```bash
cd ~/Git/OpenJarvis
make boot        # llama-start + jarvis-up, in dependency order
make status      # llama state + container state, one shot
```

## Full walkthrough

### 0. Prerequisites — verify once

```bash
nvidia-smi                                          # GPU driver + CUDA visible
docker info --format '{{.ServerVersion}}'           # Docker daemon running
ls ~/Git/llama.cpp/models/Qwen3-8B-Q3_K_M.gguf      # model file present
ls ~/Git/openjarvis-workspace                       # host workspace (from setup)
```

Defaults baked into `scripts/llamaserver.sh`: binary
`~/Git/llama.cpp/build/bin/llama-server`, model
`~/Git/llama.cpp/models/Qwen3-8B-Q3_K_M.gguf`. Override with `OJ_LLAMA_BIN` /
`OJ_LLAMA_MODEL` if they ever move.

### 1. First time only: build the image

```bash
make jarvis-rebuild    # docker compose up -d --build
```

Builds `openjarvis:lean` (multi-stage, ~600 MB). Only needed once, or after
changing `deploy/docker/Dockerfile.lean` / `scripts/entrypoint.sh`. If you run
`make boot` before building, compose auto-builds the missing image anyway —
`jarvis-rebuild` just forces it.

### 2. Start the engine (host llama-server)

```bash
make llama-start
```

What it does:

1. Checks the binary and model exist, refuses otherwise.
2. Starts `llama-server` with the ctx-8192 production flags (threads 12/8,
   flash-attn, kv-offload, q8_0 cache) via `nohup`, logging to
   `logs/llamaserver.log`.
3. Polls `http://localhost:8080/health` for up to 120 s (first load must pull
   ~8 GB of weights into VRAM).
4. Sends one tiny **warmup request** on healthy (primes CUDA kernels so the
   first real prompt is fast).
5. Reports whether it sees the Jarvis container.

Expected output: `healthy after ~Xs (pid NNNN)` and `offloaded 30/37 layers`.

### 3. Start the container (Jarvis API + SPA)

```bash
make jarvis-up
```

Compose creates the container with:

- port 9000 published (API + SPA)
- `host.docker.internal:host-gateway` so it can reach llama
- workspace `~/Git/openjarvis-workspace` mounted at `/workspace`
  (configurable via `OPENJARVIS_WORKSPACE_HOST` in `deploy/docker/.env`)
- persistent Jarvis state `~/.openjarvis` mounted at
  `/home/openjarvis/.openjarvis` (configurable via `OPENJARVIS_STATE_HOST`) —
  memory/knowledge DBs, agents, and skills survive `docker compose down`
- GPU reservation (nvidia runtime)

The container's `scripts/entrypoint.sh` then boots from `/workspace`, waits for
the backend, and **retries against a not-yet-ready llama-server with backoff
(~90 s engine-outage tolerance)** — so starting the container before llama
finishes loading recovers instead of crash-looping.

### 4. Verify everything

```bash
make status                # llama: RUNNING + health OK + GPU; container: up + :9000 OK
make llama-health          # prints "ok" (exit 0) or "down" (exit 1)
make jarvis-health         # curl :9000/health — "Jarvis :9000 OK"
```

End-to-end smoke test from the host:

```bash
curl -s http://localhost:8080/health                          # engine
curl -s http://localhost:9000/health                          # container API
curl -s http://localhost:9000/v1/chat/completions \           # full stack
  -H "Authorization: Bearer oj_sk_container_default_key_0000000000000000" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen3-8B-Q3_K_M.gguf","messages":[{"role":"user","content":"hi"}],"max_tokens":8}'
```

The last one should return JSON with `"content"` filled in (default dev key —
overridden by `OPENJARVIS_API_KEY` if you set one).

### 5. Use it

- **Browser UI (SPA):** http://localhost:9000
- **API:** http://localhost:9000 (OpenAI-compatible)
- **Inside the container:** `make jarvis-shell` (non-root user),
  `make jarvis-shell-root` (debugging only)
- **One-off commands:** `make jarvis-exec CMD='jarvis telemetry stats'`
- **Workspace:** files created inside `/workspace` appear in
  `~/Git/openjarvis-workspace`, owned by the host user (uid 1001).

### 6. On-demand market research (`scripts/research.sh`)

An autonomous IT-market research analyst that produces a structured, sourced,
math-consistent markdown report on demand (it is **never** scheduled).

```bash
./scripts/research.sh "Subject: AI infrastructure market | Scope: global, 2025-2030"
./scripts/research.sh "Subject: RISC-V CPUs | Scope: Europe, 2024-2028"
```

The topic string is free-form; include a subject and a scope (region, years,
segment) for focused results. The agent is **one phase at a time** because the
deployment model (Qwen3-8B-Q3_K_M, the max that fits the 6 GB GPU at ctx-8192)
tends to shortcut big open-ended tasks — each phase produces a checked artifact
and the pipeline fails loudly rather than accepting an empty result:

| Phase | Prompt | Artifact (must appear) | Gate |
|---|---|---|---|
| 1 — GATHER | live-web searches + page fetches, facts with source/date/URL saved incrementally | `findings.md` | ≥2 `web_search` calls |
| 2 — VERIFY | every CAGR/projection/share recomputed with the calculator tool | `numbers.md` | ≥1 `calculator` call + table validator |
| 3a — REPORT pt 1 | Title / Introduction / Executive Summary / Detailed Analysis written in **sequential chunks** | `report.md` | Intro+ExecSummary+DetailedAnalysis present |
| 3b — REPORT pt 2 | Conclusions / Sources & References / Confidence Assessment appended | `report.md` | all six sections + ≥1 URL |

Details worth knowing:

- **One phase per agent invocation**, ≤3 attempts each; artifact checks run on
  the host against the bind-mounted workspace (no docker/make quoting needed).
- **Large `file_write` calls are forbidden**: a single big write breaks the
  tool-call JSON grammar and kills the turn (llama.cpp HTTP 500). The template
  forces chunked writes of ~1500 chars.
- **`web_search` output is size-capped** (engine patch) so 5+ searches do not
  blow the 8K context window; use a URL as the query to fetch a page.
- **Search provider is configurable.** The `web_search` tool uses **DuckDuckGo
  by default** (free, no API key) and never contacts Tavily unless you opt in.
  To enable the Tavily API:
  1. Save a key: `jarvis tools credentials save web_search TAVILY_API_KEY <key>`
     (or set `TAVILY_API_KEY` in the container env).
  2. Add to `~/.openjarvis/config.toml`:
     ```toml
     [tools.web_search]
     provider = "tavily"
     ```
  3. Restart the stack (`make jarvis-restart`) — provider is read at startup.
  With Tavily enabled, a missing/expired key or an API error falls back to
  DuckDuckGo automatically. To disable again, set `provider = "duckduckgo"`.
- Each run **propagates the current template `system_prompt`** into the managed
  agent (agents.db bakes the prompt at creation) and resets `summary_memory` —
  this is what keeps prompt fixes effective without recreating the agent.
- Reports land in the container workspace `/workspace/<slug>/` → host
  `~/Git/openjarvis-workspace/<slug>/`:
  `findings.md`, `numbers.md`, `report.md`.
- The final report is structurally verified, but the 8B model is not a senior
  analyst — **skim the report before publishing** (check the Sources URLs are
  real and the numbers in `report.md` match `numbers.md`).

## Shutting down

```bash
make jarvis-stop    # stop container (keeps it)
make llama-stop     # stop engine (SIGTERM, SIGKILL fallback)
```

- `make jarvis-down` — also **removes** the container, keeps the workspace
  volume and the image. Use for a clean slate.
- `make jarvis-rebuild` — recreate after image changes.

## After a reboot

Nothing autostarts by design. After any reboot:

```bash
make boot
```

llama died with the reboot; `make boot` restarts it (waits for health) and then
brings the container up. Order is handled: llama first, container second.

## Troubleshooting

| Symptom | Check |
|---|---|
| `make jarvis-health` fails | Almost always llama is down (backend won't serve without an engine). Run `make llama-status` first. |
| llama won't start | `make llama-logs` (log tail). `make llama-config` shows effective bin/model/flags. Common: wrong path, port 8080 taken. |
| Container crash-looping | `make jarvis-logs-follow`. Entrypoint retries ~90 s; if it still fails, llama wasn't up in time. Start order matters: llama first. |
| Root-owned workspace files | Normal only if you used `jarvis-shell-root`. Use `jarvis-shell` (non-root) for regular work. |
| Slow first response after boot | Normal-ish: warmup happens on `llama-start`, so first chat should be fast. If you see a 10 s first token, the warmup curl didn't complete — harmless. |
| `web_search` ignores a Tavily key | Provider defaults to `duckduckgo` — Tavily is opt-in via `[tools.web_search] provider = "tavily"` in `~/.openjarvis/config.toml`, read at server start (restart required). |
| Unknown `make` target | `make help` lists everything. |

## Reference

### Makefile targets

| Group | Targets |
|---|---|
| dev | `setup`, `build`, `test`, `lint`, `format` |
| llama (host engine) | `llama-start`, `llama-stop`, `llama-restart`, `llama-status`, `llama-health`, `llama-logs`, `llama-logs-follow`, `llama-config` |
| jarvis (container) | `jarvis-up`, `jarvis-down`, `jarvis-stop`, `jarvis-start`, `jarvis-restart`, `jarvis-rebuild`, `jarvis-ps`, `jarvis-logs`, `jarvis-logs-follow`, `jarvis-shell`, `jarvis-shell-root`, `jarvis-exec`, `jarvis-health` |
| combined | `boot` (llama-start + jarvis-up), `status` (llama-status + jarvis-ps) |

### Configuration override chain

`llamaserver.sh`: defaults → `OJ_LLAMA_*` env vars → `~/.config/openjarvis/llamaserver.conf`
(sourced if present). Container side reads `deploy/docker/.env`
(`OPENJARVIS_WORKSPACE_HOST`, API key); the real `.env` is gitignored.

### Key files

| Path | Purpose |
|---|---|
| `Makefile` | All stack operations (`make boot` …) |
| `scripts/llamaserver.sh` | Host engine lifecycle + status + config (owner of ctx-8192 flags) |
| `scripts/entrypoint.sh` | Container boot: workspace CWD, backend health, engine retry backoff |
| `scripts/research.sh` | On-demand market research pipeline (4 gated phases, see §6) |
| `deploy/docker/Dockerfile.lean` | Multi-stage ~600 MB image (no CUDA) |
| `deploy/docker/docker-compose.gpu.nvidia.yml` | Standalone compose: :9000, host-gateway, workspace mount, GPU |
| `deploy/docker/.env` / `.env.example` | Workspace host path + API key + state dir (gitignored / template) |
| `~/.openjarvis` | Persistent Jarvis state (memory/knowledge DBs, agents, skills) mounted into the container |
| `~/.config/openjarvis/llamaserver.conf` | Optional llama-server overrides (sourced, may not exist) |
