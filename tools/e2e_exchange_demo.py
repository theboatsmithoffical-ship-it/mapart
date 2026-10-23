"""Offline end-to-end demo of the .ffevopack exchange workflow.

Two independent "engines" on one machine, no network anywhere:

  engine A: evolve (real pipeline, tiny budget) → export .ffevopack
  engine B: drop it in optimizer-inbox → inbox scan (imports, quarantines a
            corrupt sibling, dedupes) → evaluate-imported (sender scores stay
            claims) → seed-from-store → seeded evolution run → re-export
            fresh, self-measured discoveries → merge the two packages

Usage: python tools/e2e_exchange_demo.py [WORKROOT]
Every step prints compact JSON; the script exits non-zero on the first
failure so it can run as a CI smoke test.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402


def fixture_png(path: Path) -> Path:
    """A small deterministic 'photo-like' benchmark fixture (offline)."""
    x = np.linspace(28, 226, 128, dtype=np.float64)
    y = np.linspace(226, 28, 128, dtype=np.float64)
    r = np.broadcast_to(x[None, :], (128, 128))
    b = np.broadcast_to(y[:, None], (128, 128))
    g = 0.5 * (r + b)
    waves = np.sin(r / 11.0) * 14 + np.cos(b / 7.0) * 10
    arr = np.stack([r + waves, g + waves * 0.5, b], axis=-1).clip(0, 255)
    Image.fromarray(arr.astype(np.uint8), "RGB").save(path, format="PNG")
    return path


def run(cmd: list[str]) -> dict:
    """Run a CLI command; return its parsed JSON stdout."""
    import os
    import subprocess
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run([sys.executable, "-m", "firefly.evolution.cli", *cmd],
                          cwd=str(ROOT), env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
                         f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout[-2000:]}")
    blob = proc.stdout[proc.stdout.find("{"):]
    if not blob:
        raise SystemExit(f"command produced no JSON: {' '.join(cmd)}\n{proc.stdout[-2000:]}")
    return json.loads(blob)


def _clip(v, n=3):
    return [str(x) for x in v[:n]] if isinstance(v, list) else v


def step(title, fn):
    print(f"\n=== {title} " + "=" * max(1, 58 - len(title)))
    out = fn()
    print(json.dumps(out, default=_clip)[:600])


def main() -> int:
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("evolution_demo")
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    fixture = fixture_png(base / "demo_fixture.png")
    pkg_a = base / "from_engine_a.ffevopack"
    wsB = base / "engine_b"
    inbox_b = wsB / "optimizer-inbox"
    run_dir_a = base / "run_engine_a"

    step("engine A: evolve", lambda: run([
        "run", "--fixtures", str(fixture), "--run-dir", str(run_dir_a),
        "--pop", "6", "--gens", "2", "--seed", "42", "--eval-budget", "8"]))

    step("engine A: export package (compact, self-verified only)", lambda: run(
        ["package", "export", "--run-dir", str(run_dir_a), "--input", str(fixture),
         "--out", str(pkg_a), "--include-assets", "--label", "demo-engine-a"]))

    step("engine A: inspect", lambda: run(["package", "inspect", "--input", str(pkg_a)]))

    inbox_b.mkdir(parents=True)
    (inbox_b / pkg_a.name).write_bytes(pkg_a.read_bytes())
    (inbox_b / "not.really.ffevopack").write_bytes(b"PK\x01\x02 garbage")
    step("engine B: inbox scan (import + quarantine of a corrupt sibling)",
         lambda: run(["inbox", "--root", str(wsB)]))

    def _in(handler):
        pass
    _in  # noqa

    def _inbox_details():
        reg = json.loads((wsB / "store" / "registry.json").read_text(encoding="utf-8"))
        return {"registry_kinds": [e.get("kind") for e in reg["entries"]],
                "quarantine_files": sorted(p.name for p in (wsB / "quarantine").iterdir())}
    step("engine B: registry + quarantine proof", _inbox_details)

    step("engine B: independent evaluation against the core suite", lambda: run([
        "evaluate-imported", "--root", str(wsB)]))

    step("engine B: seed a fresh run from locally-confirmed candidates", lambda: run([
        "seed-from-store", "--root", str(wsB), "--fixtures", str(fixture),
        "--run-dir", str(wsB / "runs" / "seeded"), "--pop", "5", "--gens", "2",
        "--seed", "9", "--eval-budget", "6"]))

    step("engine B: re-export its own independently-verified discoveries",
        lambda: run(["package", "export", "--run-dir", str(wsB / "runs" / "seeded"),
                     "--input", str(fixture),
                     "--out", str(base / "from_engine_b.ffevopack")]))

    step("merge both packages", lambda: run([
        "package", "merge", "--a", str(pkg_a), "--b", str(base / "from_engine_b.ffevopack"),
        "--out", str(base / "merged.ffevopack"), "--notes", "two-engine exchange demo"]))

    print("\n=== demo artifacts:")
    for p in sorted(base.rglob("*.ffevopack")):
        print(f"  {p}  ({p.stat().st_size} bytes)")
    for p in sorted((wsB / "quarantine").iterdir()):
        print(f"  quarantined: {p.name}")
    print("\nE2E exchange demo complete (fully offline: no HTTP/socket used).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
