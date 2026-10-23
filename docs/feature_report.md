# Feature report — breeding, desktop runtime, datasets, planner

Everything below ran as real commands in this workspace; measurements and
exit codes come from those runs (see the listed artifacts).

## Implementation status by feature

### 1. Genetic breeding (§12) — working
- Module: [src/firefly/mapart/breeding.py](../src/firefly/mapart/breeding.py)
- Percentage-based multi-parent mixing with validation + normalization; the
  effective percentages are recorded on every offspring and shown in the GUI.
- Crossover methods:
  | method | status | evidence |
  |---|---|---|
  | weighted_params | **working** | 0.8@75% + 0.4@25% = 0.7 exact (test_breeding t02) |
  | categorical | **working** | whole-component draws, 90/10 probabilities verified over 400 trials (t03) |
  | module | **working** | per-component draws with recorded fallback adaptation (t04) |
  | ensemble_vote | **experimental** | real per-pixel weighted voting; NOT promotable into the champion pool (gate rejects, t08) |
  | code_crossover | **unsupported** | raises NotImplementedError — no source-evolution pipeline exists; text is never spliced |
- Elite pool with diversity floor, tournament/diversity/manual selection,
  mutation-after-crossover with per-key logs and forced-dedupe anti-collapse.
- Workflow gates: screening → dev benchmark → comparison vs parents and
  champion → **buildability gate (real schematic validation) before promotion**.
- Holdout: aggregate-only results in holdout.jsonl; optimizer state never
  contains holdout data (t11 in test_gui_runtime).
- Tests: `tests/test_breeding.py` — **39 checks, exit 0**.
- CLI: `python -m firefly.evolution.cli breed|lineage|methods` (verified exit 0;
  deterministic rerun reports `matches_original: true`).

### 2. Desktop GUI + 8 GB runtime — working (measured)
- Pages: Control Center, Map-Art, Test Suite, Benchmark & Test Center,
  Evolution Lab (incl. breeding studio), Visual Studio, Image Library &
  Datasets, Resources, History, Configuration & Recovery — all wired to live
  controllers (`labcore.LabController`, `breeding.Breeder`), no GUI-side
  business-logic duplication.
- Framework decision: tkinter retained (documented in
  [desktop_runtime_plan.md](desktop_runtime_plan.md)); PySide6 evaluated and
  rejected for this codebase.
- Resource manager: [src/firefly/mapart/resourcemanager.py](../src/firefly/mapart/resourcemanager.py)
  — ctypes sysinfo (honest "unavailable" off-Windows), safe/balanced/performance
  profiles, pressure admission gates wired into lab + breeder loops, disposable
  cache cleanup that never touches state/checkpoints/champions.
- Measured (see [resource_usage_report.md](resource_usage_report.md)):
  representative 3-generation run = **17.4 s wall, 14 evals, 32 MB heap peak,
  42 MB working set / 73 MB peak** — far inside the 2 GB worker budget.
- Screenshots: [screenshots/](../screenshots) — 10 real per-tab captures from
  the running app (`tools/capture_tabs.py` regenerates them).

### 3. Image library & dataset builder — working
- Module: [src/firefly/mapart/imagelibrary.py](../src/firefly/mapart/imagelibrary.py)
- Discovery: **Wikimedia Commons (No API key)** — real call verified: 4
  candidates for "mountain landscape", license + author metadata retained
  (e.g. `Public domain`), attribution stored.
- Download: verified real download → hash + PIL integrity check → capability
  analysis (tags, palette coverage 0.92) → manifest entry → re-download
  dedupes. Resume via `.part` files; corrupt/missing files are reported, not
  crash.
- Datasets: dev / stress / holdout / synthetic purposes; membership changes
  bump `dataset_revision` and preserved revision snapshots are comparable
  (`compare_revisions` shows membership_changed), so experiments can be
  attributed to a revision.
- Unsplash: requires an API key — documented in the GUI (`UNSPLASH_NOTICE`);
  never required for basic functionality.
- IMPORTANT Windows fix surfaced by real downloads: deep workspace roots
  overflow MAX_PATH(260) with long file names — downloaded names are now
  sanitized to ~46 chars (`_safe_name`), which is what made the download work
  from this 200+-char workspace root. On the user's normal shallow folders
  this is not an issue, but as installed here it mattered.

### 4. Autonomous planner — working
- Module: [src/firefly/mapart/planner.py](../src/firefly/mapart/planner.py)
- Weakness analysis from recorded history; one-factor-at-a-time proposals with
  hypothesis + estimated cost (from measured `process_time_s` medians) +
  priority; conclusions state observed deltas and explicitly refuse causal
  claims.
- Specialist populations (photographs/pixel-art/gradients/...) persisted with
  champions; component migration uses the breeding crossover machinery;
  worse candidates are never promoted.
- Failure classification + auto-generated regression tests from real failing
  rows; controlled-comparison factor field; stopping conditions incl. stall →
  "measured plateau" (never a global-optimum claim).
- Tests: all planner acceptance checks live in `tests/test_gui_runtime.py`
  (t03-t10), exit 0.

## Test status (this session, all exit 0)
| battery | coverage | result |
|---|---|---|
| tests/test_breeding.py | 12 mandated breeding checks + extras | **39 PASS** |
| tests/test_gui_runtime.py | GUI pages, lifecycle, offline, memory, pressure, datasets, planner | **20 PASS** |
| tests/test_engine.py | engine pipeline, exchange, portable build | **30 PASS** |
| tests/test_tools.py | tools suite incl. nevergrad | **15 PASS** |

## Known limitations / not done
- Visual Studio pixel inspector shows decoded RGB pixels; per-block
  Minecraft-tile mapping of the challenger is by map_color ID (no per-block
  sprite tiles rendered).
- Planner proposals are executed one at a time by the GUI button; fully
  autonomous unattended planning loops are not scheduled automatically.
- Holdout-refresh recommendation (§6 dataset spec) is computed in
  `gaps_report` but not yet wired to automatic discovery searches.
- No hard OS-level memory cap: budgets are policy + monitoring (measured
  peaks reported instead).
- The 8 GB compatibility was verified on THIS machine (32 GB visible,
  ~19 GB free at test time) via measured working sets, not on an 8 GB box.
