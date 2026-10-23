"""Spec §7 acceptance test: the evolution engine must run with NO network.

Executes the full scenario under a HARD network guard:
- socket.connect / getaddrinfo raise on any non-loopback destination and the
  attempt is RECORDED (never silently swallowed),
- ImageLibrary.download + discover_wikimedia are instrumented: a call means a
  component wanted images and must fail loudly, not quietly serve stale data.

Scenario (spec §7):
 1. hold-out-protected dataset offline preflight (verify + offline readiness)
 2. generate a synthetic benchmark image
 3. screening experiment (evolution lab) on fixtures + REAL local dev images
 4. development experiment (separate lab run)
 5. breed + evaluate candidates (breeding studio)
 6. generate + validate the Minecraft schematic from the lab's best candidate
    applied to a real downloaded dev image
 7. restart-and-resume experiment (kill mid-run, new controller, resume)
 8. assert zero network attempts and no downloader calls

Prints a JSON report; exit 0 only if ALL steps pass and the network log is
empty.
"""
from __future__ import annotations

import json
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DATA_DIR = ROOT / "mapart-data"
WORK = ROOT / "acceptance_run"

# --------------------------------------------------------------- network guard


class OfflineBlocked(Exception):
    pass


NET_ATTEMPTS: list = []


def _loopback(address) -> bool:
    if not isinstance(address, tuple) or not address:
        return True  # AF_UNIX etc.
    s = str(address[0]).lower()
    return s in ("localhost",) or s.startswith("127.") or s == "::1"


_orig_connect = socket.socket.connect
_orig_getaddrinfo = socket.getaddrinfo


def _guard_connect(self, address):  # type: ignore[no-untyped-def]
    if not _loopback(address):
        NET_ATTEMPTS.append({"what": "connect", "to": str(address)})
        raise OfflineBlocked(f"network connect blocked offline: {address}")
    return _orig_connect(self, address)


def _guard_getaddrinfo(*a, **k):  # type: ignore[no-untyped-def]
    host = str(a[0]).lower() if a else ""
    if host and not (host in ("localhost",) or host.startswith("127.")
                     or host == "::1"):
        NET_ATTEMPTS.append({"what": "getaddrinfo", "to": host})
        raise OfflineBlocked(f"DNS resolution blocked offline: {host}")
    return _orig_getaddrinfo(*a, **k)


DOWNLOADER_CALLS: list = []


def main() -> int:
    socket.socket.connect = _guard_connect          # type: ignore[method-assign]
    socket.getaddrinfo = _guard_getaddrinfo          # type: ignore[assignment]

    report: dict = {"steps": []}

    def step(name: str, ok: bool, detail) -> bool:
        report["steps"].append({"step": name, "ok": bool(ok), "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
        return ok

    t_all = time.time()

    # ---------------- 0. instrument the downloader ----------------
    from firefly.mapart import imagelibrary as il

    def _no_discover(*a, **k):  # type: ignore[no-untyped-def]
        DOWNLOADER_CALLS.append("discover_wikimedia")
        raise OfflineBlocked("offline run must not search remote sources")

    def _no_download(*a, **k):  # type: ignore[no-untyped-def]
        DOWNLOADER_CALLS.append("ImageLibrary.download")
        raise OfflineBlocked("offline run must not download images")

    il.discover_wikimedia = _no_discover
    il.ImageLibrary.download = _no_download

    # ---------------- 1. offline preflight ----------------
    lib = il.ImageLibrary(DATA_DIR)
    v = lib.verify()
    o = lib.offline_ready()
    images = lib.manifest["images"]
    from collections import Counter
    purposes = Counter(p for e in images for p in e.get("datasets", []))
    report["preflight"] = {
        "manifest_images": len(images),
        "verified": v["verified"],
        "offline_ready": v["offline_ready"],
        "dataset_revision": lib.manifest.get("dataset_revision", 0),
        "purpose_counts": dict(purposes),
        "library_disk_mb": sum(
            Path(lib.file_path(e) or "").stat().st_size for e in images
            if Path(lib.file_path(e) or "").is_file()) // (1024 * 1024),
    }
    step1 = step("preflight_local_dataset", v["offline_ready"] and not o["missing"],
                 f'{v["verified"]}/{v["total"]} verified, '
                 f'rev {report["preflight"]["dataset_revision"]}, '
                 f'{dict(purposes)}')

    # ---------------- 2. synthetic benchmark image ----------------
    from firefly.mapart import testsuite

    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    synth_ids = list(testsuite.GENERATORS)[:4]
    synth_dir = WORK / "synthetic"
    saved = testsuite.save_all("small", synth_dir)
    step_two = step("generate_synthetic_benchmark_locally",
                    len(saved) == len(testsuite.GENERATORS)
                    and all((synth_dir / f"small_{i}.png").is_file() for i in saved),
                    f"{len(saved)} deterministic synthetic images generated "
                    f"(ids: {', '.join(synth_ids[:4])}…)")

    # ---------------- 3+4. screening + development lab runs ----------------
    from firefly.mapart import labcore
    from firefly.mapart.labcore import LabController

    dev_ids = [e["id"] for e in images if "dev" in e.get("datasets", [])]
    stress_ids = [e["id"] for e in images if "stress" in e.get("datasets", [])][:2]
    screening_ids = (["lib:" + i for i in dev_ids[:2]] +
                     ["grad-smooth", "faux-photo"])
    dev_ids_suite = (["lib:" + i for i in dev_ids[2:5]] +
                     ["lib:" + i for i in stress_ids] +
                     ["pixel-art", "out-of-palette"])
    lab_dir = WORK / "lab"

    def run_lab(name: str, ids: list, evals: int) -> bool:
        m = LabController(lab_dir / name, evals_limit=evals, time_limit_s=150,
                          suite_ids=ids, library=lib, seed=11)
        m.start()
        for _ in range(800):
            time.sleep(0.25)
            if m.status()["finished"]:
                break
        st = m.status()
        rows = m.history()
        ok = (st["finished"] and st["evaluated"] >= evals
              and not m.state.get("crash")
              and all(r.get("score") is not None for r in rows))
        return step(name, ok,
                    f'evals {st["evaluated"]}, best '
                    f'{(st.get("best") or {}).get("score")}, '
                    f'suites {ids[:4]}…, crash={m.state.get("crash")}')

    step3 = run_lab("screening", screening_ids, 5)
    step4 = run_lab("development", dev_ids_suite, 5)

    # ---------------- 5. breeding + evaluation ----------------
    from firefly.mapart.breeding import Breeder

    parents = [{"algorithm": "floyd_steinberg", "version": "survival-wool-1.8",
                "strength": 1.0, "suppress": ()},
               {"algorithm": "bayer4", "version": "survival-wool-1.8",
                "strength": 0.8, "suppress": ()}]
    br = Breeder(WORK / "breeder", parents, method="weighted_params",
                 mutation_prob=0.5, offspring=2, generations=2,
                 population_size=6, elite_size=3, holdout_every=1,
                 evals_limit=24, seed=5)
    br.start()
    for _ in range(1200):
        time.sleep(0.25)
        if br.status()["finished"]:
            break
    stb = br.status()
    holdout_rows = 0
    hp = WORK / "breeder" / "holdout.jsonl"
    if hp.exists():
        holdout_rows = len([l for l in hp.read_text(encoding="utf-8").splitlines()
                            if l.strip()])
    champ = stb.get("champion") or {}
    step5 = step("breed_and_evaluate_candidates",
                 stb["finished"] and stb["evals"] > 0
                 and (br.state.get("crash") is None)
                 and "score" in champ,
                 f'evals {stb["evals"]}, generations {stb["generation"]}, '
                 f'champion score {champ.get("score")}, '
                 f'holdout rows {holdout_rows} (aggregate-only), '
                 f'crash={br.state.get("crash")}')

    # ---------------- 6. schematic generate + validate ----------------
    from firefly.mapart import appcore, schem

    best = (labcore.LabController(lab_dir / "screening", suite_ids=[],
                                  library=lib).state.get("best") or {})
    cand = best.get("candidate") or parents[0]
    src = Path(lib.file_path(lib.entry(dev_ids[0])) or "")
    settings = appcore.Settings(version=cand.get("version", "survival-wool-1.8"),
                                maps_w=1, maps_h=1,
                                algorithm=cand["algorithm"],
                                strength=cand.get("strength", 1.0),
                                suppress=tuple(cand.get("suppress", ())))
    res = appcore.process(src, settings)
    out = appcore.export(res, str(WORK / "schem"), "acceptance")
    validation = schem.validate_schem(
        Path(out["schem"]), expect={"size": (128, int(res.heights.max()) + 1, 128)})
    step6 = step("generate_and_validate_schematic",
                 Path(out["schem"]).is_file() and bool(validation.get("ok", validation)),
                 f'.schem {Path(out["schem"]).name} validated '
                 f'(size {validation.get("size")}) from real dataset image '
                 f'{src.name}')

    # ---------------- 7. restart + resume ----------------
    resume_dir = WORK / "resume"
    r1 = LabController(resume_dir, evals_limit=8, time_limit_s=240,
                       suite_ids=["grad-smooth", "lib:" + dev_ids[1]],
                       library=lib, seed=7)
    r1.start()
    deadline = time.time() + 90
    while time.time() < deadline and r1.status()["evaluated"] < 3 \
            and not r1.status()["finished"]:
        time.sleep(0.2)
    before = r1.status()["evaluated"]
    r1.stop()                       # hard stop mid-run -> checkpoint
    del r1                          # "close the app"
    # fresh controller = restarted engine; read the checkpoint IT loads
    r2 = LabController(resume_dir, evals_limit=8, time_limit_s=240,
                       suite_ids=["grad-smooth", "lib:" + dev_ids[1]],
                       library=lib, seed=7)   # fresh controller = restart
    resumed_evals = r2.state["evaluated"]
    r2.start()
    for _ in range(1200):
        time.sleep(0.25)
        if r2.status()["finished"]:
            break
    st2 = r2.status()
    # the live counter sampled before stop lags the checkpoint (thread may
    # have finished one more eval after the sample) — the invariant is:
    # the fresh controller sees >= before, NEVER a reset
    step7 = step("restart_and_resume_experiment",
                 resumed_evals >= before and st2["evaluated"] > resumed_evals
                 and st2["finished"] and not r2.state.get("crash"),
                 f'live counter {before} at stop request; checkpoint resumed by '
                 f'fresh controller with {resumed_evals} evals (>= before: no '
                 f'reset) and finished at {st2["evaluated"]} '
                 f'crash={r2.state.get("crash")}')

    # ---------------- holdout isolation inside the guard ----------------
    hold_ids = [e["id"] for e in images if "holdout" in e.get("datasets", [])]
    refused = False
    if hold_ids:
        try:
            m_attempt = LabController(
                WORK / "holdout_refusal_probe", evals_limit=1,
                suite_ids=["lib:" + hold_ids[0]], library=lib, seed=1)
            m_attempt.start()
            for _ in range(40):
                time.sleep(0.1)
                if m_attempt.status()["finished"]:
                    break
            refused = str(m_attempt.state.get("crash", "")).startswith(
                "PermissionError")
        except Exception as e:  # noqa: BLE001
            refused = type(e).__name__ == "PermissionError"
    step8 = step("holdout_never_trains_optimizer", refused,
                 f'lab refused holdout item {hold_ids[0] if hold_ids else "?"}'
                 f' (PermissionError recorded) — optimizer never trains on '
                 f'the final-evaluation set')

    # ---------------- 9. zero network ----------------
    total = round(time.time() - t_all, 1)
    report["network_attempts"] = NET_ATTEMPTS
    report["downloader_calls"] = DOWNLOADER_CALLS
    report["dataset_stats"] = report.pop("preflight")
    report["wall_time_s"] = total
    ok_all = (step1 and step_two and step3 and step4 and step5 and step6
              and step7 and step8 and not NET_ATTEMPTS and not DOWNLOADER_CALLS)
    step("no_network_required",
         not NET_ATTEMPTS and not DOWNLOADER_CALLS,
         f'{len(NET_ATTEMPTS)} network attempts, '
         f'{len(DOWNLOADER_CALLS)} downloader calls (offline under Wi-Fi '
         f'disconnect equivalent: hard socket guard)')

    receipts = WORK / "report.json"
    receipts.write_text(json.dumps(report, indent=1, sort_keys=True),
                        encoding="utf-8")
    print("\nREPORT", str(receipts), "exit", 0 if ok_all else 1)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
