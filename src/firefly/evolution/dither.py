"""Dithering core: fixed palette, real algorithms, deterministic scoring.

Every algorithm here is deterministic for a given genome + seed; the
`random` algorithm is seeded explicitly so results are reproducible.
"""
from __future__ import annotations

import numpy as np

# Fixed default palette (24 colors). This is a design constant, not a
# Minecraft-accurate map palette — see docs/PORTABLE.md limitations.
PALETTE = np.array(
    [
        (0, 0, 0), (28, 28, 28), (58, 58, 58), (88, 88, 88), (120, 120, 120),
        (152, 152, 152), (185, 185, 185), (217, 217, 217), (255, 255, 255),
        (100, 102, 106), (122, 86, 56), (162, 121, 88), (189, 152, 117),
        (97, 35, 18), (128, 54, 25), (170, 85, 40), (207, 129, 70),
        (40, 106, 32), (62, 152, 41), (95, 189, 65), (122, 223, 92),
        (26, 92, 140), (35, 130, 168), (80, 170, 200),
    ],
    dtype=np.float32,
)


def _bayer_matrix(n: int) -> np.ndarray:
    m = np.array([[0.0, 2.0], [3.0, 1.0]], dtype=np.float64)
    while m.shape[0] < n:
        t = m * 4.0
        m = np.block([[t, t + 2.0], [t + 3.0, t + 1.0]])
    return m / float(n * n)


BAYER_SIZES = {2, 4, 8}
BAYER_ALGORITHMS = {f"bayer{n}": n for n in BAYER_SIZES}

ERROR_KERNELS = {
    "floyd_steinberg": ([(1, 0, 7), (-1, 1, 3), (0, 1, 5), (1, 1, 1)], 16),
    "atkinson": ([(1, 0, 1), (2, 0, 1), (-1, 1, 1), (0, 1, 1), (1, 1, 1), (0, 2, 1)], 8),
    "burkes": ([(1, 0, 2), (2, 0, 2), (-2, 1, 1), (-1, 1, 1), (0, 1, 2), (1, 1, 1)], 10),
    "sierra_lite": ([(1, 0, 2), (-1, 1, 1), (0, 1, 1)], 4),
    "jjn": ([(1, 0, 7), (2, 0, 3), (-2, 1, 5), (-1, 1, 5), (0, 1, 5), (1, 1, 3),
             (2, 1, 1), (-1, 2, 3), (0, 2, 2)], 48),
    "stucki": ([(1, 0, 8), (2, 0, 4), (-2, 1, 2), (-1, 1, 4), (0, 1, 8), (1, 1, 4),
                (2, 1, 2), (-2, 2, 1), (-1, 2, 2), (0, 2, 4), (1, 2, 2), (2, 2, 1)], 42),
}

ERROR_ALGORITHMS = tuple(ERROR_KERNELS)
ALGORITHMS = tuple(ERROR_ALGORITHMS) + tuple(BAYER_ALGORITHMS) + ("threshold", "random")


def _nearest_indices(arr: np.ndarray) -> np.ndarray:
    """Index of the nearest palette color for each RGB pixel of a (.., 3) array."""
    flat = arr.reshape(-1, 3).astype(np.float32)
    idx = np.empty(flat.shape[0], dtype=np.int64)
    for s in range(0, flat.shape[0], 16384):
        block = flat[s:s + 16384]
        d = block[:, None, :] - PALETTE[None, :, :]
        idx[s:s + 16384] = np.argmin(np.einsum("ijk,ijk->ij", d, d), axis=1)
    return idx


def _nearest_colors(arr: np.ndarray) -> np.ndarray:
    """The nearest palette colors, same shape as input."""
    idx = _nearest_indices(arr)
    return PALETTE[idx].reshape(arr.shape[0], arr.shape[1], 3)


def _apply_gamma(v: np.ndarray, gamma: float) -> np.ndarray:
    if gamma == 1.0:
        return v
    return 255.0 * np.power(np.clip(v, 0.0, 255.0) / 255.0, 1.0 / float(gamma))


def _error_diffuse(img: np.ndarray, genome: dict) -> np.ndarray:
    kernel, div = ERROR_KERNELS[genome["algorithm"]]
    strength = float(genome["strength"])
    src = _apply_gamma(img.astype(np.float32), genome["gamma"])
    h, w, _ = src.shape
    work = src.copy()
    out = np.empty_like(work)
    pal = PALETTE
    for y in range(h):
        reverse = genome["serpentine"] and (y % 2 == 1)
        xs = range(w - 1, -1, -1) if reverse else range(w)
        for x in xs:
            v = np.clip(work[y, x], 0.0, 255.0)
            d = v - pal
            i = int(np.argmin(np.einsum("ij,ij->i", d, d)))
            p = pal[i]
            out[y, x] = p
            err = (v - p) * strength
            for dx, dy, wk in kernel:
                sx = -dx if reverse else dx
                xx, yy = x + sx, y + dy
                if 0 <= xx < w and 0 <= yy < h:
                    work[yy, xx] += err * (wk / div)
    return np.clip(out, 0, 255).round().astype(np.uint8)


def _pattern(img: np.ndarray, genome: dict, seed: int, kind: str) -> np.ndarray:
    strength = float(genome["strength"])
    bias = float(genome["bias"])
    src = _apply_gamma(img.astype(np.float32), genome["gamma"])
    h, w, _ = src.shape
    if kind in BAYER_ALGORITHMS:
        n = BAYER_ALGORITHMS[kind]
        m = _bayer_matrix(n)
        tile = np.tile(m, ((h + n - 1) // n, (w + n - 1) // n))[:h, :w]
        delta = (tile[..., None] - 0.5) * (strength * 96.0)
        v = src + delta + bias
    elif kind == "random":
        rng = np.random.default_rng(int(seed) % (2 ** 31))
        u = rng.random((h, w, 1), dtype=np.float32)
        v = src + (u - 0.5) * (strength * 255.0) + bias
    else:  # threshold: pure bias shift then nearest-color quantization
        v = src + bias
    v = np.clip(v, 0.0, 255.0)
    out = _nearest_colors(v.reshape(h, w, 3))
    return np.clip(out, 0, 255).round().astype(np.uint8)


def dither_image(img: np.ndarray, genome: dict, seed: int = 0) -> np.ndarray:
    """Render img (H, W, 3 uint8 RGB) through the genome's dithering settings."""
    if img.ndim != 3 or img.shape[2] != 3:
        raise ValueError(f"expected H,W,3 RGB image, got shape {img.shape}")
    algo = genome["algorithm"]
    if algo in ERROR_KERNELS:
        return _error_diffuse(img, genome)
    return _pattern(img, genome, seed, algo)


def _luma(x: np.ndarray) -> np.ndarray:
    return 0.2126 * x[..., 0].astype(np.float64) + 0.7152 * x[..., 1] + 0.0722 * x[..., 2]


def score_pair(orig: np.ndarray, out: np.ndarray) -> float:
    """Lower = better. Palette-space reproduction error + edge-preservation penalty."""
    pal_orig = _nearest_colors(orig.astype(np.float32)).astype(np.float32)
    res = out.astype(np.float32)
    mse = float(np.mean((pal_orig - res) ** 2))
    ga = np.stack(np.gradient(_luma(orig.astype(np.float32))))
    gb = np.stack(np.gradient(_luma(res)))
    edge = float(np.mean(np.abs(ga - gb))) * 64.0
    return round(mse + 0.5 * edge, 6)
