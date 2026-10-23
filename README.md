# FireFly Dithering Optimization Engine (standalone)

This tree is the **standalone engine** reconstructed from the exchange demo
contract (`tools/e2e_exchange_demo.py`). It is deliberately GUI-independent:
nothing here opens FireFly Map Studio, the JVM, or any jar.

## Layout
- `src/firefly/evolution/` — engine package
  - `dither.py` — 24-color palette, 11 algorithms (Floyd–Steinberg, Atkinson,
    Burkes, Sierra-lite, JJN, Stucki, Bayer 2/4/8, threshold, random), scoring
  - `genome.py` — parameter-schema enforcement + content-hash ids
  - `evolve.py` — deterministic evolution with eval budget, checkpoints, resume
  - `pack.py` — `.ffevopack` strict reading/export/merge/inspect
  - `store.py` — inbox/quarantine/dedupe + local re-benchmark + seeds
  - `cli.py` — `python -m firefly.evolution.cli`
- `tools/` — `build_portable.py`, `e2e_exchange_demo.py`
- `tests/test_engine.py` — 15-case battery (`python tests/test_engine.py`)

## Quick start
```
python -m firefly.evolution.cli run --fixtures demo.png --run-dir runs/a --pop 6 --gens 2 --seed 42 --eval-budget 8
python -m firefly.evolution.cli package export --run-dir runs/a --input demo.png --out a.ffevopack --include-assets --label demo
python tools/e2e_exchange_demo.py            # full two-engine offline exchange
python tests/test_engine.py                  # evidence battery (15 checks)
python tools/build_portable.py               # build portable/firefly-portable
portable/firefly-portable/firefly_portable.py doctor
portable/firefly-portable/firefly_portable.py demo [WORKROOT]
```

## Security stance
Packages are zips of JSON parameters + PNG assets, never code: foreign members
and traversal paths are quarantined, genome ids are content-verified, sender
scores are stored as claims only and always re-benchmarked locally before use.

## Desktop app — runbook (Map-Art engine + Evolution Lab + more)

One-command launch of the FULL desktop application (Map-Art, benchmark,
Evolution Lab with breeding, Visual Studio, Image Library, Resources,
Config & Recovery):

```
python src/firefly/mapart/gui.py        # or: python firefly_engine.py gui
```

The 8 GB low-memory profile is the default: resource mode `safe` (1 eval
worker, 1.6 GB pressure floor, small image caps, frequent checkpoints).
Switch modes in the Resources tab or:

```
python -c "from firefly.mapart.resourcemanager import init_manager; "\
         "init_manager('mapart-data').set_mode('balanced')"
```

Breed via CLI (offline, deterministic, records lineage):
```
python -m firefly.evolution.cli breed --parents-file parents.json --preset "70/30 exploitation" --dir mapart-data/breeding
python -m firefly.evolution.cli lineage --dir mapart-data/breeding --rerun 0
python -m firefly.evolution.cli methods      # working/experimental/unsupported status
```

Operator step for a shared champion package: after `inbox` + `evaluate-imported`, review every sender claim next to its locally verified result (reads suite origin, collapses duplicate claims, writes `<root>/import_review.md`):

```
python -m firefly.evolution.cli review-imported --root operator-ws
```

Dataset rebuild + offline acceptance (§7 scenario, no images download implicitly):

```
python tools/rebuild_dataset.py --data-dir mapart-data     # explicit download+synthetic, prints JSON report
python tools/acceptance_offline.py                          # full offline run under a hard network guard
```

Asset QA tooling (offline; see `docs/asset_quality_milestone.md`):
```
python tools/asset_health.py --data-dir mapart-data --out docs/asset_health_report.md
python tools/verify_manifest.py --library mapart-data
python tools/dataset_sets.py list            # dev / validation / holdout / stress
python tools/dataset_sets.py assign --id img_x --set holdout --confirm --reason "..."
```

Key test batteries:
```
python tests/test_breeding.py      # breeding spec (12 mandated checks) — 39 checks
python tests/test_gui_runtime.py   # desktop runtime + datasets + planner — 20 checks
python tests/test_engine.py
python tests/test_tools.py
python tests/test_assets.py        # asset catalog, sets, limits, offline checks — 14 checks
```

Details: `docs/feature_report.md` (implementation status + limitations),
`docs/resource_usage_report.md` (measured memory), `docs/desktop_runtime_plan.md`
(framework decision). Screenshots of all live tabs: `screenshots/`.

## Honesty notes
- Documented version check: `python -m firefly.evolution.cli --version`
- The `random` algorithm is seeded per (suite, fixture, genome) so it is
  reproducible while still per-candidate.
- See `PORTABLE.md` inside the built distribution for limits (palette is a
  fixed 24-color set, not Minecraft-accurate).
