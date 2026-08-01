#!/usr/bin/env python3
"""Host-side GPU power/util/temp sampler via nvidia-smi.

Writes a CSV (epoch-ts, power_w, gpu_util_pct, temp_c) every --interval
seconds until --seconds elapse. Stdlib only, so it runs anywhere.

Usage:
    power_sample.py out.csv --seconds 900 --interval 0.5
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import time


def sample() -> tuple[float, float, float]:
    out = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=power.draw,utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()
    parts = [p.strip() for p in out.split(",")]
    return float(parts[0]), float(parts[1]), float(parts[2])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("output", help="CSV output path")
    ap.add_argument("--seconds", type=float, default=60.0, help="sampling window (s)")
    ap.add_argument("--interval", type=float, default=0.5, help="sampling interval (s)")
    args = ap.parse_args()

    end = time.time() + args.seconds
    with open(args.output, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts", "power_w", "gpu_util_pct", "temp_c"])
        while time.time() < end:
            try:
                p, u, t = sample()
                writer.writerow(
                    [f"{time.time():.3f}", f"{p:.2f}", f"{u:.1f}", f"{t:.1f}"]
                )
            except Exception as exc:  # keep the window intact on transient failures
                writer.writerow([f"{time.time():.3f}", "ERR", str(exc), ""])
            # Flush each row so a SIGTERM (e.g. `kill $power_pid` from run.sh)
            # cannot lose buffered samples — the sampler is killed mid-window.
            fh.flush()
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
