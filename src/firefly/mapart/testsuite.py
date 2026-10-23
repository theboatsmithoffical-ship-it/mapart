"""Built-in deterministic test-image suite (no downloads, fully local).

Twelve categories x three tiers:
  small  128x128  (one map, fast)
  medium 256x256  (2x2 maps)
  large  512x512  (4x4 maps, slow)

Every generator is seeded/deterministic; ids are stable.
"""
from __future__ import annotations

import hashlib

import numpy as np

_MAP = 128
TIERS = {"small": 128, "medium": 256, "large": 512}


def _grid(size: int):
    y, x = np.mgrid[0:size, 0:size]
    return x, y, size


def _gradient_smooth(s, rng):
    x, y, n = _grid(s)
    r = (x / (n - 1)) * 255
    b = (y / (n - 1)) * 255
    g = 0.5 * (r + b)
    return np.stack([np.broadcast_to(r, (n, n)), np.broadcast_to(g, (n, n)), np.broadcast_to(b, (n, n))], axis=-1)


def _gradient_abrupt(s, rng):
    x, y, n = _grid(s)
    q = np.where(x < n / 2, 40 + (x / (n / 2)) * 120, 180 + (x - n / 2) / (n / 2) * 70)
    b = np.where(y < n / 2, 255, 30).astype(float)
    return np.stack([np.broadcast_to(q + x * 0.1, (n, n)) + (y * 0.05), np.full((n, n), 120.0)
                     + x * 0.2, np.broadcast_to(b - y * 0.02, (n, n))], axis=-1)


def _color_chart(s, rng):
    x, y, n = _grid(s)
    r = np.where((x // (n // 4)) % 2 == 0, 240, 40)
    g = np.where((y // (n // 4)) % 2 == 0, 220, 60)
    b = np.where((x + y) % 2 == 0, 30, 235)
    j = rng.uniform(-4, 4, (n, n, 3))
    return np.stack([r, g, b], axis=-1) + j


def _fine_lines_and_details(s, rng):
    x, y, n = _grid(s)
    base = 128.0 + 40.0 * np.sin(x * np.pi / 8.0) + 30.0 * np.cos(y * np.pi / 7.0)
    thin = np.where(np.abs(x - y) % 9 < 1, 80.0, 0.0)
    micro = np.where((x % 4 == 0) & (y % 4 == 0), 60.0, 0.0)
    return (base + thin + micro)[..., None] * np.array([0.85, 1.0, 1.2]) + 20.0 * np.sin(x / 23.0)[..., None]


def _checker_highfreq(s, rng):
    x, y, n = _grid(s)
    small = ((x % 2) ^ (y % 2)) * 200 + 20
    big = ((x // 8) ^ (y // 8)) * 190 + 30
    return np.stack([np.broadcast_to(small.astype(float) + big * 0.2, (n, n)),
                     np.broadcast_to(big.astype(float) + small * 0.1, (n, n)),
                     np.broadcast_to(small.astype(float) * 0.4 + big * 0.5, (n, n))], axis=-1)


def _faux_photo(s, rng):
    sky = np.linspace(210, 120, s)[:, None, None] * np.array([0.8, 1.05, 1.6])[None, None, :]
    ground = np.linspace(60, 110, s)[None, :, None] * np.array([1.5, 1.2, 0.6])[None, None, :]
    img = sky * np.linspace(1.0, 0.0, s)[None, :, None] + ground * np.linspace(0.0, 1.0, s)[None, :, None]
    # foliage blobs
    cx, cy = rng.integers(0, s), rng.integers(0, s)
    yy, xx = np.mgrid[0:s, 0:s]
    mask = (xx - cx) ** 2 + (yy - cy) ** 2 < (s // 7) ** 2
    foliage = np.array([50, 120, 45], dtype=float)
    img[mask] = img[mask] * 0.4 + foliage * 0.6
    # skin-tone circle (a "face")
    mask2 = (xx - s // 2) ** 2 + (yy - int(s * 0.62)) ** 2 < (s // 12) ** 2
    img[mask2] = img[mask2] * 0.3 + np.array([205, 150, 120], dtype=float) * 0.7
    return img + rng.normal(0, 5, (s, s, 3))


def _landscape_flat_areas(s, rng):
    x, y, n = _grid(s)
    sky = np.linspace(190, 230, n)[:, None]
    field = np.full((n,), 120) + 14 * rng.uniform(-1, 1, n)
    img_y = np.linspace(0, 1, n)
    top = sky * (1 - img_y)[None].T if False else sky
    arr = np.zeros((n, n, 3))
    arr[:, :, :] = top[:, :, None] * np.array([0.9, 1.0, 1.3])[None, None, :]
    below = y > int(n * 0.55)
    arr[below] = (field[None, :, None] * np.array([1.1, 1.0, 0.4]))[0][0] * 0 + arr[below] * 0.2 + \
                 np.array([90, 118, 50], dtype=float) * 0.8
    horizon = np.abs(y - int(n * 0.55)) < 1
    arr[horizon] = [228, 205, 160]
    return arr


def _high_contrast(s, rng):
    x, y, n = _grid(s)
    blob = np.where((x - y) ** 2 + ((x - n / 2) * 0.5) ** 2 < (n ** 2) / 9, 250.0, 8.0)
    return blob[..., None] * np.array([1.0, 0.95, 0.9])


def _grayscale(s, rng):
    x, y, n = _grid(s)
    v = (x / (n - 1)) * 0.55 * 255 + (y / (n - 1)) * 0.45 * 255 + 18 * np.sin(y / 11.0)
    return np.stack([v, v, v], axis=-1)


def _transparent_regions(s, rng):
    """RGBA with alpha=0 center rectangle; alpha channel is exported in meta but
    the pipeline composites over a checker by design (documented behavior)."""
    arr = np.zeros((s, s, 4), dtype=np.uint8)
    arr[..., :3] = _gradient_smooth(s, rng).astype(np.uint8)
    arr[..., 3] = 255
    arr[s // 4:3 * s // 4, s // 4:3 * s // 4, 3] = 0
    return arr


def _composited_transparent(s, rng):
    raw = _transparent_regions(s, rng)
    a = raw[..., 3:4].astype(float) / 255.0
    checker = ((np.mgrid[0:s, 0:s][0] + np.mgrid[0:s, 0:s][1]) % 2 == 0)
    under = np.where(checker[..., None], np.array([60, 60, 70]), np.array([110, 110, 100])).astype(float)
    return raw[..., :3].astype(float) * a + under * (1 - a)


def _out_of_palette(s, rng):
    # neon magenta / electric cyan / chartreuse — likely absent from palettes
    x, y, n = _grid(s)
    mag = np.broadcast_to(np.stack([np.full(n, 245), np.full(n, 25), np.full(n, 200)], axis=1), (n, n))
    return np.where((x + y) % 2 == 0, 238, 25), None


def _out_of_palette2(s, rng):
    x, y, n = _grid(s)
    neon = np.zeros((n, n, 3))
    neon[..., 0] = np.where((x + y) % 2 == 0, 243, 240)
    neon[..., 1] = np.where((x < n / 2), 15, 247)
    neon[..., 2] = np.where((y < n / 2), 212, 18)
    return neon


def _pixel_art(s, rng):
    img = np.zeros((s, s, 3))
    gr = s // 16
    for iy in range(16):
        for ix in range(16):
            v = hashlib.sha256(f"{iy}/{ix}".encode()).digest()[0]
            c = np.array([int(v) * 1.4 % 255, (v * 2) % 255, (v * 3) % 255], dtype=float)
            img[iy * gr:(iy + 1) * gr, ix * gr:(ix + 1) * gr] = c
    return img


def _noisy_subtle(s, rng):
    x, y, n = _grid(s)
    base = 110 + 28 * np.sin(x / 19.0) * np.cos(y / 23.0) + rng.normal(0, 9, (n, n))
    # many similar colors: hue barely varies
    return base[..., None] * np.array([[0.98, 1.0, 1.03]]) + rng.normal(0, 4, (n, n, 3))


def _noisy_subtle_many(s, rng):
    x, y, n = _grid(s)
    base = 120 + 18 * np.sin(x / 17.0) + 14 * np.cos(y / 21.0) + rng.normal(0, 12, (n, n))
    tint = np.linspace(-6, 6, n)[:, None, None] * np.array([1.0, 0.4, -0.3])[None, None, :]
    return base[..., None] + tint + rng.normal(0, 3, (n, n, 3))


GENERATORS = {
    "grad-smooth":     (_gradient_smooth, "smooth color gradients"),
    "grad-abrupt":     (_gradient_abrupt, "gradients with abrupt transitions"),
    "color-chart":     (_color_chart, "primary and secondary color chart"),
    "fine-details":    (_fine_lines_and_details, "fine lines, small details, geometry"),
    "checker-hf":      (_checker_highfreq, "checkerboards, alternating pixels, high frequency"),
    "faux-photo":      (_faux_photo, "skin tones, foliage, sky, shadows, textures"),
    "landscape-flat":  (_landscape_flat_areas, "landscapes with large similar-color regions"),
    "high-contrast":   (_high_contrast, "strong contrast and sharp boundaries"),
    "grayscale":       (_grayscale, "grayscale images"),
    "transparency":    (_composited_transparent, "transparency composited over checker"),
    "out-of-palette":  (_out_of_palette2, "colors absent from the selected palette"),
    "pixel-art":       (_pixel_art, "pixel art with sharp pixel boundaries"),
    "noisy-subtle":    (_noisy_subtle, "noise, subtle shading"),
    "similar-colors":  (_noisy_subtle_many, "many very similar colors"),
}


def np_image(item_id: str, size: int, seed: int = 4) -> np.ndarray:
    gen = GENERATORS[item_id][0]
    rng = np.random.default_rng(seed + size)
    return np.clip(np.nan_to_num(gen(size, rng)), 0, 255).astype(np.uint8)


def suite(tier: str = "small", items=None, seed: int = 4) -> dict:
    """{id: (purpose, np.ndarray HxWx3 uint8)} for the requested tier."""
    s = TIERS[tier]
    ids = list(items or GENERATORS)
    return {i: (GENERATORS[i][1], np_image(i, s, seed)) for i in ids}


def rasterize(arr: np.ndarray):
    from PIL import Image
    if arr.ndim == 2:
        arr = np.stack([arr, arr, arr], axis=-1)
    if arr.shape[2] == 4:  # composited alpha handled by generator for 'transparency'
        arr = arr[..., :3]
    return Image.fromarray(arr.astype(np.uint8), "RGB")


def save_all(tier: str, out_dir, seed: int = 4) -> dict:
    out_dir = __import__("pathlib").Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = {}
    for i in GENERATORS:
        purpose, arr = suite(tier, [i], seed)[i]
        rasterize(arr).save(out_dir / f"{tier}_{i}.png", format="PNG")
        saved[i] = purpose
    (out_dir / f"suite_{tier}_manifest.json").write_text(
        __import__("json").dumps({"tier": tier, "seed": seed, "resolution": GL_size(tier),
                                  "items": {i: {"purpose": p, "deterministic": True}
                                            for i, p in saved.items()}}, indent=2),
        encoding="utf-8")
    return saved


def GL_size(tier: str) -> int:
    return TIERS[tier]
