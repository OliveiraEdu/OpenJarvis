---
title: Deployment
description: Deploy OpenJarvis in production environments
---

# Deployment

OpenJarvis supports multiple deployment strategies for different environments
and scales.

## Docker

The recommended way to deploy OpenJarvis in production. Multi-stage builds
with CPU and GPU (NVIDIA CUDA, AMD ROCm) variants.

[:octicons-arrow-right-24: Docker deployment](docker.md)

## GPU host stack (llama-server on host + lean container)

A deployment where the inference engine runs on the host (direct GPU access)
while the Jarvis API/SPA run in a slim ~600 MB container, managed from the repo
root via the Makefile (`make boot`).

[:octicons-arrow-right-24: GPU host runbook](gpu-host-runbook.md)

## systemd (Linux)

Run OpenJarvis as a managed system service on Linux servers.

[:octicons-arrow-right-24: systemd setup](systemd.md)

## launchd (macOS)

Register OpenJarvis as a launch agent on macOS.

[:octicons-arrow-right-24: launchd setup](launchd.md)

## API Server

Run OpenJarvis as an OpenAI-compatible HTTP server via `jarvis serve`.

[:octicons-arrow-right-24: API server guide](api-server.md)
