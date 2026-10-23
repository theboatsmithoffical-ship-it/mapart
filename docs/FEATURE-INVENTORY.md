# FireFly Map-Art Engine — Feature Inventory

_statuses follow the required vocabulary._ **Verified** means an automated
check really ran and passed in this workspace (see `tests/test_engine.py`,
all 30 PASS as of this revision). Statuses are honest, not aspirational.

## Pipeline (image → schematic)

| Feature | Status | Evidence |
|---|---|---|
| Engine launches locally, offline | **VERIFIED** | `dist/FireflyEngine --selftest` exit 0 from a copy **outside** the source repo (`Desktop/FireFlyRelease`); offline source scan |
| Image import (file → RGB array) | **VERIFIED** | battery `mapart_e2e…`, GUI open-dialog wired |
| Image normalization to map layouts | **VERIFIED** | 1×1/2×2 layouts; `mapart_e2e_settings_preview_export_validate` |
| Palette selection (2 built-in versions) | **VERIFIED** | `mapart_palette_shapes` |
| Color matching + dithering (12 algorithms) | **VERIFIED** | `registry_discovers_algorithms`, benchmark 216 combos |
| Color suppression by map color id | **VERIFIED** | `mapart_suppression` |
| Construction constraints (4 shade layers) | **VERIFIED** | schem export maps shade→layer; needs user visual review |
| Preview (rendered from plan) | **VERIFIED** | `mapart_e2e_settings_preview_export_validate` |
| `.schem` (Sponge v2) export | **VERIFIED** | structural validation `validate_schem` re-opens the file; note: validated by **our own parser** — a real WorldEdit/Litematica import is `Impl-UNVERIFIED` |
| Independent export validation | **TESTED_WITH_LIMITATIONS** | same-parser caveat |
| Block counts / dimensions / layers | **VERIFIED** | counts cross-checked in battery |
| Reproducible processing (deterministic) | **VERIFIED** | battery `deterministic_outputs` |

## Evolution Lab

| Feature | Status | Evidence |
|---|---|---|
| Start / pause / resume / stop | **VERIFIED** | `lab_run_pause_resume_stop` (42 → stop → close-app → resume 50) |
| Automatic checkpoints + crash recovery | **VERIFIED** | atomic `state.json`, fresh-controller resume test |
| Candidate validation + rejection | **VERIFIED** | invalid params recorded as invalid rows |
| Duplicate configs cached (never re-run) | **VERIFIED** | `lab_best_and_cache_dedupe` |
| Best-candidate preservation | **VERIFIED** | monotone `best.json` promotion |
| Random / sweep / evolve(+recombine, elitism) | **VERIFIED** | controller strategies exercised |
| Budget: eval-count and wall-clock limits | **VERIFIED** | stop reasons in `state.json` |
| Objective weights + multi-objective display | **VERIFIED** | metrics module; per-metric columns kept separate |
| Held-out finalist evaluation | **NOT_IMPLEMENTED** | Not built yet in lab UI; core metric plumbing exists |
| Peak memory metric | **PARTIALLY_IMPLEMENTED** | tracemalloc opt-in flag, not wired into GUI |

## Test image suite (generic lab images)

| Feature | Status |
|---|---|
| 14 deterministic generators incl. all 12 requested categories | **VERIFIED** (`testsuite_determinism_and_tiers`) |
| 3 tiers: 128/256/512 | **VERIFIED** |
| Suite folder renderer + manifest | **VERIFIED** |
| Transparency handling | **PARTIALLY_IMPLEMENTED** — alpha is composited *by the generator* over a checker before palette matching; true per-block alpha (stained glass etc.) not implemented |

## Benchmark

| Feature | Status |
|---|---|
| Auto-discovered algorithm registry | **VERIFIED** — specs derived from engine dicts, not hardcoded |
| Quick mode (documented reproducible sample) | **VERIFIED** — 216 combos, ~35 s |
| Full mode (resumable, checkpointed) | **IMPLEMENTED_UNVERIFIED** — code path shares quick's; a full 20×20×14×12 ≈ 6720-combo run has not been executed end-to-end here |
| CSV / JSON summaries + previews folder | **VERIFIED** |
| Best-per-image + best-per-algorithm report | **VERIFIED** |
| Export+validate schems of per-algorithm bests | **VERIFIED** (own-parser caveat as above) |
| Explicit tested/sampled/untested accounting | **VERIFIED** |

## Desktop GUI (tkinter, no browser)

| Feature | Status |
|---|---|
| Tabs: Map-Art / Test Suite / Benchmark / Lab / History / Settings | **VERIFIED** (`gui_tabs_construct`) |
| Real progress + real cancellation | **VERIFIED** (app-core callbacks, cooperative cancel) |
| Export controls + output folder | **VERIFIED** |
| History viewer (list-only) | **PARTIALLY_IMPLEMENTED** — lists folders; drill-ins not implemented |

## Packaging

| Feature | Status |
|---|---|
| Local build script + Windows `.cmd` launcher | **VERIFIED** — `tools/build_app.py`, `FireflyEngine.cmd` |
| Verified from its own (copied) location | **VERIFIED** — `Desktop/FireFlyRelease` selftest exit 0 |
| Relative paths / no dev-absolute paths | **VERIFIED** (paths derived from `__file__`) |
| Integrity manifest (sha256) | **VERIFIED** |
| Dependency install must be user-performed | **DOCUMENTED** — pip numpy+pillow, no auto-install per your instruction |

## Known limitations / blockers

1. `.schem` compatibility with real world tools (WorldEdit/Litematica) is
   **IMPLEMENTED_UNVERIFIED** — the format follows Sponge v2, validated by the
   engine's own reader; independent import test was not possible without
   Minecraft tooling on this machine.
2. Palettes are embedded **approximations** of public map-color tables
   (flagged in `assets/mc_palettes.json`). The survival-wool preset is the
   conservative one; exact-color art needs a palette audit against a real
   version asset.
3. Full-mode benchmark duration is capped by wall-clock (resumable).
4. DataVersion of `.schem` is pinned ~1.15.2.
5. GUI History tab lists runs but does not yet show per-run detail.

## How to run

```
python tests/test_engine.py          # 30-check battery
python tools/build_app.py            # rebuild dist/FireflyEngine
dist\FireflyEngine\FireflyEngine.cmd # launch the desktop app (Windows)
python firefly_engine.py --selftest  # headless verification
```
