#!/usr/bin/env python3
"""Quality benchmark for the map-art pipeline (Phase 8) — also the Phase-1 baseline.

For each representative image CATEGORY, runs the REAL pipeline (appcore.process)
under a set of (metric, algorithm) configurations and measures how close the
PREDICTED map render (result.preview) is to the actual target image
(result.processed = post-normalize source). All perceptual numbers use the SAME
srgb_to_lab + ciede2000 the engine uses — one source of truth, no second metric.

Reported per run (never collapsed to one number):
  - CIEDE2000 error: mean / median / p95 / max, + % pixels within dE<5 and dE<10
  - OKLab RMS error (independent cross-check)
  - process wall time and tracemalloc peak memory
  - distinct blocks used
Aggregate is grouped by CONFIG and by CATEGORY so a config that helps photos but
hurts pixel-art is visible, not averaged away.

Usage:
  python tools/quality_benchmark.py                     # full matrix
  python tools/quality_benchmark.py --quick             # representative subset
  python tools/quality_benchmark.py --out docs/baseline_metrics.json
Exit 0 always (a benchmark records; it does not gate). Writes JSON (+ .md table).
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from firefly.mapart import appcore, mapart, testsuite  # noqa: E402

# Representative categories covering the spec's benchmark requirements.
CATEGORIES = {
    "photograph": ["faux-photo"],
    "noisy-photo": ["noisy-subtle"],
    "gradient": ["grad-smooth", "grad-abrupt"],
    "pixel-art": ["pixel-art"],
    "high-frequency": ["checker-hf", "fine-details"],
    "transparency": ["transparency"],
    "out-of-palette": ["out-of-palette"],
}


def _dE_stats(src_u8: np.ndarray, prev_u8: np.ndarray) -> dict:
    """CIEDE2000 between target and predicted render, flattened over pixels."""
    lab_s = mapart.srgb_to_lab(src_u8.reshape(-1, 3).astype(np.float64))
    lab_p = mapart.srgb_to_lab(prev_u8.reshape(-1, 3).astype(np.float64))
    d = mapart.ciede2000(lab_s, lab_p)
    return {
        "dE2000_mean": round(float(np.mean(d)), 4),
        "dE2000_median": round(float(np.median(d)), 4),
        "dE2000_p95": round(float(np.percentile(d, 95)), 4),
        "dE2000_max": round(float(np.max(d)), 4),
        "pct_within_dE5": round(float(np.mean(d < 5.0)) * 100, 2),
        "pct_within_dE10": round(float(np.mean(d < 10.0)) * 100, 2),
    }


def _oklab_rms(src_u8: np.ndarray, prev_u8: np.ndarray) -> float:
    from firefly.mapart.metrics import perceptual_oklab
    return round(float(np.sqrt(perceptual_oklab(src_u8, prev_u8))), 6)


def run_one(image_path: Path, version: str, metric: str, algorithm: str,
            strength: float, category: str, item_id: str) -> dict:
    s = appcore.Settings(version=version, maps_w=1, maps_h=1,
                         algorithm=algorithm, strength=strength, metric=metric)
    tracemalloc.start()
    t0 = time.perf_counter()
    res = appcore.process(image_path, s)
    wall = time.perf_counter() - t0
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    prev = res.preview.astype(np.uint8)
    tgt = res.processed.astype(np.uint8)
    row = {
        "category": category, "item": item_id, "metric": metric,
        "algorithm": algorithm, "strength": strength, "palette": version,
        "wall_s": round(wall, 3), "peak_mem_mb": round(peak / 1e6, 1),
        "blocks_unique": int(len(res.counts)),
    }
    row.update(_dE_stats(tgt, prev))
    row["oklab_rms"] = _oklab_rms(tgt, prev)
    return row


def _export_valid(image_path: Path, version: str, algorithm: str) -> dict:
    """Independently validate a real schematic export for one representative run."""
    s = appcore.Settings(version=version, maps_w=1, maps_h=1, algorithm=algorithm,
                         strength=1.0, metric="rgb", formats=("schem", "litematic"))
    with tempfile.TemporaryDirectory(prefix="qbench_export_") as td:
        out = appcore.full_build(image_path, s, Path(td), base_name="bench")
        v = out["files"]["validation"]
        return {
            "schem_valid": bool(v.get("schem", {}).get("ok", v.get("schem"))),
            "litematic_valid": bool(v.get("litematic", {}).get("ok", v.get("litematic"))),
            "schem_size": v.get("schem", {}).get("size"),
            "blocks_placed": v.get("schem", {}).get("blocks_placed"),
        }


def aggregate(rows: list) -> dict:
    by_cfg: dict[str, list] = {}
    by_cat: dict[str, list] = {}
    for r in rows:
        by_cfg.setdefault(f'{r["metric"]}|{r["algorithm"]}', []).append(r)
        by_cat.setdefault(r["category"], []).append(r)

    def _agg(rs):
        if not rs:
            return {}
        return {
            "n": len(rs),
            "dE2000_mean_avg": round(float(np.mean([x["dE2000_mean"] for x in rs])), 4),
            "dE2000_p95_avg": round(float(np.mean([x["dE2000_p95"] for x in rs])), 4),
            "dE2000_max_worst": round(float(np.max([x["dE2000_max"] for x in rs])), 4),
            "pct_within_dE10_avg": round(float(np.mean([x["pct_within_dE10"] for x in rs])), 2),
            "oklab_rms_avg": round(float(np.mean([x["oklab_rms"] for x in rs])), 6),
            "wall_s_avg": round(float(np.mean([x["wall_s"] for x in rs])), 3),
            "peak_mem_mb_max": round(float(np.max([x["peak_mem_mb"] for x in rs])), 1),
        }
    return {"by_config": {k: _agg(v) for k, v in sorted(by_cfg.items())},
            "by_category": {k: _agg(v) for k, v in sorted(by_cat.items())}}


def _write_md(report: dict, path: Path) -> None:
    lines = ["# Map-art quality benchmark", "",
             f"Generated {report['generated']}  ·  tier {report['tier']}  ·  "
             f"{report['n_runs']} runs", "",
             "## By configuration (metric | algorithm)", "",
             "| config | n | dE2000 mean | dE2000 p95 | dE2000 max | %≤dE10 | oklab RMS | time s | peak MB |",
             "|---|---|---|---|---|---|---|---|---|"]
    for k, v in report["aggregate"]["by_config"].items():
        lines.append(f"| {k} | {v['n']} | {v['dE2000_mean_avg']} | {v['dE2000_p95_avg']} "
                     f"| {v['dE2000_max_worst']} | {v['pct_within_dE10_avg']} "
                     f"| {v['oklab_rms_avg']} | {v['wall_s_avg']} | {v['peak_mem_mb_max']} |")
    lines += ["", "## By category (averaged over configs)", "",
              "| category | n | dE2000 mean | dE2000 p95 | %≤dE10 | oklab RMS |",
              "|---|---|---|---|---|---|"]
    for k, v in report["aggregate"]["by_category"].items():
        lines.append(f"| {k} | {v['n']} | {v['dE2000_mean_avg']} | {v['dE2000_p95_avg']} "
                     f"| {v['pct_within_dE10_avg']} | {v['oklab_rms_avg']} |")
    if report.get("export"):
        e = report["export"]
        lines += ["", "## Export validation (representative)", "",
                  f"- schem valid: {e['schem_valid']} (size {e['schem_size']}, "
                  f"placed {e['blocks_placed']})", f"- litematic valid: {e['litematic_valid']}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="survival-wool-1.8")
    ap.add_argument("--tier", default="small", choices=["small", "medium"])
    ap.add_argument("--metrics", default="rgb,lab,ciede2000")
    ap.add_argument("--algorithms", default="floyd_steinberg,none,bayer4")
    ap.add_argument("--strength", type=float, default=1.0)
    ap.add_argument("--quick", action="store_true", help="one item per category + fewer configs")
    ap.add_argument("--out", default=str(ROOT / "docs" / "baseline_metrics.json"))
    ap.add_argument("--no-export", action="store_true")
    a = ap.parse_args()

    metrics = [m.strip() for m in a.metrics.split(",") if m.strip()]
    algs = [x.strip() for x in a.algorithms.split(",") if x.strip()]
    if a.quick:
        metrics = metrics[:2]
        algs = algs[:2]
        categories = {k: v[:1] for k, v in CATEGORIES.items()}
    else:
        categories = CATEGORIES

    size = testsuite.TIERS[a.tier]
    rows = []
    with tempfile.TemporaryDirectory(prefix="qbench_") as td:
        tdp = Path(td)
        for cat, items in categories.items():
            for item_id in items:
                arr = testsuite.np_image(item_id, size)
                img = tdp / f"{item_id}.png"
                testsuite.rasterize(arr).save(img, format="PNG")
                for metric in metrics:
                    for alg in algs:
                        try:
                            rows.append(run_one(img, a.version, metric, alg,
                                                a.strength, cat, item_id))
                            print(f"ok {cat}/{item_id} {metric}|{alg} "
                                  f"dE={rows[-1]['dE2000_mean']}")
                        except Exception as e:  # benchmark records, never hides
                            print(f"FAIL {cat}/{item_id} {metric}|{alg}: "
                                  f"{type(e).__name__}: {e}")
        export = None
        if not a.no_export:
            rep = tdp / "export_src.png"
            testsuite.rasterize(testsuite.np_image("faux-photo", size)).save(rep, format="PNG")
            try:
                export = _export_valid(rep, a.version, "floyd_steinberg")
            except Exception as e:  # noqa: BLE001
                export = {"error": f"{type(e).__name__}: {e}"}

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "tier": a.tier, "size_px": size, "palette": a.version,
        "strength": a.strength, "n_runs": len(rows),
        "matching_metrics": metrics, "algorithms": algs,
        "note": "quality measured between post-normalize target and predicted "
                "preview render, using the engine's own CIEDE2000/OKLab",
        "export": export, "runs": rows, "aggregate": aggregate(rows),
    }
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    _write_md(report, out.with_suffix(".md"))
    print(json.dumps({"runs": len(rows), "out": str(out),
                      "md": str(out.with_suffix('.md'))}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
