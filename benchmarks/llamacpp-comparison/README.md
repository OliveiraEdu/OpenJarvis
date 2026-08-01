# OpenJarvis × llama.cpp binary comparison

Benchmarks **OpenJarvis** (running in Docker) against two different
`llama-server` binaries serving the same model:

| Variant | Binary | Libs |
|---|---|---|
| `stock` | `~/Git/llama.cpp/build/bin/llama-server` | alongside binary |
| `custom` | `~/Git/custom-llama-bin/bin/llama-server` | `~/Git/custom-llama-bin/lib` |

Both binaries are llama.cpp commit `f9cd456ea` (build 9079) but compiled
differently (different binary/lib hashes) — the comparison isolates the
effect of the build configuration on OpenJarvis performance.

## How it works

The harness uses OpenJarvis's own benchmarking surfaces:

1. **`uv run jarvis bench run`** — the engine microbenchmark (latency,
   throughput, energy benchmarks, n=10 default) run inside the Docker
   container against the configured `llamacpp` engine
   (`host.docker.internal:8080`).
2. **`uv run jarvis telemetry stats` / `export`** — aggregates the telemetry
   SQLite DB, which is populated by the **instrumented** path (the real API
   on `:9000`), not by `bench run`.

It layers a third, server-side measurement on top: llama.cpp's own
`/metrics` Prometheus endpoint (enabled via `--metrics`) for token counts and
throughput, plus host-side `nvidia-smi` sampling for GPU power.

### Phases

- **Phase 0 — fingerprint**: GPU, driver, model sha256, binary/lib hashes and
  build versions for both builds, OpenJarvis version, server flags →
  `results/<tag>/env/environment.json`.
- **Phase 1 — engine microbenchmark**, order **A/B/B/A** (stock, custom,
  custom, stock) to cancel thermal drift:
  - swap server on the same `:8080` port with identical flags (`--metrics`
    added),
  - 30 s idle power baseline,
  - per benchmark (`latency`, `throughput`, `energy`): host power sampling +
    `/metrics` snapshot + `jarvis bench run` inside the container,
  - bench JSONL pulled out of the container via `docker cp`.
- **Phase 2 — application-level**: per binary: clear telemetry, run a fixed
  12-request API workload through `:9000` (5 short Q&A, 5 paragraph
  generations, 2 multi-turn conversations), then capture
  `jarvis telemetry stats` and `telemetry export`.
- **Phase 3 — compare**: `compare.py` merges everything into
  `comparison_report.md` + `comparison.json`.

## Usage

```bash
cd benchmarks/llamacpp-comparison
# 1. review/edit config.env (container id, paths, API key, server flags)
./run.sh                       # default tag = timestamp
./run.sh my-comparison-01      # custom tag

# run only some phases (Phase 0 fingerprint + Phase 3 report always run)
PHASES=phase1 ./run.sh my-tag  # only the engine microbenchmark
PHASES=phase2 ./run.sh my-tag  # only the API workload + telemetry

# individual pieces (useful for debugging)
./server_ctl.sh status                     # which server is up
./server_ctl.sh start stock                # start one variant
./server_ctl.sh stop
python3 power_sample.py out.csv --seconds 5
python3 api_workload.py --base-url http://localhost:9000 --api-key "$KEY" --output r.csv
python3 compare.py results/<tag> --tag <tag>
```

Outputs land in `results/<tag>/` — the `results/` directory is gitignored.

## Fairness controls

- Same model file (single 4.12 GB GGUF), same server flags, same port.
- Same OpenJarvis build/container, same prompts, same sample counts.
- A/B/B/A ordering + 30 s idle baseline per server start.
- Single-flight requests (`--parallel 1`), GPU-bound workload
  (all layers offloaded, `--flash-attn on`).
- The bge-small embedding server on `:8081` is left untouched.

## Known limitations

- `jarvis bench run` does **not** write telemetry records (raw engine, no
  `InstrumentedEngine` wrapper) — that's why Phase 2 drives the real API.
- NVML is unavailable to *fresh* processes in the container (`nvidia-smi` and
  a new `pynvml` init fail), so the built-in energy benchmark reports zero
  energy; Phase 1 GPU power comes from host-side `nvidia-smi` instead.
  Energy values are estimates (mean W × mean latency) and include small
  orchestration overhead.
- The long-running API server holds a working NVML handle, so the Phase 2
  telemetry records carry **real** per-request GPU energy/power/util — this
  is the authoritative in-app energy comparison.
- `power_sample.py` flushes every row so SIGTERM mid-window (how `run.sh`
  stops it) never loses buffered samples.
- Bench sends `temperature=0.7`, `max_tokens=1024` (engine defaults); output
  length varies per sample — mitigated by the two-run A/B/B/A design.
- Per-request latency in the API workload includes the OpenJarvis agent
  system prompt (~2.2–3.1 k tokens injected per call), identical for both
  variants.
