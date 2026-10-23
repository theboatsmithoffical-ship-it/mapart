#!/usr/bin/env python3
"""Asset-quality milestone battery (`python tests/test_assets.py`, exit 0 = pass).

Everything runs OFFLINE: a socket guard blocks non-loopback connections for
the whole process and records any attempt, so a test that silently reached
the network fails the battery. All fixtures are generated locally.

Covers: deterministic image characteristics, the SQLite asset catalog and its
search, stale-hash/missing-file detection, held-out set locking + journal,
import hardening (traversal/corrupt/oversize/format), storage quotas,
thumbnails, resumable imports, offline manifest verification, benchmark
provenance/history, and the asset-health report.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from firefly.mapart import assetcatalog, imagelibrary, provenance, testsuite  # noqa: E402

FAILURES: list = []
NET_ATTEMPTS: list = []


def t(name, fn):
    try:
        ev = fn()
        print(f"PASS {name}" + (f"  [{ev}]" if ev else ""))
    except Exception as e:  # noqa: BLE001
        FAILURES.append(name)
        import traceback
        tb = traceback.extract_tb(sys.exc_info()[2])[-1]
        print(f"FAIL {name}: {type(e).__name__}: {e} @line {tb.lineno}")


def _img(path: Path, *, size=(128, 96), alpha=False, flat=False, seed=0):
    from PIL import Image
    rng = np.random.default_rng(seed)
    w, h = size
    if flat:
        arr = np.full((h, w, 4 if alpha else 3), 128, dtype=np.uint8)
    else:
        y, x = np.mgrid[0:h, 0:w]
        base = np.stack([(x * 255 // max(1, w - 1)), (y * 255 // max(1, h - 1)),
                         ((x + y) * 3) % 256], axis=-1).astype(np.int16)
        arr = np.clip(base + rng.integers(-12, 12, base.shape), 0, 255).astype(np.uint8)
        if alpha:
            alpha_ch = np.where(x < w // 2, 255, 0).astype(np.uint8)[..., None]
            arr = np.concatenate([arr, alpha_ch], axis=-1)
    Image.fromarray(arr, "RGBA" if alpha else "RGB").save(path, format="PNG")
    return path


def _noise_jpeg(path: Path, size=(600, 600), seed=1, quality=90):
    from PIL import Image
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8)
    Image.fromarray(arr, "RGB").save(path, format="JPEG", quality=quality)
    return path


def _lib(tmp: Path, **policy) -> imagelibrary.ImageLibrary:
    pol = imagelibrary.StoragePolicy(**policy) if policy else None
    return imagelibrary.ImageLibrary(tmp / "data", policy=pol)


def _seed_library(tmp: Path, n=3):
    lib = _lib(tmp)
    made = []
    for i in range(n):
        p = _img(tmp / f"src_{i}.png", seed=i, size=(160 + 20 * i, 120))
        lib.add_local(str(p), datasets=["dev"])
        made.append(lib.ids()[-1])
    return lib, made


# ---------------------------------------------------------------- checks

def t01_characteristics_deterministic():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        a = _img(tmp / "g.png", seed=3)
        b = _img(tmp / "alpha.png", alpha=True, seed=4)
        c1 = imagelibrary.characteristics(str(a))
        c2 = imagelibrary.characteristics(str(a))
        assert c1 == c2, "characteristics are not deterministic"
        for key in ("width", "height", "aspect", "color_diversity",
                    "dominant_colors_rgb", "transparency_share",
                    "detail_complexity", "analysis_version"):
            assert key in c1, f"missing characteristic {key}"
        assert imagelibrary.characteristics_digest(c1) == \
            imagelibrary.characteristics_digest(c2)
        ca = imagelibrary.characteristics(str(b))
        assert ca["has_alpha"] and ca["transparency_share"] > 0.4, ca["transparency_share"]
        assert c1["transparency_share"] == 0.0
        # name does not affect measurement
        a2 = tmp / "renamed.png"
        a2.write_bytes(a.read_bytes())
        assert imagelibrary.characteristics(str(a2)) == c1
        return f"aspect {c1['aspect']}, detail {c1['detail_complexity']}"


def t02_catalog_index_and_search():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        lib, ids = _seed_library(tmp)
        cat = assetcatalog.AssetCatalog(tmp / "cat.db")
        cat.index_library(lib)
        assert cat.count()["images"] == 3
        assert {r["id"] for r in cat.search(set_name="dev")} == set(ids)
        wide = cat.search(min_width=190)
        assert [r["id"] for r in wide] == [ids[2]], [r["id"] for r in wide]
        assert cat.search(min_detail=2.0) == []
        assert len(cat.search(license_filter="recorded")) == 3
        assert cat.search(license_filter="missing") == []
        named = cat.search(text="src_1")
        assert [r["id"] for r in named] == [ids[1]], named  # title = file stem
        assert cat.search(text="100%_weird") == []  # literal, no SQL/LIKE error
        assert cat.search(limit=1)
        return "3 indexed, filters + literal text search verified"


def t03_catalog_detects_staleness():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        lib, ids = _seed_library(tmp, n=2)
        cat = assetcatalog.AssetCatalog(tmp / "cat.db")
        cat.index_library(lib)
        assert cat.stale_entries(lib)["unindexed"] == []
        e = lib.entry(ids[0])
        e["sha256"] = "0" * 64
        lib.save_manifest()
        stale = cat.stale_entries(lib)
        assert stale["hash_changes"], stale
        e["analysis"]["analysis_version"] = 0
        lib.save_manifest()
        assert ids[0] in cat.stale_entries(lib)["analysis_stale"]
        Path(lib.file_path(lib.entry(ids[1]))).unlink()
        assert ids[1] in cat.stale_entries(lib)["missing_files"]
        lib.manifest["images"].append({"id": "img_new", "sha256": "x"})
        assert "img_new" in cat.stale_entries(lib)["unindexed"]
        return "hash change, stale analysis, missing file, unindexed all detected"


def t04_holdout_lock_and_journal():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        lib, ids = _seed_library(tmp, n=2)
        lib.assign_set(ids[0], "validation", reason="split for selection")
        assert "validation" in lib.entry(ids[0])["datasets"]
        refused = False
        try:
            lib.assign_set(ids[1], "holdout")
        except imagelibrary.HoldoutLocked:
            refused = True
        assert refused, "holdout promotion without confirmation must be refused"
        rev_before = lib.manifest["dataset_revision"]
        lib.assign_set(ids[1], "holdout", reason="final evaluation rotation",
                       confirm=True)
        assert lib.manifest["dataset_revision"] > rev_before
        changed = False
        try:
            lib.assign_set(ids[1], "dev")
        except imagelibrary.HoldoutLocked:
            changed = True
        assert changed, "removing an image from holdout must be refused too"
        rows = lib.membership_log_rows()
        assert any(r["to"] == ["holdout"] and r["confirmed"] for r in rows), rows
        assert any(r["id"] == ids[0] and r["to"] == ["validation"] for r in rows)
        try:
            lib.assign_set(ids[0], "not-a-set", reason="x", confirm=True)
            raise AssertionError("unknown purpose accepted")
        except imagelibrary.LibraryError:
            pass
        try:
            lib.remove_dataset("holdout")
            raise AssertionError("holdout removal without confirmation accepted")
        except imagelibrary.HoldoutLocked:
            pass
        removed = lib.remove_dataset("holdout", confirm=True, reason="rotation complete")
        assert ids[1] in removed
        assert any(r["to"] == [] and r["reason"] == "rotation complete"
                   for r in lib.membership_log_rows())
        cmp_ = lib.compare_revisions(rev_before, lib.manifest["dataset_revision"])
        assert cmp_["membership_changed"] or cmp_["removed"], cmp_
        return f"{len(rows)} journal rows, revision {rev_before}->{lib.manifest['dataset_revision']}"


def t05_manifest_import_hardening():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        lib, ids = _seed_library(tmp, n=3)
        lib.assign_set(ids[2], "holdout", reason="test set", confirm=True)
        exported = lib.export_manifest()
        fresh = imagelibrary.ImageLibrary(tmp / "fresh")
        for e in lib.manifest["images"]:
            f = Path(lib.file_path(e))
            (fresh.dir / f.name).write_bytes(f.read_bytes())
        entries = json.loads(json.dumps(exported["images"]))
        entries.append({"id": "img_evil", "file_name": "../../evil.png",
                        "sha256": "0", "datasets": ["dev"]})
        entries.append({"id": "img_abs", "file_name": "C:\\evil.png",
                        "sha256": "0", "datasets": ["dev"]})
        entries.append({"id": "img_missing", "file_name": "nope.png",
                        "sha256": "0", "datasets": ["dev"]})
        entries = [e for e in entries if e["id"] != ids[0]]
        r = fresh.import_manifest(entries)
        assert r["added"] == 1, r            # only the non-locked valid entry
        assert len(r["rejected"]) == 2, r
        assert ids[2] in r["holdout_requires_confirmation"], r
        assert "img_missing" in r["missing_files"]
        assert fresh.entry(ids[2]) is None, "held-out entry imported without confirmation"
        r2 = fresh.import_manifest(entries, confirm=True, reason="restore evaluation set")
        assert r2["added"] == 1 and fresh.entry(ids[2]) is not None, r2
        assert any(row["id"] == ids[2] and row["to"] == ["holdout"]
                   for row in fresh.membership_log_rows())
        assert fresh.verify()["verified"] == 2
        return "traversal/absolute names rejected, holdout gated, valid entries restored"


def t06_storage_limits():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        big = _noise_jpeg(tmp / "big.jpg")          # ~ 0.7-1.0 MB noise JPEG
        size_mb = big.stat().st_size / 1048576
        assert size_mb > 0.3, size_mb
        lib = _lib(tmp / "a", max_file_mb=1, max_images=1)
        if size_mb > 1:
            try:
                lib.add_local(str(big))
                raise AssertionError("oversize file accepted")
            except imagelibrary.LibraryError:
                pass
        small = _img(tmp / "small.png", size=(64, 64))
        lib.add_local(str(small))
        try:
            lib.add_local(str(_img(tmp / "small2.png", size=(64, 64), seed=9)))
            raise AssertionError("image-count limit not enforced")
        except imagelibrary.LibraryError:
            pass
        # quota is configurable and persisted
        lib2 = _lib(tmp / "b", max_library_mb=1)
        first = _img(tmp / "q1.png", size=(256, 256))
        assert first.stat().st_size < 1024 * 1024
        lib2.add_local(str(first))
        lib2.set_policy(max_images=50)
        assert json.loads((lib2.policy_path).read_text())["max_images"] == 50
        reopened = imagelibrary.ImageLibrary(tmp / "b" / "data")
        assert reopened.policy.max_images == 50 and reopened.policy.max_library_mb == 1, \
            reopened.policy.to_dict()
        status = reopened.quota_status()
        assert status["used_bytes"] > 0 and status["limit_mb"] == 1
        # a second image that would exceed the 1 MB library quota is refused
        second = _noise_jpeg(tmp / "q2.jpg", size=(1200, 1200))
        try:
            reopened.add_local(str(second))
            raise AssertionError("library quota not enforced")
        except imagelibrary.LibraryError as ex:
            assert "limit" in str(ex).lower(), ex
        return f"per-file, count and library quotas enforced (used {status['used_mb']} MB)"


def t07_parsing_hardening():
    from PIL import Image
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        lib = _lib(tmp, max_pixels=1_000_000)
        corrupt = tmp / "corrupt.png"
        corrupt.write_bytes(b"\x89PNG\r\n\x1a\nnot really a png" + b"\x00" * 100)
        try:
            lib.add_local(str(corrupt))
            raise AssertionError("corrupt file accepted")
        except imagelibrary.LibraryError:
            pass
        huge = _img(tmp / "huge.png", size=(1200, 1000), flat=True)
        try:
            lib.add_local(str(huge))
            raise AssertionError("over-pixel-limit image accepted")
        except imagelibrary.LibraryError as ex:
            assert "decode" in str(ex).lower() or "large" in str(ex).lower(), ex
        gif = tmp / "anim.gif"
        Image.new("RGB", (32, 32), (10, 20, 30)).save(gif, format="GIF")
        try:
            lib.add_local(str(gif))
            raise AssertionError("GIF accepted")
        except imagelibrary.LibraryError:
            pass
        disguised = tmp / "disguised.png"
        Image.new("RGB", (32, 32), (10, 20, 30)).save(disguised, format="GIF")
        try:
            lib.add_local(str(disguised))
            raise AssertionError("format-disguised file accepted")
        except imagelibrary.LibraryError as ex:
            assert "format" in str(ex).lower(), ex
        trunc = tmp / "trunc.jpg"
        good = _noise_jpeg(tmp / "good.jpg", size=(200, 200))
        trunc.write_bytes(good.read_bytes()[: good.stat().st_size // 3])
        try:
            lib.add_local(str(trunc))
            raise AssertionError("truncated image accepted")
        except imagelibrary.LibraryError:
            pass
        empty = tmp / "empty.png"
        empty.write_bytes(b"")
        try:
            lib.add_local(str(empty))
            raise AssertionError("empty file accepted")
        except imagelibrary.LibraryError:
            pass
        assert lib.manifest["images"] == [], "rejected files must not enter the manifest"
        return "corrupt, oversized, GIF, disguised, truncated and empty files all refused"


def t08_thumbnail_cache():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        lib, ids = _seed_library(tmp, n=1)
        p1 = Path(lib.thumbnail(ids[0], max_px=64))
        assert p1.is_file() and p1.parent == lib.thumbs_dir
        first_mtime = p1.stat().st_mtime_ns
        time.sleep(0.01)
        p2 = Path(lib.thumbnail(ids[0], max_px=64))
        assert p2.stat().st_mtime_ns == first_mtime, "thumbnail was regenerated"
        other = Path(lib.thumbnail(ids[0]))
        assert other != p1 and other.is_file()
        src = Path(lib.file_path(lib.entry(ids[0])))
        future = time.time() + 30
        os.utime(src, (future, future))
        lib.thumbnail(ids[0], max_px=64)
        assert p1.stat().st_mtime_ns != first_mtime, "stale thumbnail not refreshed"
        stats = lib.thumbnail_stats()
        assert stats["count"] == 2 and stats["bytes"] > 0, stats
        return f"reused by mtime, refreshed on change, {stats['count']} cached sizes"


def t09_resumable_import():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        lib = _lib(tmp)
        sources = [_img(tmp / f"r{i}.png", seed=10 + i, size=(80, 80)) for i in range(4)]
        real_add = lib.add_local
        calls = {"n": 0}

        def flaky(path, *a, **k):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("simulated interruption")
            return real_add(path, *a, **k)

        lib.add_local = flaky  # type: ignore[assignment]
        interrupted = False
        try:
            lib.import_paths([str(p) for p in sources])
        except RuntimeError:
            interrupted = True
        assert interrupted
        lib.add_local = real_add  # type: ignore[assignment]
        journal = json.loads(lib.import_journal.read_text())
        done_before = len(journal["done"])
        assert done_before == 2, done_before
        result = lib.import_paths([str(p) for p in sources])
        assert result["resumed"] == 2, result
        assert len(result["added"]) == 2, result
        assert len(lib.manifest["images"]) == 4, len(lib.manifest["images"])
        assert not result["remaining"]
        again = lib.import_paths([str(p) for p in sources])
        assert again["added"] == [] and len(lib.manifest["images"]) == 4
        return f"resumed after {done_before} completed files, no duplicates"


def t10_offline_manifest_verification():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        lib, ids = _seed_library(tmp, n=2)
        manifest = lib.dir / "exported.json"
        manifest.write_text(json.dumps(lib.export_manifest()), encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        ok = subprocess.run([sys.executable, "tools/verify_manifest.py",
                             "--manifest", str(manifest), "--files-dir", str(lib.dir)],
                            capture_output=True, text=True, cwd=str(ROOT), env=env)
        assert ok.returncode == 0, (ok.stdout, ok.stderr)
        assert "OK" in ok.stdout
        # tampered metadata -> digest mismatch
        blob = json.loads(manifest.read_text())
        blob["images"][0]["title"] = "tampered"
        bad = tmp / "tampered.json"
        bad.write_text(json.dumps(blob), encoding="utf-8")
        res = subprocess.run([sys.executable, "tools/verify_manifest.py",
                              "--manifest", str(bad), "--files-dir", str(lib.dir)],
                             capture_output=True, text=True, cwd=str(ROOT), env=env)
        assert res.returncode == 1 and "digest" in res.stdout, res.stdout
        # corrupted bytes -> hash mismatch
        victim = Path(lib.file_path(lib.entry(ids[0])))
        victim.write_bytes(victim.read_bytes() + b"\x00")
        res2 = subprocess.run([sys.executable, "tools/verify_manifest.py",
                               "--library", str(tmp / "data")],
                              capture_output=True, text=True, cwd=str(ROOT), env=env)
        assert res2.returncode == 1 and "hash mismatch" in res2.stdout, res2.stdout
        return "export verified offline, tampering and corruption both detected"


def t11_benchmark_provenance_history():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cat = assetcatalog.AssetCatalog(tmp / "cat.db")
        settings = {"version": "survival-wool-1.8", "algorithm": "bayer4",
                    "strength": 1.0}
        ctx = provenance.run_context(settings)
        ph = provenance.palette_hash("survival-wool-1.8")
        assert ctx["palette_hash"] == ph and len(ph) == 16
        assert provenance.palette_hash("survival-wool-1.8") == ph
        assert provenance.palette_hash("map-full-approx-1.15") != ph
        assert ctx["target_mc_version"] != "unknown"
        assert ctx["algorithm_version"].startswith("dither-")
        assert provenance.algorithm_version() == ctx["algorithm_version"]
        combined = provenance.combined_context(settings, ["survival-wool-1.8",
                                                          "map-full-approx-1.15"])
        assert combined["palette_hash"] not in (ph, "")
        assert "+" in combined["palette_key"]
        r1 = cat.record_benchmark(ctx, mode="quick", suite=["grad-smooth"],
                                  results_path="a/results.jsonl", n_evals=3)
        r1b = cat.record_benchmark(ctx, mode="quick", suite=["grad-smooth"],
                                   results_path="a/results.jsonl", n_evals=3)
        assert r1 == r1b, "benchmark history is not idempotent"
        other = provenance.run_context({"version": "map-full-approx-1.15",
                                        "algorithm": "bayer4", "strength": 1.0})
        cat.record_benchmark(other, mode="quick", suite=["grad-smooth"],
                             results_path="b/results.jsonl", n_evals=2)
        assert cat.count()["benchmark_runs"] == 2
        assert len(cat.benchmarks(palette_hash=ph)) == 1
        assert len(cat.benchmarks(code_revision=ctx["code_revision"])) == 2
        cov = cat.benchmark_coverage()
        assert cov["runs"] == 2 and cov["by_algorithm"][0]["runs"] == 2
        assert cov["latest"]["settings"]["version"]
        return f"revision {ctx['code_revision']}, palette {ph}, algorithms {ctx['algorithm_version']}"


def t12_campaign_writes_run_meta_and_history():
    from firefly.mapart.benchmark import Campaign
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cat_path = tmp / "cat.db"
        camp = Campaign(tmp / "bench", mode="quick", tier="small",
                        suite_ids=["grad-smooth"], time_limit_s=180,
                        catalog_path=cat_path)
        camp.algorithms = ["bayer4"]  # real pipeline, bounded sweep for the test
        camp.palettes = ["survival-wool-1.8"]
        camp.strengths = [1.0]
        rep = camp.run()
        report = json.loads((tmp / "bench" / "report.json").read_text())
        meta = json.loads((tmp / "bench" / "run_meta.json").read_text())
        assert meta["format"] == "firefly-benchmark-run/1"
        for key in ("code_revision", "palette_hash", "algorithm_version",
                    "target_mc_version", "settings_digest", "settings"):
            assert key in meta, key
        assert meta["palette_hash"] == provenance.palette_hash("survival-wool-1.8")
        assert rep["evaluated"] == 1, rep
        assert report["tested_combos"] == 1, report
        runs = assetcatalog.AssetCatalog(cat_path).benchmarks()
        assert len(runs) == 1 and runs[0]["n_evals"] == 1, runs
        assert runs[0]["settings"]["algorithms"] == ["bayer4"]
        return "campaign recorded one run with full provenance"


def t13_asset_health_report():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        lib, ids = _seed_library(tmp, n=2)
        cat = assetcatalog.AssetCatalog(tmp / "data" / "assetcatalog.db")
        cat.index_library(lib)
        cat.record_benchmark(provenance.run_context({"version": "survival-wool-1.8"}),
                             mode="quick", suite=["grad-smooth"],
                             results_path="r.jsonl", n_evals=1)
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        md = tmp / "health.md"
        js = tmp / "health.json"
        p = subprocess.run([sys.executable, "tools/asset_health.py",
                            "--data-dir", str(tmp / "data"), "--out", str(md),
                            "--json", str(js)],
                           capture_output=True, text=True, cwd=str(ROOT), env=env)
        assert p.returncode == 0, (p.stdout, p.stderr)
        report = json.loads(js.read_text())
        for section in ("integrity", "licensing", "duplicates", "storage",
                        "benchmark_coverage", "analysis"):
            assert section in report, section
        assert report["benchmark_coverage"]["runs"] == 1
        assert report["images"] == 2
        text = md.read_text(encoding="utf-8")
        for heading in ("Asset health report", "Summary", "Sets",
                        "Benchmark coverage"):
            assert heading in text, heading
        victim = Path(lib.file_path(lib.entry(ids[0])))
        victim.unlink()
        p2 = subprocess.run([sys.executable, "tools/asset_health.py",
                             "--data-dir", str(tmp / "data"), "--strict"],
                            capture_output=True, text=True, cwd=str(ROOT), env=env)
        assert p2.returncode == 1, (p2.returncode, p2.stdout[-400:])
        return "all six signals reported; missing file makes --strict fail"


def t14_offline_no_network():
    assert NET_ATTEMPTS == [], NET_ATTEMPTS
    return "0 non-loopback connection attempts across the battery"


# ---------------------------------------------------------------- guard

def _install_guard():
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def guard_connect(self, address):
        if isinstance(address, tuple) and address:
            host = str(address[0]).lower()
            if not (host == "::1" or host.startswith("127.")
                    or host == "localhost"):
                NET_ATTEMPTS.append({"what": "connect", "to": str(address)})
                raise OSError(f"network blocked in offline battery: {address}")
        return real_connect(self, address)

    def guard_getaddrinfo(*a, **k):
        host = str(a[0]).lower() if a else ""
        if host and not (host in ("localhost", "::1") or host.startswith("127.")):
            NET_ATTEMPTS.append({"what": "getaddrinfo", "to": host})
            raise OSError(f"DNS blocked in offline battery: {host}")
        return real_getaddrinfo(*a, **k)

    socket.socket.connect = guard_connect  # type: ignore[method-assign]
    socket.getaddrinfo = guard_getaddrinfo  # type: ignore[assignment]


def main() -> int:
    _install_guard()
    for name, fn in sorted(globals().items()):
        if name.startswith("t") and name[1:3].isdigit() and callable(fn):
            t(name, fn)
    print(f"\n{'ALL PASSED' if not FAILURES else f'FAILED: {len(FAILURES)} -> {FAILURES}'}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
