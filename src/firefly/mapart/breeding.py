"""Genetic breeding system for map-art candidates (feature §12).

WEIGHT SEMANTICS — defined per crossover method. A parent's "50%" never
means "guaranteed 50% of the offspring's quality":

  weighted_params : numeric genes (strength, bias) are combined as linear
                    mixes Σ p_i · gene_i over parents that actually HAVE the
                    gene; a parent lacking the gene donates none of its
                    numeric weight and remaining weights are renormalized
                    (recorded in the offspring lineage as
                    weight_redistribution). Categorical genes (algorithm,
                    palette version, serpentine, suppress set) are inherited
                    by weighted-roulette draw using the percentages as
                    probabilities.
  categorical     : complete components (dithering = algorithm + its params;
                    palette = version + suppress set) inherited WHOLE from a
                    parent chosen by weighted roulette. Nothing is averaged.
  module          : like categorical, but palette and dither components each
                    do their OWN weighted draw, so a child can take the
                    palette from parent A and the dither kernel from parent
                    B. Incompatible components fall back to the nearest
                    valid ancestor and the adaptation is recorded.
  ensemble_vote   : EXPERIMENTAL. Parents keep their own configs; the child
                    is resolved per pixel by weighted voting among the
                    parents' rendered palette indices. Evaluation is real,
                    but ensemble children cannot be promoted into the stable
                    champion pool (downstream needs a single Settings), so
                    the promotion gate rejects them with that reason.
  code_crossover  : UNSUPPORTED in this repository — no source-code evolution
                    pipeline exists; attempting it raises NotImplementedError
                    instead of silently splicing text.

Percentages: weights must total 100 (validated); normalize=True renormalizes
any positive weights and the EFFECTIVE percentages are always reported on
the offspring record and in `status()`.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from pathlib import Path

import numpy as np

from firefly.evolution.dither import ALGORITHMS
from .mapart import DEFAULT_COMBO_FRONT_BLOCK as _COMBO_DEFAULT

from . import appcore, metrics as M, schem, testsuite
from .appcore import Settings, process
from .labcore import canonical, hashlib_sha, normalize_config
from .registry import get as _rget

NUMERIC_GENES = ("strength", "bias")
_SAME_PAL = None  # ensembles require a shared palette version (enforced in MixSpec)


# --------------------------------------------------------------------------
# MixSpec — percentage-based parent mixing
# --------------------------------------------------------------------------

class MixError(ValueError):
    pass


class MixSpec:
    """Parents + contribution percentages (validated, optional normalization)."""

    def __init__(self, parents, weights=None, normalize=False, method=None):
        parents = [dict(p) for p in (parents or [])]
        if len(parents) < 1:
            raise MixError("at least one parent is required")
        parents = [normalize_config(p) for p in parents]
        if weights is None:
            weights = [100.0 / len(parents)] * len(parents)
        weights = [float(w) for w in weights]
        if any(w < 0 for w in weights):
            raise MixError("percentages must be non-negative")
        if any(w == 0 for w in weights):
            raise MixError("zero-percentage parents contribute nothing; remove them")
        total_raw = sum(weights)
        if normalize:
            if total_raw <= 0:
                raise MixError("total percentage must be positive")
            weights = [w * 100.0 / total_raw for w in weights]
        else:
            if abs(total_raw - 100.0) > 0.01:
                raise MixError(
                    f"percentages must total 100 (got {total_raw:.4f}); "
                    "pass normalize=True to auto-normalize")
        if method == "ensemble_vote":
            vers = {p["version"] for p in parents}
            if len(vers) > 1:
                raise MixError(
                    "ensemble crossover requires a shared palette version "
                    f"(parents mix {sorted(vers)}); palette indices cannot be "
                    "voted across different palettes")
        self.parents = parents
        self.weights = [round(w, 4) for w in weights]
        self.normalized = bool(normalize)
        self.raw_total = round(total_raw, 4)

    @property
    def effective(self):
        """Final effective percentages (always displayed on records/UI)."""
        return [{"id": genome_id_of(p), "pct": w}
                for p, w in zip(self.parents, self.weights)]

    @property
    def total(self):
        return round(sum(self.weights), 4)


def genome_id_of(cand: dict) -> str:
    """Stable content hash of a candidate; tolerant of invalid parents so
    lineage lookups never crash on an incompatible component."""
    try:
        base = canonical(cand)
    except Exception:  # noqa: BLE001 — invalid/foreign candidate
        base = json.dumps(cand, sort_keys=True, default=str)
    return hashlib.sha256(base.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Crossover registry
# --------------------------------------------------------------------------

class CrossoverInfo:
    def __init__(self, code, kind, status, note):
        self.code, self.kind, self.status, self.note = code, kind, status, note


REGISTRY = {
    "weighted_params": CrossoverInfo(
        "weighted_params", "numeric", "working",
        "numeric genes weighted-mixed (renormalized when a parent lacks the "
        "gene); categorical genes inherited by weighted-roulette draw"),
    "categorical": CrossoverInfo(
        "categorical", "component", "working",
        "whole dither component and whole palette component inherited from "
        "weight-drawn parents; nothing averaged"),
    "module": CrossoverInfo(
        "module", "pipeline", "working",
        "independent weighted draw per component with compatibility "
        "fallback (recorded), e.g. palette from A + dither kernel from B"),
    "ensemble_vote": CrossoverInfo(
        "ensemble_vote", "ensemble", "experimental",
        "per-pixel weighted voting among parent renders; real evaluation "
        "but NOT promotable to the stable champion pool"),
    "code_crossover": CrossoverInfo(
        "code_crossover", "source", "unsupported",
        "no source-code evolution pipeline exists in this repository; "
        "attempting it raises NotImplementedError"),
}


def describe_methods() -> dict:
    return {code: {"kind": ci.kind, "status": ci.status, "note": ci.note}
            for code, ci in REGISTRY.items()}


def _weighted_draw(rng: random.Random, items, weights):
    """Weighted roulette; weights need not sum to 1."""
    total = sum(weights)
    roll = rng.random() * total
    acc = 0.0
    for item, w in zip(items, weights):
        acc += w
        if roll < acc:
            return item
    return items[-1]


def _components(parent: dict) -> dict:
    """Split a candidate into inheritance components."""
    return {
        "palette": {"version": parent["version"],
                    "suppress": tuple(parent.get("suppress", ()))},
        "dither": {k: parent[k] for k in ("algorithm", "strength", "bias",
                                          "serpentine") if k in parent},
    }


def _version_valid(v) -> bool:
    try:
        appcore.load_palette(v)
        return True
    except Exception:  # noqa: BLE001
        return False


def _algorithm_valid(a) -> bool:
    return a in ALGORITHMS


def _component_valid(fixed: dict) -> bool:
    """Validate a single component directly (works for palette-only or
    dither-only components, which normalize_config cannot handle)."""
    if "version" not in fixed:
        return False
    try:
        appcore.load_palette(fixed["version"])
    except Exception:  # noqa: BLE001
        return False
    if "algorithm" in fixed:
        params_in = {k: v for k, v in fixed.items()
                     if k not in ("version", "suppress")}
        try:
            _rget(fixed["algorithm"]).validate(
                {k: v for k, v in params_in.items() if k != "algorithm"})
        except Exception:  # noqa: BLE001
            return False
    try:
        {int(x) for x in fixed.get("suppress", ())}
    except Exception:  # noqa: BLE001
        return False
    return True


def _adapt_component(comp: dict, parents, notes: list) -> dict:
    """Compatibility check + safe adaptation (fallback to nearest valid ancestor).
    A component missing 'version' inherits the first parent's palette; a
    component missing 'algorithm' is a palette-only component and never needs
    one."""
    if "version" not in comp:
        comp = {**comp, "version": parents[0].get("version")}
    try:
        working = _component_valid(comp)
    except Exception:  # noqa: BLE001
        working = False
    if working:
        return comp
    for p in parents:
        fixed = dict(comp)
        if not _version_valid(fixed.get("version")):
            fixed["version"] = p["version"]
        alg = fixed.get("algorithm")
        if alg is not None and not _algorithm_valid(alg):
            fixed["algorithm"] = p["algorithm"]
        fixed["suppress"] = fixed.get("suppress", p.get("suppress", ()))
        if _component_valid(fixed):
            notes.append({"adapted": True,
                          "broken": {k: comp.get(k) for k in ("version", "algorithm")},
                          "fallback": {k: fixed.get(k) for k in ("version", "algorithm")}})
            return fixed
    raise MixError("could not adapt an incompatible component from any parent")


# ---- method 1: weighted numeric + categorical draws -------------------------

def crossover_weighted_params(spec: MixSpec, rng: random.Random):
    """Weighted-parameter offspring. Returns (child, lineage_info)."""
    px = list(zip(spec.parents, spec.weights))
    alg = _weighted_draw(rng, [p["algorithm"] for p, _ in px],
                         [w for _, w in px])
    supported = {p.name for p in _rget(alg).params}
    info = {"method": "weighted_params",
            "chosen_algorithm": alg, "supported_params": sorted(supported),
            "weight_redistribution": {}}
    child = {"algorithm": alg,
             "version": spec.parents[0]["version"],
             "suppress": tuple(spec.parents[0].get("suppress", ()))}
    for gene in NUMERIC_GENES:
        if gene not in supported:
            continue
        have = [(p, w) for p, w in px if gene in p]
        missing = [genome_id_of(p) for p, _ in px if gene not in p]
        if missing:
            info["weight_redistribution"][gene] = {
                "parents_without_gene": missing,
                "effective_weights": {
                    genome_id_of(p): round(w * 100.0 / sum(w2 for _, w2 in have), 4)
                    for p, w in have}}
        if not have:
            continue
        mix = sum(w * float(p[gene]) for p, w in have) / sum(w for _, w in have)
        child[gene] = int(round(mix)) if gene == "bias" else round(mix, 6)
    if "serpentine" in supported:
        p_true = sum(w for p, w in px if p.get("serpentine")) / sum(w for _, w in px)
        child["serpentine"] = rng.random() < p_true
        info["serpentine_p_true"] = round(p_true, 4)
    return normalize_config(child), info


# ---- method 2: categorical whole-component ----------------------------------

def crossover_categorical(spec: MixSpec, rng: random.Random):
    """Whole-component inheritance: dither component and palette component
    each inherited WHOLE from a weight-drawn parent."""
    px, ws = spec.parents, spec.weights
    d_p = _weighted_draw(rng, px, ws)
    pal_p = _weighted_draw(rng, px, ws)
    comp_d = _adapt_component(_components(d_p)["dither"], px, [])
    comp_p = _adapt_component(_components(pal_p)["palette"], px, [])
    child = {"algorithm": comp_d["algorithm"], "version": comp_p["version"],
             "suppress": tuple(comp_p["suppress"]),
             **{k: v for k, v in comp_d.items() if k != "algorithm"}}
    info = {"method": "categorical",
            "dither_from": genome_id_of(d_p), "palette_from": genome_id_of(pal_p),
            "adaptations": []}
    return normalize_config(child), info


# ---- method 3: module / pipeline crossover ----------------------------------

def crossover_module(spec: MixSpec, rng: random.Random):
    """Independent weighted draw per component + compatibility adaptation."""
    adapted = []
    px, ws = spec.parents, spec.weights
    d_p = _weighted_draw(rng, px, ws)
    pal_p = _weighted_draw(rng, px, ws)
    comp_d = _adapt_component(_components(d_p)["dither"], px, adapted)
    comp_p = _adapt_component(_components(pal_p)["palette"], px, adapted)
    child = {"algorithm": comp_d["algorithm"], "version": comp_p["version"],
             "suppress": tuple(comp_p["suppress"]),
             **{k: v for k, v in comp_d.items() if k != "algorithm"}}
    info = {"method": "module",
            "dither_from": genome_id_of(d_p), "palette_from": genome_id_of(pal_p),
            "adaptations": adapted}
    return normalize_config(child), info


def run_crossover(method: str, spec: MixSpec, rng: random.Random):
    if method == "code_crossover":
        raise NotImplementedError(
            "code-level crossover is unsupported in this repository: no "
            "source-code evolution pipeline exists; refusing to splice source")
    if method not in REGISTRY:
        raise MixError(f"unknown crossover method {method!r}; registered: "
                       f"{sorted(k for k in REGISTRY if k != 'code_crossover')}")
    if method == "ensemble_vote":
        raise MixError("ensemble offspring are produced by evaluate_ensemble(), "
                       "not run_crossover (see REGISTRY note)")
    return {"weighted_params": crossover_weighted_params,
            "categorical": crossover_categorical,
            "module": crossover_module}[method](spec, rng)


# --------------------------------------------------------------------------
# Ensemble crossover (experimental): per-pixel weighted voting
# --------------------------------------------------------------------------

def evaluate_ensemble(spec: MixSpec, rng: random.Random | None = None,
                      item_id: str = "grad-smooth", tier: str = "small",
                      out_dir=None) -> dict:
    """Weighted vote among parent renders. Real pipeline evaluation only.

    The child is an ENSEMBLE, not a single Settings, so its `candidate` dict
    carries `ensemble: [configs]` and can never pass the champion promotion
    gate (documented experimental limitation)."""
    _ = rng  # voting is deterministic given configs + weights
    purpose, arr = testsuite.suite(tier, [item_id])[item_id]
    img = (Path(out_dir) / f"ens_{item_id}.png") if out_dir else None
    if img is not None:
        testsuite.rasterize(arr).save(img, format="PNG")
        src = img
    else:
        import tempfile
        tmp = Path(tempfile.gettempdir()) / f"firefly_ens_{abs(hash(item_id)) % 99999}.png"
        testsuite.rasterize(arr).save(tmp, format="PNG")
        src = tmp
    orig = np.asarray(testsuite.rasterize(arr), dtype=np.uint8)
    per_parent = []
    votes = None
    for p in spec.parents:
        settings = Settings(version=p["version"], maps_w=1, maps_h=1,
                            algorithm=p["algorithm"],
                            strength=p.get("strength", 1.0),
                            suppress=p.get("suppress", ()),
                            bias=p.get("bias", 0))
        res = process(src, settings)
        if votes is None:
            votes = np.zeros((res.pal.arr.shape[0],) + res.idx.shape,
                             dtype=np.float64)
        for color_idx in np.unique(res.idx):
            votes[color_idx][res.idx == color_idx] += sum(
                w for pp, w in spec.effective if genome_id_of(pp) == genome_id_of(p))
        per_parent.append({"id": genome_id_of(p), "colors": int(len(np.unique(res.idx)))})
    winner = np.argmax(votes, axis=0).astype(np.int32)
    pal = appcore.load_palette(spec.parents[0]["version"])
    out_rgb = pal.arr[winner]
    # metrics.compute(orig_plan, palette_arr, reconstruction_rgb, ...): the
    # fidelity verdict must compare the SOURCE with the RECONSTRUCTION, never
    # the source with itself (that made every visual term constant).
    met = M.compute(orig, np.asarray(pal.arr, dtype=np.float64),
                    np.asarray(out_rgb, dtype=np.float64), winner,
                    0.0, {}, np.empty(winner.shape), pal, schem_valid=True)
    score = M.weighted(True, met)
    return {"candidate": {"ensemble": [dict(p) for p in spec.parents],
                          "weights": spec.weights, "rule": "weighted_vote",
                          "version": spec.parents[0]["version"]},
            "score": score, "metrics": met, "per_parent": per_parent}



# --------------------------------------------------------------------------
# Mutation after crossover
# --------------------------------------------------------------------------

_MUT_ORDER = ("algorithm", "strength", "bias", "serpentine", "suppress")


def mutate_child(child: dict, rng: random.Random, prob: float,
                 strength: float, allow_combos: bool = False):
    """Controlled post-crossover mutation. Returns (child, mutation_log).

    prob      = per-key mutation probability; 0 disables (crossover-only runs)
    strength  = 0..1 scale: numeric perturbation magnitude (±0.5·strength on
                the strength axis, ±(4·strength) on bias) and categorical
                flip/replace chance (0.3·strength)."""
    child = dict(child)
    log = []
    if prob <= 0:
        return child, log
    alg = child["algorithm"]
    defaults = {"strength": 1.0, "bias": 0, "serpentine": True, "suppress": ()}
    supported = {p.name for p in _rget(alg).params}
    for k in _MUT_ORDER:
        if k != "algorithm" and k != "suppress" and k not in supported:
            continue
        if rng.random() > prob:
            continue
        old = child.get(k, defaults.get(k))
        new = old
        if k == "algorithm":
            choices = [a for a in ALGORITHMS if a != old]
            if choices:
                new = rng.choice(choices)
                child["algorithm"] = new
        elif k == "strength":
            new = round(min(2.0, max(0.0, float(old) +
                                     rng.uniform(-0.5, 0.5) * 2 * strength)), 4)
            child["strength"] = new
        elif k == "bias":
            new = int(min(64, max(-64, int(old) + rng.randint(-4, 4) *
                                  max(1, round(strength * 4)))))
            child["bias"] = new
        elif k == "serpentine":
            new = not old
            child["serpentine"] = new
        elif k == "suppress":
            pool = [1, 7, 16, 30]
            new = tuple(sorted(set(old) ^ {rng.choice(pool)}))
            child["suppress"] = new
        if new != old:
            log.append({"key": k, "from": old, "to": child.get(k)})
            supported = {p.name for p in _rget(child["algorithm"]).params}
    # two-block combo exploration (opt-in per breeder): combo fields are NOT
    # registry parameters, so normalize_config would drop them — carry them
    # explicitly. Mutation turns combos on and explores the covered share.
    if allow_combos:
        if rng.random() <= prob:
            old_on = bool(child.get("combos", False))
            new_on = not old_on
            log.append({"key": "combos", "from": old_on, "to": new_on})
            child["combos"] = new_on
        if child.get("combos") and rng.random() <= prob:
            old_r = float(child.get("combo_ratio", 1.0))
            new_r = round(min(1.0, max(0.05, old_r + rng.uniform(-0.25, 0.25))), 4)
            if new_r != old_r:
                log.append({"key": "combo_ratio", "from": old_r, "to": new_r})
            child["combo_ratio"] = new_r
    nc = normalize_config(child)
    if child.get("combos"):
        nc["combos"] = True
        nc["combo_ratio"] = float(child.get("combo_ratio", 1.0))
        nc["combo_front_block"] = child.get("combo_front_block", _COMBO_DEFAULT)
    return nc, log


# --------------------------------------------------------------------------
# Elite pool + diversity
# --------------------------------------------------------------------------

class ElitePool:
    """Configurable top-K pool with a structural diversity floor."""

    def __init__(self, k: int, diversity_ratio: float = 0.4):
        self.k = max(1, int(k))
        self.diversity_ratio = float(diversity_ratio)
        self.rows = []  # [{"id","candidate","score","gen"}], best first

    def update(self, row):
        for r in self.rows:
            if r["id"] == row["id"]:
                if (row.get("score") or 0) > r.get("score") or 0:
                    r.update(row)
                return
        self.rows.append(dict(row, score=row.get("score") or 0))
        self.rows.sort(key=lambda r: r["score"], reverse=True)
        self.rows = self._prune_diverse(self.rows)

    def _prune_diverse(self, rows):
        """Top-K by score, but if the top-K contains fewer than
        min_div distinct algorithm families, promote the best rows of the
        missing families from the untruncated list (swapping out the
        lowest-scored replaceable rows)."""
        min_div = max(1, int(round(self.k * self.diversity_ratio)))
        rows = sorted(rows, key=lambda r: r["score"], reverse=True)
        sel = rows[: self.k]
        fams_in = {r["candidate"].get("algorithm") for r in sel}
        need = min_div - len(fams_in)
        if need > 0:
            outside = [r for r in rows[self.k:]
                       if r["candidate"].get("algorithm") not in fams_in]
            for rep in outside[: max(0, need)]:
                # drop the lowest-scored row whose family has a spare member
                # in sel; otherwise drop the lowest-scored row overall
                fam_counts = {}
                for r in sel:
                    f = r["candidate"].get("algorithm")
                    fam_counts[f] = fam_counts.get(f, 0) + 1
                droppable = [r for r in reversed(sel)
                             if fam_counts.get(r["candidate"].get("algorithm"), 0) > 1]
                victim = (droppable[0] if droppable else sel[-1])
                sel = [r for r in sel if r["id"] != victim["id"]] + [rep]
                fams_in.add(rep["candidate"].get("algorithm"))
        return sorted(sel, key=lambda r: r["score"], reverse=True)[: self.k]

    def distinct_algorithms(self) -> int:
        return len({r["candidate"].get("algorithm") for r in self.rows})

    def select(self, mode: str, rng: random.Random, manual=None):
        """mode: 'elite' (top rows) | 'tournament' | 'diversity' | 'manual(ids)'.
        Returns a list of pool rows (parents for mixing)."""
        if not self.rows:
            return []
        if mode == "manual":
            rows = [r for r in self.rows if r["id"] in set(manual or [])]
            if not rows:
                raise MixError("manual selection: none of the requested ids "
                               "are in the elite pool")
            return rows
        if mode == "tournament":
            out, n = [], min(3, len(self.rows))
            for _ in range(n):
                trio = rng.sample(self.rows, min(3, len(self.rows)))
                out.append(max(trio, key=lambda r: r["score"]))
            # dedupe by id, keep order
            seen, ded = set(), []
            for r in out:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    ded.append(r)
            return ded
        if mode == "diversity":
            fams, out = set(), []
            for r in self.rows:
                f = r["candidate"].get("algorithm")
                if f not in fams:
                    out.append(r)
                    fams.add(f)
            return out
        return self.rows[: max(1, self.k // 2)]


# --------------------------------------------------------------------------
# Breeder — the full §F workflow
# --------------------------------------------------------------------------

def _engine_sha16() -> str:
    root = Path(__file__).resolve().parents[1]
    blob = b"".join(sorted(p.read_bytes() for p in root.rglob("*.py")))
    return hashlib.sha256(blob).hexdigest()[:16]


class BreederError(RuntimeError):
    pass


class Breeder:
    """Generational breeding loop over real evaluations only.

    Gate contract: hard validity (exceptions, buildability) is kept separate
    from the weighted visual score; a candidate with score=None can never be
    promoted, and a promoted candidate must pass the schematic buildability
    gate. Holdout results are written to holdout.jsonl only and are never
    read back by selection (holdout isolation)."""

    def __init__(self, work_dir, parents, weights=None, normalize=True,
                 method="weighted_params", mutation_prob=0.7,
                 mutation_strength=0.5, offspring=2, generations=2,
                 population_size=8, elite_size=4, selection="elite",
                 manual_ids=None, holdout_every=2, seed=0,
                 evals_limit=60, time_limit_s=900, allow_combos=False):
        self.allow_combos = bool(allow_combos)
        self.dir = Path(work_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.spec = MixSpec(parents, weights, normalize=normalize, method=method)
        self.method = method
        self.mutation_prob = float(mutation_prob)
        self.mutation_strength = float(mutation_strength)
        self.offspring_n = int(offspring)
        self.generations_n = int(generations)
        self.pop_size = max(int(population_size), 2)
        self.selection = selection
        self.manual_ids = list(manual_ids or [])
        self.evals_limit = int(evals_limit)
        self.time_limit_s = float(time_limit_s)
        self.holdout_every = max(0, int(holdout_every))
        self.seed = int(seed)
        self.stop_evt = threading.Event()
        self.pause_evt = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.pool = ElitePool(elite_size)
        self.state = {
            "config": {"method": method, "mutation_prob": self.mutation_prob,
                       "mutation_strength": self.mutation_strength,
                       "offspring": self.offspring_n,
                       "generations": self.generations_n,
                       "population_size": self.pop_size,
                       "elite_size": int(elite_size),
                       "selection": selection,
                       "seed": self.seed,
                       "engine_sha16": _engine_sha16(),
                       "raw_weights": [float(w) for w in (weights or [])],
                       "normalized": bool(normalize),
                       "effective_parent_pcts": self.spec.effective,
                       "screen_suite": "grad-smooth",
                       "dev_suite": ["grad-smooth", "faux-photo"],
                       "holdout_suite": "pixel-art"},
            "population": {},   # id -> row (scored)
            "champion": None,
            "evals": 0, "invalid": 0, "generation": 0,
            "finished": True,   # set False on start
            "population_suites": None,  # suites recorded in config, not feedback
        }
        self._flush()

    # ---------------- state ----------------
    def _flush(self):
        tmp = self.dir / "breeder.json.tmp"
        tmp.write_text(json.dumps(self.state, indent=1, default=list),
                       encoding="utf-8")
        os.replace(tmp, self.dir / "breeder.json")

    def _lineage(self, rec: dict):
        with open(self.dir / "lineage.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=list) + "\n")

    def _holdout(self, rec: dict):
        with open(self.dir / "holdout.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=list) + "\n")

    def events(self) -> list:
        path = self.dir / "lineage.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out

    # ---------------- lifecycle ----------------
    def start(self):
        if self.is_running():
            raise BreederError("breeder already running")
        if not self.state.get("finished"):
            raise BreederError("breeder has an unfinished checkpoint; "
                               "use resume()")
        self.state["finished"] = False
        self.stop_evt.clear()
        self.pause_evt.clear()
        self._flush()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def resume(self):
        if self.is_running():
            raise BreederError("breeder already running")
        if self.state.get("finished"):
            raise BreederError("breeder finished; start a new run")
        # recover lineage-thread suffix so seeds stay deterministic on resume
        done = len([e for e in self.events()
                    if e.get("generation") == self.state["generation"]])
        self.rng = random.Random(self.seed * 100003 + self.state["generation"]
                                 * 7919 + done)
        self.pause_evt.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def pause(self):
        self.pause_evt.set()

    def stop(self):
        self.stop_evt.set()
        self.pause_evt.clear()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=900)
        self._flush()

    def is_running(self):
        return self.thread is not None and self.thread.is_alive()

    def status(self) -> dict:
        s = self.state
        return {"running": self.is_running(),
                "paused": self.pause_evt.is_set(),
                "finished": s["finished"],
                "generation": s["generation"],
                "evals": s["evals"], "invalid": s["invalid"],
                "population": len(s["population"]),
                "distinct_algorithms": len(
                    {r["candidate"]["algorithm"] for r in s["population"].values()}),
                "champion": s["champion"],
                "effective_parent_pcts": self.spec.effective,
                "mixed_effective": round(self.spec.total, 4)}

    # ---------------- evaluation (real pipeline) ----------------
    def _settings(self, cand: dict) -> Settings:
        combos = bool(cand.get("combos", False)) and self.allow_combos
        return Settings(version=cand["version"], maps_w=1, maps_h=1,
                        algorithm=cand["algorithm"],
                        strength=cand.get("strength", 1.0),
                        suppress=cand.get("suppress", ()),
                        bias=cand.get("bias", 0),
                        combos=combos,
                        combo_ratio=float(cand.get("combo_ratio", 1.0)) if combos else 0.0)

    def _counts_with_covers(self, res) -> dict:
        """Counts including combo covers so the material-cost objective sees
        the real construction the candidate builds (visible + cover blocks)."""
        counts = {k: dict(v) for k, v in res.counts.items()}
        fb = res.combo_block
        covered = int(res.combo_cover.sum())
        counts.setdefault(fb, {"item": fb, "count": 0})["count"] += covered
        return counts

    def _evaluate_settings(self, cand: dict, item_ids, tag: str) -> dict:
        scores, per = [], {}
        for name in item_ids:
            _purpose, arr = testsuite.suite("small", [name])[name]
            img = self.dir / f"eval_{tag}_{name}.png"
            testsuite.rasterize(arr).save(img, format="PNG")
            orig = np.asarray(testsuite.rasterize(arr), dtype=np.uint8)
            t0 = time.perf_counter()
            res = process(img, self._settings(cand))
            elapsed = time.perf_counter() - t0
            if elapsed > 120:
                raise ValueError(f"per-eval time cap exceeded ({elapsed:.1f}s)")
            met = M.compute(orig, np.asarray(res.pal.arr, dtype=np.float64),
                            res.preview.astype(np.float64), res.idx, elapsed,
                            (self._counts_with_covers(res) if res.settings.combos
                             else res.counts),
                            np.empty(res.idx.shape), res.pal, schem_valid=True)
            sc = M.weighted(True, met)
            scores.append(sc)
            per[name] = {"score": sc,
                         "oklab_norm": met["oklab_error_norm"],
                         "edge": met["edge_preservation"]}
        return {"mean": round(float(np.mean(scores)), 6), "per": per}

    def _buildability_gate(self, cand: dict):
        """A promoted candidate must build a VALIDATED schematic (1x1 map)."""
        if self.method == "ensemble_vote" or "ensemble" in cand:
            return False, ("ensemble children are not promotable to the "
                           "stable champion pool (experimental crossover)")
        try:
            _purpose, arr = testsuite.suite("small", ["grad-smooth"])["grad-smooth"]
            img = self.dir / "gate_fixture.png"
            testsuite.rasterize(arr).save(img, format="PNG")
            out = appcore.full_build(img, self._settings(cand),
                                     self.dir / "gate", base_name="gate")
            v = out["files"]["validation"]
            placed = v["schem"]["blocks_placed"]
            expected = 128 * 128
            if placed != expected:
                return False, f"schematic placed {placed} != {expected} map pixels"
            return True, f"placed={placed} (validated)"
        except Exception as ex:  # noqa: BLE001
            return False, f"{type(ex).__name__}: {ex}"[:200]

    # ---------------- the workflow loop (§F) ----------------
    def _loop(self):
        try:
            self._loop_body()
        except Exception as e:  # noqa: BLE001  record, never silently vanish
            self.state["finished"] = True
            self.state["stop_reason"] = "crash"
            self.state["crash"] = f"{type(e).__name__}: {e}"[:400]
            self._flush()

    def _loop_body(self):
        from . import resourcemanager as rm
        if getattr(self, "rng", None) is None:
            self.rng = random.Random(self.seed)
        rng = self.rng
        screen_ids = [self.state["config"]["screen_suite"]]
        dev_ids = list(self.state["config"]["dev_suite"])
        t0 = time.time()
        while not self.stop_evt.is_set() and not self.state["finished"]:
            if self.pause_evt.is_set():
                self._flush()
                self.wake.wait(timeout=3600)
                self.wake.clear()
                if self.stop_evt.is_set():
                    break
                continue
            try:
                # emergency memory-pressure response: schedule nothing while
                # free memory is under the configured floor
                if not rm.manager().admission_check("breeder"):
                    self.pause_evt.set()
                    self._flush()
                    continue
            except Exception as _rm_err:  # noqa: BLE001 — manager not initialized
                pass
            s = self.state
            if s["generation"] >= self.generations_n:
                s["finished"] = True
                s["stop_reason"] = "generations"
                self._flush()
                break
            if time.time() - t0 > self.time_limit_s and s["evals"] > 0:
                s["finished"] = True
                s["stop_reason"] = "time"
                self._flush()
                break
            if s["evals"] >= self.evals_limit:
                s["finished"] = True
                s["stop_reason"] = "evals"
                self._flush()
                break
            gen = s["generation"]
            seen_child_ids = set()
            # 1. evaluate parents (screening tier) and 2. reject invalid
            for p in self.spec.parents:
                pid = genome_id_of(p)
                if pid in s["population"]:
                    continue
                try:
                    ev = self._evaluate_settings(p, screen_ids, "p")
                    row = {"id": pid, "candidate": p, "score": ev["mean"],
                           "source": "parent", "gen": gen}
                except Exception as ex:  # noqa: BLE001
                    s["invalid"] += 1
                    self._lineage({"uid": f"{gen}:p:{pid}", "id": pid,
                                   "kind": "parent", "score": None,
                                   "rejected": f"{type(ex).__name__}: {ex}"[:200],
                                   "severed": True})
                    continue
                s["evals"] += 1
                s["population"][pid] = row
                self.pool.update({**row, "gen": gen})
            # 3. rank + 4. select parents from elite/diversity pools
            try:
                parent_rows = self.pool.select(self.selection, rng,
                                               manual=self.manual_ids)
            except MixError as ex:
                s["finished"] = True
                s["stop_reason"] = f"selection: {ex}"
                self._flush()
                break
            if not parent_rows:
                s["finished"] = True
                s["stop_reason"] = "no valid parents survived screening"
                self._flush()
                break
            # §F semantics: the SELECTED parents are what's actually mixed,
            # and the configured percentages apply positionally to the
            # selection order (best-first for elite/manual/diversity, draw
            # order for tournament). Excess weights are renormalized over
            # however many parents were selected and shown on every record.
            sel_w = self.spec.weights[: len(parent_rows)]
            if len(sel_w) < len(self.spec.parents):
                total = sum(sel_w)
                sel_w = [w * 100.0 / total for w in sel_w]
            event_spec = MixSpec([r["candidate"] for r in parent_rows],
                                 sel_w, normalize=True, method=self.method)
            par_scores = [r["score"] for r in parent_rows]
            # 5-6. offspring via crossover + optional mutation. Each event is
            # driven by a DERIVED seed (seed:gen:i), so a breeding event can be
            # rerun deterministically from its lineage record alone.
            for i in range(self.offspring_n):
                rec_uid = f"{gen}:{i}:{self.seed}"
                event_seed = f"{self.seed}:{gen}:{i}"
                rng_e = random.Random(event_seed)
                child, info = run_crossover(self.method, event_spec, rng_e)
                child, mutations = mutate_child(
                    child, rng_e, self.mutation_prob, self.mutation_strength,
                    allow_combos=self.allow_combos)
                cid = genome_id_of(child)
                dedupe_forced = False
                if cid in seen_child_ids:
                    # anti-collapse: a duplicate child is mutated with a
                    # minimum strength so it is actually a new candidate;
                    # recorded like every other mutation
                    child, forced_muts = mutate_child(
                        child, rng_e, 1.0, max(self.mutation_strength, 0.15),
                        allow_combos=self.allow_combos)
                    mutations = mutations + forced_muts
                    cid = genome_id_of(child)
                    dedupe_forced = True
                seen_child_ids.add(cid)
                rec = {
                    "uid": rec_uid, "kind": "offspring", "id": cid,
                    "generation": gen,
                    "parents": [genome_id_of(p) for p in event_spec.parents],
                    "parent_scores": par_scores,
                    "pct": event_spec.effective,
                    "method": self.method, "seed": event_seed,
                    "parents_cfg": [dict(p) for p in event_spec.parents],
                    "engine_sha16": _engine_sha16(),
                    "inherited": info, "mutations": mutations,
                    "candidate": child, "mutation_config":
                        {"prob": self.mutation_prob,
                         "strength": self.mutation_strength},
                }
                # 7. inexpensive screening
                try:
                    screen = self._evaluate_settings(child, screen_ids, "o")
                    screen_score = screen["mean"]
                except Exception as ex:  # noqa: BLE001
                    s["invalid"] += 1
                    rec["score"] = None
                    rec["rejected"] = f"{type(ex).__name__}: {ex}"[:200]
                    self._lineage(rec)
                    continue
                s["evals"] += 1
                # 8. dev benchmark for promising offspring only
                best_screen = max(par_scores) if par_scores else screen_score
                dev_row = None
                if screen_score >= best_screen * 0.9:
                    try:
                        dev_row = self._evaluate_settings(child, dev_ids, "d")
                        s["evals"] += 1
                    except Exception as ex:  # noqa: BLE001
                        dev_row = {"error": f"{type(ex).__name__}: {ex}"[:160]}
                champ = s["champion"] or {}
                rec["score"] = screen["mean"]
                if dedupe_forced:
                    rec["dedupe_forced_mutation"] = True
                rec["scores"] = {"screen": screen["mean"], "dev": dev_row}
                # 9. comparison vs parents + champion
                rec["comparison"] = {
                    "parents_best": max(par_scores) if par_scores else None,
                    "champion_prev": champ.get("score"),
                    "outperforms_all_parents":
                        bool(par_scores) and screen_score > max(par_scores),
                    "outperforms_champion":
                        champ.get("score") is not None and screen_score > champ["score"]}
                # 10. promotion gate: beat champion AND pass buildability
                promoted, gate_reason = False, "score did not beat champion"
                if champ.get("score") is None or screen_score > champ["score"]:
                    gate_reason = ""
                    ok, detail = self._buildability_gate(child)
                    if ok:
                        s["champion"] = {"id": cid, "candidate": child,
                                         "score": screen_score,
                                         "child_of": [genome_id_of(p)
                                                      for p in event_spec.parents],
                                         "pct": event_spec.effective,
                                         "generation": gen}
                        promoted = True
                        gate_reason = (f"promoted: {champ.get('score')} -> "
                                       f"{screen_score}; {detail}")
                    else:
                        gate_reason = f"buildability gate rejected: {detail}"
                        s["invalid"] += 1
                rec["promotion"] = {"promoted": promoted, "reason": gate_reason}
                # 11. periodic holdout WITHOUT optimizer feedback — runs on the
                # schedule even when nothing was promoted this generation
                holdout_summary = None
                if self.holdout_every and gen > 0 and \
                        gen % self.holdout_every == 0 and i == 0:
                    holdout_summary = self._evaluate_holdout()
                    rec["holdout_excerpt"] = holdout_summary
                rec["holdout_protected"] = bool(holdout_summary)
                self._lineage(rec)
                # population + elite pool
                row = {"id": cid, "candidate": child, "score": screen_score,
                       "source": "offspring", "gen": gen, "parents":
                           [genome_id_of(p) for p in event_spec.parents]}
                s["population"][cid] = row
                self.pool.update(row)
                self.state["population"] = self._diverse_population(
                    s["population"])
            s["generation"] = gen + 1
            self._flush()
        self._flush()

    def _evaluate_holdout(self):
        champ = (self.state.get("champion") or {})
        if not champ:
            return {"suite": self.state["config"]["holdout_suite"], "rows": 0}
        ev = self._evaluate_settings(champ["candidate"],
                                     [self.state["config"]["holdout_suite"]], "h")
        self._holdout({"ts": time.time(), "champion_id": champ.get("id"),
                       "mean": ev["mean"], "per": ev["per"],
                       "note": "holdout result — never optimizer feedback"})
        return {"suite": self.state["config"]["holdout_suite"], "rows": 1,
                "mean": ev["mean"]}

    def _diverse_population(self, pop: dict) -> dict:
        """Keep top-K by score, but never fewer than min(3, K) distinct
        algorithm families — prevents collapse into near-identical candidates."""
        rows = sorted(pop.values(), key=lambda r: r["score"], reverse=True)
        fams, keep = set(), []
        min_div = min(3, self.pop_size)
        for r in rows:
            f = r["candidate"]["algorithm"]
            if f not in fams:
                fams.add(f)
            keep.append(r)
            if len(keep) >= self.pop_size and len(fams) >= min_div:
                break
        else:
            keep = keep[: self.pop_size]
        return {r["id"]: r for r in keep}


# --------------------------------------------------------------------------
# Presets (§G)
# --------------------------------------------------------------------------

# GUI slider presets (comma strings, parent order = selection order)
PRESET_WEIGHTS_GUI = {
    "50/50 crossover": "50,50",
    "70/30 exploitation": "70,30",
    "three-parent 50/30/20": "50,30,20",
    "elite-only breeding": "50,50",
    "diversity-focused exploration": "50,50",
    "crossover without mutation": "50,50",
    "crossover with aggressive mutation": "50,50",
    "ensemble parents (experimental)": "50,50",
}


PRESETS = {
    "50/50 crossover": {"method": "weighted_params", "normalize": True},
    "70/30 exploitation": {"method": "weighted_params", "normalize": True},
    "three-parent 50/30/20": {"method": "weighted_params", "normalize": True},
    "elite-only breeding": {"method": "categorical", "selection": "elite"},
    "diversity-focused exploration": {"method": "module",
                                      "selection": "diversity",
                                      "mutation_prob": 0.5,
                                      "mutation_strength": 0.8},
    "crossover without mutation": {"method": "weighted_params",
                                   "mutation_prob": 0.0},
    "crossover with aggressive mutation": {"method": "weighted_params",
                                           "mutation_prob": 1.0,
                                           "mutation_strength": 1.0},
    "ensemble parents (experimental)": {"method": "ensemble_vote"},
}


# --------------------------------------------------------------------------
# Deterministic rerun of one breeding event (§H)
# --------------------------------------------------------------------------

def rerun_event(event: dict) -> dict:
    """Rerun a breeding event from its lineage record: derived seed, recorded
    percentages/method/mutation config reproduce the identical offspring
    (verified via `matches_original`). Dedupe-forced events rerun with their
    forced diversification applied in the same order."""
    parents_cfg = event["parents_cfg"]
    weights = event["weights"]
    method = event["method"]
    spec = MixSpec(parents_cfg, weights, normalize=True, method=method)
    rng = random.Random(event["seed"])
    if method == "ensemble_vote":
        child, mutations = {"ensemble": [dict(p) for p in spec.parents],
                            "weights": spec.weights}, []
    else:
        child, _info = run_crossover(method, spec, rng)
        child, mutations = mutate_child(child, rng,
                                        float(event.get("mutation_prob", 0.0)),
                                        float(event.get("mutation_strength", 0.0)),
                                        allow_combos=bool(event.get("allow_combos", False)))
        if event.get("dedupe_forced_mutation"):
            _extra_forced, forced = mutate_child(
                child, rng, 1.0, max(float(event.get("mutation_strength", 0.0)), 0.15),
                allow_combos=bool(event.get("allow_combos", False)))
            child = _extra_forced
            mutations = mutations + forced
    return {"id": genome_id_of(child), "candidate": child,
            "mutations": mutations,
            "matches_original": event["child_id"] == genome_id_of(child)}


def lineage_tree(path) -> str:
    """ASCII lineage tree from a breeder's lineage.jsonl (promotions marked *)."""
    path = Path(path)
    events = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    lines = [f"lineage — {len(events)} events; '*' = promoted"]
    for e in events:
        if e.get("kind") not in ("offspring",):
            continue
        mark = "*" if e.get("promotion", {}).get("promoted") else " "
        pcts = "+".join(f"{p['pct']:.0f}%" for p in (e.get("pct") or []))
        muts = len(e.get("mutations", []))
        score = e.get("scores", {}).get("screen")
        lines.append(
            f"{mark} gen{e.get('generation')} {e.get('id')} [{pcts}] "
            f"{e.get('method')} muts={muts} score={score} "
            f"parents={','.join(e.get('parents') or [])}")
    return "\n".join(lines)
