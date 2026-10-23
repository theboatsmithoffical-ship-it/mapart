"""Synthetic benchmark fixtures — fully offline, deterministic.

The suite is generated from a seed; pixel hashes are content-addressed so
rankings/claims recorded in one engine are interpretable in another.
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import numpy as np

BUILTIN_SUITE_NAME = "builtin-core-v1"
BUILTIN_SUITE_SEED = 7
_NICHES = ("steady", "tilted", "shattered", "crystal")


def _steady(h: int, w: int, rng: np.random.Generator) -> np.ndarray:
    y, x = np.mgrid[0:h, 0:w]
    r = (x / max(1, w - 1)) * 250 + rng.uniform(-6, 6)
    b = (y / max(1, h - 1)) * 250 + rng.uniform(-6, 6)
    g = 0.5 * (r + b) + 8.0 * np.sin(y / 13.0) * np.cos(x / 9.0)
    return np.stack([np.broadcast_to(r, (h, w)), np.broadcast_to(g, (h, w)), np.broadcast_to(b, (h, w))], axis=-1)


def _tilted(h: int, w: int, rng: np.random.Generator) -> np.ndarray:
    y, x = np.mgrid[0:h, 0:w]
    ang = rng.uniform(0.2, 1.2)
    t = (x * np.cos(ang) + y * np.sin(ang)) / (h + w)
    a = 250.0 * t + rng.uniform(-8, 8)
    wave = 22.0 * np.sin(t * 34.0)
    return np.stack([a - wave, a + wave * 0.4, a + np.rot90(wave, 1)], axis=-1)


def _shattered(h: int, w: int, rng: np.random.Generator) -> np.ndarray:
    base = rng.uniform(40, 220, size=(h, w, 3))
    noise = rng.normal(0.0, 26.0, size=(h, w, 3))
    edges = np.zeros((h, w, 3))
    edges[::17, :, :] += 90.0
    edges[:, ::11, :] -= 70.0
    return base + noise + edges


def _crystal(h: int, w: int, rng: np.random.Generator) -> np.ndarray:
    y, x = np.mgrid[0:h, 0:w]
    waves = (
        40.0 * np.sin(x / 6.0 + y / 15.0) + 30.0 * np.cos(y / 5.0 - x / 19.0)
    )
    tint = np.array([0.9, 1.0, 1.15])
    jitter = rng.uniform(-0.8, 0.8, size=(h, w, 3))
    base = (128.0 + waves)[..., None] * tint + jitter  # (h, w, 3)
    return base + 12.0 * np.sin(x / 21.0)[..., None]


def builtins(seed: int = BUILTIN_SUITE_SEED) -> dict:
    out = {}
    for i, niche in enumerate(_NICHES):
        rng = np.random.default_rng(seed + i)
        builder = {"steady": _steady, "tilted": _tilted, "shattered": _shattered, "crystal": _crystal}[niche]
        arr = np.clip(builder(128, 128, rng), 0, 255)
        out[niche] = arr.round().astype(np.uint8)
    return out


def pixel_sha256(arr: np.ndarray) -> str:
    return hashlib.sha256(arr.tobytes() + b"|%dx%d" % (arr.shape[0], arr.shape[1])).hexdigest()[:16]


def builtin_suite_hashes(seed: int = BUILTIN_SUITE_SEED) -> dict:
    return {name: pixel_sha256(img) for name, img in builtins(seed).items()}


def stable_seed(suite_id: str, fixture_name: str, genome_id: str) -> int:
    digest = hashlib.sha256(f"{suite_id}|{fixture_name}|{genome_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % (2 ** 31)


def load_image(path) -> np.ndarray:
    """Load an image file as H,W,3 uint8 RGB (uses Pillow)."""
    from PIL import Image  # local import: only needed when custom fixtures are used

    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"))


def load_files(path) -> list:
    """File or dir-path -> the concrete image files used as fixtures."""
    from pathlib import Path

    p = Path(path)
    if p.is_dir():
        files = sorted(q for q in p.iterdir() if q.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"})
        if not files:
            raise ValueError(f"no image files in directory: {p}")
        return files
    if not p.is_file():
        raise ValueError(f"fixtures path not found: {p}")
    return [p]


def load_fixtures(path) -> list:
    """A file or a directory of images -> [(name, np.ndarray uint8 RGB), ...]."""
    files = load_files(path)
    return [(f.stem, load_image(f)) for f in files]


def png_bytes(arr: np.ndarray) -> bytes:
    import io

    from PIL import Image

    with Image.fromarray(arr.astype(np.uint8), "RGB") as im:
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()


def fixture_env(fixtures: dict, suite_id: str, sources: dict | None = None) -> SimpleNamespace:
    names = sorted(fixtures)
    if not names:
        raise ValueError("fixture set is empty")
    return SimpleNamespace(
        suite_id=suite_id,
        fixtures=[(n, fixtures[n]) for n in names],
        hashes={n: pixel_sha256(fixtures[n]) for n in names},
        sources={n: str((sources or {}).get(n, "<generated>")) for n in names},
    )


def builtin_env() -> SimpleNamespace:
    imgs = builtins()
    return fixture_env(imgs, BUILTIN_SUITE_NAME)
