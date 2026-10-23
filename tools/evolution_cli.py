#!/usr/bin/env python3
"""Evolution CLI wrapper (keeps package CLI importable without pip install).

Statuses the audit expects:
  --help / -h                       usage + exit 0
  no args / --status                engine status JSON, exit 0
  anything else                     forwarded to firefly.evolution.cli
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ,
       "PYTHONPATH": str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")}


def status() -> dict:
    from firefly.evolution.dither import ALGORITHMS
    return {"engine": "firefly.evolution", "algorithms": len(ALGORITHMS),
            "cli": "python -m firefly.evolution.cli", "run_demo": "tools/e2e_exchange_demo.py"}


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("--status", "status"):
        print(json.dumps(status(), sort_keys=True))
        return 0
    if args[0] in ("--help", "-h"):
        print(__doc__)
        return 0
    # drop a redundant status-ish token, forward the rest to the real CLI
    proc = subprocess.run([sys.executable, "-m", "firefly.evolution.cli", *args],
                          env=ENV, cwd=str(ROOT), capture_output=True, text=True,
                          timeout=600)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
