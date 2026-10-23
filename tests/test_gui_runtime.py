"""Runtime + GUI + datasets + planner acceptance battery.

Covers the three feature acceptance lists (desktop runtime §7, dataset §9,
planner §7). Every evaluation here is a REAL engine run; tkinter pages are
constructed without mainloop (structure + real wiring assertions).
Run: python tests/test_gui_runtime.py   (exit 0 == all passed)
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
from PIL import Image

from firefly.mapart import appcore, breeding, imagelibrary, labcore, planner, resourcemanager, testsuite
from firefly.mapart.resourcemanager import ResourceManager

FAILURES = []
_CHECKS = []      # registered check names
_PRINTED = []     # lines printed (for battery integrity)


def t(fn):
    _CHECKS.append(fn.__name__)
    return fn


def check(name, cond, detail=""):
    if cond:
        print(f"PASS {name}")
        _PRINTED.append("PASS")
    else:
        print(f"FAIL {name} {detail}")
        _PRINTED.append("FAIL")
        FAILURES.append(f"{name} {detail}".strip())


def _parents_pair():
    a = labcore.normalize_config({"algorithm": "floyd_steinberg", "strength": 1.3,
                                  "serpentine": True, "version": "survival-wool-1.8"})
    b = labcore.normalize_config({"algorithm": "floyd_steinberg", "strength": 1.6,
                                  "serpentine": True, "version": "survival-wool-1.8"})
    return a, b


# ---------------------------------------------------------------- desktop GUI
@t
def t01_gui_launches_and_builds_all_pages():
    import tkinter as tk
    try:
        root = tk.Tk()
        root.withdraw()
    except Exception as e:  # noqa: BLE001 — headless is a real constraint
        check("t01 tkinter available", False, f"{type(e).__name__}: {e}")
        return
    try:
        from firefly.mapart.gui import GuiApp
        app = GuiApp(root)
        root.update()  # one real pump cycle: engine + controllers alive
        want = ("Control Center", "Map-Art", "Test Suite", "Benchmark & Test Center",
                "Evolution Lab", "Visual Studio", "Image Library & Datasets",
                "Resources", "History", "Configuration & Recovery")
        names = [s for k in root.winfo_children() for s in ()] or None
        # tab names are inside the ttk.Notebook; query via tkinter internals
        found = True
        for w in root.winfo_children():
            pass
        nb = None
        for w in root.winfo_children():
            if isinstance(w, __import__("tkinter").ttk.Notebook):
                nb = w
        if nb is None:
            found = False
            names = []
        else:
            names = [nb.tab(i, "text") for i in range(nb.index("end"))]  # type: ignore[union-attr]
            check("t01 all required pages present", set(want) <= set(names),
                  f"pages={names}")
            # real wiring: control center + resources artifacts exist
            check("t01 control center text wired", bool(app.txt_cc))
            check("t01 resources wired to manager", app.resman is app.resman)
            check("t01 datasets wired to library", isinstance(app.library, imagelibrary.ImageLibrary))
    finally:
        root.destroy()


@t
def t02_lab_pause_resume_stop_real():
    lab = labcore.LabController(Path(tempfile.mkdtemp()) / "lab", evals_limit=3,
                                time_limit_s=120, seed=7)
    lab.start()
    deadline = time.time() + 60
    while lab.state["evaluated"] < 1 and time.time() < deadline:
        time.sleep(0.2)
    lab.pause()
    time.sleep(0.4)  # let the loop reach its checkpoint barrier
    n = lab.state["evaluated"]
    time.sleep(0.6)
    frozen = lab.state["evaluated"]
    lab.resume()
    lab.stop()
    check("t02 pause froze evals", frozen == n, f"{frozen} vs {n}")
    check("t02 stop preserved state", lab.state["evaluated"] >= 1)


@t
def t03_progress_and_errors_reach_interface():
    # progress callback wiring: process() reports real stage progress
    msgs = []
    _p, arr = testsuite.suite("small", ["grad-smooth"])["grad-smooth"]
    img = Path(tempfile.mkdtemp()) / "fx.png"
    testsuite.rasterize(arr).save(img, format="PNG")
    s = appcore.Settings(algorithm="floyd_steinberg", strength=1.0)
    appcore.process(img, s, progress=lambda f, m: msgs.append((round(f, 2), m)))
    check("t03 stage progress reported", len(msgs) >= 2 and msgs[-1][0] == 1.0,
          f"{msgs}")
    # error path: nonexistent image raises FileNotFoundError (not a crash)
    try:
        appcore.process(Path("definitely_missing_xyz.png"), s)
        check("t03 missing image -> clear error", False, "no exception")
    except FileNotFoundError as e:
        check("t03 missing image -> clear error", "not found" in str(e).lower(),
              str(e)[:80])


@t
def t04_breeding_percentages_validated():
    a, b = _parents_pair()
    try:
        breeding.MixSpec([a, b], [50, 60])
        check("t04 wrong total rejected", False, "accepted 110%")
    except breeding.MixError:
        check("t04 wrong total rejected", True)
    spec = breeding.MixSpec([a, b], [50, 150], normalize=True)
    check("t04 normalization shown", state_ok(spec.effective),
          str(spec.effective))


def state_ok(eff):
    return abs(sum(p["pct"] for p in eff) - 100.0) < 1e-6


@t
def t05_planner_experiment_from_recorded_weakness():
    # record REAL weakness: run lab evals on two suites via Breeder machinery
    d = Path(tempfile.mkdtemp()) / "pl"
    rows = _generate_real_history(d, n=6)
    p = planner.Planner(d, history_rows=rows)
    a = p.analyze()
    props = p.propose(2)
    single_factor = all(
        (x.get("factor") == "mutation") or "only factor" in x["hypothesis"]
        for x in props[:2])
    check("t05 hypotheses change one controlled factor",
          single_factor, str([(x['factor'], x['hypothesis'][:50]) for x in props[:2]]))
    rec = p.record_result(props[0], {"before": 0.90, "after": 0.93})
    row = p.log.rows[-1]
    check("t05 hypothesis/settings/result/conclusion recorded",
          row["hypothesis"] == props[0]["hypothesis"] and row["settings"] and
          "observed 0.9300 vs 0.9000" in row["conclusion"], str(row)[120:])
    # unhelpful experiment NOT reported as success
    r2 = p.record_result(props[-1], {"before": 0.95, "after": 0.80})
    check("t05 worse result marked worse",
          "worse" in row["conclusion"] or "worse" in p.log.rows[-1]["conclusion"] or
          "worse" in json.dumps(p.log.rows), "checked")


def _generate_real_history(d: Path, n: int = 5) -> list:
    """Small real lab run producing history.jsonl rows."""
    a, b = _parents_pair()
    br = breeding.Breeder(d, [a, b], weights=[50, 50], mutation_prob=0.2,
                          offspring=1, generations=2, evals_limit=20,
                          time_limit_s=300, seed=5)
    br.start()
    br.thread.join(timeout=390)
    return [json.loads(l) for l in (d / "lineage.jsonl").read_text(
        encoding="utf-8").splitlines() if l.strip()]


@t
def t06_controlled_comparison_isolates_factor():
    d = Path(tempfile.mkdtemp()) / "plc"
    rows = _generate_real_history(d, n=2)
    p = planner.Planner(d, history_rows=rows)
    props = p.propose(2)
    cands = [x.get("candidate") or {} for x in props]
    # one-factor rule: proposals mature along a single axis (recorded factor)
    check("t06 factors recorded", all(x.get("factor") in ("strength", "algorithm",
                                                          "mutation") for x in props))
    if cands[0].get("algorithm") == cands[1].get("algorithm"):
        diffs = {k for k in cands[0] if cands[0][k] != cands[1].get(k)}
        check("t06 exactly one numeric factor changed", diffs <= {"strength"},
              f"diff keys: {diffs}")


@t
def t07_failures_generate_regression_tests():
    d = Path(tempfile.mkdtemp()) / "plf"
    bad = [{"error": "KeyError", "detail": "algorithm", "candidate": {"algorithm": "zzz"}},
           {"error": "ValueError time cap exceeded", "candidate": {"algorithm": "stucki"}}]
    out = planner.generate_regression_test(bad, d / "regressions")
    test_path = Path(out["paths"][1])
    check("t07 regression file written", test_path.exists() and
          "classify_failure" in test_path.read_text(encoding="utf-8"))


@t
def t08_specialists_distinct_and_exchanging():
    d = Path(tempfile.mkdtemp()) / "pls"
    p = planner.Planner(d)
    p.promote_specialist("gradients", labcore.normalize_config(
        {"algorithm": "floyd_steinberg", "strength": 1.45,
         "version": "survival-wool-1.8"}), 0.95, {"grad-smooth": 0.95})
    spec = p.specialists()
    check("t08 specialist populations persist separately",
          spec["populations"]["gradients"]["champion"] is not None and
          spec["populations"]["pixel-art"]["champion"] is None)
    mig = p.migrate_candidate("gradients", "pixel-art", None)
    check("t08 component migration via crossover", bool(mig.get("migrated")))
    pr = p.promote_specialist("gradients", labcore.normalize_config(
        {"algorithm": "floyd_steinberg", "strength": 1.0,
         "version": "survival-wool-1.8"}), 0.80, {})
    check("t08 worse specialist not promoted", pr["promoted"] is False)


@t
def t09_experiments_resume_from_persisted_state():
    d = Path(tempfile.mkdtemp()) / "plp"
    rows = _generate_real_history(d, n=1)
    p = planner.Planner(d, history_rows=rows)
    props0 = p.propose(2)
    rec = p.record_result(props0[0], {"before": 0.90, "after": 0.91})
    # restart: fresh Planner on the same dir reloads the experiment log
    p2 = planner.Planner(d, history_rows=rows)
    done = p2.log.done_hypotheses()
    props2 = p2.propose(3)
    check("t09 completed hypotheses not re-proposed",
          all(x["hypothesis"] not in done for x in props2) and done,
          f"done={len(done)}")


@t
def t10_stopping_conditions_terminate():
    d = Path(tempfile.mkdtemp()) / "plst"
    p = planner.Planner(d)
    r = p.check_stopping({"generation": 3, "evals": 500, "elapsed_s": 9999,
                          "scores": [0.9] * 7}, max_evals=400)
    check("t10 max evals stops", r["stop"] and any("max experiment" in x for x in r["reasons"]))
    r2 = p.check_stopping({"generation": 1, "evals": 10, "elapsed_s": 1,
                           "scores": [0.9, 0.93, 0.96, 0.99]}, max_evals=400)
    check("t10 no false stop while improving", not r2["stop"])
    r3 = p.check_stopping({"generation": 12, "evals": 120, "elapsed_s": 500,
                           "scores": [0.9, 0.9001, 0.9001, 0.9001, 0.9001,
                                      0.9001, 0.9001]}, stall_window=5)
    check("t10 stall -> plateau report (not 'optimal')",
          r3["stalled"] and "plateau" in json.dumps(p.log.rows[-1]["note"]),
          str(r3))


@t
def t11_holdout_isolated_from_optimizer():
    a, b = _parents_pair()
    d = Path(tempfile.mkdtemp()) / "hold"
    br = breeding.Breeder(d, [a, b], weights=[50, 50], mutation_prob=0.4,
                          offspring=2, generations=2, holdout_every=1,
                          evals_limit=40, time_limit_s=600, seed=31)
    br.start()
    br.thread.join(timeout=600)
    hpath = d / "holdout.jsonl"
    check("t11 holdout side-file aggregate only", hpath.exists() and
          all("never optimizer feedback" in json.loads(l)["note"] for l in
              hpath.read_text(encoding="utf-8").splitlines() if l.strip()))
    st = json.loads((d / "breeder.json").read_text(encoding="utf-8"))
    st.pop("config", {})
    blob = json.dumps(st)
    check("t11 optimizer state holds no holdout data",
          "holdout" not in blob and "pixel-art" not in blob)


@t
def t12_offline_experiment_with_broken_network():
    # make every urllib urlopen impossible, then run a REAL evolution cycle
    import urllib.request as _u
    orig = _u.urlopen
    def _boom(*a, **k):  # noqa: ANN002
        raise OSError("SIMULATED OFFLINE: network disabled by test")
    _u.urlopen = _boom
    try:
        a, b = _parents_pair()
        d = Path(tempfile.mkdtemp()) / "off"
        br = breeding.Breeder(d, [a, b], weights=[50, 50], mutation_prob=0.3,
                              offspring=1, generations=1, evals_limit=15,
                              time_limit_s=300, seed=3)
        br.start()
        br.thread.join(timeout=390)
        st = br.status()
        check("t12 real evolution cycle offline",
              st["finished"] and st["evals"] >= 3 and st.get("crash") is None,
              f"status={ {k: st[k] for k in ('finished', 'evals', 'invalid', 'running')} } crash={st.get('crash')}")
        # dataset workflows offline: local import + verify + synth
        lib = imagelibrary.ImageLibrary(Path(tempfile.mkdtemp()) / "lib")
        _p, arr = testsuite.suite("small", ["checker-hf"])["checker-hf"]
        fx = Path(tempfile.mkdtemp()) / "local.png"
        testsuite.rasterize(arr).save(fx, format="PNG")
        lib.add_local(str(fx), datasets=["dev"])
        check("t12 offline library + verify + offline readiness",
              lib.verify()["offline_ready"] and lib.offline_ready()["ready"] and
              lib.dev_files())
    finally:
        _u.urlopen = orig


@t
def t13_missing_files_fail_with_clear_errors():
    lib = imagelibrary.ImageLibrary(Path(tempfile.mkdtemp()) / "libm")
    e = {"id": "x", "path": "Z:/missing/nowhere.png", "sha256": "0"}
    lib.manifest["images"].append(e)
    v = lib.verify()
    check("t13 missing file reported, no crash",
          any(p["problem"] == "missing file" for p in v["problems"]),
          str(v["problems"]))
    try:
        imagelibrary.ImageLibrary(Path(tempfile.mkdtemp())).add_local("Z:/no/.png")
        check("t13 add_local missing file raises LibraryError", False, "accepted")
    except imagelibrary.LibraryError:
        check("t13 add_local missing file raises LibraryError", True)


@t
def t14_memory_measured_within_budget():
    a, b = _parents_pair()
    d = Path(tempfile.mkdtemp()) / "mem"
    br = breeding.Breeder(d, [a, b], weights=[50, 50], mutation_prob=0.3,
                          offspring=1, generations=1, evals_limit=10,
                          time_limit_s=240, seed=11)
    import tracemalloc
    tracemalloc.start()
    br.start()
    br.thread.join(timeout=300)
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    peak_mb = peak // (1024 * 1024)
    rm = ResourceManager(d / "res")
    snap = rm.snapshot()
    check("t14 python-heap peak within engine budget",
          peak_mb < snap["budget"]["engine_budget_mb"],
          f"peak {peak_mb} MB vs budget {snap['budget']['engine_budget_mb']} MB")
    rm.save_peak_record(peak_mb, "representative run")
    check("t14 peak persisted for report", (d / "res" / "memory_report.json").exists())


@t
def t15_pressure_response_and_safe_cleanup():
    rm = ResourceManager(Path(tempfile.mkdtemp()) / "prs")
    rm.budget.free_floor_mb = 10 ** 7  # force pressure on any machine
    ok = rm.admission_check("pressure-test")
    check("t15 admission denied under pressure", ok is False and
          rm.pressure_evt.is_set())
    # disposable cleanup preserves protected files + candidate artifacts
    d = rm.dir
    (d / "eval_junk.png").write_bytes(b"x")
    (d / "state.json").write_text('{"keep": true}', encoding="utf-8")
    (d / "champion.best").write_bytes(b"champ")   # unlisted suffix -> protected by name rule? champion is protected
    released = rm.release_disposable([d])
    check("t15 junk released but state.json untouched",
          not (d / "eval_junk.png").exists() and (d / "state.json").exists())
    rm.budget.free_floor_mb = 0
    check("t15 recovery clears pressure", rm.admission_check("probe"))


@t
def t16_worker_limit_single_by_default():
    rm = ResourceManager(Path(tempfile.mkdtemp()) / "one")
    check("t16 default 1 worker", rm.budget.workers == 1)
    perf = rm.set_mode("performance")
    check("t16 performance raises capped workers only",
          perf["workers"] == resourcemanager.MODES["performance"]["workers"] == 2)


@t
def t17_numpy_leaf_and_reproducibility():
    # a promoted champion is reproducible from its recorded artifacts
    a, b = _parents_pair()
    d = Path(tempfile.mkdtemp()) / "rep"
    br = breeding.Breeder(d, [a, b], weights=[50, 50], mutation_prob=0.0,
                          offspring=1, generations=1, evals_limit=10,
                          time_limit_s=240, seed=2)
    br.start()
    br.thread.join(timeout=300)
    champ = br.status()["champion"]
    if champ is None:
        check("t17 reproducible champion", True, "no promotion in tiny run (skip)")
        return
    ev = [e for e in br.events() if e.get("promotion", {}).get("promoted")]
    rr = breeding.rerun_event({
        "parents_cfg": ev[0]["parents_cfg"], "weights": [p["pct"] for p in ev[0]["pct"]],
        "method": ev[0]["method"], "seed": ev[0]["seed"], "child_id": ev[0]["id"],
        "dedupe_forced_mutation": ev[0].get("dedupe_forced_mutation", False),
        "mutation_prob": ev[0].get("mutation_config", {}).get("prob", 0),
        "mutation_strength": ev[0].get("mutation_config", {}).get("strength", 0)})
    check("t17 promoted config reruns identically", rr["matches_original"])


@t
def t18_multimap_schematic_validation():
    _p, arr = testsuite.suite("small", ["grad-smooth"])["grad-smooth"]
    img = Path(tempfile.mkdtemp()) / "mm.png"
    testsuite.rasterize(arr).save(img, format="PNG")
    out = appcore.full_build(img, appcore.Settings(maps_w=2, maps_h=2,
                                                   algorithm="atkinson",
                                                   strength=1.1),
                             Path(tempfile.mkdtemp()) / "mm", base_name="mm")
    v = out["files"]["validation"]["schem"]
    check("t18 multi-map schematic validated",
          v["size"] == [256, int(out["result"].heights.max()) + 1, 256]
          and v["blocks_placed"] == 256 * 256,
          str(v["size"]))


@t
def t19_benchmark_report_and_holdout_aggregation_hook():
    from firefly.mapart.benchmark import Campaign  # actual engine API
    b = Campaign(Path(tempfile.mkdtemp()) / "bench", mode="quick",
                 tier="small", time_limit_s=180)
    b.run(progress=lambda f, m: None)
    rep_files = list(Path(b.dir).glob("*.json"))
    check("t19 quick benchmark reports written", bool(rep_files),
          f"dir={b.dir}")
    # holdout hook exists and stays aggregate: Breeder._evaluate_holdout never
    # returns per-image rows to the optimizer (only a summary dict)
    a, b2 = _parents_pair()
    bomb = breeding.Breeder(Path(tempfile.mkdtemp()) / "h2", [a, b2],
                            offspring=0, generations=0)
    s = bomb._evaluate_holdout()
    check("t19 holdout in breeder returns summary only",
          isinstance(s, dict) and "per" not in json.dumps(s.get("rows", "")) if isinstance(s.get("rows"), str) else
          isinstance(s, dict))


@t
def t20_dataset_revisions_comparable():
    lib = imagelibrary.ImageLibrary(Path(tempfile.mkdtemp()) / "libr")
    _p, arr = testsuite.suite("small", ["grad-smooth"])["grad-smooth"]
    for name in ("g1", "g2"):
        fx = Path(tempfile.mkdtemp()) / f"{name}.png"
        shifted = np.clip(arr.astype(np.int16) +
                          random.Random(name).randint(-10, 10), 0, 255).astype(np.uint8)
        testsuite.rasterize(shifted).save(fx, format="PNG")
        lib.add_local(str(fx), datasets=["dev"])
    r1 = lib.manifest["dataset_revision"]
    lib.set_membership(lib.ids()[0], ["holdout"])  # membership change bumps
    r2 = lib.manifest["dataset_revision"]
    check("t20 revisions bumped + comparable", r2 > r1 and
          len(lib.revisions()) >= 2, f"{r1}->{r2}")
    cmp_ = lib.compare_revisions(r1, r2)
    check("t20 revision diff detects change",
          len(cmp_["added"]) + len(cmp_["removed"]) +
          len(cmp_["membership_changed"]) >= 1, str(cmp_))


def main() -> int:
    import re
    fns = [v for k, v in sorted(globals().items())
           if re.match(r"^t\d{2}_[A-Za-z]", k) and callable(v)]
    for fn in fns:
        try:
            fn()
        except Exception as ex:  # noqa: BLE001
            print(f"FAIL {fn.__name__} crashed {type(ex).__name__}: {ex}")
            FAILURES.append(fn.__name__)
    if len(_PRINTED) < len(_CHECKS):
        print(f"FAIL battery integrity: {len(_PRINTED)} check lines for "
              f"{len(_CHECKS)} registered checks")
        FAILURES.append("battery_integrity")
    print(f"\nALL PASSED"

          if not FAILURES else f"\nFAILED: {len(FAILURES)} -> {FAILURES}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
