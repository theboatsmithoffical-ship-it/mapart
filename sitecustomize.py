"""Auto-bootstrap the src layout when Python runs with this directory as cwd.

Python puts the current working directory on sys.path for `python -m ...`,
so this file at the repo root makes `firefly.evolution` importable from
CLI subprocesses launched inside the engine root (demo scripts, CI smoke
tests) without a pip install.
"""
from __future__ import annotations

import sys
from pathlib import Path

_src = str(Path(__file__).resolve().parent / "src")
if _src not in sys.path:
    sys.path.append(_src)
