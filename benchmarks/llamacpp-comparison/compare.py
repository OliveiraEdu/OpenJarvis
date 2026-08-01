#!/usr/bin/env python3
"""Merge raw benchmark outputs into a stock-vs-custom comparison report.

Reads a run directory produced by run.sh and writes:
  comparison_report.md   — human-readable Markdown tables
  comparison.json        — structured results for further analysis

Usage:
    compare.py <run_dir> [--tag TAG]
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from pathlib import Path

BENCHES = ["latency", "throughput", "energy"]
VARIANT_RUNS = {"stock": ["stock_1", "stock_4"], "custom": ["custom_2", "custom_3"]}


# ── helpers ────────────────────────────────────────────────────────────────


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load_windows(path: Path) -> dict[tuple[str, str], tuple[float, float]]:
    windows: dict[tuple[str, str], tuple[float, float]] = {}
    if not path.exists():
        return windows
    with open(path) as fh:
        for line in fh:
            parts = line.strip().split("\t")
            if len(parts) == 4:
                windows[(parts[0], parts[1])] = (float(parts[2]), float(parts[3]))
    return windows


def load_power_csv(path: Path, window: tuple[float, float] | None = None) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                ts = float(row["ts"])
            except ValueError:
                continue
            if window and not (window[0] <= ts <= window[1]):
                continue
            try:
                power = float(row["power_w"])
                util = float(row["gpu_util_pct"])
                temp = float(row["temp_c"])
            except ValueError:
                continue
            rows.append({"ts": ts, "power_w": power, "util": util, "temp": temp})
    return rows


def mean(values: list[float]) -> float:
    return statistics.mean(values) if values else 0.0


def parse_metrics_file(path: Path) -> dict[str, float]:
    """Parse llama.cpp /metrics output: lines like `llamacpp:name value`."""
    metrics: dict[str, float] = {}
    if not path.exists():
        return metrics
    pat = re.compile(r"^llamacpp:(\w+)\s+([\d.eE+-]+)")
    with open(path) as fh:
        for line in fh:
            m = pat.match(line.strip())
            if m:
                try:
                    metrics[m.group(1)] = float(m.group(2))
                except ValueError:
                    pass
    return metrics


def delta_pct(stock: float, custom: float) -> float:
    if stock == 0:
        return 0.0
    return (custom - stock) / stock * 100.0


def fmt(v: float | None, digits: int = 3) -> str:
    return f"{v:.{digits}f}" if v is not None else "—"


def pct_str(d: float) -> str:
    return f"{d:+.1f}%"


def variant_mean(per_variant: dict, variant: str, bench: str, key: str) -> float:
    """Mean of `key` across the two runs of one variant for one bench."""
    vals: list[float] = []
    for run_tag, run in per_variant[variant]["runs"].items():
        if key in run["bench"].get(bench, {}):
            vals.append(run["bench"][bench][key])
    return mean(vals)


def variant_power_mean(per_variant: dict, variant: str, bench: str, key: str) -> float:
    vals: list[float] = []
    for run_tag, run in per_variant[variant]["runs"].items():
        if bench in run["power"] and key in run["power"][bench]:
            vals.append(run["power"][bench][key])
    return mean(vals)


def variant_idle_mean(per_variant: dict, variant: str) -> float:
    vals: list[float] = []
    for run_tag, run in per_variant[variant]["runs"].items():
        if run.get("idle_power_w") is not None:
            vals.append(run["idle_power_w"])
    return mean(vals)


# ── phase 1: engine microbenchmark ─────────────────────────────────────────


def aggregate_run_metrics(run_dir: Path, run_tag: str) -> dict[str, dict[str, float]]:
    """Return {bench: {metric: value}} for one run tag (stock_1, custom_2, ...)."""
    result: dict[str, dict[str, float]] = {}
    for bench in BENCHES:
        for candidate in (
            run_dir / "phase1" / run_tag / f"{bench}.jsonl",
            run_dir / "phase1" / "container_bench" / run_tag / f"{bench}.jsonl",
        ):
            rows = load_jsonl(candidate)
            if rows:
                result[bench] = dict(rows[0].get("metrics", {}))
                result[bench]["_samples"] = float(rows[0].get("samples", 0))
                result[bench]["_errors"] = float(rows[0].get("errors", 0))
                break
    return result


def gather_phase1(run_dir: Path) -> dict:
    windows = load_windows(run_dir / "phase1" / "windows.tsv")
    per_variant: dict[str, dict] = {}
    for variant, run_tags in VARIANT_RUNS.items():
        runs: dict[str, dict] = {}
        for run_tag in run_tags:
            metrics = aggregate_run_metrics(run_dir, run_tag)
            idle_power = None
            power_rows = load_power_csv(
                run_dir / "phase1" / f"{run_tag}_idle.csv",
                windows.get((run_tag, "idle")),
            )
            if power_rows:
                idle_power = mean([r["power_w"] for r in power_rows])
            run: dict = {
                "bench": metrics,
                "idle_power_w": idle_power,
                "power": {},
                "server_metrics": {},
            }
            for bench in BENCHES:
                rows = load_power_csv(
                    run_dir / "phase1" / f"{run_tag}_{bench}_power.csv",
                    windows.get((run_tag, bench)),
                )
                if rows:
                    run["power"][bench] = {
                        "mean_w": mean([r["power_w"] for r in rows]),
                        "mean_util_pct": mean([r["util"] for r in rows]),
                        "mean_temp_c": mean([r["temp"] for r in rows]),
                        "window_s": rows[-1]["ts"] - rows[0]["ts"]
                        if len(rows) > 1
                        else 0.0,
                    }
                s = parse_metrics_file(
                    run_dir / "phase1" / f"{run_tag}_{bench}_metrics_start.txt"
                )
                e = parse_metrics_file(
                    run_dir / "phase1" / f"{run_tag}_{bench}_metrics_end.txt"
                )
                run["server_metrics"][bench] = {
                    "start": s,
                    "end": e,
                    "delta_prompt_tokens": e.get("prompt_tokens_total", 0)
                    - s.get("prompt_tokens_total", 0),
                    "delta_predicted_tokens": e.get("tokens_predicted_total", 0)
                    - s.get("tokens_predicted_total", 0),
                    "delta_prompt_seconds": e.get("prompt_seconds_total", 0)
                    - s.get("prompt_seconds_total", 0),
                    "delta_predicted_seconds": e.get(
                        "tokens_predicted_seconds_total", 0
                    )
                    - s.get("tokens_predicted_seconds_total", 0),
                }
            runs[run_tag] = run
        per_variant[variant] = {"runs": runs}
    return per_variant


def phase1_report(per_variant: dict) -> list[str]:
    lines: list[str] = []
    lines.append("## 1. Engine microbenchmark (`jarvis bench run`, n=10, warmup=0)")
    lines.append("")
    lines.append(
        "Each variant ran twice (A/B/B/A order); values below are the mean across the two runs."
    )
    lines.append("")

    def metric_row(
        bench: str, label: str, key: str, digits: int = 3, to_ms: bool = False
    ) -> str:
        s = variant_mean(per_variant, "stock", bench, key)
        c = variant_mean(per_variant, "custom", bench, key)
        if to_ms:
            s, c = s * 1000, c * 1000
        d = delta_pct(s, c) if s else 0.0
        return f"| {label} | {fmt(s, digits)} | {fmt(c, digits)} | {pct_str(d)} |"

    lines.append("### latency (per-call, short prompts)")
    lines.append("| metric | stock | custom | Δ |")
    lines.append("|---|---|---|---|")
    for key, label, digits, to_ms in [
        ("mean_latency", "mean (ms)", 1, True),
        ("p50_latency", "p50 (ms)", 1, True),
        ("p95_latency", "p95 (ms)", 1, True),
    ]:
        lines.append(metric_row("latency", label, key, digits, to_ms))

    lines.append("")
    lines.append("### throughput (paragraph generation)")
    lines.append("| metric | stock | custom | Δ |")
    lines.append("|---|---|---|---|")
    for key, label, digits in [
        ("mean_tokens_per_second", "tokens/s (mean)", 1),
        ("p50_tokens_per_second", "tokens/s (p50)", 1),
        ("p95_tokens_per_second", "tokens/s (p95)", 1),
        ("total_tokens", "total tokens", 0),
    ]:
        lines.append(metric_row("throughput", label, key, digits))

    lines.append("")
    lines.append("### energy / efficiency (host nvidia-smi, GPU-level)")
    lines.append("| metric | stock | custom | Δ |")
    lines.append("|---|---|---|---|")

    s_idle = variant_idle_mean(per_variant, "stock")
    c_idle = variant_idle_mean(per_variant, "custom")
    lines.append(
        f"| idle GPU power — 30s baseline (W) | {fmt(s_idle)} | {fmt(c_idle)} | {pct_str(delta_pct(s_idle, c_idle))} |"
    )

    s_w = variant_power_mean(per_variant, "stock", "energy", "mean_w")
    c_w = variant_power_mean(per_variant, "custom", "energy", "mean_w")
    lines.append(
        f"| mean GPU power — energy bench (W) | {fmt(s_w)} | {fmt(c_w)} | {pct_str(delta_pct(s_w, c_w))} |"
    )
    lines.append(
        f"| net GPU power — energy bench, minus idle (W) | {fmt(s_w - s_idle)} | {fmt(c_w - c_idle)} | {pct_str(delta_pct(s_w - s_idle, c_w - c_idle))} |"
    )

    for bench, label in [
        ("throughput", "throughput bench"),
        ("energy", "energy bench"),
    ]:
        s_lat = variant_mean(per_variant, "stock", bench, "mean_latency_seconds")
        c_lat = variant_mean(per_variant, "custom", bench, "mean_latency_seconds")
        s_pw = variant_power_mean(per_variant, "stock", bench, "mean_w")
        c_pw = variant_power_mean(per_variant, "custom", bench, "mean_w")
        s_tps = variant_mean(per_variant, "stock", bench, "mean_tokens_per_second")
        c_tps = variant_mean(per_variant, "custom", bench, "mean_tokens_per_second")
        s_j = s_pw * s_lat
        c_j = c_pw * c_lat
        s_ept = s_j / s_tps if s_tps else 0.0
        c_ept = c_j / c_tps if c_tps else 0.0
        s_tpw = s_tps / s_pw if s_pw else 0.0
        c_tpw = c_tps / c_pw if c_pw else 0.0
        lines.append(
            f"| est. energy/call — {label} (J) | {fmt(s_j, 1)} | {fmt(c_j, 1)} | {pct_str(delta_pct(s_j, c_j))} |"
        )
        lines.append(
            f"| est. energy/token — {label} (J/tok) | {fmt(s_ept, 4)} | {fmt(c_ept, 4)} | {pct_str(delta_pct(s_ept, c_ept))} |"
        )
        lines.append(
            f"| tok/s per watt — {label} | {fmt(s_tpw, 2)} | {fmt(c_tpw, 2)} | {pct_str(delta_pct(s_tpw, c_tpw))} |"
        )

    lines.append("")
    lines.append("### server-side cross-check (llama.cpp `/metrics`)")
    lines.append("| metric | stock | custom | Δ |")
    lines.append("|---|---|---|---|")
    for bench, label in [
        ("throughput", "throughput bench"),
        ("energy", "energy bench"),
    ]:
        s_pt, c_pt = 0.0, 0.0
        s_gt, c_gt = 0.0, 0.0
        for variant in ("stock", "custom"):
            vals_pt: list[float] = []
            vals_gt: list[float] = []
            for run_tag, run in per_variant[variant]["runs"].items():
                sm = run["server_metrics"][bench]
                if sm["delta_prompt_seconds"] > 0:
                    vals_pt.append(
                        sm["delta_prompt_tokens"] / sm["delta_prompt_seconds"]
                    )
                if sm["delta_predicted_seconds"] > 0:
                    vals_gt.append(
                        sm["delta_predicted_tokens"] / sm["delta_predicted_seconds"]
                    )
            if variant == "stock":
                s_pt, s_gt = mean(vals_pt), mean(vals_gt)
            else:
                c_pt, c_gt = mean(vals_pt), mean(vals_gt)
        lines.append(
            f"| prompt tok/s — {label} | {fmt(s_pt, 1)} | {fmt(c_pt, 1)} | {pct_str(delta_pct(s_pt, c_pt))} |"
        )
        lines.append(
            f"| generation tok/s — {label} | {fmt(s_gt, 1)} | {fmt(c_gt, 1)} | {pct_str(delta_pct(s_gt, c_gt))} |"
        )

    return lines


# ── phase 2: application-level ─────────────────────────────────────────────


def phase2_report(run_dir: Path) -> list[str]:
    lines: list[str] = []
    lines.append("")
    lines.append("## 2. Application-level (real API workload + `jarvis telemetry`)")
    lines.append("")

    def telemetry_agg(variant: str) -> dict | None:
        path = run_dir / "phase2" / f"{variant}_telemetry.json"
        if not path.exists():
            return None
        try:
            records = json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
        if not records:
            return {"calls": 0}
        return {
            "calls": len(records),
            "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in records),
            "completion_tokens": sum(r.get("completion_tokens", 0) for r in records),
            "latency_s": mean([r.get("latency_seconds", 0) for r in records]),
            "throughput_tps": mean(
                [r.get("throughput_tok_per_sec", 0) for r in records]
            ),
            "mean_itl_ms": mean([r.get("mean_itl_ms", 0) for r in records]),
            "p95_itl_ms": mean([r.get("p95_itl_ms", 0) for r in records]),
            "energy_j": sum(r.get("energy_joules", 0) for r in records),
            "power_w": mean([r.get("power_watts", 0) for r in records]),
            "gpu_util_pct": mean([r.get("gpu_utilization_pct", 0) for r in records]),
            "gpu_temp_c": mean([r.get("gpu_temperature_c", 0) for r in records]),
            "energy_per_token_j": (
                sum(r.get("energy_joules", 0) for r in records)
                / sum(r.get("completion_tokens", 0) for r in records)
                if sum(r.get("completion_tokens", 0) for r in records) > 0
                else 0.0
            ),
        }

    def requests_agg(variant: str) -> dict | None:
        path = run_dir / "phase2" / f"{variant}_requests.csv"
        if not path.exists():
            return None
        rows = []
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    rows.append(
                        {
                            "latency": float(row["latency_s"]),
                            "ct": int(row["completion_tokens"]),
                            "pt": int(row["prompt_tokens"]),
                        }
                    )
                except (ValueError, KeyError):
                    continue
        if not rows:
            return {"calls": 0}
        lats = sorted(r["latency"] for r in rows)
        return {
            "calls": len(rows),
            "latency_mean_s": statistics.mean(lats),
            "latency_p50_s": lats[len(lats) // 2],
            "completion_tokens": mean([r["ct"] for r in rows]),
            "prompt_tokens": mean([r["pt"] for r in rows]),
        }

    t_stock, t_custom = telemetry_agg("stock"), telemetry_agg("custom")
    r_stock, r_custom = requests_agg("stock"), requests_agg("custom")

    lines.append(
        "### telemetry stats (records written by the OpenJarvis instrumented path)"
    )
    lines.append("| metric | stock | custom | Δ |")
    lines.append("|---|---|---|---|")
    if t_stock and t_custom and t_stock.get("calls"):
        for key, label, digits in [
            ("calls", "calls", 0),
            ("prompt_tokens", "prompt tokens", 0),
            ("completion_tokens", "completion tokens", 0),
            ("latency_s", "avg latency (s)", 2),
            ("throughput_tps", "avg throughput (tok/s)", 1),
            ("mean_itl_ms", "mean ITL (ms)", 2),
            ("p95_itl_ms", "p95 ITL (ms)", 2),
            ("energy_j", "total energy (J, in-app GPU)", 1),
            ("energy_per_token_j", "energy/completion token (J/tok)", 4),
            ("power_w", "mean GPU power (W)", 1),
            ("gpu_util_pct", "mean GPU util (%)", 1),
            ("gpu_temp_c", "mean GPU temp (°C)", 1),
        ]:
            s, c = t_stock.get(key, 0), t_custom.get(key, 0)
            d = delta_pct(s, c) if s else 0.0
            lines.append(
                f"| {label} | {fmt(s, digits)} | {fmt(c, digits)} | {pct_str(d)} |"
            )
    else:
        lines.append("| _telemetry export missing or empty_ | | | |")

    lines.append("")
    lines.append("### API workload (client-observed request latency)")
    lines.append("| metric | stock | custom | Δ |")
    lines.append("|---|---|---|---|")
    if r_stock and r_custom and r_stock.get("calls"):
        for key, label, digits in [
            ("calls", "calls", 0),
            ("latency_mean_s", "mean latency (s)", 2),
            ("latency_p50_s", "p50 latency (s)", 2),
            ("completion_tokens", "avg completion tokens", 0),
            ("prompt_tokens", "avg prompt tokens", 0),
        ]:
            s, c = r_stock.get(key, 0), r_custom.get(key, 0)
            d = delta_pct(s, c) if s else 0.0
            lines.append(
                f"| {label} | {fmt(s, digits)} | {fmt(c, digits)} | {pct_str(d)} |"
            )
    else:
        lines.append("| _request CSV missing or empty_ | | | |")
    return lines


# ── main ───────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--tag", default="run")
    args = ap.parse_args()

    env_path = args.run_dir / "env" / "environment.json"
    env_info = json.loads(env_path.read_text()) if env_path.exists() else {}

    per_variant = gather_phase1(args.run_dir)

    report: list[str] = []
    report.append(f"# OpenJarvis × llama.cpp comparison — `{args.tag}`")
    report.append("")
    if env_info:
        report.append(f"- Date: {env_info.get('timestamp', '?')}")
        report.append(f"- GPU: {env_info.get('gpu', {}).get('query', '?')}")
        report.append(
            f"- Model: {env_info.get('model', {}).get('path', '?')} ({env_info.get('model', {}).get('size_bytes', '?')} B)"
        )
        model_sha = env_info.get("model", {}).get("sha256", "?")
        report.append(f"  sha256 `{model_sha[:16]}…`")
        report.append(
            f"- OpenJarvis (container {env_info.get('openjarvis', {}).get('container', '?')}): {env_info.get('openjarvis', {}).get('version', '?')}"
        )
        for variant in ("stock", "custom"):
            b = env_info.get("binaries", {}).get(variant, {})
            version_line = b.get("version", "?")
            if version_line and version_line != "?":
                # prefer the `version: 9079 (…hash)` line over the final line
                for line in version_line.strip().splitlines():
                    if line.strip().startswith("version:"):
                        version_line = line.strip()
                        break
                else:
                    version_line = version_line.strip().splitlines()[-1]
            report.append(f"- {variant}: md5 `{b.get('md5', '?')}` — `{version_line}`")
        flags = env_info.get("server_flags", "?")
        report.append(f"- Server flags: `{flags}`")
    report.append("")
    report.append(
        "Methodology: `jarvis bench run` engine microbenchmark (A/B/B/A order), "
        "host-side `nvidia-smi` sampling, llama.cpp `/metrics` cross-check; "
        "then a fixed API workload with telemetry cleared per binary."
    )
    report.append("")
    report.append(
        "> Power is measured at the GPU level on the host (RTX 3050 6GB). "
        "Energy values are estimates (mean W × mean latency)."
    )

    report.extend(phase1_report(per_variant))
    report.extend(phase2_report(args.run_dir))

    (args.run_dir / "comparison_report.md").write_text("\n".join(report) + "\n")

    comparison = {
        "tag": args.tag,
        "environment": env_info,
        "phase1": per_variant,
        "phase2": {
            "telemetry": {
                v: json.loads(
                    (args.run_dir / "phase2" / f"{v}_telemetry.json").read_text()
                )
                for v in ("stock", "custom")
                if (args.run_dir / "phase2" / f"{v}_telemetry.json").exists()
            },
            "requests": {
                v: (args.run_dir / "phase2" / f"{v}_requests.csv").read_text()
                for v in ("stock", "custom")
                if (args.run_dir / "phase2" / f"{v}_requests.csv").exists()
            },
        },
    }
    (args.run_dir / "comparison.json").write_text(
        json.dumps(comparison, indent=2, default=str)
    )

    print(f"[compare] wrote {args.run_dir / 'comparison_report.md'}")
    print(f"[compare] wrote {args.run_dir / 'comparison.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
