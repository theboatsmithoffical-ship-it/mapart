"""Evolution loop: mutation, crossover, tournament selection, diversity cap,
evaluation budget, per-generation checkpoints and resume."""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

from . import dither
from . import genome as gmod
from . import synth


def evaluate(env, genome: dict) -> tuple:
    gid = gmod.genome_id(genome)
    per = {}
    for name, img in env.fixtures:
        out = dither.dither_image(img, genome, synth.stable_seed(env.suite_id, name, gid))
        per[name] = dither.score_pair(img, out)
    mean = round(sum(per.values()) / len(per), 6)
    return mean, per


def run_evolution(env, run_dir, pop=8, gens=5, seed=0, budget=64,
                  seed_population=None, resume=False):
    """Run a deterministic evolution cycle. `budget` caps *new* evaluations
    for this invocation. With resume=True and no state.json, the latest
    checkpoint continues the run."""
    run_dir = Path(run_dir)
    (run_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
    (run_dir / "cache").mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    cache_file = run_dir / "cache" / "candidates.jsonl"

    rng = random.Random(seed)

    if resume and state_path.exists():
        st = json.loads(state_path.read_text(encoding="utf-8"))
        return {"ok": True, "resume_noop": True, "status": st.get("status"),
                "run_dir": str(run_dir),
                "best_score": (st.get("best") or {}).get("score")}

    start_gen = 0
    individuals = []
    if resume:
        ckpts = sorted((run_dir / "checkpoints").glob("gen_*.json"),
                       key=lambda p: int(p.stem.split("_")[1]))
        if ckpts:
            blob = json.loads(ckpts[-1].read_text(encoding="utf-8"))
            individuals = list(blob["individuals"])
            start_gen = blob["gen"] + 1
    else:
        # a fresh run clears this run-dir's engine artifacts
        state_path.write_text(json.dumps({"status": "running"}), encoding="utf-8")
        cache_file.write_text("", encoding="utf-8")

    if not individuals:
        individuals = [{"genome": g, "score": None} for g in (seed_population or [])]
        while len(individuals) < pop:
            individuals.append({"genome": gmod.random_genome(rng), "score": None})
    individuals = individuals[:pop]

    evals_used = 0

    def _ensure(ind):
        nonlocal evals_used
        if ind["score"] is None:
            if evals_used >= budget:
                return
            score, per = evaluate(env, ind["genome"])
            ind["score"], ind["fixture_scores"] = score, per
            evals_used += 1
            rec = {"id": gmod.genome_id(ind["genome"]), "genome": ind["genome"],
                   "score": score, "fixture_scores": per}
            with cache_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, sort_keys=True) + "\n")

    for gen in range(start_gen, gens):
        for ind in individuals:
            _ensure(ind)
        scored = sorted((i for i in individuals if i["score"] is not None),
                        key=lambda i: i["score"])
        ranked = scored + [i for i in individuals if i["score"] is None][:pop - len(scored)]
        best = scored[0] if scored else None
        blob = {
            "gen": gen,
            "evals_used": evals_used,
            "best": ({"genome": best["genome"], "score": best["score"]} if best else None),
            "individuals": [{"genome": i["genome"], "score": i["score"]} for i in ranked],
        }
        (run_dir / "checkpoints" / f"gen_{gen}.json").write_text(
            json.dumps(blob, sort_keys=True), encoding="utf-8")
        if gen == gens - 1:
            break

        if not scored:
            children = [{"genome": gmod.random_genome(rng), "score": None}
                        for _ in range(pop)]
            individuals = children
        else:
            # keep the best half as elites; replace the rest with offspring
            n_keep = max(1, pop // 2)
            parents = scored

            def pick():
                cands = rng.sample(parents, min(3, len(parents)))
                return min(cands, key=lambda i: i["score"])

            children = []
            while len(children) < pop - n_keep:
                roll = rng.random()
                if roll < 0.5:
                    children.append({"genome": gmod.mutate(pick()["genome"], rng), "score": None})
                elif roll < 0.9:
                    children.append({"genome": gmod.crossover(pick()["genome"], pick()["genome"], rng),
                                     "score": None})
                else:
                    children.append({"genome": gmod.random_genome(rng), "score": None})

            individuals = scored[:n_keep] + children

        # diversity floor: keep at least 3 distinct algorithms in pop >= 3
        algos = {i["genome"]["algorithm"] for i in individuals}
        if pop >= 3 and len(algos) < 3:
            i = 0
            while len(algos) < 3 and i < len(individuals):
                p = dict(individuals[i]["genome"])
                alts = [a for a in dither.ALGORITHMS if a not in algos]
                p["algorithm"] = rng.choice(alts)
                individuals[i]["genome"] = gmod.sanitize(p)
                algos = {x["genome"]["algorithm"] for x in individuals}
                i += 1

    scored = sorted((i for i in individuals if i["score"] is not None),
                    key=lambda i: i["score"])
    best = scored[0] if scored else None
    summary = {
        "ok": True,
        "status": "budget_limited" if evals_used >= budget else "complete",
        "run_dir": str(run_dir),
        "pop": pop, "gens": gens, "seed": seed, "eval_budget": budget,
        "evals_used": evals_used,
        "generations_completed": gens,
        "best": None if best is None else {
            "id": gmod.genome_id(best["genome"]), "score": best["score"],
            "genome": best["genome"], "fixture_scores": best.get("fixture_scores"),
        },
        "suite": {"id": env.suite_id, "fixtures": env.hashes},
    }
    state_path.write_text(json.dumps({
        "status": summary["status"],
        "config": {"pop": pop, "gens": gens, "seed": seed, "eval_budget": budget,
                   "suite_id": env.suite_id},
        "suite": summary["suite"],
        "evals_used": evals_used,
        "best": summary["best"],
        "fixture_paths": env.sources,
    }, indent=2), encoding="utf-8")
    return summary
