#!/usr/bin/env python3
"""Generate data/blocks.json deterministically from the palette asset.

- Every entry carries verification metadata (status, checked_against).
- The six previously flagged blocks (cinnabar, cinnabar_bricks, closed_eyeblossom,
  bush, firefly_bush, wildflowers) are preserved as *unresolved* entries with
  an explicit reason: they are not referenced by the current palette asset and
  no offline vanilla block registry was available to verify them. Nothing is
  invented; resolving them requires an authoritative version-specific source.
- Pathway notes are plain English (the previous 'possibilité' wording is
  corrected to 'possible' with the same meaning).
- Output is byte-identical across runs (sorted keys, fixed order).
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from firefly.evolution import dither as dmod  # noqa: E402

ASSET = ROOT / "assets" / "mc_palettes.json"
OUT = ROOT / "data" / "blocks.json"

# Blocks flagged for version verification in the audit. Kept OUT of the
# working palette entries (they do not participate in the map-art pipeline)
# and listed with an honest status instead of fabricated properties.
FLAGGED_UNRESOLVED = {
    "minecraft:cinnabar":        "not in any included palette asset; name matches mod blocks (e.g. Create), vanilla existence not verifiable offline",
    "minecraft:cinnabar_bricks": "same basis: not in palette data; no offline registry confirms it",
    "minecraft:closed_eyeblossom": "a 1.21.4-era plant block; not referenced by the roadmap palette; version-specific properties unverified offline",
    "minecraft:bush":            "added in recent snapshot content; pipeline does not reference it; unverified offline",
    "minecraft:firefly_bush":    "added in recent snapshot content; pipeline does not reference it; unverified offline",
    "minecraft:wildflowers":     "added in recent snapshot content; pipeline does not reference it; unverified offline",
}


def build() -> dict:
    data = json.loads(ASSET.read_text(encoding="utf-8"))
    engine_sha = hashlib.sha256(
        b"".join(f.read_bytes() for f in sorted((ROOT / "src" / "firefly").rglob("*.py")))).hexdigest()[:12]
    blocks = {}
    for version, ver in data["versions"].items():
        for c in ver["colors"]:
            blk = c["block"]
            key = blk if blk.startswith("minecraft:") else "minecraft:" + blk
            entry = blocks.setdefault(key, {
                "map_colors": [],
                "survival": bool(c.get("survival", True)),
                "pipeline_role": "palette-color candidate",
                "verification": {
                    "status": "verified-in-palette",
                    "checked_against": f"assets/mc_palettes.json@{data['_meta'].get('accuracy', 'approximation')}",
                    "note": data["_meta"]["note"],
                    "engine_sha": engine_sha,
                },
            })
            entry["map_colors"].append({"version": version, "id": c["id"],
                                        "rgb": c["rgb"], "shade_basis": c["rgb"]})
    for key, reason in FLAGGED_UNRESOLVED.items():
        blocks[key] = {
            "map_colors": [],
            "survival": None,
            "pipeline_role": "none — unresolved; excluded from palettes",
            "verification": {
                "status": "UNRESOLVED",
                "checked_against": None,
                "note": reason,
                "engine_sha": engine_sha,
            },
        }
    return {
        "_meta": {
            "format": "firefly-blocks-db-v1",
            "pathway_note": ("blocks.json is generated; edits belong in "
                             "assets/mc_palettes.json or this tool's registry. "
                             "Regeneration is deterministic; UNRESOLVED entries "
                             "stay unresolved until authoritative version data "
                             "is supplied. It is possible to re-check offline "
                             "only when a trusted block registry is bundled."),
        },
        "blocks": {k: blocks[k] for k in sorted(blocks)},
    }


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(build(), indent=2, sort_keys=True, ensure_ascii=False)
    OUT.write_text(payload + "\n", encoding="utf-8")
    unf = [k for k, v in build()["blocks"].items() if v["verification"]["status"] == "UNRESOLVED"]
    print(json.dumps({"ok": True, "path": str(OUT), "entries": len(build()["blocks"]),
                      "unresolved": unf}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
