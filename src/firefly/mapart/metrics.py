"""Objective scoring for lab/benchmark candidates.

Hard validity requirements are reported separately from the weighted visual
score — an invalid candidate can never be promoted (check caller contract).
All functions take numpy arrays; nothing here touches the network.
"""
from __future__ import annotations

import numpy as np


def palette_error(orig_plan: np.ndarray, out: np.ndarray, arr: np.ndarray) -> float:
    """MSE in palette color space between the palette-quantized original and rendered result."""
    idx = np.argmin(np.linalg.norm(orig_plan[..., None, :].astype(np.float32) - arr[None, :, :], axis=2), axis=1)
    pal_orig = arr[idx].reshape(orig_plan.shape[0], orig_plan.shape[1], 3)
    return float(np.mean((np.asarray(pal_orig, dtype=np.float64) - out.astype(np.float64)) ** 2))


def perceptual_oklab(orig: np.ndarray, out: np.ndarray) -> float:
    def ok(img):
        c = np.clip(img.astype(np.float64) / 255.0, 0, 1)
        r, g, b = c[..., 0], c[..., 1], c[..., 2]
        l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
        m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
        s = 0.0883024619 * r + 0.2817188376 * g + 0.6299780075 * b
        l_, m_, s_ = np.cbrt(l), np.cbrt(m), np.cbrt(s)
        L = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
        a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
        bb = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
        return np.stack([L, a, bb], axis=-1)
    d = ok(orig) - ok(out.astype(np.float64))
    return float(np.mean(np.sum(d * d, axis=-1)))


def edge_preservation(orig: np.ndarray, out: np.ndarray) -> float:
    """1.0 - mean abs gradient error, clamped to [0,1] — higher is better."""
    from firefly.evolution.dither import _luma
    ga = np.gradient(_luma(orig.astype(np.float32)))
    gb = np.gradient(_luma(out.astype(np.float32)))
    diff = sum(float(np.mean(np.abs(a - b))) for a, b in zip(ga, gb))
    return float(max(0.0, 1.0 - diff / 16.0))


def coverage(idx, palette_colors: int) -> float:
    """Distinct palette colors used / palette size — higher is better."""
    return float(len(np.unique(idx)) / max(1, palette_colors))


def distribution(idx) -> float:
    """Shannon entropy of block-color usage normalized by log(n) — higher is better."""
    vals, counts = np.unique(idx, return_counts=True)
    p = counts / counts.sum()
    return float(-(p * np.log(p)).sum() / np.log(len(vals)) if len(vals) > 1 else 0.0)


def material_cost(blocks, counts: dict, pal, rarity: dict | None = None) -> float:
    """Sum over blocks of count*rarity (rarity 0..1, 1 = very hard to get)."""
    total = 0.0
    for blk, c in counts.items():
        r = (rarity or {}).get(blk, 0.02 if "wool" in blk else 0.05)
        total += c["count"] * r
    return float(total / max(1, blocks.size))


def compute(orig_plan, arr, out_rgb, idx, elapsed, counts, blocks, pal,
            schem_valid: bool, schem_report=None, peak_mem=None) -> dict:
    """All metrics independently — never collapse silently."""
    cov = coverage(idx, len(pal.colors))
    dist = float(distribution(idx))
    edge = edge_preservation(orig_plan, out_rgb)
    perr = perceptual_oklab(orig_plan, out_rgb)
    perr_norm = min(1.0, perr / 0.35)  # 0.35 dE*100-ish scale cap
    cost = material_cost(blocks, counts, pal)
    return {
        "oklab_error": round(perr, 6),
        "oklab_error_norm": round(perr_norm, 6),
        "edge_preservation": round(edge, 6),
        "palette_coverage": round(cov, 6),
        "distribution_entropy": round(dist, 6),
        "material_cost": round(cost, 6),
        "process_time_s": round(float(elapsed), 6),
        "peak_mem_bytes": peak_mem if peak_mem is not None else None,
        "schem_valid": schem_valid,
        "schem_report": schem_report or {},
        "blocks_unique": len(counts),
        "blocks_total": sum(v["count"] for v in counts.values()),
    }


DEFAULT_WEIGHTS = {"oklab_error_norm": 0.40, "edge_preservation": 0.25,
                   "palette_coverage": 0.10, "distribution_entropy": 0.05,
                   "material_cost": 0.20}


def weighted(valid: bool, metrics: dict, weights: dict | None = None) -> float | None:
    """Only *valid* candidates get a weighted score; invalid candidate returns None."""
    if not valid:
        return None
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)
    vals = {"oklab_error_norm": 1.0 - float(metrics["oklab_error_norm"]),
            "edge_preservation": float(metrics["edge_preservation"]),
            "palette_coverage": float(metrics["palette_coverage"]),
            "distribution_entropy": float(metrics["distribution_entropy"]),
            "material_cost": 1.0 - min(1.0, float(metrics["material_cost"]) / 0.08)}
    total = 0.0
    for k, weight in w.items():
        total += weight * vals.get(k, 0.0)
    return round(total, 6)
