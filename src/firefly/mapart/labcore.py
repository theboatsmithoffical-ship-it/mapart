"""Evolution Lab — long-running candidate search over map-art settings.

States live in `dir/state.json` (atomic), history lines in `history.jsonl`,
best candidate in `best.json`. Closing the app mid-run leaves a valid
checkpoint; `LabController(dir)` on next start resumes it.

Candidate = map-art settings {algorithm, version, strength, suppress}.
Search strategies: "random", "sweep" (grid of strengths), "evolve" (mutation +
recombination + elitism over the best N).

Duplicate configs reuse cached scores — never re-evaluated. Invalid configs
are recorded with their reason and do not count as candidates (but do consume
their attempt slot, since refusing to fail is how budgets lie).
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from pathlib import Path

from firefly.evolution.dither import ALGORITHMS

from . import testsuite
from .appcore import Settings, process
from .mapart import DEFAULT_COMBO_FRONT_BLOCK
from .metrics import DEFAULT_WEIGHTS, compute, weighted
from .registry import default_params, validate

MAX_EVAL_TIME_S = 120
MAX_SUPPRESS_IDS = 16

# Lab suite items may reference real dataset images as "lib:<image-id>".
# They are loaded from the local image-library manifest ON DEMAND at suite
# resolution (never the whole library), and ONLY dev/stress entries are
# allowed -- holdout entries are refused here so the optimizer can never
# train on the final-evaluation set.


def _lib_fixture(library, image_id: str, size: int):
    """Load one local dataset image from disk at tier resolution. Purpose
    comes from the library manifest; holdout entries are refused here so the
    optimizer can never train on the final-evaluation set."""
    from .imagelibrary import ImageLibrary
    if library is None or not isinstance(library, ImageLibrary):
        raise ValueError(
            f"'lib:{image_id}' needs a wired ImageLibrary — pass "
            "library=ImageLibrary(<data-dir>) to LabController or use "
            "generated suite ids")
    entry = library.entry(image_id)
    if entry is None:
        raise ValueError(f"local dataset image not in manifest: {image_id}")
    purposes = entry.get("datasets", [])
    if "holdout" in purposes:
        raise PermissionError(
            f"{image_id} is holdout: the optimizer must not train on it "
            "(aggregate-only evaluation happens through a separate process)")
    path = library.file_path(entry)
    if not path or not Path(path).is_file():
        raise FileNotFoundError(f"local dataset image missing on disk: {image_id}")
    from PIL import Image
    import numpy as np
    with Image.open(path) as im:
        w, h = im.size
        scale = size / max(1, max(w, h))
        small = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        im.load()
        arr = np.asarray(small.convert("RGB"), dtype=np.uint8)
    purpose = ("stress" if "stress" in purposes else
               "validation" if "validation" in purposes else
               "dev" if "dev" in purposes else "dev")
    label = f"local dataset image {image_id} ({'/'.join(purposes)})"
    return (label, arr)


def canonical(candidate: dict) -> str:
    from .registry import get as _g
    _combo_keys = ("combos", "combo_ratio", "combo_front_block")
    # combo keys are construction control fields, NOT dither-registry
    # parameters; they still participate in candidate identity (a different
    # cover ratio is a different construction) but never reach the registry's
    # strict parameter validation.
    params = _g(candidate["algorithm"]).validate(
        {k: candidate[k] for k in candidate
         if k not in ("algorithm", "version", "suppress", *_combo_keys)})
    combos = bool(candidate.get("combos", False))
    return json.dumps({"algorithm": candidate["algorithm"],
                       "version": candidate.get("version", "survival-wool-1.8"),
                       "suppress": sorted(candidate.get("suppress", ())),
                       "combos": combos,
                       "combo_ratio": round(float(candidate.get("combo_ratio", 0.0)), 4) if combos else 0.0,
                       "combo_front_block": candidate.get("combo_front_block") if combos else None,
                       **params}, sort_keys=True)


def random_candidate(rng: random.Random, version="survival-wool-1.8",
                     allow_combos: bool = False) -> dict:
    from .registry import get as _g
    alg = rng.choice([a for a in ALGORITHMS + ("none",)])
    supported = {p.name for p in _g(alg).params}
    base = {"algorithm": alg, "version": version}
    base.update({k: v for k, v in default_params(alg).items()
                 if k != "strength" or "strength" in supported})
    if "strength" in supported:
        base["strength"] = round(rng.uniform(0.0, 2.0) if rng.random() < .8 else 1.0, 4)
    base = {k: v for k, v in base.items() if k in supported or k in ("algorithm", "version", "suppress")}
    out = normalize_config(base)
    if allow_combos and rng.random() < 0.3:
        # opt-in two-block combos in the search space: the cover block does
        # not change modeled colors (HYPOTHESIS until an audit specimen passes)
        out["combos"] = True
        out["combo_ratio"] = round(rng.uniform(0.1, 1.0), 4)
    return out


def normalize_config(cfg: dict) -> dict:
    from .registry import get as _g
    supported = {p.name for p in _g(cfg["algorithm"]).params}
    params_in = {k: v for k, v in cfg.items()
                 if k in supported}
    params = validate(cfg["algorithm"], params_in)
    return {"algorithm": cfg["algorithm"], "version": cfg.get("version", "survival-wool-1.8"),
            "suppress": tuple(sorted(int(x) for x in cfg.get("suppress", ()))),
            **params}


def mutate(candidate: dict, rng: random.Random, allow_combos: bool = False) -> dict:
    child = dict(candidate)
    fields = ["algorithm", "strength", "strength2", "suppress"]
    if allow_combos:
        fields += ["combo_ratio"]
    field = rng.choice(fields)
    if field == "algorithm":
        child["algorithm"] = rng.choice(ALGORITHMS)
    elif field == "strength":
        child["strength"] = round(min(2.0, max(0.0, child["strength"] + rng.uniform(-0.5, 0.5))), 4)
    elif field == "strength2":
        child["strength"] = round(rng.uniform(0.0, 2.0), 4)
    elif field == "combo_ratio":
        child["combos"] = True
        child["combo_ratio"] = round(min(1.0, max(0.05, child.get("combo_ratio", 0.5)
                                                   + rng.uniform(-0.25, 0.25))), 4)
    else:
        child["suppress"] = tuple(sorted(set(child["suppress"]) ^ {rng.choice([1, 7, 16])}))
    out = normalize_config(child)
    if child.get("combos") and "combo_ratio" in child:
        out["combos"] = True
        out["combo_ratio"] = child["combo_ratio"]
    return out


def recombine(a: dict, b: dict, rng: random.Random) -> dict:
    """Categorical per-key recombination: each key comes from a or b with
    configured 50/50 probability; b-only keys (and c) always materialize."""
    child = {}
    for k in set(a) | set(b):
        if k in a and k in b:
            child[k] = a[k] if rng.getrandbits(1) else b[k]
        elif k in a:
            child[k] = a[k]
        else:
            child[k] = b[k]
    return normalize_config(child)


class LabController:
    """Own exactly one search run at a time. All public methods return quickly;
    blocking lives on the worker thread. Reasonable CPU/memory profile: one
    eval at a time, cached dicts are small."""

    def __init__(self, work_dir, tier="small", suite_ids=None, weights=None,
                 evals_limit=40, time_limit_s=600, strategy="evolve",
                 version="survival-wool-1.8", seed=0, mem_track=False,
                 library=None, allow_combos=False):
        self.dir = Path(work_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        # opt-in two-block combo exploration: combo candidates change the
        # construction (extra cover layer, material cost), never the modeled
        # colors; outputs are labeled hypothesis until in-game verification.
        self.allow_combos = bool(allow_combos)
        self.tier = tier
        self.suite_ids = list(suite_ids or ["grad-smooth", "faux-photo", "checker-hf"])
        self.weights = dict(DEFAULT_WEIGHTS if weights is None else weights)
        self.evals_limit = int(evals_limit)
        self.time_limit_s = float(time_limit_s)
        self.strategy = strategy
        self.version = version
        self.seed = int(seed)
        self.mem_track = bool(mem_track)
        # Optional local-dataset bridge: suite ids may be "lib:<image-id>";
        # fixtures load from the user's local image library on demand.
        self.library = library
        self._pause_evt = threading.Event()
        self._stop_evt = threading.Event()
        self._wakeup = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._load_state()

    # ---------------- state ----------------
    def _state_path(self):
        return self.dir / "state.json"

    def _load_state(self):
        st = {"finished": False,
              "mode": {"tier": self.tier, "suite": self.suite_ids,
                       "weights": self.weights, "strategy": self.strategy,
                       "version": self.version, "seed": self.seed},
              "evaluated": 0, "invalid": 0,
              "cache": {},
              "best": None,
              "started_at": None}
        if self._state_path().exists():
            try:
                st.update(json.loads(self._state_path().read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass
        self.state = st
        self.rng = random.Random(self.seed + int(st.get("evaluated", 0)) * 7919)
        self._t_start = None
        self._flush()

    def _flush(self):
        tmp = self._state_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, default=list), encoding="utf-8")
        os.replace(tmp, self._state_path())

    def history(self) -> list:
        path = self.dir / "history.jsonl"
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
            raise RuntimeError("experiment already running")
        if self.state.get("finished"):
            return
        self._stop_evt.clear(); self._pause_evt.clear()
        if not self.state.get("evaluated"):
            self.state["started_at"] = time.time()
            self._flush()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def pause(self):
        self._pause_evt.set()

    def resume(self):
        if self.state.get("finished"):
            raise RuntimeError("experiment finished; create a new run")
        self._pause_evt.clear()
        self._wakeup.set()

    def stop(self):
        self._stop_evt.set()
        self._pause_evt.clear()
        self._wakeup.set()
        if self._thread:
            self._thread.join(timeout=MAX_EVAL_TIME_S + 20)
        self._flush()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        s = self.state
        return {"ok": True, "running": self.is_running(), "paused": self._pause_evt.is_set(),
                "finished": s.get("finished", False), "evaluated": s["evaluated"],
                "invalid": s["invalid"], "cached": len(s["cache"]),
                "best": s.get("best"),
                "time_limit_s": self.time_limit_s, "evals_limit": self.evals_limit}

    # ---------------- worker ----------------
    def _loop(self):
        try:
            self._loop_body()
        except Exception as e:  # noqa: BLE001  (record, never silently vanish)
            self.state["finished"] = True
            self.state["stop_reason"] = "crash"
            self.state["crash"] = f"{type(e).__name__}: {e}"[:400]
            self._flush()

    def _loop_body(self):
        from . import resourcemanager as rm
        suite = self._build_suite()
        while not self._stop_evt.is_set() and not self.state["finished"]:
            if self._pause_evt.is_set():
                self._flush()
                self._wakeup.wait(timeout=3600)
                self._wakeup.clear()
                continue
            try:
                # emergency memory-pressure response: stop scheduling new
                # jobs; pause at this safe checkpoint until headroom recovers
                if not rm.manager().admission_check("lab"):
                    self._pause_evt.set()
                    self._flush()
                    continue
            except Exception as _rm_err:  # noqa: BLE001 — manager not ready yet
                pass
            s = self.state
            if (s["evaluated"] + s["invalid"]) >= self.evals_limit:
                s["finished"] = True; s["stop_reason"] = "evals"
                self._flush(); break
            if self._t_start and (time.time() - self._t_start) > self.time_limit_s \
               and s["evaluated"] > 0:  # time limit starts ticking after first eval row
                s["finished"] = True; s["stop_reason"] = "time"
                self._flush(); break
            if s["evaluated"] == 0:
                self._t_start = time.time()
            cand, ngcand = self._next_candidate()
            self._evaluate_candidate(cand, suite, ngcand=ngcand)
            self._flush()

    def _build_suite(self) -> dict:
        """Generated fixtures + (optionally) real local dataset images.

        "lib:<image-id>" items resolve to ONE image loaded from the local
        image library (on demand, tier-resolved; dev/stress only — holdout
        is never exposed to the optimizer). All items are native np arrays
        in (purpose, arr) form."""
        out = {}
        for item in self.suite_ids:
            if item.startswith("lib:"):
                purpose, arr = _lib_fixture(self.library, item[4:],
                                            testsuite.TIERS.get(self.tier, 128))
                out[item] = (purpose, arr)
            else:
                out[item] = testsuite.suite(self.tier, [item])[item]
        if self.library is not None:
            try:
                unused = [i for i in self.library.ids() if f"lib:{i}" not in out]
                self.state["library_offline"] = {
                    "wired": True, "available_items": len(self.library.ids()),
                    "unscheduled_items": len(unused)}
            except Exception:  # noqa: BLE001 — informational only
                pass
        return out

    def _next_candidate(self):
        """returns (candidate_dict, nevergrad Candidate|None)"""
        rng = self.rng
        rows = self.history()
        good = [r for r in rows if r.get("score") is not None]
        if self.strategy == "nevergrad":
            try:
                cand = self._ng_backend().propose()
            except ImportError:
                raise ValueError("strategy 'nevergrad' requires the optional dependency:\n"
                                 "  pip install nevergrad\n"
                                 "built-in strategies (evolve/random/sweep) still work offline")
            from .registry import get as _g
            supported = {p.name for p in _g(cand[0]["algorithm"]).params}
            c = {k: v for k, v in cand[0].items() if k in supported or k in ("algorithm", "version", "suppress")}
            return normalize_config(c), cand[1]
        if self.strategy == "sweep" or not good or (self.state["evaluated"] < 2 * rng.randint(1, 4)):
            return random_candidate(rng, self.version, allow_combos=self.allow_combos), None
        if self.strategy == "random" or len(good) < 3:
            return random_candidate(rng, self.version, allow_combos=self.allow_combos), None
        good.sort(key=lambda r: r["score"], reverse=True)
        r1 = rng.choice(good[:max(1, len(good) // 3)])["candidate"]
        r2 = rng.choice(good[:max(1, len(good) * 2 // 3)])["candidate"]
        child = recombine(normalize_config(r1), normalize_config(r2), rng)
        if rng.random() < 0.7:
            child = mutate(child, rng, allow_combos=self.allow_combos)
        # elitism: never morph *the* best; small visual gains below material
        # cost do not matter -> force at least one parameter change
        if canonical(child) == canonical(self.state["best"]["candidate"] if self.state["best"] else r1):
            child = mutate(child, rng, allow_combos=self.allow_combos)
        return child, None

    def _ng_backend(self):
        if getattr(self, "_ng_obj", None) is None:
            self._ng_obj = NevergradBackend(self.evals_limit, seed=self.seed)
        return self._ng_obj

    def _evaluate_candidate(self, cand: dict, suite: dict, ngcand=None):
        key = canonical(cand)
        cache = self.state["cache"]
        entry = {"ts": time.time(), "candidate": cand, "canonical": key}
        try:
            # deterministic fixture choice per candidate
            item_ids = list(suite)
            item = suite[item_ids[int(hashlib_sha(key) % len(item_ids))]]
            purpose, arr = item
            from . import testsuite as ts
            img_path = self.dir / f"eval_img_{hashlib_sha(key):08x}.png"
            if not img_path.exists():
                ts.rasterize(arr).save(img_path, format="PNG")
            if key in cache:
                cached = cache[key]
                entry.update({**cached, "cached": True})
                self.state["evaluated"] += 0
            else:
                import numpy as np
                import tracemalloc
                if self.mem_track:
                    tracemalloc.start()
                t0 = time.perf_counter()
                combos_on = bool(cand.get("combos", False)) and getattr(self, "allow_combos", False)
                settings = Settings(version=cand["version"], maps_w=1, maps_h=1,
                                    algorithm=cand["algorithm"],
                                    strength=cand.get("strength", 1.0),
                                    suppress=cand.get("suppress", ()),
                                    combos=combos_on,
                                    combo_ratio=(float(cand.get("combo_ratio", 1.0))
                                                 if combos_on else 0.0),
                                    combo_front_block=cand.get("combo_front_block",
                                                               DEFAULT_COMBO_FRONT_BLOCK))
                res = process(img_path, settings)
                import numpy as np
                from PIL import Image
                orig_plan = np.asarray(Image.open(img_path).convert("RGB"))
                metrics = compute(orig_plan, res.preview.astype(np.float64),
                                  orig_plan, res.idx, time.perf_counter() - t0,
                                  res.counts, np.empty(res.idx.shape), res.pal,
                                  schem_valid=True, peak_mem=None)
                if self.mem_track:
                    cur, peak = tracemalloc.get_traced_memory()
                    metrics["peak_mem_bytes"] = peak
                    tracemalloc.stop()
                if metrics["process_time_s"] > MAX_EVAL_TIME_S:
                    self.state["invalid"] += 1
                    raise ValueError(f"candidate exceeded per-eval time cap "
                                     f"({metrics['process_time_s']:.1f}s > {MAX_EVAL_TIME_S}s)")
                score = weighted(True, metrics, self.weights)
                entry.update({"metrics": metrics, "score": score, "cached": False})
                cache[key] = {"metrics": metrics, "score": score}
                if ngcand is not None:
                    try:
                        self._ng_backend().observe(ngcand, score)
                    except Exception:  # noqa: BLE001
                        pass
                # baseline: first-evaluated entry kept as comparison anchor
                self.state["evaluated"] += 1
                self._maybe_promote(entry)
                self._append_history(entry)
                self._housekeeping(img_path)
                return
            # cached path still counts as an observation row but not a re-run
            self.state["evaluated"] += 0
            if ngcand is not None:
                try:
                    self._ng_backend().observe(ngcand, entry.get("score"))
                except Exception:  # noqa: BLE001
                    pass
            self._maybe_promote(entry)
            self._append_history(entry)
        except Exception as e:  # noqa: BLE001
            self.state["invalid"] += 1
            entry.update({"error": f"{type(e).__name__}", "detail": str(e)[:200],
                          "score": None})
            if ngcand is not None:
                try:
                    self._ng_backend().observe(ngcand, None)
                except Exception:  # noqa: BLE001
                    pass
            self._append_history(entry)

    def _maybe_promote(self, entry):
        best = self.state.get("best")
        cand = entry.get("candidate")
        score = entry.get("score")
        if score is None:
            return
        if best is None or score > best.get("score", float("-inf")):
            self.state["best"] = {"score": score, "candidate": cand,
                                  "metrics": entry.get("metrics"),
                                  "verified": entry.get("cached", False),
                                  "ts": time.time()}
        # when a later generation underperforms, best remains untouched (preserved)

    def _append_history(self, entry):
        with open(self.dir / "history.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=list) + "\n")

    def _housekeeping(self, img_path):
        try:
            img_path.unlink()
        except OSError:
            pass


def hashlib_sha(x: str) -> int:
    import hashlib
    return int.from_bytes(hashlib.sha256(x.encode()).digest()[:4], "big") % (2 ** 31)


class NevergradBackend:
    """Optional nevergrad optimizer over the map-art search space.

    Enabled only when `pip install nevergrad` succeeded on this interpreter;
    the Lab keeps its built-in strategies if it is missing, so normal local
    operation never depends on it (and it is fully offline once installed).
    """

    def __init__(self, budget: int, seed: int = 0):
        # keep OpenBLAS from spinning up huge thread pools (memory-capped hosts)
        for k in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
            os.environ.setdefault(k, "1")
        import nevergrad as ng
        self.ng = ng
        self.algorithms = [a for a in ALGORITHMS + ("none",)]
        strength = ng.p.Scalar(lower=0.05, upper=2.0)
        bias = ng.p.Scalar(lower=0, upper=64).set_integer_casting()
        self.inst = ng.p.Instrumentation(
            algorithm=ng.p.Choice(self.algorithms),
            strength=strength,
            bias=bias,
        )
        opt_cls = ng.optimizers.registry["RandomSearch"]
        self.opt = opt_cls(parametrization=self.inst, budget=max(8, int(budget)), num_workers=1)
        try:
            self.opt.parametrization.value.recombination  # touch only
        except Exception:  # noqa: BLE001
            pass

    def propose(self) -> dict:
        cand = self.opt.ask()
        self._pending = cand
        args, kwargs = cand.args, cand.kwargs
        cfg = dict(kwargs)
        return {"algorithm": cfg["algorithm"],
                "strength": round(float(cfg["strength"]), 4),
                "bias": int(cfg["bias"]),
                "version": "survival-wool-1.8",
                "suppress": ()}, cand

    def observe(self, cand, score: float | None):
        try:
            self.opt.tell(cand, score if score is not None else 0.0)
        except Exception:  # noqa: BLE001  (never let the optimizer kill a run)
            pass

    @property
    def available(self) -> bool:
        return self.ng is not None
