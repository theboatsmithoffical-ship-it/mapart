#!/usr/bin/env python3
"""Dithering benchmark across registered algorithms (engine, offline).

Renders one fixture through every registered algorithm and writes a contact
sheet PNG + metrics CSV. State lives in a small dataclass, not globals.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from firefly.evolution.dither import ALGORITHMS, dither_image  # noqa: E402
from firefly.evolution.synth import builtins, pixel_sha256  # noqa: E402
from firefly.mapart import registry  # noqa: E402

try:
    from PIL import Image
except ImportError as e:  # pragma: no cover
    print(f"error: Pillow required: {e}", file=sys.stderr)
    sys.exit(2)


@dataclass
class BenchState:
    fixture: str = "steady"
    results: list = field(default_factory=list)
    tiles: list = field(default_factory=list)


def contact_sheet(st: BenchState, out: Path) -> None:
    """Compose results side by side; state passed in explicitly (no globals)."""
    if not st.tiles:
        raise ValueError("contact sheet needs rendered tiles")
    import numpy as np
    arrs = [t for t in st.tiles]
    h, w = arrs[0].shape[:2]
    cols = min(4, len(arrs))
    rows = (len(arrs) + cols - 1) // cols
    sheet = np.full((rows * h, cols * w, 3), 30, dtype=np.uint8)
    for i, arr in enumerate(arrs):
        r, c = divmod(i, cols)
        sheet[r * h:(r + 1) * h, c * w:(c + 1) * w] = arr
    Image.fromarray(sheet, "RGB").save(out, format="PNG")


def run(opts) -> int:
    st = BenchState(fixture=opts.fixture)
    imgs = builtins()
    fixture_img = imgs[st.fixture]
    out_dir = Path(opts.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    genome = {"algorithm": "floyd_steinberg", "strength": 0.0, "bias": 0,
              "serpentine": True, "gamma": 1.0, "mapping": "nearest"}
    for alg in sorted(ALGORITHMS) + ["none"]:
        params = registry.get(alg).default_params_for() if alg in registry.all() else {}
        genome2 = dict(genome)
        genome2.update(params)
        genome2["algorithm"] = alg
        if "serpentine" not in genome2:
            genome2["serpentine"] = True
        t0 = time.perf_counter()
        out = dither_image(fixture_img, genome2)
        elapsed = time.perf_counter() - t0
        from firefly.evolution.dither import score_pair
        st.results.append({"algorithm": alg, "score": score_pair(fixture_img, out),
                           "seconds": round(elapsed, 4),
                           "output_sha": pixel_sha256(out)})
        st.tiles.append(out)
    contact_sheet(st, out_dir / "contact_sheet.png")
    with open(out_dir / "metrics.csv", "w", newline="", encoding="utf-8") as f:
        wcsv = csv.writer(f)
        wcsv.writerow(["algorithm", "score", "seconds", "output_sha"])
        for r in st.results:
            wcsv.writerow([r["algorithm"], r["score"], r["seconds"], r["output_sha"]])
    import json
    (out_dir / "metrics.json").write_text(json.dumps(st.results, indent=1), encoding="utf-8")
    best = min(st.results, key=lambda r: r["score"])
    print(f"{len(st.results)} algorithms; best {best['algorithm']} score {best['score']:.3f}")
    print(f"artifacts: {out_dir / 'contact_sheet.png'}, metrics.csv")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", choices=sorted(builtins()), default="steady")
    ap.add_argument("--out", default=str(ROOT / "dither-bench-out"))
    a = ap.parse_args()
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
