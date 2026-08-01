#!/usr/bin/env python3
"""Capture a reproducible environment fingerprint for the benchmark run.

Writes environment.json with: GPU info, driver, model hash, binary/library
hashes and build versions for BOTH llama.cpp builds, OpenJarvis version,
and the canonical server flags. Stdlib only.

Usage:
    fingerprint.py <output-dir> --config <config.env> [--container <id>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def sh(cmd: list[str], timeout: int = 60, env: dict | None = None) -> str:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env
        )
        return (r.stdout + r.stderr).strip()
    except Exception as exc:  # pragma: no cover
        return f"<error: {exc}>"


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def md5(path: str) -> str:
    h = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_config(path: str) -> dict[str, str]:
    cfg: dict[str, str] = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = os.path.expandvars(v.strip().strip('"').strip("'"))
    return cfg


def server_version(bin_path: str, libs_dir: str) -> str:
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = f"{libs_dir}:{env.get('LD_LIBRARY_PATH', '')}"
    return sh([bin_path, "--version"], timeout=30, env=env)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("output_dir", help="directory to write environment.json into")
    ap.add_argument("--config", default="config.env")
    ap.add_argument("--container", default=None)
    args = ap.parse_args()

    cfg = parse_config(args.config)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stock_dir = cfg.get("STOCK_DIR", os.path.expanduser("~/Git/llama.cpp"))
    custom_dir = cfg.get("CUSTOM_DIR", os.path.expanduser("~/Git/custom-llama-bin"))
    model = cfg.get("MODEL", "")

    stock_bin = os.path.join(stock_dir, "build/bin/llama-server")
    custom_bin = os.path.join(custom_dir, "bin/llama-server")

    env_info: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "gpu": {
            "query": sh(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total,driver_version",
                    "--format=csv,noheader",
                ],
                timeout=15,
            ),
        },
        "os": {
            "uname": sh(["uname", "-a"]),
            "cpu_count": str(os.cpu_count()),
        },
        "model": {
            "path": model,
            "size_bytes": str(Path(model).stat().st_size)
            if model and Path(model).exists()
            else "MISSING",
            "sha256": sha256(model) if model and Path(model).exists() else "MISSING",
        },
        "openjarvis": {
            "container": args.container or "",
        },
        "server_flags": " ".join(
            sh(
                ["bash", "-c", f"source {args.config} && echo ${{SERVER_FLAGS[*]}}"]
            ).split()
        ),
        "binaries": {
            "stock": {
                "path": stock_bin,
                "md5": md5(stock_bin) if Path(stock_bin).exists() else "MISSING",
                "version": server_version(
                    stock_bin, os.path.join(stock_dir, "build/bin")
                )
                if Path(stock_bin).exists()
                else "MISSING",
                "libs": {
                    p: md5(os.path.join(stock_dir, "build/bin", p))
                    for p in [
                        "libllama.so.0.0.9079",
                        "libggml-cuda.so.0.11.0",
                        "libggml-cpu.so.0.11.0",
                    ]
                },
            },
            "custom": {
                "path": custom_bin,
                "md5": md5(custom_bin) if Path(custom_bin).exists() else "MISSING",
                "version": server_version(custom_bin, os.path.join(custom_dir, "lib"))
                if Path(custom_bin).exists()
                else "MISSING",
                "libs": {
                    p: md5(os.path.join(custom_dir, "lib", p))
                    for p in [
                        "libllama.so.0.0.9079",
                        "libggml-cuda.so.0.11.0",
                        "libggml-cpu.so.0.11.0",
                    ]
                },
            },
        },
    }

    if args.container:
        env_info["openjarvis"]["version"] = sh(
            [
                "docker",
                "exec",
                args.container,
                "bash",
                "-c",
                "cd /app && uv run jarvis --version 2>&1 | tail -1",
            ],
            timeout=120,
        )

    with open(out_dir / "environment.json", "w") as fh:
        json.dump(env_info, fh, indent=2)
    print(f"[fingerprint] wrote {out_dir / 'environment.json'}")
    print(json.dumps(env_info, indent=2)[:2000])


if __name__ == "__main__":
    sys.exit(main())
