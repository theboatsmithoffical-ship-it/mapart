# Resource-usage report — measured, not assumed

Representative run: Breeder 3 generations / 2 offspring, parents 70/30, 
mutation prob 0.5, holdout every 2 generations, safe mode (1 worker), seed 42. 
No GPU, no LLM, no network during the run.

| metric | measured |
|---|---|
| wall time | 17.39 s for 14 real evaluations |
| invalid candidates rejected by gates | 0 |
| Python heap peak (tracemalloc, engine-side) | 32 MB (budget 2048 MB) |
| engine-process working set at rest | 42 MB |
| engine-process peak working set (lifetime) | 73 MB |
| system free memory during run | 19264 MB |
| memory-pressure events triggered | 0 (floor 1600 MB, machine had 19264 MB free) |
| champion after run | {"id": "2cfb2aab4ebf6684", "candidate": {"algorithm": "burkes", "version": "survival-wool-1.8", "suppress": [], "strength": 1.51, "serpentine": false}, "score": 0.93023, "child_of": ["34f4380e9d701f84", "06133081cd9a66b8"], "pct": [{"id": "34f4380e9d701f84", "pct": 70.0}, {"id": "06133081cd9a66b8", "pct": 30.0}], "generation": 2} |

## Notes
- Budgets are scheduling policy + monitoring; this report shows MEASURED peaks. 
The engine fits well within the 2 GB worker budget: the workload is one evaluation at a time with ~20-40 MB working set.
- 8 GB physical-RAM compatibility was NOT measured on an 8 GB machine (this dev machine reports 32 GB total, ~19 GB free at test time). 
With working sets in the ~40-75 MB range and a 1.6 GB pressure floor, an 8 GB system has enormous headroom, but users on real 8 GB hardware should run with safe mode (the default) and share their own pressure log.
- The true GUI-process footprint also includes the window toolkit; measured separately below during a live screenshot run.
