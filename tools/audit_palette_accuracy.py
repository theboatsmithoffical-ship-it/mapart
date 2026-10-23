#!/usr/bin/env python3
"""Headless in-game-accuracy audit of the map-art palettes.

Renders every palette color through an external render simulator (the
MAPART_SIMULATOR contract also used by tools/validate_render_against_mapart.py)
and writes a per-color verification verdict sidecar:
  assets/palette_audit_<version>.json

The palette loader reads that sidecar back, so every afterwards-loaded palette
reports a REAL per-color verification status instead of the baseline
"approximation — never confirmed" label for the colors the simulator could
check. Honesty rules:
- with no simulator configured this tool REFUSES and records nothing;
- the verdicts are relative to your simulator's rendering fidelity;
- a stub renderer can only validate the plumbing, never game accuracy.

Usage:
  python tools/audit_palette_accuracy.py --version survival-wool-1.8 \
         --simulator C:/path/to/sim.exe            # or MAPART_SIMULATOR env
  python tools/audit_palette_accuracy.py --version map-full-approx-1.15 \
         --config sim_config.json
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

from firefly.mapart import palette_audit  # noqa: E402
from firefly.mapart.mapart import available_versions  # noqa: E402  # explicit submodule


def main(argv=None) -> int:
    available = sorted(available_versions())
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--version", required=True, help=f"palette version: {available}")
    ap.add_argument("--simulator", help="simulator executable/script path")
    ap.add_argument("--config", help="JSON config file with {\"simulator\": PATH}")
    ap.add_argument("--out", help="sidecar output path (default assets/palette_audit_<v>.json)")
    ap.add_argument("--tolerance", type=float, default=palette_audit.DEFAULT_TOLERANCE,
                    help="CIEDE2000 max delta for 'verified'")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--workdir", help="job/render scratch dir (default: managed temp dir)")
    a = ap.parse_args(argv)
    try:
        sim = palette_audit.resolve_simulator(a)
    except palette_audit.AuditError as e:
        print(f"configuration refused: {e}", file=sys.stderr)
        return 2
    try:
        t0 = time.time()
        if a.workdir:
            summary = palette_audit.audit_palette(
                a.version, sim, workdir=Path(a.workdir),
                tolerance=a.tolerance, timeout=a.timeout,
                out_path=Path(a.out) if a.out else None)
        else:
            with tempfile.TemporaryDirectory(prefix="palette_audit_") as scratch:
                summary = palette_audit.audit_palette(
                    a.version, sim, workdir=Path(scratch),
                    tolerance=a.tolerance, timeout=a.timeout,
                    out_path=Path(a.out) if a.out else None)
    except palette_audit.AuditError as e:
        print(f"audit failed: {e}", file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "version": a.version, "simulator": str(sim),
                      "verified": summary["summary"]["verified"],
                      "failed": summary["summary"]["failed"],
                      "total": summary["summary"]["total"],
                      "sim_calls": summary["sim_calls"],
                      "wall_time_s": round(time.time() - t0, 1)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
