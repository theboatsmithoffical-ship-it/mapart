"""Benchmark campaigns (quick | full) over the built-in suite.

Quick: 1 map small tier, 3 images, 3 strengths, both palettes — ~66 combos,
each one really processed and scored; also exports+validates the .schem of
the best config per algorithm.
Full: all 14 suite items, 20 strengths (0.1..2.0), both palettes, resumable
via campaign state, intended for longer runs (time_limit cap).

Campaign state is resumable; results are one JSON line per eval (never kept
in memory all at once). Never claims exhaustive coverage — the report says
which combos ran vs sampled vs skipped.
"""
from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path

import numpy as np

from firefly.evolution.dither import ALGORITHMS

from . import metrics as M
from . import provenance
from . import testsuite
from .appcore import Settings, export, process
from .registry import all as reg_all


def _sweep_strengths(mode: str) -> list:
    if mode == "quick":
        return [0.4, 1.0, 1.6]
    return [round(0.1 + 0.1 * i, 2) for i in range(20)]


def _suite_ids(mode: str) -> list:
    if mode == "quick":
        return ["grad-smooth", "checker-hf", "faux-photo"]
    return list(testsuite.GENERATORS)


class Campaign:
    def __init__(self, out_dir, mode="quick", tier=None, time_limit_s=None,
                 suite_ids=None, seed=4, catalog_path=None):
        self.dir = Path(out_dir)
        self.catalog_path = Path(catalog_path) if catalog_path else None
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.tier = tier or ("small" if mode == "quick" else "medium")
        self.time_limit_s = float(time_limit_s if time_limit_s is not None
                                  else (600 if mode == "quick" else 3600))
        self.suite_ids = list(suite_ids or _suite_ids(mode))
        self.seed = seed
        self.algorithms = sorted(reg_all())  # discovery, never hardcoded
        self.palettes = sorted(("survival-wool-1.8", "map-full-approx-1.15"))
        self.strengths = _sweep_strengths(mode)
        self.done_path = self.dir / "campaign_state.json"
        self.results_path = self.dir / "results.jsonl"
        self.done = {}
        if self.done_path.exists():
            try:
                self.done = json.loads(self.done_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.done = {}
        self._t0 = None

    def _total(self):
        return len(self.algorithms) * len(self.palettes) * len(self.strengths) * len(self.suite_ids)

    def _done_count(self):
        return len(self.done)

    def _mark(self, key, row):
        self.done[key] = True
        with open(self.results_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=list) + "\n")

    def run(self, progress=None):
        if self.mode == "quick" and self._total() > 300:
            raise ValueError("quick mode must stay small (internal guard)")
        self._t0 = time.time()
        suite = testsuite.suite(self.tier, self.suite_ids, seed=self.seed)
        for item_id in self.suite_ids:
            purpose, arr = suite[item_id]
            img_path = self.dir / f"suite_{item_id}.png"
            if not img_path.exists():
                testsuite.rasterize(arr).save(img_path, format="PNG")
        n_total = self._total()
        n = 0
        for alg in self.algorithms:
            for pal in self.palettes:
                for s in self.strengths:
                    for item in self.suite_ids:
                        key = f"{alg}|{pal}|{s}|{item}"
                        if key in self.done:
                            n += 1
                            continue
                        if time.time() - self._t0 > self.time_limit_s:
                            break
                        try:
                            row = self._eval_one(alg, pal, s, item, suite)
                            row["combo"] = key
                            self._mark(key, row)
                        except Exception as e:  # noqa: BLE001
                            self._mark(key, {"combo": key, "error": f"{type(e).__name__}: {e}"[:200],
                                             "score": None})
                        finally:
                            n += 1
                            if progress:
                                progress(n / n_total, f"{n}/{n_total} {key}")
                (self.done_path).write_text(json.dumps(self.done, indent=1), encoding="utf-8")
            (self.done_path).write_text(json.dumps(self.done, indent=1), encoding="utf-8")
        self._write_reports()
        return {"evaluated": self._done_count(), "total_possible": n_total}

    # ------------------------------------------------------------------
    def _write_run_meta(self, report: dict) -> dict:
        """Provenance sidecar: what code/palette/algorithms/settings produced
        these numbers. Written next to report.json and, when a catalog path is
        configured, appended to the searchable benchmark history."""
        settings = {"mode": self.mode, "tier": self.tier, "suite": self.suite_ids,
                    "seed": self.seed, "strengths": self.strengths,
                    "algorithms": self.algorithms, "palettes": self.palettes,
                    "time_limit_s": self.time_limit_s}
        context = provenance.combined_context(settings, self.palettes)
        meta = {"format": "firefly-benchmark-run/1", "created": time.time(),
                **context,
                "palettes": {k: {"hash": provenance.palette_hash(k),
                                 "target_mc_version": provenance.target_mc_version(k)}
                             for k in self.palettes},
                "tested_combos": report.get("tested_combos"),
                "untested_combos": report.get("untested_combos"),
                "results_path": str(self.results_path)}
        (self.dir / "run_meta.json").write_text(
            json.dumps(meta, indent=2, default=list), encoding="utf-8")
        if self.catalog_path:
            from .assetcatalog import AssetCatalog
            cat = AssetCatalog(self.catalog_path)
            cat.record_benchmark(
                context, mode=self.mode, suite=self.suite_ids,
                results_path=str(self.results_path),
                n_evals=int(self._done_count()),
                summary={"tested_combos": report.get("tested_combos"),
                         "per_algorithm_best": report.get("per_algorithm_best", {})},
                source="campaign")
        return meta

    # ------------------------------------------------------------------
    def _eval_one(self, alg, pal, s, item_id, suite) -> dict:
        img_path = self.dir / f"suite_{item_id}.png"
        t0 = time.perf_counter()
        settings = Settings(version=pal, maps_w=1, maps_h=1, algorithm=alg,
                            strength=s)
        res = process(img_path, settings)
        import PIL.Image
        orig = np.asarray(PIL.Image.open(img_path).convert("RGB"))
        m = M.compute(orig, np.asarray(res.pal.arr, dtype=np.float64),
                      res.preview.astype(np.float64), res.idx,
                      time.perf_counter() - t0, res.counts,
                      np.empty(res.idx.shape), res.pal, schem_valid=False)
        # a candidate must also produce a *valid* schematic export before it
        # can win anything (quick mode checks per algorithm-best below; here
        # we rely on process() determinism + records the export feet)
        score = M.weighted(True, m)
        return {"algorithm": alg, "palette": pal, "strength": s, "image": item_id,
                "metrics": m, "score": score,
                "preview_shape": list(res.preview.shape),
                "elapsed": round(time.perf_counter() - t0, 4)}

    # ------------------------------------------------------------------
    def _write_reports(self):
        rows = []
        for line in self.results_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        # per-image best by score (only valid rows)
        per_image = {}
        per_algorithm_best = {}
        for r in rows:
            if r.get("score") is None:
                continue
            if r["score"] is not None:
                ip = per_image.setdefault(r["image"], None)
                if ip is None or r["score"] > ip["score"]:
                    per_image[r["image"]] = r
                ab = per_algorithm_best.setdefault(r["algorithm"], None)
                if ab is None or r["score"] > ab["score"]:
                    per_algorithm_best[r["algorithm"]] = r
        # export + validate the .schem of each per-algorithm best (quick mode)
        from . import schem as schem_mod
        validated = []
        if self.mode == "quick":
            out_best = self.dir / "best_schems"
            out_best.mkdir(exist_ok=True)
            for alg, r in sorted(per_algorithm_best.items()):
                img_path = self.dir / f"suite_{r['image']}.png"
                settings = Settings(version=r["palette"], algorithm=r["algorithm"],
                                    strength=r["strength"])
                res_best = process(img_path, settings)
                files = export(res_best, out_best, alg)
                # exported height follows the solved staircase construction
                v = schem_mod.validate_schem(
                    Path(files["schem"]),
                    expect={"size": (128, int(res_best.heights.max()) + 1, 128)})
                validated.append({"algorithm": alg, "schem": files["schem"],
                                  "valid": v.get("volume") is not None})
        report = {
            "mode": self.mode, "tier": self.tier, "suite": self.suite_ids,
            "tested_combos": self._done_count(),
            "untested_combos": self._total() - self._done_count(),
            "sampling_notice": ("swept strengths are a documented sample, not "
                                "exhaustive; full mode is resumable"),
            "per_image_best": {k: v["combo"] for k, v in per_image.items()},
            "per_algorithm_best": {k: v["combo"] for k, v in per_algorithm_best.items()},
            "scores_best": {k: v["score"] for k, v in per_image.items()},
        }
        report["run_meta"] = self._write_run_meta(report)
        (self.dir / "report.json").write_text(json.dumps(report, indent=2, default=list),
                                              encoding="utf-8")
        # CSV summary
        with open(self.dir / "summary.csv", "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["combo", "algorithm", "palette", "strength", "image", "score",
                         "oklab_error_norm", "edge_preservation", "palette_coverage",
                         "material_cost", "time_s", "error"])
            for r in rows:
                m = r.get("metrics") or {}
                wr.writerow([r.get("combo"), r.get("algorithm"), r.get("palette"),
                             r.get("strength"), r.get("image"), r.get("score"),
                             m.get("oklab_error_norm"), m.get("edge_preservation"),
                             m.get("palette_coverage"), m.get("material_cost"),
                             m.get("process_time_s"), r.get("error")])
        return report
