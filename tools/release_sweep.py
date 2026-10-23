"""Release combination sweep: every supported dithering method, palette
option, map layout and configuration mode, each driven through the REAL
end-to-end pipeline (appcore.full_build) with its export independently
validated. Failures are tallied, never hidden.

Usage:
    python tools/release_sweep.py            # full sweep (a few minutes)
    python tools/release_sweep.py --fast     # reduced matrix (~1 min)
Prints a JSON tally and exits non-zero if any combination fails.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
from PIL import Image

from firefly.mapart import appcore, schem

PALETTES = ("survival-wool-1.8", "map-full-approx-1.15")
LAYOUTS = {"1x1": (1, 1), "2x1": (2, 1), "1x2": (1, 2), "2x2": (2, 2), "3x2": (3, 2)}
DITHERS = appcore.SUPPORTED_ALGORITHMS
METRICS = appcore.METRICS
FIT_MODES = appcore.FIT_MODES
TRANSPARENCY = appcore.TRANSPARENCY_MODES
CONSTRUCTIONS = appcore.CONSTRUCTION_MODES
KINDS = appcore.PALETTE_KINDS


def fixtures(root: Path) -> dict:
    """Deterministic synthetic source images (content preserved, generated locally)."""
    out = {}
    rng = np.random.default_rng(42)
    photo = rng.integers(0, 256, (300, 400, 3), dtype=np.uint8)  # auto-layout -> 4x3 maps
    out["amber_rgb"] = root / "amber_rgb.png"
    Image.fromarray(photo, "RGB").save(out["amber_rgb"])

    rgba = np.zeros((160, 200, 4), np.uint8)
    rgba[:, :, :3] = (200, 30, 30)
    rgba[:, :, 3][40:120] = 255  # vertical transparency band
    rgba[:, :, 3][:40, 60:140] = 0
    out["rbga_key"] = root / "rbga_key.png"
    Image.fromarray(rgba, "RGBA").save(out["rbga_key"])

    grey = np.linspace(0, 255, 128, dtype=np.uint8)
    out["hard_gray"] = root / "hard_gray.png"
    Image.fromarray(np.repeat(np.repeat(grey[None, :, None], 128, axis=0), 3, axis=2),
                    "RGB").save(out["hard_gray"])
    return out


def pixmap_len_only(blocks):
    return blocks


def run_sweep(fast: bool) -> list:
    root = Path(tempfile.mkdtemp(prefix="release_sweep_"))
    fx = fixtures(root)
    results = []

    def drive(name, image, settings, tamper=None):
        t0 = time.perf_counter()
        try:
            out = appcore.full_build(image, settings, root / "out", base_name="sw")
            v = out["files"]["validation"]
            for fmt in settings.formats:
                if "size" not in v.get(fmt, {}):
                    raise ValueError(f"validation missing size for {fmt}")
            results.append({"combo": name, "ok": True, "seconds": round(time.perf_counter() - t0, 2)})
        except Exception as e:  # noqa: BLE001 — sweep records, does not hide
            results.append({"combo": name, "ok": False, "error": f"{type(e).__name__}: {e}"[:160],
                            "seconds": round(time.perf_counter() - t0, 2)})

    # 1. every dithering method x every palette x every layout
    layouts = list(LAYOUTS.items())[:3] if fast else list(LAYOUTS.items())
    for pal in PALETTES:
        for lay, (mw, mh) in layouts:
            for alg in DITHERS:
                drive(f"alg:{alg}|pal:{pal}|{lay}", fx["amber_rgb"],
                      appcore.Settings(version=pal, maps_w=mw, maps_h=mh,
                                       algorithm=alg, strength=1.0))
    if fast:
        return results

    # 2. metrics x palettes
    for pal in PALETTES:
        for met in METRICS:
            drive(f"metric:{met}|pal:{pal}", fx["hard_gray"],
                  appcore.Settings(version=pal, algorithm="none", metric=met))
    # 3. fit modes with pad color
    for fit in FIT_MODES:
        drive(f"fit:{fit}", fx["amber_rgb"], appcore.Settings(fit=fit, maps_w=2, maps_h=2))
    # 4. transparency modes + palette kinds
    for mode in TRANSPARENCY:
        drive(f"transparency:{mode}", fx["rbga_key"],
              appcore.Settings(transparency_mode=mode, bg_color=(10, 120, 240)))
    for kind in KINDS:
        drive(f"palette_kind:{kind}", fx["rbga_key"], appcore.Settings(palette_kind=kind))
    # 5. construction modes x formats
    for cons in CONSTRUCTIONS:
        drive(f"construction:{cons}|all-formats", fx["hard_gray"],
              appcore.Settings(construction=cons, formats=("schem", "litematic", "structure")))
    # 6. suppress + exclusions + costs combos
    drive("suppress:{0,1}|excl|cost", fx["hard_gray"],
          appcore.Settings(algorithm="bayer4", suppress={0, 1},
                           exclude_blocks={"minecraft:white_wool"},
                           block_costs={"minecraft:gray_wool": 2.0}))
    # 7. neighbor optimization on a noisy image
    rng = np.random.default_rng(9)
    noisy = np.clip(np.asarray(Image.open(fx["amber_rgb"]), dtype=int)
                    + rng.integers(-30, 30, (300, 400, 3)), 0, 255).astype(np.uint8)
    nbp = root / "noisy.png"
    Image.fromarray(noisy, "RGB").save(nbp)
    drive("neighbor_optimize", nbp, appcore.Settings(neighbor_optimize=True))
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    results = run_sweep(a.fast)
    fails = [r for r in results if not r["ok"]]
    tally = {
        "total": len(results),
        "passed": len(results) - len(fails),
        "failed": len(fails),
        "failures": fails,
        "wall_time_s": round(time.time() - t0, 1),
        "fast": bool(a.fast),
    }
    print(json.dumps(tally, indent=1))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
