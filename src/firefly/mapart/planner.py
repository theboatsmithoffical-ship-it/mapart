"""Autonomous experiment planner + innovation layer.

Works on REAL recorded artifacts only:
- lab history rows (score, candidate, process_time_s, errors)
- breeder lineage (crossover events, promotions, mutations)
- benchmark reports
- image dataset revisions/weaknesses (imagelibrary.gaps_report)

Distinctions enforced everywhere: observed result vs hypothesis vs conclusion.
The planner never claims causation from correlation: controlled comparisons
change exactly one factor (recorded as `factor`) and conclusion text states
the observed delta, not a mechanism it cannot verify.

Specialist populations: persisted per image-category; champions can migrate
candidates between populations via the same MixSpec crossover machinery.

Stopping: max_gens / max_evals / time / stall (consecutive generations
without meaningful improvement) / target threshold / user cancel; a stall
triggers a documented plateau report — never a claim of global optimum.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import numpy as np

from .breeding import MixSpec, MixError, crossover_weighted_params, mutate_child
from .labcore import random_candidate, normalize_config, canonical, hashlib_sha


class ExperimentLog:
    """Structured local experiment history (JSON lines, atomically rewritten)."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rows = []
        self._load()

    def _load(self):
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        self.rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

    def _flush(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, default=list) + "\n"
                               for r in self.rows), encoding="utf-8")
        tmp.replace(self.path)

    def append(self, row: dict):
        base = json.dumps(row, sort_keys=True, default=str) + str(time.time_ns())
        row.setdefault("id", hashlib.sha256(base.encode()).hexdigest()[:16])
        row["ts"] = time.time()
        self.rows.append(row)
        self._flush()
        return row["id"]

    def done_hypotheses(self) -> set:
        return {r.get("hypothesis") for r in self.rows
                if r.get("kind") == "experiment" and r.get("conclusion")}

    def by_kind(self, kind: str) -> list:
        return [r for r in self.rows if r.get("kind") == kind]


# --------------------------------------------------------------------------
# Failure classification + regression-test generation
# --------------------------------------------------------------------------

def classify_failure(row: dict) -> dict:
    """Classify one failure row from the lab/breeder lineage."""
    err = f"{row.get('error', '')} {row.get('detail', '')} {row.get('rejected', '')}"
    if "unknown algorithm" in err or "unregistered" in err:
        kind = "invalid-config"
        repair = "repair: candidate regressed to an unregistered algorithm; " \
                 "re-normalize via labcore.normalize_config and rerun"
    elif "time cap" in err:
        kind = "resource-time"
        repair = "repair: per-eval timeout; smaller tier image or faster algorithm"
    elif "schem" in err.lower() or "blocks_placed" in err:
        kind = "export-validation"
        repair = "repair: export produced non-matching schematic; inspect export path"
    elif "palette" in err:
        kind = "palette-mismatch"
        repair = "repair: palette version invalid for this setting; pin version"
    elif "KeyError" in err or "AttributeError" in err:
        kind = "engine-bug"
        repair = "repair: engine defect — record repro config as a regression test"
    else:
        kind = "unclassified"
        repair = "inspection required"
    return {"kind": kind, "repair": repair,
            "candidate": row.get("candidate"), "error": err.strip()[:300]}


def generate_regression_test(failures: list, out_dir: Path) -> dict:
    """Write a REAL regression test file that asserts the failure classes are
    classified as recorded (visible failure if behavior changes)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cls = [classify_failure(f) for f in failures]
    blob = {"generated": time.time(), "failures": [
        {"kind": c["kind"], "candidate": c["candidate"], "error": c["error"]}
        for c in cls]}
    path = out_dir / "regression_failures.json"
    path.write_text(json.dumps(blob, indent=1), encoding="utf-8")
    test_path = out_dir / "test_regression_failures.py"
    test_path.write_text(
        '"Auto-generated regression checks from classified failures.\\n"\n'
        '"Run: python tests/regressions/test_regression_failures.py\\n"\n'
        "from pathlib import Path\n"
        "import json, sys\n"
        "HERE = Path(__file__).parent\n"
        "sys.path.insert(0, str(HERE.parents[2] / 'src'))\n"
        "from firefly.mapart.planner import classify_failure\n\n\n"
        "def main():\n"
        "    blob = json.loads((HERE / 'regression_failures.json').read_text(encoding='utf-8'))\n"
        "    bad = []\n"
        "    for f in blob['failures']:\n"
        "        got = classify_failure(f)['kind']\n"
        "        if got != f['kind']:\n"
        "            bad.append((f['kind'], got, f['error'][:80]))\n"
        "    print('REGRESSION FAILURES:', bad if bad else 'none')\n"
        "    return 1 if bad else 0\n\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n", encoding="utf-8")
    return {"rows": len(cls), "paths": [str(path), str(test_path)],
            "classes": sorted({c["kind"] for c in cls})}


# --------------------------------------------------------------------------
# weakness analysis + proposals
# --------------------------------------------------------------------------

def weaknesses(history_rows: list) -> dict:
    """Mean score per suite family from REAL recorded rows."""
    by_suite = {}
    for r in history_rows:
        sid = r.get("suite_id") or r.get("item") or r.get("suite")
        if sid and r.get("score") is not None:
            by_suite.setdefault(sid, []).append(float(r["score"]))
    stats = {k: {"mean": round(float(np.mean(v)), 4), "n": len(v),
                 "std": round(float(np.std(v)), 4)}
             for k, v in by_suite.items()}
    worst = sorted(stats.items(), key=lambda kv: (kv[1]["mean"], -kv[1]["std"]))
    return {"stats": stats, "weakest": [k for k, _ in worst[:4]]}


def estimate_cost_seconds(history_rows: list) -> float:
    times = [r.get("metrics", {}).get("process_time_s")
             for r in history_rows
             if isinstance(r.get("metrics"), dict)]
    times = [t for t in times if t]
    median = float(np.median(times)) if times else 1.2
    return max(0.2, round(median, 4))


class Planner:
    def __init__(self, data_dir, history_rows=None, library=None,
                 breeder_lineage=None):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = ExperimentLog(self.dir / "experiments.jsonl")
        self.history = history_rows or []
        self.library = library
        self.lineage = breeder_lineage or []
        self.pop_file = self.dir / "specialists.json"

    # ---------------- proposals ----------------
    def analyze(self) -> dict:
        wk = weaknesses(self.history)
        failures = [r for r in self.history
                    if r.get("score") is None or r.get("rejected")]
        classes = {}
        for f in failures:
            c = classify_failure(f)
            classes[c["kind"]] = classes.get(c["kind"], 0) + 1
        gaps = (self.library.gaps_report(self.history)
                if self.library else {"weak_categories": ["library not attached"],
                                      "library_tags_covered": []})
        return {"weaknesses": wk, "failure_classes": classes, "gaps": gaps}

    def propose(self, n: int = 3) -> list:
        """One-factor-at-a-time proposals ranked by priority."""
        analysis = self.analyze()
        done = self.log.done_hypotheses()
        champ = self._current_champion()
        cost = estimate_cost_seconds(self.history)
        weak = analysis["weaknesses"]["weakest"] or ["grad-smooth"]
        proposals = []
        base = dict(champ["candidate"]) if champ else \
            random_candidate(__import__("random").Random(0))
        # factor 1: strength ±0.15 (only ONE factor changed)
        for delta in (+0.15, -0.15):
            new_s = min(2.0, max(0.0, float(base.get("strength", 1.0)) + delta))
            cand = normalize_config({**base, "strength": new_s})
            hyp = f"changing strength {base.get('strength')} -> {new_s} (only factor) improves the weakest suites {weak}"
            proposals.append({
                "kind": "experiment", "factor": "strength",
                "hypothesis": hyp, "candidate": cand,
                "target_suites": weak, "estimated_cost_s": cost,
                "expected_improvement": 0.5 * abs(delta),
                "uncertainty": 0.4, "dataset_revision": (self.library.manifest.get(
                    "dataset_revision", 0) if self.library else None)})
        # factor 2: algorithm swap candidate (if champion exists)
        if champ:
            from .registry import get as _g
            algos = [a for a in ("burkes", "sierra_lite", "jjn", "atkinson",
                                 "floyd_steinberg") if a != base.get("algorithm")]
            alt = normalize_config({k: v for k, v in base.items()
                                    if k not in ("bias", "serpentine")
                                    } | {"algorithm": algos[0]})
            hyp = f"swapping algorithm {base.get('algorithm')} -> {algos[0]} (only factor) improves {weak}"
            proposals.append({"kind": "experiment", "factor": "algorithm",
                              "hypothesis": hyp, "candidate": alt,
                              "target_suites": weak, "estimated_cost_s": cost,
                              "expected_improvement": 0.8, "uncertainty": 0.6,
                              "dataset_revision": (self.library.manifest.get(
                                  "dataset_revision", 0) if self.library else None)})
        # factor 3: mutation probe of a promissing breeder child (existing
        # component reuse): mutate champion with fixed seed
        if champ:
            rng = __import__("random").Random(1234)
            mut, mlog = mutate_child(base, rng, 0.8, 0.6)
            hyp = (f"seeded mutation ({len(mlog)} keys changed) explores config "
                   f"space near the champion")
            proposals.append({"kind": "experiment", "factor": "mutation",
                              "hypothesis": hyp, "candidate": mut,
                              "target_suites": weak, "estimated_cost_s": cost,
                              "expected_improvement": 0.3, "uncertainty": 0.7,
                              "dataset_revision": (self.library.manifest.get(
                                  "dataset_revision", 0) if self.library else None)})
        seen, out = set(), []
        for p in sorted(proposals,
                        key=lambda x: -x["expected_improvement"] *
                        (0.5 + x["uncertainty"]) / max(0.2, x["estimated_cost_s"])):
            if p["hypothesis"] in done or p["hypothesis"] in seen:
                continue  # uninformative repeats are dropped, not re-run
            seen.add(p["hypothesis"])
            out.append(p)
            if len(out) >= n:
                break
        return out

    def _current_champion(self):
        best = None
        for r in self.history:
            s = r.get("score")
            if s is None:
                continue
            if best is None or s > best.get("score", -1):
                best = r
        if best:
            return {"candidate": best["candidate"], "score": best["score"]}
        return None

    def record_result(self, proposal: dict, result: dict) -> str:
        """Record hypothesis -> settings -> result -> measurable conclusion."""
        before = result.get("before")
        after = result.get("after")
        if before is None or after is None:
            conclusion = "unverifiable: missing measured score pair"
        else:
            conc = f"observed {after:.4f} vs {before:.4f} (" + \
                   ("better" if after > before else
                    "worse" if after < before else "no change") + ")"
            conclusion = conc + "; single-factor change only — causation not claimed"
        row = {"kind": "experiment",
               "hypothesis": proposal["hypothesis"],
               "factor": proposal.get("factor"),
               "settings": proposal.get("candidate"),
               "target_suites": proposal.get("target_suites"),
               "dataset_revision": proposal.get("dataset_revision"),
               "estimated_cost_s": proposal.get("estimated_cost_s"),
               "result": result, "conclusion": conclusion,
               "promotion": result.get("promoted", False)}
        return self.log.append(row)

    # ---------------- specialist populations ----------------
    SPECIALTIES = {
        "photographs": ["faux-photo", "noisy-subtle"],
        "pixel-art": ["pixel-art", "checker-hf"],
        "gradients": ["grad-smooth", "grad-abrupt"],
        "portraits-animals": ["color-chart", "faux-photo"],
        "landscape-architecture": ["landscape-flat", "high-contrast"],
        "high-detail": ["fine-details", "similar-colors"],
    }

    def specialists(self) -> dict:
        if self.pop_file.exists():
            try:
                return json.loads(self.pop_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {"versioned": 1, "updated": time.time(),
                "populations": {k: {"champion": None, "rows": [], "suite_ids": v}
                                for k, v in self.SPECIALTIES.items()},
                "generalist": {"champion": None, "rows": []}}

    def save_specialists(self, spec: dict):
        spec["updated"] = time.time()
        tmp = self.pop_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(spec, indent=1), encoding="utf-8")
        tmp.replace(self.pop_file)

    def promote_specialist(self, population: str, candidate: dict, score: float,
                           suite_scores: dict):
        spec = self.specialists()
        pop = spec["populations"].get(population, spec["populations"]["photographs"])
        if pop["champion"] is None or score > pop["champion"].get("score", -1):
            pop["champion"] = {"candidate": candidate, "score": score,
                               "suite_scores": suite_scores}
            self.save_specialists(spec)
            self.log.append({"kind": "promotion", "population": population,
                             "score": score, "candidate": candidate})
            return {"promoted": True, "population": population, "score": score}
        return {"promoted": False, "reason": "specialist champion retains its "
                                             "higher measured score"}

    def migrate_candidate(self, src_pop: str, dst_pop: str, cand: dict) -> dict:
        """Component exchange between specialists via real crossover machinery."""
        spec = self.specialists()
        src = (spec["populations"].get(src_pop) or {}).get("champion")
        dst = (spec["populations"].get(dst_pop) or {}).get("champion")
        if not src:
            return {"migrated": False, "reason": f"no champion in {src_pop}"}
        parents = [src["candidate"]] + ([dst["candidate"]] if dst else [])
        pct = [60, 40] if dst else [100]
        try:
            ms = MixSpec(parents, pct, normalize=True)
            rng = __import__("random").Random(hashlib_sha(src_pop + dst_pop) % (2**32))
            child, info = crossover_weighted_params(ms, rng)
        except MixError as e:
            return {"migrated": False, "reason": str(e)}
        self.log.append({"kind": "migration", "from": src_pop, "to": dst_pop,
                         "child_id": child.get("algorithm"), "child": child,
                         "pct": ms.effective})
        return {"migrated": True, "child": child, "pct": ms.effective}

    # ---------------- stopping / stall ----------------
    def check_stopping(self, state: dict, max_gens=None, max_evals=None,
                       time_limit_s=None, target_score=None,
                       stall_window=5, min_gain=0.001) -> dict:
        """state: {'generation','evals','elapsed_s','scores': [gen_mean,...]}"""
        scores = state.get("scores") or []
        reasons = []
        if max_gens is not None and state.get("generation", 0) >= max_gens:
            reasons.append("max generations reached")
        if max_evals is not None and state.get("evals", 0) >= max_evals:
            reasons.append("max experiment count reached")
        if time_limit_s is not None and state.get("elapsed_s", 0) >= time_limit_s:
            reasons.append("time budget reached")
        if target_score is not None and scores and max(scores[-1:]) >= target_score:
            reasons.append(f"target score {target_score} reached")
        stalled = (len(scores) >= stall_window and
                   max(scores[-stall_window:]) - max(scores[-(stall_window * 2):]) \
                   < min_gain) if len(scores) >= max(4, stall_window + 1) else False
        if stalled:
            reasons.append(f"stalled: < {min_gain} gain over the last {stall_window} runs")
        if reasons:
            self.log.append({"kind": "stop",
                             "reasons": reasons,
                             "note": "stall implies a measured plateau, "
                                     "NOT a proven global optimum"})
        return {"stop": bool(reasons), "reasons": reasons,
                "stalled": stalled}
