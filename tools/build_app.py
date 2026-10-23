#!/usr/bin/env python3
"""Build the launchable local FireFly Map-Art Engine distribution.

Production layout (offline by design; no GitHub/cloud involvement):

    FireflyEngine/
      firefly_engine.py      launcher/core (GUI default, --selftest, process)
      FireflyEngine.cmd      Windows launcher stub
      firefly-engine.sh      POSIX launcher stub
      FireflyEngine-user.md  what this is, how to launch, requirements
      MANIFEST.json          sha256 integrity of shipped files
      src/                   firefly package (evolution + mapart)
      assets/mc_palettes.json

Usage: python tools/build_app.py [--out DIR]
After copying, the build runs `--selftest` *from inside the output dir* to
prove the app exists and works from its own location.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CMD_STUB = """\
@echo off
rem FireFly Map-Art Engine — local Windows launcher (offline, no install)
where py >nul 2>nul && (py -3 "%~dp0firefly_engine.py" %*) || (python "%~dp0firefly_engine.py" %*)
"""

SH_STUB = """\
#!/usr/bin/env bash
exec "${0%/*}/firefly_engine.py" "$@"
"""

README = """\
# FireFly Map-Art Engine + Evolution Lab — local build

Turns an image into Minecraft map art and exports a Sponge .schem; includes a
built-in deterministic test-image suite, benchmark campaigns and an Evolution
Lab with pause/resume/checkpointing.

Tabs: Map-Art | Test Suite | Benchmark | Evolution Lab | History | Settings.

## Launch (Windows)
- double-click `FireflyEngine.cmd`
- or `python firefly_engine.py` / `py -3 firefly_engine.py`

## Requirements (one-time, local)
- Python 3.9+ with tkinter enabled (python.org installer includes it)
- `pip install numpy pillow`
No other setup. No network use at any point in normal operation.

## Commands
- `FireflyEngine.cmd`                        GUI
- `python firefly_engine.py --selftest`      headless verification
- `python firefly_engine.py process --image img.png --maps 2x2 --out out`

## Files produced by an export
- `<name>_<w>x<h>.schem`  Sponge schematic v2 (re-openable, validated)
- `<name>_preview.png`    the exact processed map-color preview
- `<name>_report.json`    settings, block counts, validation summary

Lab data: `mapart-data/lab/` (state.json checkpoints + history.jsonl).
Benchmark data: `mapart-data/benchmark-quick/` etc.

## Honesty notes
- Palettes are embedded approximations of public map-color tables; the
  survival wool preset is the conservative one. See assets/mc_palettes.json
  and docs/FEATURE-INVENTORY.md (in the source repository).
- The .schem is validated by the engine's own structural parser; a real
  WorldEdit/Litematica import test remains open work.
"""


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def build(out: Path) -> dict:
    # resolve FIRST: the selftest probe runs with cwd=out and would otherwise
    # resolve a relative --out against the build dir itself (doubled path)
    out = Path(out).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copytree(ROOT / "src", out / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (out / "assets").mkdir()
    shutil.copy2(ROOT / "assets" / "mc_palettes.json", out / "assets" / "mc_palettes.json")
    shutil.copy2(ROOT / "firefly_engine.py", out / "firefly_engine.py")
    (out / "FireflyEngine.cmd").write_text(CMD_STUB, encoding="utf-8")
    (out / "firefly-engine.sh").write_text(SH_STUB, encoding="utf-8")
    (out / "FireflyEngine-user.md").write_text(README, encoding="utf-8")
    (out / "firefly-engine.sh").chmod(0o755)

    files = []
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name != "MANIFEST.json":
            files.append({"path": p.relative_to(out).as_posix(),
                          "size": p.stat().st_size, "sha256": _sha(p)})
    manifest = {"built": datetime.datetime.now().isoformat(timespec="seconds"),
                "files": files, "network_use": "none in normal operation"}
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # self-verify *from the output dir* — the app must work where it lives
    probe = subprocess.run([sys.executable, str(out / "firefly_engine.py"), "--selftest"],
                           capture_output=True, text=True, cwd=str(out), timeout=600)
    ok = probe.returncode == 0 and '"selftest": "ok"' in probe.stdout
    return {"built": ok, "out": str(out), "files": len(files),
            "selftest_tail": (probe.stdout + probe.stderr)[-500:] if not ok else "ok"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist" / "FireflyEngine"))
    a = ap.parse_args()
    res = build(Path(a.out))
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0 if res["built"] else 1


if __name__ == "__main__":
    sys.exit(main())
