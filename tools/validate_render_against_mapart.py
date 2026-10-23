#!/usr/bin/env python3
"""Validate a simulator render against the map-art plan.

The render simulator is an EXTERNAL executable (never bundled here). Where it
lives is configurable in this order:
  1. --simulator PATH   (relative paths resolve against the CURRENT WORKING
                         directory; use an absolute path or ../ style relative
                         paths from where you invoke the tool)
  2. MAPART_SIMULATOR env var (same resolution rules)
  3. config file --config PATH  (JSON: {"simulator": "..."})
There is deliberately NO developer-specific default path.

Exit codes:
  0  validation passed against a real simulator run
  2  configuration error (simulator missing/unrunnable, bad inputs) — clean
  3  validation mismatch (simulator ran; output disagrees with the plan)
  1  unexpected internal error (bug — full traceback is correct here)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CONFIG_ERROR = 2
MISMATCH = 3


class ConfigError(Exception):
    """Expected, user-facing configuration/input problem."""


def find_simulator(args) -> Path:
    candidates = []
    if args.simulator:
        candidates.append(Path(args.simulator))
    env = os.environ.get("MAPART_SIMULATOR")
    if env:
        candidates.append(Path(env))
    if args.config:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        sim = cfg.get("simulator")
        if sim:
            candidates.append(Path(sim))
    if not candidates:
        raise ConfigError(
            "render simulator is not configured.\n"
            "  what:  this tool needs an external map-art render simulator executable\n"
            "  how:   pass --simulator PATH, or set MAPART_SIMULATOR, or use\n"
            "         --config FILE containing {\"simulator\": \"...\"} (relative paths\n"
            "         resolve against the current working directory)\n"
            "  then:  python tools/validate_render_against_mapart.py --job plan.json\n"
            "         --render render.png --simulator C:/path/to/sim.exe")
    for c in candidates:
        if c.exists() and (c.is_file() and (os.access(c, os.X_OK) or c.suffix.lower() in (".exe", ".py"))):
            return c
    raise ConfigError(
        f"configured simulator not found or not runnable: {candidates[0]}\n"
        "  check the path (quotes ok in cmd, use forward slashes in git-bash),\n"
        "  and make sure the file is the simulator executable itself.")


def run_simulator(sim: Path, job_path: Path, out_render: Path, timeout: float) -> subprocess.CompletedProcess:
    """Invoke the simulator; its genuine failures are preserved as-is.
    .py simulators are run with the current Python (script interpreters are
    not executable via CreateProcess on Windows)."""
    if sim.suffix.lower() == ".py":
        cmd = [sys.executable, str(sim), "--job", str(job_path), "--out", str(out_render)]
    else:
        cmd = [str(sim), "--job", str(job_path), "--out", str(out_render)]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:  # disappeared between check and run
        raise ConfigError(f"simulator vanished before execution: {e}")
    except PermissionError as e:
        raise ConfigError(f"simulator is not executable: {e}")
    except subprocess.TimeoutExpired as e:
        raise ConfigError(f"simulator timed out after {timeout:.0f}s")


def compare_render(out_render: Path, job: dict) -> dict:
    """Compare simulator render with the plan's expected map colors."""
    import numpy as np
    from PIL import Image

    plan = job.get("expected_map_colors")
    if not isinstance(plan, list) or not plan:
        raise ConfigError("job JSON must contain a non-empty 'expected_map_colors' 2-D array")
    h = len(plan)
    w = len(plan[0])
    rendered = np.asarray(Image.open(out_render).convert("RGB"))
    if rendered.shape[:2] != (h, w):
        raise ConfigError(f"render is {rendered.shape[:2]}, plan expects {(h, w)}")
    plan_arr = np.asarray(plan, dtype=np.int16)
    # JSON object keys are strings; coerce ids to int so comparisons hold
    palette = {tuple(c): int(cid) for cid, c in job.get("color_lookup", {}).items()}
    if not palette:
        raise ConfigError("job JSON must contain 'color_lookup' mapping id -> RGB")
    mismatches = 0
    maxrow = 0
    for y in range(h):
        for x in range(w):
            rgb = tuple(int(v) for v in rendered[y, x])
            cid = palette.get(rgb)
            if cid is None or plan_arr[y, x] != cid:
                mismatches += 1
                maxrow = y
    return {"pixels": h * w, "mismatches": mismatches, "first_bad_row": maxrow,
            "ok": mismatches == 0}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--job", required=True, help="job JSON: {expected_map_colors, color_lookup}")
    ap.add_argument("--render", help="where the simulator writes its render (temp file if omitted)")
    ap.add_argument("--simulator", help="simulator executable path (relative to cwd)")
    ap.add_argument("--config", help="JSON config file with {'simulator': PATH}")
    ap.add_argument("--timeout", type=float, default=120.0)
    a = ap.parse_args(argv)
    try:
        sim = find_simulator(a)
        try:
            job = json.loads(Path(a.job).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise ConfigError(f"job JSON unreadable/invalid: {e}")
        if not isinstance(job, dict):
            raise ConfigError("job JSON must be an object")
        tmp_render = None
        out = Path(a.render) if a.render else None
        if out is None:
            tmp_render = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            out = Path(tmp_render.name)
            tmp_render.close()  # Windows: sim must be able to (re)open the path
        proc = run_simulator(sim, Path(a.job), out, a.timeout)
        if proc.returncode != 0:
            # preserve legitimately reported simulator errors verbatim, no swallow
            print(f"simulator exited {proc.returncode}\n{proc.stderr[-2000:]}", file=sys.stderr)
            return proc.returncode or CONFIG_ERROR
        try:
            report = compare_render(out, job)
        finally:
            if tmp_render:
                Path(tmp_render.name).unlink(missing_ok=True)
        print(json.dumps({"ok": report["ok"], "report": report,
                          "simulator": str(sim)}, sort_keys=True))
        return 0 if report["ok"] else MISMATCH
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return CONFIG_ERROR
    # unexpected exceptions intentionally propagate (tracebacks are not bugs to hide)


def cli_entry() -> int:
    try:
        import errno
    except ImportError:
        pass
    try:
        return main()
    except SystemExit as e:
        return int(e.code or 0)


if __name__ == "__main__":
    sys.exit(main())
