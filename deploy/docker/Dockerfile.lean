# OpenJarvis lean image — production deployment for a host-managed llama.cpp
# engine (llama-server runs on the host, reached via host.docker.internal:8080).
#
# Deliberately NOT based on a CUDA image: nothing in the container links CUDA
# (the inference engine lives on the host). This drops the image from ~13 GB
# (legacy baked dev image) to ~1.5 GB while keeping the API + SPA fully
# functional. See Dockerfile.baked / Dockerfile.gpu.edu for the legacy variants.
#
# Base images are pinned to an immutable digest (in addition to a human-readable
# tag) so every build resolves the exact same layers — reproducible builds and
# safe rollbacks (#563).

# ── Stage 1: Build frontend SPA ───────────────────────────────────────────
FROM node:22.23.0-slim@sha256:d9f850096136edbc402debdd8729579a288aac64574ada0ff4db26b6ae58b0b2 AS frontend
# Public Supabase anon key for the savings leaderboard; empty by default so
# the image's leaderboard stays disabled (#589). Pass --build-arg to enable.
ARG OPENJARVIS_LEADERBOARD_PUBLIC_ANON=

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --ignore-scripts 2>/dev/null || npm install
COPY frontend/ .
RUN VITE_SUPABASE_ANON_KEY="${OPENJARVIS_LEADERBOARD_PUBLIC_ANON}" npm run build

# ── Stage 2: Build Python package ─────────────────────────────────────────
FROM python:3.12.13-slim-bookworm@sha256:76d4b7b6305788c6b4c6a19d6a22a3921bf802e9af4d5e1e5bd771208dba74bf AS builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.cargo/bin:${PATH}"

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
        sh -s -- -y --profile minimal --default-toolchain none && \
    rustup toolchain install 1.88 --profile minimal && \
    rustup default 1.88

WORKDIR /app

# Install dependencies from the committed lockfile (#567). `uv export --frozen`
# reads uv.lock as-is (no re-resolution) and emits a fully pinned, hash-verified
# requirements set; `--no-deps` then installs exactly that set. This is a
# separate layer from the source copy so dependency installs stay cached when
# only application code changes.
COPY pyproject.toml uv.lock README.md ./
RUN pip install --no-cache-dir uv && \
    uv export --frozen --no-dev --extra server --no-emit-project > requirements.txt && \
    uv pip install --system --no-deps -r requirements.txt && \
    uv pip install --system --no-deps "maturin>=1.12.6,<2"

# Copy the source and the non-src force-include paths (see pyproject
# [tool.hatch.build.targets.wheel.force-include]) before building the project.
# scripts/entrypoint.sh is the container boot script (baked so the image is
# self-contained; the legacy dev images mounted it from the repo).
COPY src/ src/
COPY rust/ rust/
COPY scripts/install scripts/install
COPY scripts/entrypoint.sh scripts/entrypoint.sh
COPY deploy/windows deploy/windows

# Copy built frontend into the server static directory
COPY --from=frontend /src/openjarvis/server/static src/openjarvis/server/static/

# Install the project itself without re-resolving dependencies.
RUN uv pip install --system --no-deps . && \
    maturin build --release \
        --manifest-path rust/crates/openjarvis-python/Cargo.toml \
        --interpreter python3 \
        --out /tmp/openjarvis-rust-wheel && \
    uv pip install --system --no-deps /tmp/openjarvis-rust-wheel/*.whl && \
    python3 -c "import openjarvis_rust; print('openjarvis_rust ok')" && \
    python3 -m pip uninstall -y maturin && \
    rm -rf /tmp/openjarvis-rust-wheel rust

# ── Stage 3: Runtime (minimal) ────────────────────────────────────────────
FROM python:3.12.13-slim-bookworm@sha256:76d4b7b6305788c6b4c6a19d6a22a3921bf802e9af4d5e1e5bd771208dba74bf

# curl: health checks + llama-server probing in entrypoint. ca-certificates:
# outbound API calls. Nothing else is needed at runtime.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local /usr/local
COPY --from=builder /app /app

# Shared workspace with the host: mounted over this path at runtime (see
# docker-compose.gpu.nvidia.yml). Must exist so shell_exec / code_interpreter
# working_dir validation passes; agents create projects here.
RUN mkdir -p /workspace && chmod 0775 /workspace

# Run as an unprivileged user (#565). uid/gid 1001 match the host user
# (eduardo) so files written into the shared workspace volume are owned by
# eduardo on the host. The app writes only to $HOME (config/cache/state),
# which is owned by this user.
RUN groupadd --gid 1001 openjarvis && \
    useradd --uid 1001 --gid openjarvis \
        --create-home --home-dir /home/openjarvis openjarvis && \
    chown openjarvis:openjarvis /workspace
ENV HOME=/home/openjarvis \
    OPENJARVIS_WORKSPACE=/workspace \
    PYTHONUNBUFFERED=1

WORKDIR /workspace
USER openjarvis

EXPOSE 9000

ENTRYPOINT ["/bin/bash", "-c", "/app/scripts/entrypoint.sh"]
