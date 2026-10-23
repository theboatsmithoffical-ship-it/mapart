#!/usr/bin/env python3
"""Explicit dataset-set management (dev | validation | holdout | stress | synthetic).

    python tools/dataset_sets.py list
    python tools/dataset_sets.py assign --id img_x --set validation \
        --reason "parameter selection split"
    python tools/dataset_sets.py assign --id img_x --set holdout \
        --reason "final evaluation rotation" --confirm
    python tools/dataset_sets.py events --limit 20

Nothing is promoted between sets silently: every change is written to
imagelibrary/membership_log.jsonl, bumps the dataset revision, and mirrors
into the SQLite catalog's set-event journal. Touching `holdout` without
`--confirm` and a written `--reason` is refused.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from firefly.mapart import imagelibrary as il  # noqa: E402
from firefly.mapart.assetcatalog import AssetCatalog  # noqa: E402


def _catalog(data_dir: Path) -> AssetCatalog:
    return AssetCatalog(data_dir / "assetcatalog.db")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default=str(ROOT / "mapart-data"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="sets and their members")
    p_assign = sub.add_parser("assign", help="move one image to a set")
    p_assign.add_argument("--id", required=True)
    p_assign.add_argument("--set", required=True, choices=list(il.PURPOSES))
    p_assign.add_argument("--reason", default="")
    p_assign.add_argument("--confirm", action="store_true")
    p_events = sub.add_parser("events", help="recent membership events")
    p_events.add_argument("--limit", type=int, default=20)
    a = ap.parse_args(argv)

    data_dir = Path(a.data_dir)
    lib = il.ImageLibrary(data_dir)
    if a.cmd == "list":
        out = {p: sorted(e["id"] for e in lib.manifest["images"]
                         if p in e.get("datasets", [])) for p in il.PURPOSES}
        print(json.dumps({"dataset_revision": lib.manifest.get("dataset_revision", 0),
                          "counts": {k: len(v) for k, v in out.items()},
                          "sets": out}, indent=1, sort_keys=True))
        return 0
    if a.cmd == "events":
        rows = lib.membership_log_rows()[-a.limit:]
        print(json.dumps(rows, indent=1, sort_keys=True))
        return 0
    try:
        result = lib.assign_set(a.id, a.set, reason=a.reason, confirm=a.confirm)
    except il.HoldoutLocked as ex:
        print(f"refused: {ex}", file=sys.stderr)
        return 3
    except il.LibraryError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 2
    cat = _catalog(data_dir)
    cat.index_library(lib)
    for row in lib.membership_log_rows():
        cat.record_set_event(row["id"], row.get("from", []), row.get("to", []),
                             row.get("reason", ""), row.get("confirmed", False),
                             origin="membership_log", ts=row.get("ts"))
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
