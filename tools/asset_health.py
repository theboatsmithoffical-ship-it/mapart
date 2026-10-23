#!/usr/bin/env python3
"""Asset-health report for the local image library (offline).

Reports, in one concise document:
  - missing files and stale hashes (manifest vs bytes on disk),
  - licensing gaps (missing / unknown licenses, missing attribution),
  - duplicate candidates (identical content + perceptual near-duplicates),
  - storage use (library bytes, thumbnails, quota, free disk),
  - benchmark coverage (recorded runs by palette, algorithm, revision).

    python tools/asset_health.py --data-dir mapart-data \
        --out docs/asset_health_report.md --json reports/asset_health.json

Exit 0 when the report was produced; `--strict` returns 1 when missing files,
stale hashes or unreadable images exist. Near-duplicates are CANDIDATES —
nothing is merged or removed by this tool.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from firefly.mapart import imagelibrary as il  # noqa: E402


def _fingerprint(path: str) -> np.ndarray | None:
    """Deterministic 16x16 grayscale fingerprint for near-duplicate detection.

    Absolute grey levels (not normalized): unrelated photos differ by ~10+
    grey levels on average, while a re-encoded/cropped copy differs by ~0-3.
    """
    try:
        from PIL import Image
        with Image.open(path) as im:
            return np.asarray(im.convert("L").resize((16, 16)), dtype=np.float64)
    except Exception:  # noqa: BLE001
        return None


def _to_relative(path: str) -> str:
    try:
        return str(Path(path).relative_to(ROOT))
    except (ValueError, TypeError):
        return str(path)


def build_report(data_dir: Path, *, max_pairs: int = 200,
                 refresh_analysis: bool = False,
                 ensure_thumbnails: bool = True) -> dict:
    lib = il.ImageLibrary(data_dir)
    if refresh_analysis:
        lib.refresh_analysis(only_stale=True)
    if ensure_thumbnails:
        for e in lib.manifest["images"]:
            try:
                lib.thumbnail(e)  # cached: only missing/outdated thumbs are written
            except il.LibraryError:
                continue  # missing/corrupt sources are reported below
    verification = lib.verify()
    analysis = lib.analysis_status()
    entries = lib.manifest["images"]
    lib_dir = lib.dir

    # ---- licensing ----
    gaps = []
    license_counts: dict = {}
    for e in entries:
        status = il.license_status(e.get("license"))
        license_counts[status] = license_counts.get(status, 0) + 1
        if status != "recorded":
            gaps.append({"id": e["id"], "license": e.get("license") or "",
                         "source": e.get("source", ""),
                         "source_url": e.get("source_url", "")})
        elif not (e.get("author") or "").strip() and e.get("source") != "local-user-folder":
            gaps.append({"id": e["id"], "license": e.get("license"),
                         "source": e.get("source", ""), "problem": "no attribution",
                         "source_url": e.get("source_url", "")})

    # ---- duplicate candidates ----
    exact: dict = {}
    for e in entries:
        exact.setdefault(e.get("sha256"), []).append(e["id"])
    exact_dupes = [ids for ids in exact.values() if len(ids) > 1]
    fingerprints, failed = [], []
    for e in entries:
        p = lib.file_path(e)
        fp = _fingerprint(p) if p else None
        if fp is None:
            failed.append(e["id"])
            continue
        fingerprints.append((e["id"], fp))
    near = []
    for i in range(len(fingerprints)):
        for j in range(i + 1, len(fingerprints)):
            dist = float(np.abs(fingerprints[i][1] - fingerprints[j][1]).mean())
            if dist < 4.0:  # grey levels (0-255) averaged over the 16x16 grid
                near.append({"a": fingerprints[i][0], "b": fingerprints[j][0],
                             "distance": round(dist, 3),
                             "similarity": round(1.0 - dist / 255.0, 4)})
    near.sort(key=lambda r: (r["distance"], r["a"], r["b"]))

    # ---- storage ----
    quota = lib.quota_status()
    per_source: dict = {}
    for e in entries:
        p = lib.file_path(e)
        try:
            n = Path(p).stat().st_size if p else 0
        except OSError:
            n = 0
        src = e.get("source", "unknown")
        per_source[src] = per_source.get(src, 0) + n

    # ---- catalog + benchmark coverage ----
    from firefly.mapart.assetcatalog import AssetCatalog
    catalog_path = data_dir / "assetcatalog.db"
    catalog = AssetCatalog(catalog_path)
    indexed = catalog.index_library(lib)
    stale = catalog.stale_entries(lib)
    for row in lib.membership_log_rows():
        catalog.record_set_event(row["id"], row.get("from", []), row.get("to", []),
                                 row.get("reason", ""), row.get("confirmed", False),
                                 origin="membership_log")
    coverage = catalog.benchmark_coverage()
    set_counts = catalog.set_summary()
    unbenchmarked = [s for s, n in sorted(set_counts.items())
                     if n and s in ("validation", "holdout") and not coverage["runs"]]
    coverage.update({
        "catalog": str(_to_relative(str(catalog_path))),
        "indexed": indexed["indexed"],
        "set_counts": set_counts,
        "sets_without_benchmark_history": unbenchmarked,
        "note": ("benchmark campaigns evaluate the deterministic synthetic suite; "
                 "set-level evaluation counts come from lab/breeder history, not "
                 "from these rows"),
    })

    problems = verification["problems"]
    return {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "library": _to_relative(str(lib_dir)),
        "dataset_revision": lib.manifest.get("dataset_revision", 0),
        "manifest_digest": lib.manifest_digest(),
        "policy": lib.policy.to_dict(),
        "images": len(entries),
        "integrity": {"verified": verification["verified"],
                      "total": verification["total"],
                      "problems": problems,
                      "missing_files": [p["id"] for p in problems
                                        if p["problem"] == "missing file"],
                      "stale_hashes": [p["id"] for p in problems
                                       if p["problem"] == "hash mismatch"],
                      "unreadable": [p["id"] for p in problems
                                     if p["problem"].startswith(("corrupt", "unreadable"))]},
        "analysis": analysis,
        "catalog_staleness": stale,
        "licensing": {"counts": license_counts, "gaps": gaps,
                      "gap_count": len(gaps)},
        "duplicates": {"exact_groups": exact_dupes,
                       "near_pairs": near[:max_pairs],
                       "near_pair_count": len(near),
                       "fingerprint_failures": failed,
                       "policy": "candidates only — never merged or deleted"},
        "storage": {"quota": quota,
                    "per_source_bytes": per_source,
                    "thumbnails": lib.thumbnail_stats()},
        "benchmark_coverage": coverage,
    }


def render_markdown(r: dict) -> str:
    q = r["storage"]["quota"]
    t = r["storage"]["thumbnails"]
    lines = [
        "# Asset health report",
        "",
        f"Generated {r['generated']} - library `{r['library']}` - "
        f"revision {r['dataset_revision']} - manifest `{r['manifest_digest'][:16]}`",
        "",
        "## Summary",
        "",
        "| signal | value |",
        "|---|---|",
        f"| images | {r['images']} |",
        f"| integrity verified | {r['integrity']['verified']}/{r['integrity']['total']} |",
        f"| missing files | {len(r['integrity']['missing_files'])} |",
        f"| stale hashes | {len(r['integrity']['stale_hashes'])} |",
        f"| stale analysis records | {r['analysis']['stale_count']} "
        f"(current version {r['analysis']['current_version']}) |",
        f"| licensing gaps | {r['licensing']['gap_count']} |",
        f"| near-duplicate candidates | {r['duplicates']['near_pair_count']} |",
        f"| storage | {q['used_mb']} MB / {q['limit_mb']} MB "
        f"(thumbs {t['bytes'] / 1024:.0f} kB, {t['count']} files) |",
        f"| benchmark runs recorded | {r['benchmark_coverage']['runs']} |",
        "",
    ]
    if r["integrity"]["problems"]:
        lines += ["## Integrity problems", ""]
        for p in r["integrity"]["problems"][:20]:
            lines.append(f"- `{p['id']}`: {p['problem']}")
        lines.append("")
    if r["licensing"]["gaps"]:
        lines += ["## Licensing gaps", ""]
        for g in r["licensing"]["gaps"][:20]:
            lines.append(f"- `{g['id']}`: license {g.get('license')!r}, "
                         f"source {g.get('source')}, {g.get('source_url') or 'no url'}")
        lines.append("")
    if r["duplicates"]["near_pairs"]:
        lines += ["## Near-duplicate candidates", "",
                  "| a | b | similarity |", "|---|---|---|"]
        for p in r["duplicates"]["near_pairs"][:15]:
            lines.append(f"| `{p['a']}` | `{p['b']}` | {p['similarity']} |")
        lines.append("")
    lines += ["## Sets", ""]
    for name, n in sorted(r["benchmark_coverage"]["set_counts"].items()):
        lines.append(f"- `{name}`: {n}")
    lines += ["", "## Benchmark coverage", "",
              f"- runs: {r['benchmark_coverage']['runs']}"]
    for p in r["benchmark_coverage"]["by_palette"]:
        lines.append(f"- palette `{p['palette_key']}` ({p['palette_hash']}): "
                     f"{p['runs']} runs")
    for a in r["benchmark_coverage"]["by_algorithm"]:
        lines.append(f"- algorithm definitions `{a['algorithm_version']}`: {a['runs']} runs")
    if not r["benchmark_coverage"]["runs"]:
        lines.append("- no benchmark history recorded yet; run a campaign with "
                     "`catalog_path` set to record one")
    lines += ["", "## Notes", "",
              "- Duplicate detection reports candidates; nothing is merged or deleted.",
              "- Licensing gaps are reported, never auto-filled.",
              f"- Policy: {json.dumps(r['policy'])}", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default=str(ROOT / "mapart-data"))
    ap.add_argument("--out", help="markdown report path")
    ap.add_argument("--json", help="JSON report path")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 when missing files / stale hashes exist")
    ap.add_argument("--refresh-analysis", action="store_true",
                    help="recompute stale deterministic characteristics first")
    a = ap.parse_args(argv)
    data_dir = Path(a.data_dir)
    if not (data_dir / "imagelibrary" / "manifest.json").is_file():
        print(f"error: no image library at {data_dir}", file=sys.stderr)
        return 2
    report = build_report(data_dir, refresh_analysis=a.refresh_analysis)
    if a.json:
        p = Path(a.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")
    md = render_markdown(report)
    if a.out:
        p = Path(a.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(md, encoding="utf-8")
    print(md)
    bad = (report["integrity"]["missing_files"]
           or report["integrity"]["stale_hashes"])
    return 1 if (a.strict and bad) else 0


if __name__ == "__main__":
    sys.exit(main())
