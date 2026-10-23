"""Provenance for benchmark history: what produced a number.

Every benchmark row/report is only comparable if the code revision, palette
bytes, algorithm definitions, target Minecraft version and processing
settings are known. This module computes those identifiers deterministically
and without network access:

- `code_revision()` — git HEAD (+ `dirty` flag) of the given root, or
  "unknown" when no repository is present (this workspace is often a plain
  folder, so a missing revision is reported honestly, never invented).
- `palette_hash()` — sha256 of the palette's canonical color table and
  target version (NOT of the file's comments/whitespace).
- `algorithm_version()` — stable digest of the dithering definitions
  (algorithm list, error kernels, Bayer sizes) so a kernel change is visible
  even when no version constant is bumped.
- `target_mc_version()` — the declared Minecraft version of a palette.
- `run_context()` — one dict used by benchmark reports and the asset catalog.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from functools import lru_cache
from pathlib import Path

PALETTE_ASSET = Path(__file__).resolve().parents[3] / "assets" / "mc_palettes.json"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@lru_cache(maxsize=8)
def _git_state(root: str) -> tuple:
    """(revision, dirty) for a git worktree, or ("unknown", None)."""
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=root, capture_output=True, text=True,
                              timeout=10)
        if head.returncode != 0:
            return ("unknown", None)
        status = subprocess.run(["git", "status", "--porcelain", "-uno"],
                               cwd=root, capture_output=True, text=True,
                               timeout=10)
        dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
        return (head.stdout.strip(), dirty)
    except (OSError, subprocess.SubprocessError):
        return ("unknown", None)


def code_revision(root=None) -> dict:
    """{"revision": str, "dirty": bool|None, "root": str}."""
    r = Path(root) if root else Path(__file__).resolve().parents[3]
    rev, dirty = _git_state(str(r))
    return {"revision": rev, "dirty": dirty, "root": str(r)}


def _palette_blob(version_key: str) -> dict:
    data = json.loads(PALETTE_ASSET.read_text(encoding="utf-8"))
    if version_key not in data.get("versions", {}):
        raise ValueError(f"unknown palette version {version_key!r}")
    return data["versions"][version_key]


def palette_hash(version_key: str) -> str:
    """Content address of all palette entries + declared target version."""
    ver = _palette_blob(version_key)
    colors = sorted(
        ([int(c.get("map_color", -1)), int(c.get("shade", -1)), str(c.get("block", "")),
          [int(x) for x in c.get("rgb", [])]] for c in ver.get("colors", [])),
        key=lambda row: (row[0], row[1], row[2], row[3]))
    canonical = json.dumps({"version": version_key,
                            "target_mc_version": ver.get("target_mc_version"),
                            "colors": colors}, sort_keys=True, separators=(",", ":"))
    return _sha(canonical)[:16]


def target_mc_version(version_key: str) -> str:
    return str(_palette_blob(version_key).get("target_mc_version") or "unknown")


def combined_palette_hash(version_keys) -> str:
    """One content address for a palette sweep (order-independent).

    A single palette keeps its plain `palette_hash` so history recorded for
    one palette and for a sweep of one palette is directly comparable.
    """
    keys = sorted(set(version_keys))
    if len(keys) == 1:
        return palette_hash(keys[0])
    pairs = sorted((k, palette_hash(k), target_mc_version(k)) for k in keys)
    return _sha(json.dumps(pairs, separators=(",", ":")))[:16]


def algorithm_version() -> str:
    """Digest of the dithering definitions (not of comments or formatting)."""
    from firefly.evolution import dither

    kernels = {name: {"weights": sorted([[int(dx), int(dy), int(w)]
                                         for dx, dy, w in offsets]),
                      "divisor": int(div)}
               for name, (offsets, div) in dither.ERROR_KERNELS.items()}
    blob = {"algorithms": list(dither.ALGORITHMS),
            "bayer_sizes": sorted(dither.BAYER_SIZES),
            "error_kernels": kernels}
    return "dither-" + _sha(json.dumps(blob, sort_keys=True,
                                       separators=(",", ":")))[:10]


def canonical_settings(settings) -> dict:
    """Plain, JSON-safe settings dict from a Settings dataclass or a mapping."""
    if hasattr(settings, "__dict__"):
        raw = dict(vars(settings))
    else:
        raw = dict(settings or {})
    out = {}
    for k in sorted(raw):
        v = raw[k]
        if isinstance(v, (set, tuple, list)):
            v = sorted(v, key=str) if not all(isinstance(x, (int, float)) for x in v) \
                else [x for x in v]
        out[k] = v
    return out


def combined_context(settings, version_keys, root=None) -> dict:
    """Run context for a multi-palette sweep (e.g. a benchmark campaign)."""
    keys = sorted(set(version_keys))
    s = canonical_settings(settings)
    rev = code_revision(root)
    return {
        "code_revision": rev["revision"],
        "code_dirty": rev["dirty"],
        "palette_key": "+".join(keys),
        "palette_hash": combined_palette_hash(keys),
        "algorithm_version": algorithm_version(),
        "target_mc_version": ",".join(target_mc_version(k) for k in keys),
        "settings": s,
        "settings_digest": _sha(json.dumps(s, sort_keys=True,
                                           separators=(",", ":"), default=str))[:16],
    }


def run_context(settings, palette_key: str | None = None, root=None) -> dict:
    """Everything a benchmark history row needs to stay comparable."""
    s = canonical_settings(settings)
    key = palette_key or s.get("version") or "survival-wool-1.8"
    rev = code_revision(root)
    return {
        "code_revision": rev["revision"],
        "code_dirty": rev["dirty"],
        "palette_key": key,
        "palette_hash": palette_hash(key),
        "algorithm_version": algorithm_version(),
        "target_mc_version": target_mc_version(key),
        "settings": s,
        "settings_digest": _sha(json.dumps(s, sort_keys=True,
                                           separators=(",", ":"), default=str))[:16],
    }
