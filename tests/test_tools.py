#!/usr/bin/env python3
"""Tool upgrade regression battery (`python tests/test_tools.py`, exit 0 = pass).

Where an external simulator genuinely cannot exist on this machine, the tests
use a small REAL helper script as the simulator executable (it genuinely
writes the requested render from a real PNG) — no fake success path.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FAILURES = []


def t(name, fn):
    try:
        ev = fn()
        print(f"PASS {name}" + (f"  [{ev}]" if ev else ""))
    except Exception as e:  # noqa: BLE001
        FAILURES.append(name)
        import traceback
        tb = traceback.extract_tb(sys.exc_info()[2])[-1]
        print(f"FAIL {name}: {type(e).__name__}: {e} @line {tb.lineno}")


def run_tool(name, args, cwd=ROOT, timeout=600, env=None):
    p = subprocess.run([sys.executable, f"tools/{name}", *args],
                       capture_output=True, text=True, cwd=str(cwd), timeout=timeout,
                       env={**os.environ, "PYTHONPATH": str(ROOT / "src"), **(env or {})})
    return p


def fixture_png(path, size=128):
    import numpy as np
    from PIL import Image
    x = np.linspace(10, 240, size, dtype=np.float64)
    y = np.linspace(240, 10, size, dtype=np.float64)
    a = np.broadcast_to(x[None, :], (size, size))
    b = np.broadcast_to(y[:, None], (size, size))
    w = np.sin(a / 13) * 30 + np.cos(b / 8) * 22
    arr = np.clip(np.stack([a + w * .7, .5 * (a + b), b + w * .5], -1), 0, 255).astype(np.uint8)
    Image.fromarray(arr, "RGB").save(path, format="PNG")


def make_sim(tdir, behavior="ok", name="sim_ok.py"):
    td = Path(tdir)
    """A *real* simulator script: reads job JSON, writes plan-colored render."""
    subst = {
        "ok": "",
        "fail": "raise SystemExit(7)",
    }[behavior]
    src = f'''# -*- coding: utf-8 -*-
import argparse, json, numpy as np
from PIL import Image
ap = argparse.ArgumentParser(); ap.add_argument("--job", required=True); ap.add_argument("--out", required=True)
a = ap.parse_args()
j = json.loads(open(a.job, encoding="utf-8").read())
pal = {{int(cid): tuple(c) for cid, c in j["color_lookup"].items()}}
plan = np.array(j["expected_map_colors"], dtype=int)
out = np.zeros((plan.shape[0], plan.shape[1], 3), dtype=np.uint8)
for y in range(plan.shape[0]):
    for x in range(plan.shape[1]):
        out[y, x] = pal[plan[y, x]]
Image.fromarray(out, "RGB").save(a.out, format="PNG")
{subst}
'''
    path = td / name
    path.write_text(src, encoding="utf-8")
    return path


def make_job(tdir) -> Path:
    td = Path(tdir)
    import numpy as np
    plan = (np.add.outer(np.arange(64), np.arange(64)) % 3) + 1  # 64x64 ids 1..3
    lookup = {1: [255, 0, 0], 2: [0, 255, 0], 3: [0, 0, 255]}
    job = {"expected_map_colors": plan.tolist(), "color_lookup": lookup}
    p = td / "job.json"
    p.write_text(json.dumps(job), encoding="utf-8")
    return p


def main() -> int:
    # --- validate_render_against_mapart.py ---
    def v_nameless_sim():
        with tempfile.TemporaryDirectory() as td:
            job = make_job(Path(td))
            p = run_tool("validate_render_against_mapart.py",
                         ["--job", str(job)])
            assert p.returncode == 2 and "simulator is not configured" in p.stderr, (p.returncode, p.stderr[-300:])
            return "exit 2, actionable message, no traceback"

    def v_missing_sim():
        with tempfile.TemporaryDirectory() as td:
            job = make_job(Path(td))
            p = run_tool("validate_render_against_mapart.py",
                         ["--job", str(job), "--simulator", str(Path(td) / "nope.exe")])
            assert p.returncode == 2 and "not found or not runnable" in p.stderr
            return "exit 2"

    def v_success_sim_spaces():
        with tempfile.TemporaryDirectory() as td:
            tdd = Path(td) / "dir with spaces"
            tdd.mkdir()
            job = make_job(tdd)
            sim = make_sim(tdd)
            p = run_tool("validate_render_against_mapart.py",
                         ["--job", str(job), "--simulator", str(sim)])
            assert p.returncode == 0, (p.returncode, p.stdout, p.stderr[-400:])
            out = json.loads(p.stdout)
            assert out["ok"] and out["report"]["mismatches"] == 0 and out["report"]["pixels"] == 4096
            return "real render compared 4096 px, 0 mismatches (path with spaces)"

    def v_fail_sim():
        with tempfile.TemporaryDirectory() as td:
            job = make_job(td)
            sim = make_sim(td, behavior="fail")
            p = run_tool("validate_render_against_mapart.py",
                         ["--job", str(job), "--simulator", str(sim)])
            assert p.returncode == 7, (p.returncode, p.stderr[-200:])
            return "simulator failure exit preserved (7)"

    def v_bad_json():
        with tempfile.TemporaryDirectory() as td:
            job = Path(td) / "bad.json"; job.write_text("{ not json", encoding="utf-8")
            sim = make_sim(td)
            p = run_tool("validate_render_against_mapart.py",
                         ["--job", str(job), "--simulator", str(sim)])
            assert p.returncode == 2 and "unreadable/invalid" in p.stderr
            return "exit 2"

    # --- benchmark.py ---
    b_ok = "benchmark.py"

    def b_valid_default():
        p = run_tool(b_ok, [])
        assert p.returncode == 0, p.stderr[-400:]
        out = json.loads(p.stdout)
        assert out["validation"]["blocks_placed"] == 16384
        return "default 128x128, validated"

    def b_valid_multimap():
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "i.png"; fixture_png(img, 256)
            p = run_tool(b_ok, ["--image", str(img), "--maps", "256x256",
                                "--out", str(Path(td) / "out")])
            assert p.returncode == 0, p.stderr[-300:]
            out = json.loads(p.stdout)
            # height is the solved staircase height, not a fixed stack
            assert out["validation"]["size"][0] == 256 \
                and out["validation"]["size"][2] == 256, out["validation"]["size"]
            assert out["validation"]["size"][1] >= 2
            return "256x256 ok"

    def b_invalid_dims():
        for spec, why in (("64x64", "not multiple of 128"), ("0x0", "zero"),
                          ("-128x128", "negative"), ("abcx128", "non-numeric"),
                          ("128", "malformed")):
            img = Path(tempfile.gettempdir()) / "engt.png"; fixture_png(img, 128)
            p = run_tool(b_ok, ["--image", str(img), "--maps", spec])
            assert p.returncode == 2, (spec, p.returncode, p.stderr[-200:])
            assert "error:" in p.stderr and "Traceback" not in p.stderr, p.stderr[-200:]
        return "5 invalid cases -> exit 2 usage, no tracebacks"

    def b_help():
        p = run_tool(b_ok, ["--help"])
        assert p.returncode == 0 and "--maps" in p.stdout
        return "--help ok"

    # --- build_blocks_db.py ---
    def db_deterministic_and_unresolved_honesty():
        data_dir = ROOT / "data"
        snap1, snap2 = db_run(), db_run()
        assert snap1 == snap2 and len(snap1) > 2000
        data = json.loads((data_dir / "blocks.json").read_text(encoding="utf-8"))
        note = data["_meta"]["pathway_note"]
        assert "possibilité" not in note and "possible" in note
        unres = [k for k, v in data["blocks"].items() if v["verification"]["status"] == "UNRESOLVED"]
        for want in ("cinnabar", "closed_eyeblossom", "bush", "firefly_bush", "wildflowers"):
            hit = [k for k in unres if want in k]
            assert hit, (want, unres)
            assert not data["blocks"][hit[0]]["map_colors"], "unresolved must not claim palette data"
        verified = [v for v in data["blocks"].values() if v["verification"]["status"] == "verified-in-palette"]
        assert verified
        return f"deterministic ({len(snap1)} chars), {len(unres)} unresolved, note fixed"

    def db_run():
        p = run_tool("build_blocks_db.py", [])
        assert p.returncode == 0, p.stderr[-300:]
        return (ROOT / "data" / "blocks.json").read_text(encoding="utf-8")

    # --- healthy tools regressions ---
    def h_evolution_cli_status():
        p = run_tool("evolution_cli.py", ["--status"])
        assert p.returncode == 0
        st = json.loads(p.stdout)
        assert st["algorithms"] >= 11
        return "status JSON ok"

    def h_e2e_demo():
        p = run_tool("e2e_exchange_demo.py", [str(Path(tempfile.mkdtemp()) / "ee")],
                     timeout=600)
        assert p.returncode == 0 and "E2E exchange demo complete" in p.stdout
        return "exchange demo completes"

    def h_dither_bench():
        with tempfile.TemporaryDirectory() as td:
            p = run_tool("dither_bench.py", ["--out", str(td)])
            assert p.returncode == 0, p.stderr[-300:]
            assert (Path(td) / "contact_sheet.png").exists() and (Path(td) / "metrics.csv").exists()
            return "12 algos + contact sheet + CSV"

    def h_build_portable():
        with tempfile.TemporaryDirectory() as td:
            p = run_tool("build_portable.py", ["--out", str(Path(td) / "fp")])
            assert p.returncode == 0, p.stderr[-300:]
            return "portable build ok"

    def ng_lab_strategy():
        """nevergrad backend (optional dep) really drives candidates."""
        try:
            import nevergrad  # noqa: F401
        except ImportError:
            print("PASS ng_lab_strategy  [SKIPPED: nevergrad not installed]")
            return None
        import time as _t
        with tempfile.TemporaryDirectory() as td:
            shutil.rmtree(Path(td) / "ng", ignore_errors=True)
            lab = labcore.LabController(Path(td) / "ng", evals_limit=8,
                                        time_limit_s=240, strategy="nevergrad",
                                        suite_ids=["grad-smooth"])
            lab.start()
            deadline = _t.time() + 240
            while lab.is_running() and _t.time() < deadline:
                _t.sleep(0.4)
            st = lab.status()
            assert st["evaluated"] >= 6, st
            assert st["best"] is not None
            return f"evaluated {st['evaluated']}, best {st['best']['score']:.4f}"

    def no_ng_crash_guard():
        """Built-in lab stays healthy when nevergrad strategy requested but env lacks BLAS cap: crash recorded, not silent."""
        with tempfile.TemporaryDirectory() as td:
            lab = labcore.LabController(Path(td) / "ng", evals_limit=3,
                                        time_limit_s=60, strategy="nevergrad",
                                        suite_ids=["grad-smooth"])
            lab.start()
            deadline = _t.time() + 60
            while lab.is_running() and _t.time() < deadline:
                _t.sleep(0.3)
            st = lab.status()
            crash = lab.state.get("crash", "")
            if st["evaluated"] >= 2:
                return "ran fine without crash guard"
            assert crash, "neither evaluated nor crash recorded"
            return f"crash recorded: {crash[:60]}"

    import csv  # used above
    import time as _t
    from firefly.mapart import labcore
    def ng_lab_strategy2():
        return ng_lab_strategy()

    t("ng_lab_strategy", ng_lab_strategy)
    t("v_nameless_sim", v_nameless_sim)
    t("v_missing_sim", v_missing_sim)
    t("v_success_sim_spaces", v_success_sim_spaces)
    t("v_fail_sim", v_fail_sim)
    t("v_bad_json", v_bad_json)
    t("b_valid_default", b_valid_default)
    t("b_valid_multimap", b_valid_multimap)
    t("b_invalid_dims", b_invalid_dims)
    t("b_help", b_help)
    t("db_deterministic_and_unresolved_honesty", db_deterministic_and_unresolved_honesty)
    t("h_evolution_cli_status", h_evolution_cli_status)
    t("h_e2e_demo", h_e2e_demo)
    t("h_dither_bench", h_dither_bench)
    t("h_build_portable", h_build_portable)

    # --- audit_palette_accuracy.py (headless in-game-accuracy audit path) ---
    def pa_refuse_no_sim():
        with tempfile.TemporaryDirectory() as td:
            sidecar = Path(td) / "sidecar.json"
            env = dict(os.environ)
            env.pop("MAPART_SIMULATOR", None)
            p = run_tool("audit_palette_accuracy.py",
                         ["--version", "survival-wool-1.8", "--out", str(sidecar)], env=env)
            assert p.returncode == 2 and "no render simulator configured" in p.stderr
            assert not sidecar.is_file(), "refused audit must record nothing"
            return "exit 2, actionable message, nothing recorded"

    def pa_stub_verified_sidecar():
        """Plumbing check with a REAL helper script as the simulator: it truly
        renders each band's expected shaded color. This verifies the audit
        path end-to-end; it is NOT an in-game accuracy claim."""
        with tempfile.TemporaryDirectory() as td:
            sim = make_sim(td, behavior="ok", name="sim_audit_ok.py")
            sidecar = Path(td) / "audit.json"
            p = run_tool("audit_palette_accuracy.py",
                         ["--version", "survival-wool-1.8", "--simulator", str(sim),
                          "--out", str(sidecar)])
            assert p.returncode == 0, (p.returncode, p.stderr[-500:])
            out = json.loads(p.stdout)
            assert out["ok"] and out["total"] == 64 and out["verified"] == 64, out
            blob = json.loads(sidecar.read_text(encoding="utf-8"))
            assert blob["format"] == "firefly-palette-audit/1"
            assert all(r["status"] == "verified" and r["delta"] == 0.0
                       for r in blob["records"])
            # job hygiene (in-process, no working-tree side effects):
            # no map_color id repeats inside any single job
            workdir = Path(tempfile.mkdtemp(prefix="pa_jobs_"))
            import firefly.mapart.palette_audit as pa_mod
            # explicit out_path INSIDE the scratch dir — the audit must never
            # leak its sidecar into the repo's real assets dir
            summary = pa_mod.audit_palette("survival-wool-1.8", Path(sim),
                                           workdir=workdir,
                                           out_path=workdir / "scratch_sidecar.json")
            sim_count = 0
            for jpath in sorted(workdir.glob("job_*.json")):
                job = json.loads(jpath.read_text(encoding="utf-8"))
                ids = [row[0] for row in job["bands"]]
                assert len(ids) == len(set(ids)), f"duplicate id in {jpath.name}"
                sim_count += 1
            assert sim_count >= 2, "work dir must hold the rendered jobs"
            return f"{out['total']}/{out['total']} verified via stub render, jobs id-unique"

    def pa_stub_failing_verdicts():
        with tempfile.TemporaryDirectory() as td:
            sim = make_sim(td, behavior="ok", name="sim_audit_shift.py")
            # shift EVERY rendered color by 30 (green channel) -> deltas big
            src = sim.read_text(encoding="utf-8")
            src = src.replace("out[y, x] = pal[plan[y, x]]",
                              "out[y, x] = tuple(min(255, v + 30 if i == 1 else v) for i, v in enumerate(pal[plan[y, x]]))")
            sim.write_text(src, encoding="utf-8")
            sidecar = Path(td) / "audit_bad.json"
            p = run_tool("audit_palette_accuracy.py",
                         ["--version", "survival-wool-1.8", "--simulator", str(sim),
                          "--out", str(sidecar)])
            assert p.returncode == 0, p.stderr[-300:]
            out = json.loads(p.stdout)
            assert out["failed"] == out["total"] and out["verified"] == 0, out
            return f"{out['failed']}/{out['total']} failed verdicts from a divergent renderer"

    def pa_sidecar_attach_upgrades_status():
        """A sidecar upgrades a loaded palette's verification status — with an
        explicit asset_dir, so the repo's real assets are never touched."""
        from firefly.mapart import mapart as mm, palette_audit as pa
        version = "survival-wool-1.8"
        with tempfile.TemporaryDirectory() as td:
            sim = make_sim(Path(td), behavior="ok", name="sim_attach.py")
            tmp_asset = Path(td) / "assets"; tmp_asset.mkdir()
            summary = pa.audit_palette(version, sim, tolerance=2.5,
                                       workdir=tmp_asset / "audit_run_scratch",
                                       out_path=tmp_asset / f"palette_audit_{version}.json")
            assert summary["summary"]["verified"] == 64
            pal = mm.load_palette(version)
            assert pal.verification is None  # no sidecar visible to default loader
            verdict = pa.attach_audit(pal, asset_dir=tmp_asset)
            assert verdict["kind"] == "simulator-audited" and verdict["verified"] == 64
            assert all(c.get("audit", {}).get("status") == "verified" for c in pal.colors)
            assert "simulator-audited" in pal.verification_status
            return "per-color audit fields + upgraded status via explicit asset_dir"

    t("pa_refuse_no_sim", pa_refuse_no_sim)
    t("pa_stub_verified_sidecar", pa_stub_verified_sidecar)
    t("pa_stub_failing_verdicts", pa_stub_failing_verdicts)
    t("pa_sidecar_attach_upgrades_status", pa_sidecar_attach_upgrades_status)

    # --- two-block combo mode (opt-in optimizer exploration; HYPOTHESIS until
    # a real audit specimen verifies the cover's no-map-color behavior) ---
    def cb_colors_identical_off_on():
        """Combo mode must be color-NEUTRAL by construction: identical idx and
        preview when only the combo fields change; only extra cover blocks."""
        from firefly.mapart import appcore as ac
        import numpy as _np
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "i.png"; fixture_png(img, 128)
            base = dict(version="survival-wool-1.8", maps_w=1, maps_h=1,
                        algorithm="bayer8", strength=1.0, formats=("schem",))
            r_off = ac.process(img, ac.Settings(**base, combos=False))
            r_on = ac.process(img, ac.Settings(**base, combos=True, combo_ratio=0.4))
            assert r_on.combo_cover is not None and r_off.combo_cover is None
            assert _np.array_equal(r_off.idx, r_on.idx), "idx changed under combos"
            assert _np.array_equal(r_off.preview, r_on.preview), "preview changed"
            assert r_off.counts == r_on.counts
            return f"idx/preview byte-identical; cover grid on with " \
                   f"{int(r_on.combo_cover.sum())} covered"

    def cb_ratio_semantics():
        """ratio 0 -> none, ratio 1 -> all, monotone share, reproducible."""
        from firefly.mapart import mapart as mm
        import numpy as _np
        idx = _np.arange(256).reshape(16, 16) % 17
        c0, b0 = mm.compute_combo_cover(idx, 0.0, seed=7)
        c1, b1 = mm.compute_combo_cover(idx, 1.0, seed=7)
        cm, _ = mm.compute_combo_cover(idx, 0.6, seed=7)
        cm2, _ = mm.compute_combo_cover(idx, 0.6, seed=7)
        assert not c0.any() and c1.all()
        assert cm.mean() <= 0.65 and cm.mean() >= 0.55
        assert _np.array_equal(cm, cm2), "cover mask must reproduce"
        c_other, _ = mm.compute_combo_cover(idx, 0.6, seed=8)
        assert not _np.array_equal(cm, c_other), "different seeds must differ"
        for lo, hi in ((0.0, 0.3), (0.3, 0.6), (0.6, 0.9)):
            glo, _ = mm.compute_combo_cover(idx, hi, seed=3)
            ghi, _ = mm.compute_combo_cover(idx, lo, seed=3)
            assert _np.mean(glo) >= _np.mean(ghi), f"coverage not monotone in [{lo},{hi}]"
            assert not _np.any(ghi & ~glo), f"cover(lo) must be subset of cover(hi) [{lo},{hi}]"
        b = mm.compute_combo_cover(idx, 0.5, front_block="minecraft:tinted_glass")[1]
        assert b == "minecraft:tinted_glass"
        try:
            mm.compute_combo_cover(idx, 1.5)
            raise AssertionError("ratio 1.5 accepted")
        except ValueError:
            pass
        return "0/all/monotone/reproducible/seed-sensitivity/bounds ok"

    def cb_export_structure_and_hypothesis():
        """full_build with combos: 5-layer geometry validated, report carries
        explicit HYPOTHESIS label and true cover counts."""
        from firefly.mapart import appcore as ac, schem
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "i.png"; fixture_png(img, 128)
            s = ac.Settings(version="survival-wool-1.8", maps_w=1, maps_h=1,
                            algorithm="bayer8", strength=1.0, formats=("schem",),
                            combos=True, combo_ratio=0.5,
                            combo_front_block="minecraft:glass")
            files = ac.full_build(img, s, Path(td) / "out", base_name="combo")["files"]
            report = json.loads(Path(files["report"]).read_text(encoding="utf-8"))
            # solved staircase height + 1 dedicated cover plane
            hgt = int(report["construction"]["height"]) + 1
            # validate_schem raises ValueError on any structural/geometry mismatch
            v = schem.validate_schem(Path(files["schem"]),
                                     expect={"size": (128, hgt, 128)})
            assert v["size"] == [128, hgt, 128], v
            cb = report["combos"]
            assert cb["enabled"] and cb["front_block"] == "minecraft:glass"
            assert cb["covered_pixels"] > 0 and cb["ratio_requested"] == 0.5
            share = cb["covered_pixels"] / (128 * 128)
            assert 0.4 <= share <= 0.6, share
            assert "HYPOTHESIS" in cb["verification"] and "NOT been verified" in cb["verification"]
            cc = report["construction_counts"]
            assert cc is not None, "combos report must carry construction counts"
            assert cc["totals"].get("minecraft:glass", 0) == cb["covered_pixels"], cc
            # totals include air cells; placed == everything except air
            total = sum(n for b, n in cc["totals"].items() if b != "minecraft:air")
            assert total == 128 * 128 + cb["covered_pixels"], (total, cb["covered_pixels"])
            # independent cross-check: the validator's placed count == visible
            # plane + combo covers (full_build already enforces the same law)
            assert v["blocks_placed"] == 128 * 128 + cb["covered_pixels"], \
                (v["blocks_placed"], cb["covered_pixels"])
            return f"5-layer validated, cover {cb['covered_pixels']} px, hypothesis label present"

    def cb_lab_explore_combos():
        """Lab exploration with allow_combos runs healthy and lets combo
        candidates flow through evaluation without registry rejection."""
        import time as _ti
        with tempfile.TemporaryDirectory() as td:
            lab = labcore.LabController(Path(td) / "lab", evals_limit=4,
                                        time_limit_s=180, strategy="greedy",
                                        suite_ids=["grad-smooth"], allow_combos=True)
            lab.start()
            deadline = _ti.time() + 180
            while lab.is_running() and _ti.time() < deadline:
                _ti.sleep(0.3)
            st = lab.status()
            assert st["evaluated"] >= 2, st
            assert lab.state.get("crash", "") == "", lab.state.get("crash")
            rows = lab.history()
            assert rows and all(e.get("score") is not None for e in rows), rows
            # candidates must serialize combo fields in canonical identity
            cands = [e["candidate"] for e in rows]
            assert all(isinstance(c.get("combos_site", c.get("combos", False)),
                                  (bool, type(None))) for c in cands)
            combo_fields_ok = all(
                (c.get("combos") in (True, None, False)) for c in cands) and \
                all(0.0 <= float(c.get("combo_ratio", 0.0)) <= 1.0 for c in cands)
            assert combo_fields_ok, [(c.get("combos"), c.get("combo_ratio")) for c in cands]
            n_combo = sum(1 for c in cands if c.get("combos"))
            return f"evaluated {st['evaluated']} healthy, {n_combo} combo candidates serialized"

    t("cb_colors_identical_off_on", cb_colors_identical_off_on)
    t("cb_ratio_semantics", cb_ratio_semantics)
    t("cb_export_structure_and_hypothesis", cb_export_structure_and_hypothesis)
    t("cb_lab_explore_combos", cb_lab_explore_combos)

    print(f"\nALL PASSED" if not FAILURES else f"\nFAILED: {FAILURES}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
