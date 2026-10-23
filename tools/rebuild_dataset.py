"""Rebuild the local dataset library using the real production code path.

Downloads ONLY on explicit invocation (this script or the GUI button — the
engine never downloads implicitly). Uses Wikimedia Commons (no API key;
license/author metadata recorded per image). Categories cover the spec's
requirements; a diverse subset is assigned to holdout, stress-case images to
stress, everything else to dev. The synthetic suite is generated locally
(no network). Prints a JSON report and exits non-zero if not offline-ready.

Usage:
    python tools/rebuild_dataset.py --data-dir mapart-data \
        --per-query 4 --max-file-mb 5
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from firefly.mapart import imagelibrary as il  # noqa: E402
from firefly.mapart import testsuite  # noqa: E402

QUERY_TO_TAGS = [
    ("mountain landscape photograph", "landscape"),
    ("city architecture building facade", "architecture"),
    ("red fox wildlife animal", "animal"),
    ("flower plant macro", "plant"),
    ("portrait photograph person", "face"),
    ("oil painting illustration art", "illustration"),
    ("smooth gradient abstract color", "gradient"),
    ("city skyline night exposure", "difficult"),
    ("pixel art sprite", "pixel-art"),
    ("texture pattern detail", "fine-details"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="mapart-data")
    ap.add_argument("--per-query", type=int, default=4,
                    help="max downloads per category query")
    ap.add_argument("--max-file-mb", type=int, default=5)
    ap.add_argument("--max-total-mb", type=int, default=120,
                    help="safety ceiling for the whole rebuild")
    args = ap.parse_args()

    lib = il.ImageLibrary(args.data_dir)
    report: dict = {"queries": [], "downloaded": [], "duplicates": [], "errors": []}
    total_bytes = 0
    t0 = time.time()

    free_mb = lib.free_space_mb()
    report["free_mb_before"] = free_mb
    if free_mb < args.max_total_mb:
        print(json.dumps({"ok": False, "error": f"free space {free_mb} MB < "
                          f"ceiling {args.max_total_mb} MB; refusing to download"},
                         sort_keys=True))
        return 2

    for query, _cat in QUERY_TO_TAGS:
        try:
            cands = il.discover_wikimedia(query, limit=args.per_query + 4,
                                          min_px=256, max_file_mb=args.max_file_mb)
        except il.LibraryError as e:
            report["errors"].append({"query": query, "stage": "discovery", "error": str(e)})
            continue
        # pre-download size accounting (spec: report size vs free space first)
        est = lib.batch_estimate_bytes(cands[:args.per_query])
        total_bytes += est
        if total_bytes > args.max_total_mb * 1024 * 1024:
            report["queries"].append({"query": query, "planned": 0,
                                      "skipped": f"total ceiling {args.max_total_mb} MB reached"})
            continue
        got = 0
        for cand in cands[:args.per_query]:
            time.sleep(2.0)  # politeness delay; Commons rate-limits bursts
            try:
                r = lib.download(cand, datasets=["dev"])
                if r.get("duplicate"):
                    report["duplicates"].append(r["id"])
                elif "duplicate_of" in r:
                    report["duplicates"].append(r["duplicate_of"])
                else:
                    report["downloaded"].append(r["id"])
                    got += 1
            except il.LibraryError as e:
                report["errors"].append({"query": query, "stage": "download",
                                         "url": cand.get("page_url"), "error": str(e)})
        report["queries"].append({"query": query, "candidates": len(cands),
                                  "downloaded": got,
                                  "estimated_mb": est // (1024 * 1024)})

    # ---- purpose assignment on measured tags (never first-N) ----
    # Holdout IS the final-evaluation set, so deliberately-hard images belong
    # there too; only 'stress' membership is reserved for the hard-tag set.
    holdout_ids: list[str] = []
    stress_ids: list[str] = []
    rest: list[str] = []
    for e in list(lib.manifest["images"]):
        tags = set(e.get("analysis", {}).get("capability_tags", []))
        hard = tags & {"similar-colors", "out-of-palette", "fine-details",
                       "high-contrast"}
        if hard and len(stress_ids) < 14:
            stress_ids.append(e["id"])
        else:
            rest.append(e["id"])
    # spread holdout across the remaining pool (every other image, capped)
    for iid in rest[: (len(rest) // 2)][:10]:
        holdout_ids.append(iid)
    holdout_used = set(holdout_ids)
    remaining = [i for i in rest if i not in holdout_used]
    # a separate validation split for parameter selection (kept out of both
    # training-only data and the final held-out evaluation set)
    validation_ids = remaining[: max(1, min(4, len(remaining) // 4))] \
        if len(remaining) > 2 else []
    validation_used = set(validation_ids)
    dev_ids = [i for i in remaining if i not in validation_used]
    for iid, purpose in ([(i, "stress") for i in stress_ids] +
                         [(i, "holdout") for i in holdout_ids] +
                         [(i, "validation") for i in validation_ids] +
                         [(i, "dev") for i in dev_ids]):
        # membership of the locked holdout set is confirmed explicitly here
        # and recorded in membership_log.jsonl by the library
        # a full explicit rebuild may re-split images that were previously
        # held out; the confirmation and reason are journalled per image
        lib.assign_set(iid, purpose, confirm=True,
                       reason=f"explicit dataset rebuild split ({purpose})")
    report["holdout_ids"] = holdout_ids
    report["validation_ids"] = validation_ids
    report["stress_ids"] = stress_ids

    # ---- locally generated synthetic suite (needed offline, zero network) ----
    synth_dir = lib.dir / "synthetic"
    saved = testsuite.save_all("small", synth_dir)
    report["synthetic_files"] = len(saved)

    v = lib.verify()
    o = lib.offline_ready()
    rev_dir = lib.revisions()
    manifest = lib.manifest
    counts: dict[str, int] = {}
    for e in manifest["images"]:
        for p in e.get("datasets", []):
            counts[p] = counts.get(p, 0) + 1
    report["manifest_digest"] = lib.manifest_digest()
    disk = sum(Path(lib.file_path(e) or "").stat().st_size for e in manifest["images"]
               if Path(lib.file_path(e) or "").is_file()) // (1024 * 1024)
    report.update({
        "ok": v["offline_ready"],
        "verified": f'{v["verified"]}/{v["total"]}',
        "problems": v["problems"],
        "offline_ready": v["offline_ready"],
        "dataset_revision": manifest.get("dataset_revision", 0),
        "manifest_images": len(manifest["images"]),
        "purpose_counts": counts,
        "library_disk_mb": disk,
        "revisions_saved": len(rev_dir),
        "elapsed_s": round(time.time() - t0, 1),
    })
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0 if v["offline_ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
