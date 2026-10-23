# Evolution Laboratory readiness audit

Generated 2026-10-21T04:22:54.044172+00:00  ·  gate: **CONDITIONAL**  ·  pass 21 / fail 0 / notes 2 (details: `docs/evolution_lab_readiness.json`)

## baseline (2P/0F/0N)
- [PASS] **test_engine_battery** — exit 0
- [PASS] **test_tools_battery** — exit 0

## integrity (3P/0F/0N)
- [PASS] **checkpoint_persists** — state evaluated=6 cache=6
- [PASS] **restart_recovery** — restored evaluated=6
- [PASS] **interrupted_run_crash_record** — KeyError: 'no-such-item'

## isolation (2P/0F/0N)
- [PASS] **invalid_settings_rejected** — Settings ValueError
- [PASS] **malformed_package_rejected** — foreign archive member rejected: 'payload.py'

## calibration (5P/0F/2N)
- [PASS] **invalid_never_promoted** — weighted(valid=False) -> None; Lab promotes only score-bearing entries
- [PASS] **worse_offspring_rejected** — score bad 0.8200 > worse 0.7800 -> best stays
- [PASS] **cost_independent** — cost 0.02 vs 0.5, visual terms unchanged
- [PASS] **categorical_inheritance** — 4 recombinations contain exactly parental alleles
- [NOT_APPLICABLE] **percentage_crossover** — engine implements per-key 50/50 categorical recombination; a continuous contribution-percentage crossover does not exist in this codebase and would be new work, not calibration
- [NOT_IMPLEMENTED] **pareto_ranking** — weighted scalar objective only; multi-objective metrics are reported independently, but Pareto nondomination ranking is not implemented
- [PASS] **metric_sensitivity** — oklab_error exact 0.0000, +40 shifted 0.0057

## rehearsal (3P/0F/0N)
- [PASS] **small_experiment** — {"evals": 5, "invalid": 0, "cache_hits": 5, "wall_s": 0.3, "best": 0.917604}
- [PASS] **cancellation_latency** — stop() returned in 0.11s, worker stopped cleanly
- [PASS] **peak_parent_memory** — parent-process traced peak 0.0 MB (candidates are transient)

## workflow (6P/0F/0N)
- [PASS] **1_synthetic_master** — sha 8928f4e26fa2 offline
- [PASS] **2_screening_benchmark** — 72 combos evaluated, best per image: {'grad-smooth': 'sierra_lite|survival-wool-1.8|1.6|grad-smooth'}
- [PASS] **3-8_candidates_breed_promote** — lab best 0.9176 (bayer2)
- [PASS] **9_history_persisted** — 4 rows in history.jsonl
- [PASS] **10_restart_recovery** — restored evaluated=4, cache=4
- [PASS] **11-12_reproduce_validate** — schem synthetic_master_128x128.schem; placed=16384; palette=13

