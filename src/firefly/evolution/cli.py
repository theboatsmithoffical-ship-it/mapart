"""Command-line interface: `python -m firefly.evolution.cli` (see README.md).

Emits one JSON object per command on stdout; non-zero exit + JSON error on
failure. Never opens a GUI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import pack, synth, store
from . import VERSION
from .evolve import run_evolution


def _print(d: dict) -> None:
    print(json.dumps(d, sort_keys=True))


def _fail(e: Exception) -> int:
    print(json.dumps({"ok": False, "error": str(e), "kind": type(e).__name__},
                     sort_keys=True), file=sys.stderr)
    return 2


def _custom_env(path: Path):
    loaded_files = synth.load_files(path)
    fixtures = {f.stem: synth.load_image(f) for f in loaded_files}
    blob = b"".join(f.read_bytes() for f in loaded_files)
    sid = "custom-" + hashlib.sha256(blob).hexdigest()[:8]
    return synth.fixture_env(fixtures, sid, {f.stem: str(f) for f in loaded_files})


def cmd_run(a) -> int:
    path = Path(a.fixtures)
    if not (path.is_file() or path.is_dir()):
        raise ValueError(f"fixtures path not found: {path}")
    env = _custom_env(path)
    s = run_evolution(env, Path(a.run_dir), pop=a.pop, gens=a.gens,
                      seed=a.seed, budget=a.eval_budget, resume=a.resume)
    _print(s)
    return 0


def cmd_pkg(args):
    if args.op == "export":
        _print(pack.export_package(args.run_dir, args.input, args.out,
                                   args.label, include_assets=args.include_assets))
    elif args.op == "inspect":
        _print(pack.inspect_package(args.input))
    elif args.op == "merge":
        _print(pack.merge_packages(args.a, args.b, args.out, args.notes))
    return 0


def cmd_inbox(a) -> int:
    _print(store.inbox_scan(a.root))
    return 0


def cmd_eval(a) -> int:
    _print(store.evaluate_imported(a.root))
    return 0


def cmd_review(a) -> int:
    out = store.import_review(a.root, tolerance=a.tolerance)
    # the operator gets the readable table on stdout AND the written report
    _print({"ok": out["ok"], "report": out["report"], "totals": out["totals"],
            "packages": len(out["packages"]),
            "readable": out["readable"]})
    return 0


def cmd_seedfs(a) -> int:
    seeds = store.seed_candidates(a.root)
    path = Path(a.fixtures)
    if not (path.is_file() or path.is_dir()):
        raise ValueError(f"fixtures path not found: {path}")
    env = _custom_env(path)
    s = run_evolution(env, Path(a.run_dir), pop=a.pop, gens=a.gens, seed=a.seed,
                      budget=a.eval_budget,
                      seed_population=[c["genome"] for c in seeds])
    _print({**s, "seeded_from": len(seeds)})
    return 0


# ---------------- breeding (feature §12) ----------------

def _load_parents(spec_str: str, parents_file: str | None) -> list:
    if parents_file:
        blob = json.loads(Path(parents_file).read_text(encoding="utf-8"))
        parents = blob if isinstance(blob, list) else blob.get("parents", [])
        if not parents:
            raise ValueError(f"{parents_file} contains no parents")
        return parents
    if not spec_str:
        raise ValueError("pass --parents-candidates FILE or --preset PARENTS")
    return json.loads(spec_str)


def _breed_parser(sub):
    b = sub.add_parser("breed", help="run a full breeding session (crossover, "
                                       "mutation, gates, lineage)")
    b.add_argument("--parents-file", help="JSON file containing a parent "
                    "candidate list (e.g. exported best.json rows)")
    b.add_argument("--parents", help="inline JSON list of parent candidates")
    b.add_argument("--weights", default="50,50", help="comma percentages "
                   "(default 50,50); must total 100 unless --normalize")
    b.add_argument("--method", default="weighted_params",
                   help="weighted_params|categorical|module|ensemble_vote")
    b.add_argument("--preset", help="preset name (see 'methods' command)")
    b.add_argument("--mutation-prob", type=float, default=0.7)
    b.add_argument("--mutation-strength", type=float, default=0.5)
    b.add_argument("--offspring", type=int, default=2)
    b.add_argument("--generations", type=int, default=2)
    b.add_argument("--population-size", type=int, default=8)
    b.add_argument("--elite-size", type=int, default=4)
    b.add_argument("--holdout-every", type=int, default=2)
    b.add_argument("--seed", type=int, default=0)
    b.add_argument("--evals-limit", type=int, default=60)
    b.add_argument("--time-limit", type=float, default=900.0)
    b.add_argument("--dir", required=True, help="breeding work dir")
    b.set_defaults(fn=cmd_breed)

    ln = sub.add_parser("lineage", help="render the ASCII lineage tree")
    ln.add_argument("--dir", required=True)
    ln.add_argument("--rerun", help="lineage.jsonl event index to rerun "
                    "deterministically (0-based)")
    ln.set_defaults(fn=cmd_lineage)

    mt = sub.add_parser("methods", help="list crossover methods + presets, "
                        "with working/experimental/unsupported status")
    mt.set_defaults(fn=cmd_methods)


def _preset_config(name: str) -> dict:
    from firefly.mapart.breeding import PRESETS
    if name not in PRESETS:
        raise ValueError(f"unknown preset {name!r}; available: {sorted(PRESETS)}")
    return PRESETS[name]


# parent-weight defaults carrying preset semantics (percentages always shown
# on offspring records as effective values)
PRESET_WEIGHTS = {
    "50/50 crossover": [50.0, 50.0],
    "70/30 exploitation": [70.0, 30.0],
    "three-parent 50/30/20": [50.0, 30.0, 20.0],
    "elite-only breeding": [50.0, 50.0],
    "diversity-focused exploration": [50.0, 50.0],
    "crossover without mutation": [50.0, 50.0],
    "crossover with aggressive mutation": [50.0, 50.0],
    "ensemble parents (experimental)": [50.0, 50.0],
}


def cmd_breed(a) -> int:
    from firefly.mapart.breeding import Breeder
    preset = _preset_config(a.preset) if a.preset else {}
    method = a.method or preset.get("method") or "weighted_params"
    parents = _load_parents(json.loads(a.parents) if a.parents else None,
                            a.parents_file)
    if a.preset and not any(x in sys.argv for x in ("--weights",)):
        weights = PRESET_WEIGHTS.get(a.preset)
    else:
        weights = [float(x) for x in a.weights.split(",")]
    if weights is None:
        raise ValueError("this preset needs explicit --weights")
    normalize = bool(preset.get("normalize", True))
    br = Breeder(
        Path(a.dir), parents, weights=weights, normalize=normalize,
        method=method,
        mutation_prob=float(preset.get("mutation_prob", a.mutation_prob)),
        mutation_strength=float(preset.get("mutation_strength", a.mutation_strength)),
        offspring=int(preset.get("offspring", a.offspring)),
        generations=int(preset.get("generations", a.generations)),
        population_size=a.population_size,
        elite_size=int(preset.get("elite_size", a.elite_size)),
        selection=preset.get("selection", "elite"),
        holdout_every=a.holdout_every, seed=a.seed,
        evals_limit=a.evals_limit, time_limit_s=a.time_limit)
    br.start()
    br.thread.join(timeout=a.time_limit + 120)
    st = br.status()
    if st.get("crash"):
        raise RuntimeError(f"breeder crashed: {st['crash']}")
    _print({**st, "ok": True})
    return 0


def cmd_lineage(a) -> int:
    from firefly.mapart.breeding import lineage_tree, rerun_event
    out = {"tree": lineage_tree(Path(a.dir) / "lineage.jsonl")}
    if a.rerun is not None:
        path = Path(a.dir) / "lineage.jsonl"
        events = [json.loads(l) for l in
                  path.read_text(encoding="utf-8").splitlines() if l.strip()]
        ev = events[int(a.rerun)]
        # find each parent's config in the persisted population state
        state = json.loads((Path(a.dir) / "breeder.json").read_text(encoding="utf-8"))
        pop = state.get("population") or {}
        cfg = {pid: row["candidate"] for pid, row in pop.items()}
        parents_cfg = []
        for pid in ev["parents"]:
            if pid in cfg:
                parents_cfg.append(cfg[pid])
        if not parents_cfg:
            raise ValueError("parent configs not found in breeder.json population")
        rr = rerun_event({"parents_cfg": parents_cfg,
                          "weights": [p["pct"] for p in ev["pct"]],
                          "method": ev["method"], "seed": ev["seed"],
                          "child_id": ev["id"],
                          "dedupe_forced_mutation": ev.get("dedupe_forced_mutation", False),
                          "mutation_prob": ev.get("mutation_config", {}).get("prob", 0),
                          "mutation_strength": ev.get("mutation_config", {}).get("strength", 0)})
        out["rerun"] = {"idx": int(a.rerun), "matches_original": rr["matches_original"],
                        "id": rr["id"], "candidate": rr["candidate"]}
    _print(out)
    return 0


def cmd_methods(a) -> int:
    from firefly.mapart.breeding import PRESETS, describe_methods
    _print({"crossover_methods": describe_methods(), "presets": sorted(PRESETS),
            "ok": True})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="firefly.evolution.cli",
                                description="Standalone FireFly dithering optimization engine")
    p.add_argument("--version", action="version", version=f"firefly.evolution {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="evolve against fixture images")
    r.add_argument("--fixtures", required=True)
    r.add_argument("--run-dir", required=True)
    r.add_argument("--pop", type=int, default=12)
    r.add_argument("--gens", type=int, default=5)
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--eval-budget", type=int, default=48)
    r.add_argument("--resume", action="store_true")
    r.set_defaults(fn=cmd_run)

    pq = sub.add_parser("package")
    psub = pq.add_subparsers(dest="op", required=True)
    e = psub.add_parser("export")
    e.add_argument("--run-dir", required=True)
    e.add_argument("--input", required=True, help="fixture used for self-verification")
    e.add_argument("--out", required=True)
    e.add_argument("--label", default="engine-run")
    e.add_argument("--include-assets", action="store_true")
    i = psub.add_parser("inspect")
    i.add_argument("--input", required=True)
    m = psub.add_parser("merge")
    m.add_argument("--a", required=True)
    m.add_argument("--b", required=True)
    m.add_argument("--out", required=True)
    m.add_argument("--notes", default="")
    pq.set_defaults(fn=cmd_pkg)

    b = sub.add_parser("inbox", help="scan root/optimizer-inbox")
    b.add_argument("--root", required=True)
    b.set_defaults(fn=cmd_inbox)

    v = sub.add_parser("evaluate-imported", help="re-benchmark imported candidates locally")
    v.add_argument("--root", required=True)
    v.set_defaults(fn=cmd_eval)

    rv = sub.add_parser("review-imported", help="operator review: every sender "
                        "claim next to its local verification result")
    rv.add_argument("--root", required=True)
    rv.add_argument("--tolerance", type=float, default=0.05,
                    help="max |claim - local| delta treated as 'confirmed'")
    rv.set_defaults(fn=cmd_review)

    s = sub.add_parser("seed-from-store", help="seed a new run from locally-confirmed candidates")
    s.add_argument("--root", required=True)
    s.add_argument("--fixtures", required=True)
    s.add_argument("--run-dir", required=True)
    s.add_argument("--pop", type=int, default=8)
    s.add_argument("--gens", type=int, default=4)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--eval-budget", type=int, default=32)
    s.set_defaults(fn=cmd_seedfs)

    _breed_parser(sub)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except Exception as e:  # surface any fault as JSON + non-zero exit
        return _fail(e)


if __name__ == "__main__":
    sys.exit(main())
