#!/usr/bin/env python3
"""Offline verification of a dataset manifest (no network, by construction).

Accepts either an exported manifest JSON or a live library directory:

    python tools/verify_manifest.py --manifest exported.json
    python tools/verify_manifest.py --library mapart-data
    python tools/verify_manifest.py --library mapart-data --json out.json

Checks:
  format + unique ids, safe bare file names (no traversal/absolute paths),
  every listed file present and sha256-matching, decodable image payload,
  recorded license/attribution, valid purpose sets, and the portable
  content digest (recomputed from the file, compared with the one recorded
  at export).

Exit 0 only when every hard check passes; license gaps are warnings unless
`--strict-licenses` is passed. No dependency beyond the standard library plus
Pillow, which the app already requires.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from firefly.mapart import imagelibrary as il  # noqa: E402


def _sha256(p: Path) -> str:
    return il._sha256(p)


def verify(entries: list, *, library_dir: Path, dataset_revision: int,
           recorded_digest: str = "", strict_licenses: bool = False) -> dict:
    errors, warnings, checked = [], [], 0
    ids: set = set()
    for e in entries:
        iid = e.get("id") if isinstance(e, dict) else None
        if not iid:
            errors.append({"id": None, "problem": "entry without id"})
            continue
        if iid in ids:
            errors.append({"id": iid, "problem": "duplicate id"})
            continue
        ids.add(iid)
        name = il.ImageLibrary._safe_entry_name(e.get("file_name"))
        if name is None:
            errors.append({"id": iid, "problem": f"unsafe file_name {e.get('file_name')!r}"})
            continue
        p = library_dir / name
        if not p.is_file():
            errors.append({"id": iid, "problem": "missing file"})
            continue
        if _sha256(p) != e.get("sha256"):
            errors.append({"id": iid, "problem": "hash mismatch"})
            continue
        try:
            with p.open("rb"):
                pass
            from PIL import Image
            with Image.open(p) as im:
                im.verify()
        except Exception as ex:  # noqa: BLE001
            errors.append({"id": iid, "problem": f"corrupt: {type(ex).__name__}"})
            continue
        checked += 1
        if il.license_status(e.get("license")) != "recorded":
            gaps = {"id": iid, "problem": f"license {e.get('license')!r}",
                    "source_url": e.get("source_url", "")}
            (errors if strict_licenses else warnings).append(gaps)
        bad_sets = [s for s in e.get("datasets", []) if s not in il.PURPOSES]
        if bad_sets:
            errors.append({"id": iid, "problem": f"unknown purpose(s) {bad_sets}"})
        if not e.get("datasets"):
            warnings.append({"id": iid, "problem": "no purpose set assigned"})
    computed = il.manifest_content_digest(entries, dataset_revision)
    digest_ok = (not recorded_digest) or computed == recorded_digest
    if recorded_digest and not digest_ok:
        errors.append({"id": None, "problem": "manifest digest does not match its "
                                              "recorded value (entry edited after export)"})
    return {"ok": not errors, "checked": checked, "total": len(entries),
            "errors": errors, "warnings": warnings,
            "dataset_revision": dataset_revision,
            "digest": computed, "digest_recorded": recorded_digest or None,
            "digest_ok": digest_ok}


def _from_library(data_dir: Path) -> dict:
    lib = il.ImageLibrary(data_dir)
    report = verify(lib.manifest["images"], library_dir=lib.dir,
                    dataset_revision=lib.manifest.get("dataset_revision", 0),
                    recorded_digest=lib.manifest_digest())
    report.update({"source": "library", "library_dir": str(lib.dir),
                   "offline_ready": lib.offline_ready()["ready"],
                   "set_counts": {p: sum(1 for e in lib.manifest["images"]
                                         if p in e.get("datasets", []))
                                  for p in il.PURPOSES}})
    return report


def _from_manifest_file(manifest_path: Path, library_dir: Path | None,
                        strict_licenses: bool) -> dict:
    blob = json.loads(manifest_path.read_text(encoding="utf-8"))
    fmt = blob.get("format")
    entries = blob.get("images") or []
    if fmt != "firefly-dataset-manifest/1":
        return {"ok": False, "errors": [{"problem": f"unsupported manifest format "
                                                    f"{fmt!r}"}], "checks": 0}
    lib_dir = library_dir or manifest_path.parent
    report = verify(entries, library_dir=lib_dir,
                    dataset_revision=int(blob.get("dataset_revision", 0)),
                    recorded_digest=blob.get("manifest_digest", ""),
                    strict_licenses=strict_licenses)
    report.update({"source": "manifest", "manifest": str(manifest_path),
                   "library_dir": str(lib_dir), "format": fmt})
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", help="exported dataset manifest JSON")
    ap.add_argument("--library", help="data dir or imagelibrary dir to verify")
    ap.add_argument("--files-dir", help="where the manifest's files live "
                                        "(default: the manifest's folder)")
    ap.add_argument("--json", help="write the full JSON report here")
    ap.add_argument("--strict-licenses", action="store_true",
                    help="treat missing/unknown licenses as errors")
    a = ap.parse_args(argv)
    if not a.manifest and not a.library:
        ap.error("pass --manifest FILE or --library DIR")
    try:
        if a.library:
            lib_dir = Path(a.library)
            if lib_dir.name != "imagelibrary":
                lib_dir = lib_dir / "imagelibrary"
            report = _from_library(lib_dir.parent)
        else:
            report = _from_manifest_file(Path(a.manifest),
                                         Path(a.files_dir) if a.files_dir else None,
                                         a.strict_licenses)
    except (OSError, json.JSONDecodeError, ValueError) as ex:
        print(f"error: {type(ex).__name__}: {ex}", file=sys.stderr)
        return 2
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=1, sort_keys=True),
                                encoding="utf-8")
    print(f"{'OK' if report['ok'] else 'FAILED'}  checked {report.get('checked', 0)}/"
          f"{report.get('total', 0)} files  digest {report.get('digest', '?')}")
    for err in report.get("errors", [])[:20]:
        print(f"  ERROR  {err.get('id') or '-'}: {err.get('problem')}")
    for warn in report.get("warnings", [])[:10]:
        print(f"  warn   {warn.get('id') or '-'}: {warn.get('problem')}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
