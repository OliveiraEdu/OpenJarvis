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
| `deploy/docker/Dockerfile.lean` | Multi-stage ~600 MB image (no CUDA) |
| `deploy/docker/docker-compose.gpu.nvidia.yml` | Standalone compose: :9000, host-gateway, workspace mount, GPU |
| `deploy/docker/.env` / `.env.example` | Workspace host path + API key (gitignored / template) |
| `~/.config/openjarvis/llamaserver.conf` | Optional llama-server overrides (sourced, may not exist) |
