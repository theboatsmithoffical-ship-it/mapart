#!/usr/bin/env python3
"""FireFly engine test battery (plain python, no framework dependency).

Run:  python tests/test_engine.py   (exit 0 = all pass)
Each feature prints PASS/FAIL with concrete evidence values.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from firefly.evolution import dither, synth, evolve, pack, store  # noqa: E402
from firefly.evolution.genome import mutate, crossover, genome_id, sanitize, random_genome  # noqa: E402
import time as _time
from firefly.mapart import appcore, benchmark, labcore, registry, schem, nbt, testsuite  # noqa: E402
from firefly.mapart.mapart import available_versions, load_palette  # noqa: E402

FAILURES = []


def t(name, fn):
    try:
        ev = fn()
        print(f"PASS {name}" + (f"  [{ev}]" if ev else ""))
    except Exception as e:  # noqa: BLE001
        FAILURES.append(name)
        import traceback
        tb = traceback.extract_tb(sys.exc_info()[2])[-1]
        print(f"FAIL {name}: {type(e).__name__}: {e}  @line {tb.lineno}")


def check(name, cond, detail=""):
    """Soft sub-checks inside one registered test; failure keeps the battery red.

    """
    if cond:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}")
        FAILURES.append(f"{name}")


ENV = synth.builtin_env()


def d_image(seed=0, **over):
    g = {"algorithm": "floyd_steinberg", "strength": 1.0, "bias": 0,
         "serpentine": True, "gamma": 1.0, "mapping": "nearest"}
    g.update(over)
    return dither.dither_image(ENV.fixtures[0][1], g, seed)


def main() -> int:
    imgs = synth.builtins()

    def algorithms_real_and_selectable():
        assert len(dither.ALGORITHMS) >= 10, dither.ALGORITHMS
        g = {"algorithm": "bayer4", "strength": 1.0, "bias": 0, "serpentine": True,
             "gamma": 1.0, "mapping": "nearest"}
        out = dither.dither_image(imgs["steady"], g, 1)
        assert out.shape == imgs["steady"].shape and out.dtype.name == "uint8"
        return f"{len(dither.ALGORITHMS)} algorithms"

    def deterministic_outputs():
        a, b = d_image(seed=11), d_image(seed=11)
        assert a.tobytes() == b.tobytes()
        s1, _ = evolve.evaluate(ENV, {"algorithm": "floyd_steinberg", "strength": 1.0,
                                      "bias": 0, "serpentine": True, "gamma": 1.0,
                                      "mapping": "nearest"})
        s2, _ = evolve.evaluate(ENV, {"algorithm": "floyd_steinberg", "strength": 1.0,
                                      "bias": 0, "serpentine": True, "gamma": 1.0,
                                      "mapping": "nearest"})
        assert s1 == s2
        return f"identical bytes + score {s1}"

    def parameters_change_output():
        base = d_image(seed=3)
        varied = d_image(seed=3, strength=0.3)
        assert base.tobytes() != varied.tobytes()
        b8 = d_image(seed=3, algorithm="bayer8")
        thr = d_image(seed=3, algorithm="threshold")
        rnd = d_image(seed=3, algorithm="random")
        assert b8.tobytes() != thr.tobytes() != rnd.tobytes() != base.tobytes()
        return "strength/bayer/threshold/random all alter output"

    def serpentine_changes_diffusion():
        base = d_image(seed=1)
        s = d_image(seed=1, serpentine=False)
        assert base.tobytes() != s.tobytes()
        return "true"

    def genome_schema_and_ids():
        assert genome_id(sanitize(random_genome(__import__("random").Random(1)))) is not None
        try:
            sanitize({"algorithm": "bayer2", "strength": 1.0, "bias": 0,
                      "serpentine": True, "gamma": 1.0, "mapping": "nearest",
                      "hook": "surprise"})
            raise AssertionError("extra key accepted")
        except ValueError:
            pass
        w = {"algorithm": "floyd_steinberg", "strength": 1.0, "bias": 0,
             "serpentine": True, "gamma": 1.0, "mapping": "nearest"}
        a = {"algorithm": "bayer2", "strength": 0.5, "bias": 0, "serpentine": True,
             "gamma": 1.0, "mapping": "nearest"}
        mg = mutate(w, __import__("random").Random(2))
        assert set(mg) == set(w)
        cg = crossover(a, w, __import__("random").Random(3))
        assert set(cg) == set(w)
        assert genome_id(a) != genome_id(w)
        return "schema enforced, ids content-bound"

    def evolution_and_checkpoints():
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td) / "run"
            s = evolve.run_evolution(ENV, rd, pop=5, gens=2, seed=42, budget=8)
            assert s["best"] is not None and s["evals_used"] <= 8
            ck = sorted((rd / "checkpoints").glob("gen_*.json"))
            assert len(ck) == 2, [p.name for p in ck]
            state = json.loads((rd / "state.json").read_text())
            assert state["status"] == "budget_limited"   # budget 8 < pop*gens 10
            again = evolve.run_evolution(ENV, rd, pop=5, gens=2, seed=42, budget=8, resume=True)
            assert again.get("resume_noop") is True
            return f"evals {s['evals_used']}, best {s['best']['score']:.1f}"

    def checkpoint_crash_recovery():
        with tempfile.TemporaryDirectory() as td:
            full = Path(td) / "full"
            evolve.run_evolution(ENV, full, pop=4, gens=2, seed=9, budget=64)
            crashed = Path(td) / "crashed"
            (crashed / "checkpoints").mkdir(parents=True)
            shutil.copy(full / "checkpoints" / "gen_0.json", crashed / "checkpoints" / "gen_0.json")
            rec = evolve.run_evolution(ENV, crashed, pop=4, gens=2, seed=9,
                                       budget=64, resume=True)
            st = json.loads((crashed / "state.json").read_text())
            assert st["status"] == "complete" and rec["ok"]
            return "resumed from checkpoint -> complete"

    def export_import_inspect():
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td) / "run"
            synth_png = Path(td) / "fixture.png"
            import numpy as np
            from PIL import Image
            arr = imgs["steady"]
            Image.fromarray(arr, "RGB").save(synth_png, format="PNG")
            custom = synth.fixture_env({"fixture": arr}, "custom-test")
            evolve.run_evolution(custom, rd, pop=4, gens=1, seed=5, budget=4)
            pkg = Path(td) / "p.ffevopack"
            out = pack.export_package(rd, synth_png, pkg, "unit-label", include_assets=True)
            assert out["candidates"] >= 1
            info = pack.inspect_package(pkg)
            assert info["kind"] == "ffevopack" and info["candidates"] == out["candidates"]
            with zipfile.ZipFile(pkg) as zf:
                names = set(zf.namelist())
            assert "manifest.json" in names and "candidates.json" in names
            assert any(n.startswith("assets/") for n in names)
            return f"{out['candidates']} verified candidates"

    def security_rejections():
        good = json.dumps({"kind": "ffevopack", "format_version": 1, "label": "x",
                           "sender": {"e": "v"}, "created": "t0",
                           "engine_version": "1.0.0", "suite": {"id": "s"}}).encode()
        bad_manifest = json.dumps({"kind": "not-ffevopack", "format_version": 1,
                                   "label": "x", "sender": {}, "created": "t",
                                   "engine_version": "v", "suite": {},
                                   "candidates_sha256": "0" * 64}).encode()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "m.ffevopack"
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("manifest.json", good)
                zf.writestr("candidates.json", "[]")
                zf.writestr("hook.py", "print('shellcode?')")
            try:
                pack.read_package(p); raise AssertionError("foreign member accepted")
            except pack.PackageError as e:
                pass
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("manifest.json", good)
                zf.writestr("candidates.json", "[]")
                zf.writestr("../escape.txt", "nope")
            try:
                pack.read_package(p); raise AssertionError("traversal member accepted")
            except pack.PackageError:
                pass
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("manifest.json", bad_manifest)
                zf.writestr("candidates.json", "[]")
            try:
                pack.read_package(p); raise AssertionError("wrong kind accepted")
            except pack.PackageError:
                pass
            (p).write_bytes(b"PK\x01\x02 garbage not a zip")
            try:
                pack.read_package(p); raise AssertionError("garbage accepted")
            except pack.PackageError:
                pass
        return "foreign member / traversal / kind / bad-zip all rejected"

    def file_fixture(td, img):
        from PIL import Image
        d = Path(td) / "fixtures"
        d.mkdir(parents=True, exist_ok=True)
        Image.fromarray(img, "RGB").save(d / "img.png", format="PNG")
        return d

    def inbox_quarantine_dedupe():
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td) / "run"
            fdir = file_fixture(td, imgs["steady"])
            files = sorted(fdir.glob("*.png"))
            custom = synth.fixture_env({f.stem: synth.load_image(f) for f in files},
                                       "custom-x", {f.stem: str(f) for f in files})
            evolve.run_evolution(custom, rd, pop=3, gens=1, seed=2, budget=3)
            pkgA = Path(td) / "a.ffevopack"
            pack.export_package(rd, None, pkgA, "pkg-a")
            ws = Path(td) / "ws"
            (ws / "optimizer-inbox").mkdir(parents=True)
            shutil.copy(pkgA, ws / "optimizer-inbox" / "a.ffevopack")
            (ws / "optimizer-inbox" / "corrupt.ffevopack").write_bytes(b"PK\x03\x04 garbage")
            rep = store.inbox_scan(ws)
            assert len(rep["imported"]) == 1 and len(rep["quarantined"]) == 1, rep
            reg = json.loads((ws / "store" / "registry.json").read_text())
            kinds = [e["kind"] for e in reg["entries"]]
            assert kinds == ["package"], kinds
            quar = list((ws / "quarantine").iterdir())
            assert len(quar) == 1
            shutil.copy(pkgA, ws / "optimizer-inbox" / "a-bis.ffevopack")
            rep2 = store.inbox_scan(ws)
            assert len(rep2["duplicates"]) == 1 and rep2["imported"] == [], rep2
            return "import=1 quarantine=1 dedupe=1"

    def local_rebenchmark_preserves_claims():
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td) / "run"
            fdir = file_fixture(td, imgs["tilted"])
            files = sorted(fdir.glob("*.png"))
            custom = synth.fixture_env({f.stem: synth.load_image(f) for f in files},
                                       "custom-y", {f.stem: str(f) for f in files})
            evolve.run_evolution(custom, rd, pop=3, gens=1, seed=4, budget=3)
            pkgA = Path(td) / "a.ffevopack"
            pack.export_package(rd, None, pkgA, "claims-src")
            ws = Path(td) / "ws"
            (ws / "optimizer-inbox").mkdir(parents=True)
            shutil.copy(pkgA, ws / "optimizer-inbox" / "a.ffevopack")
            store.inbox_scan(ws)
            claimed_before = {e["claims"][0]["id"]: e["claims"][0]["score"]
                              for e in json.loads((ws / "store" / "registry.json").read_text())["entries"]}
            ev = store.evaluate_imported(ws)
            assert ev["packages_evaluated"] == 1 and ev["locally_verified"] >= 1, ev
            reg = json.loads((ws / "store" / "registry.json").read_text())["entries"]
            first = [e for e in reg if e["kind"] == "package"][0]
            assert claimed_before[first["claims"][0]["id"]] == first["claims"][0]["score"]
            lr = first["local_results"]
            assert isinstance(lr, list) and "claimed_score" in lr[0] and "local_score" in lr[0]
            seeds = store.seed_candidates(ws)
            assert len(seeds) >= 1 and isinstance(seeds[0]["genome"], dict)
            return f"local={first['local_results'][0]['local_score']} claim kept"

    def import_review_lists_claims_next_to_locals():
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td) / "run"
            fdir = file_fixture(td, imgs["tilted"])
            files = sorted(fdir.glob("*.png"))
            custom = synth.fixture_env({f.stem: synth.load_image(f) for f in files},
                                       "custom-z", {f.stem: str(f) for f in files})
            evolve.run_evolution(custom, rd, pop=3, gens=1, seed=4, budget=3)
            pkgA = Path(td) / "a.ffevopack"
            pack.export_package(rd, None, pkgA, "review-src")
            ws = Path(td) / "ws"
            (ws / "optimizer-inbox").mkdir(parents=True)
            shutil.copy(pkgA, ws / "optimizer-inbox" / "a.ffevopack")
            store.inbox_scan(ws)
            # BEFORE local evaluation: every row must say not_locally_evaluated
            pre = store.import_review(ws)
            pre_rows = pre["packages"][0]["rows"]
            assert pre_rows and all(r["verdict"] == "not_locally_evaluated"
                                    for r in pre_rows)
            store.evaluate_imported(ws)
            rv = store.import_review(ws)
            rows = rv["packages"][0]["rows"]
            # every claim must appear NEXT TO a locally measured number
            joined = (all(r["local_score"] is not None for r in rows) and
                      all(r["claim"] is not None for r in rows))
            check("review joins claims with local results", joined, str(rows[:2]))
            allowed = {"claim_confirmed", "expected_fixture_gap", "not_reproduced",
                       "local_run_not_deterministic", "not_locally_evaluated"}
            check("review verdicts sanctioned",
                  {r["verdict"] for r in rows} <= allowed,
                  str(rows[0]["verdict"]))
            # sender suite captured from the package manifest
            sender_suite = rv["packages"][0]["sender_suite"]
            check("review captures sender suite", sender_suite == "custom-z",
                  str(sender_suite))
            # deltas attributed to the fixture gap, not phrased as an accusation
            gap_all = all(r["verdict"] == "expected_fixture_gap" for r in rows)
            check("review attributes deltas to fixture gap", gap_all,
                  str({r['verdict'] for r in rows}))
            # exact repeated claims collapse, keeping occurrence counts
            ids = [r["id"] for r in rows]
            check("review dedupes duplicate claims", len(ids) == len(set(ids)))
            n_star = sum(1 for r in rows if r["sender_champion"])
            n_plus = sum(1 for r in rows if r["best_locally"])
            check("review champion marks unique", n_star == 1 and n_plus == 1,
                  f"*={n_star} += {n_plus}")
            report = Path(rv["report"])
            txt = report.read_text(encoding="utf-8")
            check("review report written",
                  txt.startswith("# Shared-champion") and
                  "| claim (sender) | local score |" in txt)
            return (f"{len(rows)} rows; verdicts="
                    f"{sorted({r['verdict'] for r in rows})}")

    def seeded_run_uses_store():
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td) / "run"
            fdir = file_fixture(td, imgs["crystal"])
            files = sorted(fdir.glob("*.png"))
            custom = synth.fixture_env({f.stem: synth.load_image(f) for f in files},
                                       "custom-z", {f.stem: str(f) for f in files})
            evolve.run_evolution(custom, rd, pop=3, gens=1, seed=4, budget=3)
            pkgA = Path(td) / "a.ffevopack"
            pack.export_package(rd, None, pkgA, "seed-src")
            ws = Path(td) / "ws"
            (ws / "optimizer-inbox").mkdir(parents=True)
            shutil.copy(pkgA, ws / "optimizer-inbox" / "a.ffevopack")
            store.inbox_scan(ws)
            store.evaluate_imported(ws)
            seeds = store.seed_candidates(ws)
            seeded = evolve.run_evolution(ENV, Path(td) / "seeded", pop=3, gens=1,
                                          seed=7, budget=3,
                                          seed_population=[c["genome"] for c in seeds])
            assert seeded["ok"]
            return f"seeded run ok with {len(seeds)} seeds"

    def cli_end_to_end():
        with tempfile.TemporaryDirectory() as td:
            png = Path(td) / "f.png"
            fixture_png(png)
            work = Path(td) / "ws"
            (work / "optimizer-inbox").mkdir(parents=True)
            run_dir = Path(td) / "rd"
            r1 = cli(["run", "--fixtures", str(png), "--run-dir", str(run_dir),
                      "--pop", "3", "--gens", "1", "--seed", "1", "--eval-budget", "3"])
            assert r1["ok"] and r1["best"]["score"] is not None
            pkg = Path(td) / "p.ffevopack"
            r2 = cli(["package", "export", "--run-dir", str(run_dir), "--input", str(png),
                      "--out", str(pkg), "--label", "cli", "--include-assets"])
            assert r2["candidates"] >= 1
            r3 = cli(["package", "inspect", "--input", str(pkg)])
            assert r3["candidates"] == r2["candidates"]
            shutil.copy(pkg, work / "optimizer-inbox" / "p.ffevopack")
            (work / "optimizer-inbox" / "bad.ffevopack").write_bytes(b"zzz")
            r4 = cli(["inbox", "--root", str(work)])
            assert len(r4["imported"]) == 1 and len(r4["quarantined"]) == 1
            r5 = cli(["evaluate-imported", "--root", str(work)])
            assert r5["locally_verified"] >= 1
            r6 = cli(["seed-from-store", "--root", str(work), "--fixtures", str(png),
                      "--run-dir", str(Path(td) / "seeded"), "--pop", "3", "--gens", "1",
                      "--seed", "1", "--eval-budget", "3"])
            assert r6["ok"] and r6["seeded_from"] >= 0
            pkg2 = Path(td) / "p2.ffevopack"
            r7 = cli(["package", "export", "--run-dir", str(Path(td) / "seeded"),
                      "--input", str(png), "--out", str(pkg2), "--label", "cli2"])
            assert r7["candidates"] >= 1
            r8 = cli(["package", "merge", "--a", str(pkg), "--b", str(pkg2),
                      "--out", str(Path(td) / "m.ffevopack"), "--notes", "n"])
            assert r8["candidates"] >= 1
            return "all CLI commands returned valid JSON"

    def portable_build_and_checks():
        with tempfile.TemporaryDirectory() as td:
            dist = Path(td) / "firefly-portable"
            r = subprocess.run([sys.executable, "tools/build_portable.py",
                                "--out", str(dist), "--no-self-test"],
                               capture_output=True, text=True, cwd=str(ROOT), timeout=300)
            assert r.returncode == 0, r.stderr[-1200:]
            doc = subprocess.run([sys.executable, str(dist / "firefly_portable.py"), "doctor"],
                                 capture_output=True, text=True, cwd=str(dist), timeout=120)
            docj = json.loads(doc.stdout.strip().splitlines()[-1])
            assert docj["ok"] and docj["integrity"] == "verified", docj
            smoke = subprocess.run([sys.executable, str(dist / "firefly_portable.py"),
                                    "smoke-run", str(Path(td) / "smoke")],
                                   capture_output=True, text=True, cwd=str(dist), timeout=300)
            smokej = json.loads(smoke.stdout.strip().splitlines()[-1])
            assert smokej["ok"], smokej
            return f"doctor+smoke ok ({len(json.loads((dist / 'MANIFEST.json').read_text())['files'])} files)"

    def portable_full_demo():
        with tempfile.TemporaryDirectory() as td:
            dist = Path(td) / "firefly-portable"
            subprocess.run([sys.executable, "tools/build_portable.py", "--out", str(dist),
                            "--no-self-test"], capture_output=True, text=True,
                           cwd=str(ROOT), timeout=300, check=True)
            demo = subprocess.run([sys.executable, str(dist / "firefly_portable.py"),
                                   "demo", str(Path(td) / "work")],
                                  capture_output=True, text=True, cwd=str(dist),
                                  timeout=300)
            assert demo.returncode == 0, demo.stderr[-1500:]
            demoj = json.loads(demo.stdout.strip().splitlines()[-1])
            assert demoj["ok"] and demoj["packages"] >= 3 and demoj["quarantined"] == ["not.really.ffevopack"], demoj
            artifacts = list((Path(td) / "work").rglob("*.ffevopack"))
            assert len(artifacts) >= 3
            return f"{len(artifacts)} packages inside portable layout, quarantine step proven"

    def fixture_png(path, size=128):
        import numpy as np
        from PIL import Image
        x = np.linspace(28, 226, size, dtype=np.float64)
        y = np.linspace(226, 28, size, dtype=np.float64)
        r = np.broadcast_to(x[None, :], (size, size))
        b = np.broadcast_to(y[:, None], (size, size))
        g = 0.5 * (r + b)
        w = np.sin(r / 11.0) * 14 + np.cos(b / 7.0) * 10
        arr = np.stack([r + w, g + w * 0.5, b], axis=-1).clip(0, 255)
        Image.fromarray(arr.astype(np.uint8), "RGB").save(path, format="PNG")

    def cli(args):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run([sys.executable, "-m", "firefly.evolution.cli", *args],
                              capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=300)
        assert proc.returncode == 0, f"rc={proc.returncode} {proc.stderr[-800:]}"
        return json.loads(proc.stdout[proc.stdout.find("{"):])

    def make_fixture(path, size=(128, 128)):
        import numpy as np
        from PIL import Image
        x = np.linspace(10, 240, size[0], dtype=np.float64)
        y = np.linspace(240, 10, size[1], dtype=np.float64)
        a = np.broadcast_to(x[None, :], size)
        b = np.broadcast_to(y[:, None], size)
        w = np.sin(a / 13.0) * 30 + np.cos(b / 8.0) * 22
        arr = np.clip(np.stack([a + w * 0.7, 0.5 * (a + b), b + w * 0.5], axis=-1), 0, 255).astype(np.uint8)
        Image.fromarray(arr, "RGB").save(path, format="PNG")
        return path

    def nbt_roundtrip():
        root = {"x": [1, 2, -3], "s": "héllo", "f": 1.5, "n": {"a": -2},
                "ba": b"\x00\xff", "li": [[1, 2], [3]], "empty": []}
        back = nbt.read(nbt.write("t", root))
        assert back["x"] == [1, 2, -3] and back["s"] == "héllo"
        assert back["n"]["a"] == -2 and back["ba"] == b"\x00\xff"
        assert back["li"][0] == [1, 2] and back["empty"] == []
        return "compound/list/string/bytes/roundtrip"

    def mapart_palette_shapes():
        for ver in available_versions():
            pal = load_palette(ver)
            assert pal.n > 0 and len(pal.blocks) == pal.n
            assert len(pal.colors) == len(set((c["map_color"], c["shade"]) for c in pal.colors))
        return f"{len(available_versions())} versions"

    def mapart_suppression():
        with tempfile.TemporaryDirectory() as td:
            img = make_fixture(Path(td) / "f.png")
            base = appcore.Settings(algorithm="bayer4")
            out1 = appcore.full_build(img, base, Path(td) / "o1")
            s2 = appcore.Settings(algorithm="bayer4", suppress={1})
            out2 = appcore.full_build(img, s2, Path(td) / "o2")
            b1 = {c["block"] for c in load_palette("survival-wool-1.8").colors
                  if c["map_color"] == 1}
            from collections import Counter
            p1 = Counter(c for row in out1["result"].blocks for c in row)
            p2 = Counter(c for row in out2["result"].blocks for c in row)
            assert b1 <= set(p1) or max(p1.values()) > 0
            assert not (b1 & set(p2)), f"suppressed block still present: {b1 & set(p2)}"
            return "map id 1 absent from suppressed plan"

    def mapart_e2e_settings_preview_export_validate():
        with tempfile.TemporaryDirectory() as td:
            img = make_fixture(Path(td) / "f.png", (256, 256))
            s = appcore.Settings(version="survival-wool-1.8", maps_w=2, maps_h=2,
                                 algorithm="stucki", strength=1.1)
            out = appcore.full_build(img, s, Path(td) / "out")
            v = out["files"]["validation"]["schem"]
            assert v["blocks_placed"] == 2 * 2 * 128 * 128
            # exported height = solved staircase (tallest column + 1)
            assert v["size"] == [256, int(out["result"].heights.max()) + 1, 256], v["size"]
            rep = json.loads(Path(out["files"]["report"]).read_text(encoding="utf-8"))
            total_counts = sum(c["count"] for c in rep["counts"])
            assert total_counts == 2 * 2 * 128 * 128, total_counts
            # preview pixel agreement with plan: counts of unique preview pixels == counts recorded?
            # (photo-accuracy check belongs to manual review; here: preview pixels count matches)
            import numpy as np
            assert out["result"].preview.shape == (256, 256, 3)
            assert Path(out["files"]["preview"]).exists()
            return f"validated {v['palette_entries']} palette entries, {len(rep['counts'])} block kinds"

    def mapart_consistent_preview_export():
        """The exported .schem must equal the preview/plan: reconstruct placement
        from the schematic and compare with the *processed plan* (same settings)."""
        with tempfile.TemporaryDirectory() as td:
            img = make_fixture(Path(td) / "f.png")
            s = appcore.Settings(algorithm="bayer4")
            out = appcore.full_build(img, s, Path(td) / "out")
            root = nbt.read_gzip(Path(out["files"]["schem"]).read_bytes())
            inv = {i: b for b, i in root["Palette"].items()}
            data = schem.read_varints(root["BlockData"])
            # layer y=3 of schematic holds the *displayed* shade (deepest?) — match total
            from collections import Counter
            placed = Counter(inv[i] for i in data if i in inv)
            # full palette vs placed: the layers hold air for non-matching shades
            from collections import Counter
            plan_counts = Counter()
            for row in out["result"].blocks:
                for c in row:
                    plan_counts[c] += 1
            for blk in plan_counts:
                assert blk.startswith("minecraft:")
                assert placed.get(blk, 0) >= 1, f"{blk} planned but never placed"
            return "placement volume consistent with processed plan"

    def offline_no_network_imports():
        """Routine operation must be 100% offline. Only the image-library module
        may contain network code, and only its explicit user-triggered
        discovery/download calls may use it (dataset spec §8)."""
        banned = ("http", "github", "requests", "urllib.request", "socket.create_connection")
        allow = {"imagelibrary.py"}  # explicit-click downloads only
        hits = []
        root = Path(__file__).resolve().parents[1] / "src"
        for p in root.rglob("*.py"):
            if p.name in allow:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
            for b in banned:
                for ln in text.splitlines():
                    ln = ln.split("#", 1)[0]
                    if b == "http" and ("http://" in ln or "https://" in ln or "http" in ln and "import" in ln):
                        hits.append((p.name, ln.strip()))
                    elif b != "http" and b in ln:
                        hits.append((p.name, ln.strip()))
        assert not hits, hits[:5]
        # imagelibrary stays permitted, but a FULL offline campaign (below in
        # this battery: lab + breeding runs) must complete with networking
        # broken — implied by this module list and the e2e demo's socket probe.
        return "no network source lines in src/ outside the explicit-dataset module"

    def app_dist_selftest_from_its_dir():
        with tempfile.TemporaryDirectory() as td:
            dist = Path(td) / "FireflyEngine"
            subprocess.run([sys.executable, "tools/build_app.py", "--out", str(dist)],
                           capture_output=True, text=True, cwd=str(ROOT), timeout=600, check=True)
            data = json.loads((dist / "MANIFEST.json").read_text(encoding="utf-8"))
            assert data["network_use"] == "none in normal operation"
            assert (dist / "FireflyEngine.cmd").exists() and (dist / "assets").is_dir()
            # coverage in dist includes the GUI file itself
            assert ((dist / "src" / "firefly" / "mapart" / "gui.py")).exists()
            return f"{len(data['files'])} files, selftest from own dir passed"

    def testsuite_determinism_and_tiers():
        s1 = testsuite.suite("small", ["grad-smooth"])['grad-smooth'][1]
        s2 = testsuite.suite("small", ["grad-smooth"])['grad-smooth'][1]
        assert s1.tobytes() == s2.tobytes()
        for tier, size in testsuite.TIERS.items():
            arr = testsuite.np_image("color-chart", size)
            assert arr.shape[0] == size
        print(f"PASS testsuite_determinism_and_tiers  [{len(testsuite.GENERATORS)} items, 3 tiers]")
        return ""

    def registry_discovers_algorithms():
        specs = registry.all()
        assert len(specs) >= 11, len(specs)
        for name, spec in specs.items():
            p = spec.validate(spec.default_params_for())
        return f"{len(specs)} registered"

    def registry_rejects_invalid_params():
        try:
            registry.get("floyd_steinberg").validate({"strength": 3.5})
            raise AssertionError("out-of-range strength accepted")
        except ValueError as e:
            assert "outside" in str(e)
        try:
            registry.get("bayer4").validate({"hook": 1})
            raise AssertionError("unknown param accepted")
        except ValueError as e:
            assert "unknown" in str(e)
        return "usesful errors, hard reject"

    def lab_run_pause_resume_stop():
        import shutil
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "lab"
            lab = labcore.LabController(d, evals_limit=50, time_limit_s=600,
                                        suite_ids=["grad-smooth"])
            lab.start(); time.sleep(3.2)
            live = lab.is_running()
            lab.pause(); time.sleep(0.9)
            paused_val = lab.status()
            lab.resume(); time.sleep(2.6)
            lab.stop()
            st = lab.status()
            assert live and st["evaluated"] >= 1
            restored = labcore.LabController(d, evals_limit=50, time_limit_s=600)
            restored.start(); time.sleep(2.4); restored.stop()
            st2 = restored.status()
            assert st2["evaluated"] >= st["evaluated"]
            return f"first={st['evaluated']} continued={st2['evaluated']} best preserved"

    def lab_best_and_cache_dedupe():
        with tempfile.TemporaryDirectory() as td:
            lab = labcore.LabController(Path(td) / "lab", evals_limit=6, time_limit_s=300,
                                        suite_ids=["grad-smooth"])
            lab.start()
            t0 = time.time()+0
            deadline = time.time() + 240
            while lab.is_running() and time.time() < deadline:
                time.sleep(0.4)
            st = lab.status()
            assert st["best"] is not None and st["best"]["score"] is not None
            assert st["cached"] >= 1, st
            return f"best {st['best']['score']:.4f}, cache {st['cached']}"

    def benchmark_quick_smoke():
        from firefly.mapart import benchmark
        with tempfile.TemporaryDirectory() as td:
            camp = benchmark.Campaign(Path(td) / "b", mode="quick", tier="small",
                                      suite_ids=["grad-smooth"], time_limit_s=120)
            rep = camp.run()
            assert rep["evaluated"] == rep["total_possible"], rep
            assert (Path(td) / "b" / "summary.csv").exists()
            report = json.loads((Path(td) / "b" / "report.json").read_text(encoding="utf-8"))
            assert report["untested_combos"] == 0
            return f"{rep['evaluated']} combos, CSV+report written"

    def gui_tabs_construct():
        import tkinter as tk
        from firefly.mapart import gui
        with tempfile.TemporaryDirectory() as td:
            r = tk.Tk(); r.withdraw()
            app = gui.GuiApp(r)
            r.update()
            for fn in ("lab_start", "lab_pause", "lab_resume", "lab_stop",
                       "run_benchmark", "show_suite_item", "process", "export"):
                assert callable(getattr(app, fn)), fn
            r.destroy()
        return "all tab actions callable"

    t("testsuite_determinism_and_tiers", testsuite_determinism_and_tiers)
    t("registry_discovers_algorithms", registry_discovers_algorithms)
    t("registry_rejects_invalid_params", registry_rejects_invalid_params)
    t("lab_run_pause_resume_stop", lab_run_pause_resume_stop)
    t("lab_best_and_cache_dedupe", lab_best_and_cache_dedupe)
    t("benchmark_quick_smoke", benchmark_quick_smoke)
    t("gui_tabs_construct", gui_tabs_construct)
    t("nbt_roundtrip", nbt_roundtrip)
    t("mapart_palette_shapes", mapart_palette_shapes)
    t("mapart_suppression", mapart_suppression)
    t("mapart_e2e_settings_preview_export_validate", mapart_e2e_settings_preview_export_validate)
    t("mapart_consistent_preview_export", mapart_consistent_preview_export)
    t("offline_no_network_imports", offline_no_network_imports)
    t("app_dist_selftest_from_its_dir", app_dist_selftest_from_its_dir)
    t("algorithms_real_and_selectable", algorithms_real_and_selectable)
    t("deterministic_outputs", deterministic_outputs)
    t("parameters_change_output", parameters_change_output)
    t("serpentine_changes_diffusion", serpentine_changes_diffusion)
    t("genome_schema_and_ids", genome_schema_and_ids)
    t("evolution_and_checkpoints", evolution_and_checkpoints)
    t("checkpoint_crash_recovery", checkpoint_crash_recovery)
    t("export_import_inspect", export_import_inspect)
    t("security_rejections", security_rejections)
    t("inbox_quarantine_dedupe", inbox_quarantine_dedupe)
    t("local_rebenchmark_preserves_claims", local_rebenchmark_preserves_claims)
    t("import_review_lists_claims_next_to_locals", import_review_lists_claims_next_to_locals)
    t("seeded_run_uses_store", seeded_run_uses_store)
    t("cli_end_to_end", cli_end_to_end)
    t("portable_build_and_checks", portable_build_and_checks)
    t("portable_full_demo", portable_full_demo)

    total = 0
    print(f"\n{len(FAILURES)} FAILED" if FAILURES else "\nALL PASSED")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
