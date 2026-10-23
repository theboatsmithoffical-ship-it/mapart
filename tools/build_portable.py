#!/usr/bin/env python3
"""Build the portable FireFly Dithering Optimization Engine distribution.

Layout produced (single folder, copyable to a USB stick / clean machine):

    firefly-portable/
      firefly_portable.py      launcher: doctor | smoke-run | demo | run <args>
      firefly-portable.cmd     Windows launcher stub
      firefly-portable.sh      POSIX launcher stub
      <launcher stubs for zsh> (.zsh, mirrors .sh)
      MANIFEST.json            sha256 + size for every shipped file (integrity)
      PORTABLE.md              what ships, prerequisites, guarantees/limits
      src/                     the engine package (src layout)
      tools/e2e_exchange_demo.py  the full offline exchange demo

Usage: python tools/build_portable.py [--out DIR] [--self-test]
The build runs an internal smoke run unless --no-self-test is passed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from firefly.evolution import VERSION as ENGINE_VERSION  # noqa: E402

WINDOW_STUB = """\
@echo off
rem FireFly portable launcher — engine only, never opens FireFly Map Studio (GUI)
where py >nul 2>nul && (py -3 "%~dp0firefly_portable.py" %*) || (python "%~dp0firefly_portable.py" %*)
"""

POSIX_STUB = """\
#!/usr/bin/env bash
exec "${0%/*}/firefly_portable.py" "$@"
"""

LAUNCHER_SRC = '''#!/usr/bin/env python3
"""Portable launcher for the standalone FireFly Dithering Optimization Engine.

GUI-independent by design: this launcher never opens or touches FireFly
Map Studio (no JVM, no jar, no window). Commands:

  doctor                     environment, dependencies and file integrity
  smoke-run  [WORKROOT]      offline engine round-trip incl. quarantine check
  demo       [WORKROOT]      the full two-engine .ffevopack exchange demo
  run <engine CLI args>      pass engine CLI args through (see README.md)

All commands are fully offline (no HTTP/socket use anywhere).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "MANIFEST.json"
REQUIRED_DEPS = ("numpy", "PIL")


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _env_report() -> dict:
    deps = {}
    for dep in REQUIRED_DEPS:
        try:
            m = importlib.import_module(dep)
            deps[dep] = getattr(m, "__version__", "installed")
        except Exception as e:  # noqa: BLE001
            deps[dep] = f"missing ({e})"
    return deps


def _integrity() -> str:
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return f"manifest unreadable ({e})"
    bad = []
    for e in data.get("files", []):
        p = ROOT / e["path"]
        if not p.is_file():
            bad.append(e["path"])
            continue
        if _sha256_file(p) != e.get("sha256"):
            bad.append(e["path"])
    return "verified" if not bad else f"tampered: {bad[:5]}"


def doctor() -> dict:
    deps = _env_report()
    issues = []
    if not (ROOT / "src" / "firefly").is_dir():
        issues.append("src/ engine tree missing")
    for dep, st in deps.items():
        if isinstance(st, str) and st.startswith("missing"):
            issues.append(f"dependency missing: {dep} (pip install {dep})")
    integ = _integrity()
    if not integ == "verified":
        issues.append(f"manifest integrity not verified: {integ}")
    out = {"command": "doctor", "ok": not issues, "python": sys.version.split()[0],
           "deps": deps, "integrity": integ, "issues": issues,
           "gui_touched": False}
    print(json.dumps(out, sort_keys=True))
    return out


def _emit(d: dict) -> None:
    print(json.dumps(d, sort_keys=True))


def smoke_run(work: str | None = None) -> dict:
    """Offline self-check round-trip: fixture -> evolve -> export -> inbox import
    with a corrupt package quarantined -> evaluate-imported."""
    sys.path.insert(0, str(ROOT / "src"))
    base = Path(work or tempfile.mkdtemp(prefix="firefly_portable_smoke_"))
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    try:
        from firefly.evolution import synth, evolve, pack, store  # noqa: vendored import
        import numpy as np
        from PIL import Image

        x = np.linspace(28, 226, 128, dtype=np.float64)
        y = np.linspace(226, 28, 128, dtype=np.float64)
        r = np.broadcast_to(x[None, :], (128, 128))
        b = np.broadcast_to(y[:, None], (128, 128))
        g = 0.5 * (r + b)
        w = np.sin(r / 11.0) * 14 + np.cos(b / 7.0) * 10
        arr = np.clip(np.stack([r + w, g + w * 0.5, b], axis=-1), 0, 255).astype(np.uint8)
        fx = base / "fixture.png"
        Image.fromarray(arr, "RGB").save(fx, format="PNG")

        env = synth.fixture_env({"smoke": arr}, "custom-smoke", {"smoke": str(fx)})
        run = base / "run"
        s = evolve.run_evolution(env, run, pop=3, gens=1, budget=3, seed=1)
        _emit({"step": "evolve", "ok": bool(s.get("ok")), "best": s.get("best", {}).get("score")})
        pkg = base / "demo.ffevopack"
        e = pack.export_package(run, fx, pkg, "smoke", include_assets=True)
        assert e["candidates"] >= 1, e
        _emit({"step": "export+inspect", "ok": True, "candidates": e["candidates"]})
        (base / "optimizer-inbox").mkdir()
        shutil.copy(pkg, base / "optimizer-inbox" / "demo.ffevopack")
        (base / "optimizer-inbox" / "bad.ffevopack").write_bytes(b"PK\\x01\\x02 garbage")
        rep = store.inbox_scan(base)
        assert len(rep["imported"]) == 1 and len(rep["quarantined"]) == 1, rep
        _emit({"step": "inbox", "ok": True, "quarantined": [q["file"] for q in rep["quarantined"]]})
        ev = store.evaluate_imported(base)
        assert ev["locally_verified"] >= 1, ev
        _emit({"step": "evaluate-imported", "ok": True, "locals": ev["locally_verified"]})
        status = {"ok": True, "work": str(base), "steps": 4}
        _emit(status)
        return status
    except Exception as ex:  # noqa: BLE001
        status = {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        _emit(status)
        raise SystemExit(1)


def demo(work: str | None = None) -> dict:
    """Run the shipped tools/e2e_exchange_demo.py from inside this folder."""
    script = ROOT / "tools" / "e2e_exchange_demo.py"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    workroot = Path(work or tempfile.mkdtemp(prefix="firefly_demo_"))
    proc = subprocess.run([sys.executable, str(script), str(workroot)],
                          cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        _emit({"command": "demo", "ok": False, "stderr": proc.stderr[-600:]})
        raise SystemExit(1)
    quarantined = sorted(ln.split(":", 1)[1].strip() for ln in proc.stdout.splitlines()
                         if ln.strip().startswith("quarantined:"))
    packages = list(workroot.rglob("*.ffevopack"))
    _emit({"command": "demo", "ok": True, "packages": len(packages),
           "quarantined": quarantined})
    return {"ok": True}


def run_engine(args) -> int:
    """Pass engine arguments through unchanged."""
    proc = subprocess.run([sys.executable, "-m", "firefly.evolution.cli", *args],
                          cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("--help", "-h"):
        print(__doc__)
        return 0
    op, rest = args[0], args[1:]
    if op == "doctor":
        d = doctor()
        return 0 if d["ok"] else 1
    if op == "smoke-run":
        smoke_run(rest[0] if rest else None)
        return 0
    if op == "demo":
        demo(rest[0] if rest else None)
        return 0
    if op == "run":
        return run_engine(rest)
    if op == "gui":
        print(json.dumps({"ok": False,
                          "error": "portable engine is GUI-independent; FireFly Map Studio (GUI) is not bundled and will not be opened"}))
        return 2
    print(f"unknown command {op!r}; try doctor|smoke-run|demo|run")
    return 2


if __name__ == "__main__":
    sys.exit(main())
'''

PORTABLE_README = """\
# FireFly portable distribution (dithering optimization engine, GUI-independent)

This folder is the standalone engine. FireFly Map Studio (the GUI / jar) is NOT
here and will NOT be opened by anything in this folder.

## Prerequisites
- Python 3.9+ (3.14 tested)
- numpy and Pillow on that interpreter (`pip install numpy pillow`)
- no network access is required, none is used

## Commands
- Windows:  firefly-portable.cmd doctor
- POSIX:    ./firefly-portable.sh doctor
- Direct:   python firefly_portable.py doctor | smoke-run | demo | run

`doctor` verifies the Python version, numpy/Pillow importability and the
sha256 MANIFEST.json integrity of every shipped file.

## What ships
- src/ — the engine package (dithering, evolution, .ffevopack exchange)
- tools/e2e_exchange_demo.py — full offline two-engine exchange demo
- tools/audit_palette_accuracy.py — headless palette accuracy audit (needs an
  external render simulator via MAPART_SIMULATOR/--simulator; refuses and
  records nothing without one)
- firefly_portable.py + .cmd/.sh/.zsh launcher stubs
- MANIFEST.json — sha256 of every shipped file (checked by `doctor`)

## Exchange intent
- The engine exports/imports .ffevopack discovery packages (zips of JSON
  parameters + assets). Packages are never executed; foreign members and
  traversal paths are quarantined.
- The engine re-benchmarks imported candidates against its own built-in
  suite (claims stay claims; local scores are stored separately).

## Known limits
- The default palette is a fixed 24-color set (see
  src/firefly/evolution/dither.py); it is NOT a Minecraft-accurate map
  palette. Expect future palette files in later revisions.
- On Windows the launcher uses `py -3` (fallback `python`) from PATH; no
  Python is bundled.
"""


def build(out: Path, self_test: bool = True) -> dict:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copytree(ROOT / "src", out / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    tools_dir = out / "tools"
    tools_dir.mkdir()
    shutil.copy2(ROOT / "tools" / "e2e_exchange_demo.py",
                 tools_dir / "e2e_exchange_demo.py")
    shutil.copy2(ROOT / "tools" / "audit_palette_accuracy.py",
                 tools_dir / "audit_palette_accuracy.py")
    (out / "firefly_portable.py").write_text(LAUNCHER_SRC, encoding="utf-8")
    (out / "firefly-portable.cmd").write_text(WINDOW_STUB, encoding="utf-8")
    (out / "firefly-portable.sh").write_text(POSIX_STUB, encoding="utf-8")
    (out / "firefly-portable.zsh").write_text(POSIX_STUB, encoding="utf-8")
    (out / "firefly-portable.sh").chmod(0o755)
    (out / "firefly-portable.zsh").chmod(0o755)
    (out / "PORTABLE.md").write_text(PORTABLE_README, encoding="utf-8")

    files = []
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name != "MANIFEST.json":
            files.append({"path": p.relative_to(out).as_posix(),
                          "size": p.stat().st_size,
                          "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
    manifest = {"built": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                "engine_version": ENGINE_VERSION, "files": files,
                "gui_touched_by_design": False}
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # verify launcher works straight out of the folder, then clean up
    probe = subprocess.run([sys.executable, str(out / "firefly_portable.py"), "doctor"],
                           capture_output=True, text=True, cwd=str(out), timeout=120)
    ok = probe.returncode == 0 and '"ok": true' in probe.stdout
    if not ok:
        return {"built": False, "doctor_stdout": probe.stdout, "doctor_stderr": probe.stderr}
    files_count = len(files)
    if self_test:
        smoke = subprocess.run([sys.executable, str(out / "firefly_portable.py"),
                                "smoke-run"], capture_output=True, text=True,
                               cwd=str(out), timeout=120)
        if smoke.returncode != 0:
            return {"built": False, "self_test": smoke.stderr[-800:], "files": files_count}
    return {"built": True, "out": str(out), "files": files_count,
            "engine_version": ENGINE_VERSION}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "portable" / "firefly-portable"))
    ap.add_argument("--no-self-test", action="store_true")
    args = ap.parse_args()
    res = build(Path(args.out), self_test=not args.no_self_test)
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0 if res.get("built") else 1


if __name__ == "__main__":
    sys.exit(main())
