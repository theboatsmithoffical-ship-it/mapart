#!/usr/bin/env python3
"""Map-art benchmark CLI (offline; runs the engine's real pipeline).

Usage:
  python tools/benchmark.py [--image FILE] [--maps WxH] [--pal NAME]
      [--algorithm NAME] [--strength S] [--suite quick|full] [--out DIR]

Dimension rules enforced BEFORE any processing: positive integers and each of
W,H a multiple of 128 (the pipeline's documented per-map size). Invalid
dimensions are usage errors -> exit 2 with concise usage, never a traceback.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from firefly.mapart import appcore  # noqa: E402

USAGE = "usage: benchmark.py [--image FILE] [--maps WxH] [--pal NAME] [--algorithm NAME] [--strength S] [--out DIR]"


class UsageError(Exception):
    pass


def parse_dims(spec: str) -> tuple:
    if not spec or "x" not in spec.lower():
        raise UsageError(f"--maps expects WxH, got {spec!r}. {USAGE}")
    try:
        parts = spec.lower().split("x")
        if len(parts) != 2:
            raise ValueError(spec)
        w = int(parts[0]); h = int(parts[1])
    except ValueError:
        raise UsageError(f"--maps expects integer WxH, got {spec!r}. {USAGE}")
    if w <= 0 or h <= 0:
        raise UsageError(f"dimensions must be positive, got {w}x{h}. {USAGE}")
    if w % 128 or h % 128:
        raise UsageError(f"each dimension must be multiple of 128 (map size), got {spec}. {USAGE}")
    return w, h


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--image", default=str(ROOT / "fixtures_engine" / "real_e2e.png"))
    ap.add_argument("--maps", default="128x128")
    ap.add_argument("--pal", default="survival-wool-1.8")
    ap.add_argument("--algorithm", default="floyd_steinberg")
    ap.add_argument("--strength", type=float, default=1.0)
    ap.add_argument("--out", default=str(ROOT / "mapart-output"))
    a = ap.parse_args(argv)
    try:
        w, h = parse_dims(a.maps)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    try:
        s = appcore.Settings(maps_w=w // 128, maps_h=h // 128, version=a.pal,
                             algorithm=a.algorithm, strength=a.strength)
    except ValueError as e:
        print(f"error: {e}\n{USAGE}", file=sys.stderr)
        return 2
    if not Path(a.image).is_file():
        print(f"error: image not found: {a.image}\n{USAGE}", file=sys.stderr)
        return 2
    out = appcore.full_build(a.image, s, Path(a.out), base_name="benchmark")
    validation = dict(out["files"].pop("validation"))
    # default CLI settings export only the Sponge .schem — expose its flat
    # validation shape for tooling that reads blocks_placed/size directly
    v = validation.get("schem", validation)
    print(json.dumps({"ok": True, "dims": [w, h], **out["files"], "validation": v},
                     sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
