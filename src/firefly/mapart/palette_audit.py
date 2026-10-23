"""Headless in-game-accuracy audit for the map-art palette.

Renders the actual palette (every map color id × shade) through an EXTERNAL
render simulator (the same MAPART_SIMULATOR contract as
tools/validate_render_against_mapart.py: an executable or .py script accepting
--job <json> and writing its render PNG to --out) and compares the observed
per-pixel colors with the palette's modeled colors.

What this path makes REAL:
- a per-(map color, shade, block) verification record: expected_rgb,
  observed_rgb, CIEDE2000 delta and a verified/failed verdict;
- a durable sidecar report (assets/palette_audit_<version>.json) that the
  palette loader reads back, upgrading the honest "approximation" status to
  "simulator-audited (n/m verified)" for exactly the colors it could check.

What this path does NOT claim:
- the tool itself cannot conjure accuracy — without a configured simulator it
  refuses (typed error, nothing recorded). Verdicts are meaningful only as far
  as the simulator faithfully represents real Minecraft rendering; a self-check
  stub can validate the plumbing and nothing more.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ASSET_DIR = Path(__file__).resolve().parents[3] / "assets"
if not ASSET_DIR.exists():  # portable layout fallback
    ASSET_DIR = Path(__file__).resolve().parents[2] / "assets"

BANDS_PER_MAP = 8          # horizontal bands per 128x128 map render
BAND_HEIGHT = 128 // BANDS_PER_MAP
DEFAULT_TOLERANCE = 2.5    # CIEDE2000 units; map renders are palette-quantized

SIDECAR_FORMAT = "firefly-palette-audit/1"


class AuditError(ValueError):
    pass


def resolve_simulator(args) -> Path:
    """Same resolution order as the render validator: --simulator, then the
    MAPART_SIMULATOR env var, then --config JSON {'simulator': PATH}.
    Refuses loudly when nothing is configured — never substitutes a fake."""
    candidates = []
    sim = getattr(args, "simulator", None)
    if sim:
        candidates.append(Path(sim))
    import os
    env = os.environ.get("MAPART_SIMULATOR")
    if env:
        candidates.append(Path(env))
    cfg = getattr(args, "config", None)
    if cfg:
        blob = json.loads(Path(cfg).read_text(encoding="utf-8"))
        s = blob.get("simulator")
        if s:
            candidates.append(Path(s))
    for c in candidates:
        if c.is_file() and (c.suffix.lower() in (".py", ".exe") or c.is_absolute()):
            return c
    if candidates:
        raise AuditError(f"configured simulator not found or not runnable: {candidates[0]}")
    raise AuditError(
        "no render simulator configured — this audit measures REAL rendering\n"
        "  how: pass --simulator PATH, or set MAPART_SIMULATOR, or use\n"
        "       --config FILE containing {\"simulator\": \"...\"}")


def _run_simulator(sim: Path, job_path: Path, out_render: Path, timeout: float):
    if sim.suffix.lower() == ".py":
        cmd = [sys.executable, str(sim), "--job", str(job_path), "--out", str(out_render)]
    else:
        cmd = [str(sim), "--job", str(job_path), "--out", str(out_render)]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise AuditError(f"simulator vanished before execution: {e}")
    except subprocess.TimeoutExpired:
        raise AuditError(f"simulator timed out after {timeout:.0f}s")


def audit_palette(version_key: str, sim: Path, workdir=None, timeout: float = 120.0,
                  tolerance: float = DEFAULT_TOLERANCE,
                  out_path: Path | None = None) -> dict:
    """Audit every (map color, shade) of one palette version.

    Builds banded render jobs (8 bands per 128px map), invokes the simulator
    once per job, and measures each band's median RGB against the palette's
    expected value. Writes a sidecar verdict file and returns the summary.
    """
    from .mapart import load_palette, ciede2000, MAP_SIZE

    pal = load_palette(version_key)
    work = Path(workdir) if workdir else (ASSET_DIR / f"audit_run_{version_key}")
    work.mkdir(parents=True, exist_ok=True)

    targets = [{"map_color": c["map_color"], "shade": c["shade"],
                "block": c["block"], "expected_rgb": list(map(int, c["rgb"]))}
               for c in pal.colors]

    # Group into jobs whose BANDS_PER_MAP slots never repeat a map_color id:
    # the job's color_lookup is keyed by id alone, so two shades of one id in
    # the same job would be indistinguishable to a renderer keyed by id.
    jobs: list = []
    for t in targets:
        placed = False
        for job in jobs:
            if len(job) < BANDS_PER_MAP and \
                    not any(item["map_color"] == t["map_color"] for item in job):
                job.append(t)
                placed = True
                break
        if not placed:
            jobs.append([t])
    records = []
    sim_calls = 0
    for ji, band in enumerate(jobs):
        job_path = work / f"job_{ji}.json"
        render_path = work / f"render_{ji}.png"
        grid = np.zeros((MAP_SIZE, MAP_SIZE), dtype=int)
        lookup = {}
        had_id = set()
        for bi, spec in enumerate(band):
            grid[bi * BAND_HEIGHT:(bi + 1) * BAND_HEIGHT, :] = spec["map_color"]
            key = str(spec["map_color"])
            if key not in lookup:
                base_rgb = spec["expected_rgb"]
                lookup[key] = base_rgb  # informational; measurement is ours
            had_id.add(key)
        job = {"expected_map_colors": grid.tolist(),
               "color_lookup": lookup,
               "engine": "firefly-palette-audit",
               "bands": [[b["map_color"], b["shade"]] for b in band]}
        job_path.write_text(json.dumps(job), encoding="utf-8")
        proc = _run_simulator(sim, job_path, render_path, timeout)
        if proc.returncode != 0:
            raise AuditError(f"simulator exited {proc.returncode} on job {ji}: "
                             f"{(proc.stderr or '')[-400:]}")
        from PIL import Image
        rendered = np.asarray(Image.open(render_path).convert("RGB"))
        if rendered.shape[:2] != (MAP_SIZE, MAP_SIZE):
            raise AuditError(f"render is {rendered.shape[:2]}, expected "
                             f"{(MAP_SIZE, MAP_SIZE)}")
        for bi, spec in enumerate(band):
            band_px = rendered[bi * BAND_HEIGHT:(bi + 1) * BAND_HEIGHT, :]
            observed = np.median(band_px.reshape(-1, 3), axis=0)
            expected = np.array(spec["expected_rgb"], dtype=float)
            delta = float(ciede2000(expected[None, :], observed[None, :])[0])
            records.append({**spec, "observed_rgb": [int(x) for x in observed],
                            "delta": round(delta, 3),
                            "status": "verified" if delta <= tolerance else "failed"})
        sim_calls += 1

    verified = sum(1 for r in records if r["status"] == "verified")
    summary = {
        "format": SIDECAR_FORMAT,
        "version": version_key,
        "simulator": str(sim),
        "tolerance": tolerance,
        "created": time.time(),
        "sim_calls": sim_calls,
        "summary": {"verified": verified, "failed": len(records) - verified,
                    "total": len(records)},
        "records": records,
        "honesty": "verdicts reflect THIS simulator's rendering of each color; "
                   "accuracy beyond that depends on the simulator's fidelity "
                   "to the actual Minecraft version",
    }
    if out_path is None:
        out_path = ASSET_DIR / f"palette_audit_{version_key}.json"
    out_path = Path(out_path)
    out_path.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def read_audit(version_key: str, asset_dir: Path | None = None) -> dict | None:
    """Load the sidecar verdict file for a version, or None if none exists."""
    p = (Path(asset_dir) if asset_dir else ASSET_DIR) / f"palette_audit_{version_key}.json"
    if not p.is_file():
        return None
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if blob.get("format") != SIDECAR_FORMAT:
        return None
    return blob


def attach_audit(pal, asset_dir: Path | None = None) -> dict:
    """Attach sidecar verdicts to a loaded Palette in place.

    Returns the audit summary added to pal.verification / per-color fields."""
    audit = read_audit(pal.version_key, asset_dir)
    if not audit:
        pal.verification = None
        return None
    by_key = {(r["map_color"], r["shade"]): r for r in audit.get("records", [])}
    for c in pal.colors:
        rec = by_key.get((c["map_color"], c["shade"]))
        if rec:
            c["audit"] = {"status": rec["status"], "observed_rgb": rec["observed_rgb"],
                          "delta": rec["delta"]}
    s = audit.get("summary", {})
    pal.verification = {
        "kind": "simulator-audited",
        "verified": s.get("verified", 0), "total": s.get("total", 0),
        "tolerance": audit.get("tolerance"),
        "created": audit.get("created"),
        "simulator": audit.get("simulator"),
        "honesty": audit.get("honesty"),
    }
    pal.verification_status = (
        f"simulator-audited {s.get('verified', 0)}/{s.get('total', '?')} colors "
        f"verified vs '{Path(str(audit.get('simulator'))).name}' "
        f"(tolerance {audit.get('tolerance')}); "
        "approximation basis otherwise unchanged")
    return pal.verification
