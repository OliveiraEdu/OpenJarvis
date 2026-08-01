#!/usr/bin/env python3
"""Fixed API workload against the OpenJarvis backend (:9000).

Sends a deterministic mix of requests through the REAL OpenJarvis API so the
telemetry database (consumed by `jarvis telemetry stats`) records an
identical workload for each llama-server binary.

Stdlib only (urllib). Writes a per-request CSV.

Usage:
    api_workload.py --base-url http://localhost:9000 --api-key KEY \
        --model Qwen3-8B-Q3_K_M.gguf --output requests.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

# Deterministic workload: 5 short Q&A, 5 paragraph generation,
# 2 multi-turn conversations (4 calls). 14 requests total.
WORKLOAD: list[tuple[str, list[dict[str, str]]]] = [
    ("short", [{"role": "user", "content": "What is 2+2?"}]),
    ("short", [{"role": "user", "content": "What is the capital of France?"}]),
    ("short", [{"role": "user", "content": "Name three primary colors."}]),
    ("short", [{"role": "user", "content": "What day comes after Monday?"}]),
    ("short", [{"role": "user", "content": "Is water wet?"}]),
    (
        "para",
        [
            {
                "role": "user",
                "content": "Write a short paragraph about artificial intelligence.",
            }
        ],
    ),
    (
        "para",
        [{"role": "user", "content": "Explain the water cycle in a short paragraph."}],
    ),
    (
        "para",
        [
            {
                "role": "user",
                "content": "Describe the benefits of reading books in a short paragraph.",
            }
        ],
    ),
    (
        "para",
        [
            {
                "role": "user",
                "content": "Write a short paragraph about the solar system.",
            }
        ],
    ),
    (
        "para",
        [
            {
                "role": "user",
                "content": "Explain why regular exercise is important in a short paragraph.",
            }
        ],
    ),
    (
        "multi",
        [
            {
                "role": "user",
                "content": "Hi, I'm planning a trip to Japan in spring. What should I pack?",
            },
            {
                "role": "assistant",
                "content": "Pack layers, a rain jacket, comfortable walking shoes, and an adapter for Japanese outlets.",
            },
            {
                "role": "user",
                "content": "What about cherry blossom viewing? Where should I go?",
            },
        ],
    ),
    (
        "multi",
        [
            {"role": "user", "content": "I want to start learning to cook. Any tips?"},
            {
                "role": "assistant",
                "content": "Start with simple recipes, prep ingredients before cooking, and keep your knives sharp.",
            },
            {"role": "user", "content": "What are three easy beginner dishes?"},
        ],
    ),
]


def call(
    base_url: str, api_key: str, model: str, messages: list[dict[str, str]]
) -> tuple[int, float, int, int]:
    payload = json.dumps(
        {"model": model, "messages": messages, "temperature": 0.7}
    ).encode()
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = json.loads(resp.read().decode())
            latency = time.time() - t0
            usage = body.get("usage", {})
            return (
                resp.status,
                latency,
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
            )
    except urllib.error.HTTPError as exc:
        latency = time.time() - t0
        return exc.code, latency, 0, 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:9000")
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--model", default="Qwen3-8B-Q3_K_M.gguf")
    ap.add_argument("--output", required=True)
    ap.add_argument(
        "--interval", type=float, default=1.0, help="seconds between requests"
    )
    args = ap.parse_args()

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "ts",
                "kind",
                "http_status",
                "latency_s",
                "prompt_tokens",
                "completion_tokens",
            ]
        )
        for kind, messages in WORKLOAD:
            status, latency, pt, ct = call(
                args.base_url, args.api_key, args.model, messages
            )
            writer.writerow(
                [f"{time.time():.3f}", kind, status, f"{latency:.3f}", pt, ct]
            )
            print(
                f"[api_workload] {kind:5s} status={status} latency={latency:.2f}s "
                f"prompt={pt} completion={ct}",
                flush=True,
            )
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
