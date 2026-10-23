# Milestone 0 — Repository audit and baseline

Date: 2026-10-23
Scope: establish what exists, what runs, what fails, and which blockers must be
fixed before production-readiness work. **No source code was changed in this
milestone.** Evidence logs and the machine-readable record are in
`reports/production_readiness/` (see `BASELINE_RESULTS.json`).

Status words: **VERIFIED** = the named command ran here and passed;
**BLOCKED** = cannot be checked in this environment; **UNVERIFIED** = asserted
by prior docs but not confirmed in this audit.

## 1. Repository state

| item | value |
|---|---|
| revision | `9d8c272` (`chore: ignore generated run outputs`) |
| branch | `main` |
| working tree | **dirty** — uncommitted asset-quality milestone from the previous task (11 modified + 7 new files); `data/blocks.json` was modified before this task |
| environment | Windows 11, Python 3.14.8, numpy 2.5.4, Pillow 12.3.0 (`env.json`) |
| entry points | `firefly_engine.py` (GUI / `--selftest` / `--process`), `src/firefly/mapart/gui.py` (Tkinter), `tools/*.py`, `tests/*.py` |
| test files | 8 batteries, run individually; no pytest configuration, no network in any battery |

The audit ran against the working tree, not a clean commit. The uncommitted
changes were the asset-quality milestone (catalog, sets, limits, health report)
and do not touch the rendering, layout, export or palette code audited below.
After the baseline was recorded, that work was committed as `e6d5806..aebd4e1`
and pushed; the sources measured here are byte-identical, so the evidence still
applies, but the recorded revision above is the pre-commit state.
`data/blocks.json` is an unrelated pre-existing local modification and was not
touched or committed.

## 2. Commands executed (all from the repository root)

| command | result | log |
|---|---|---|
| `python tests/test_mapart_spec.py` | **exit 0**, 56 checks | `logs/test_mapart_spec.log` |
| `python tests/test_rendermodel.py` | **exit 0**, 15 checks | `logs/test_rendermodel.log` |
| `python tests/test_tools.py` | **exit 0**, 23 checks | `logs/test_tools.log` |
| `python tests/test_assets.py` | **exit 0**, 14 checks | `logs/test_assets.log` |
| `python tests/test_repo_audit.py` | **exit 0**, 14 checks | `logs/test_repo_audit.log` |
| `python tests/test_engine.py` | **exit 0**, 38 checks | `logs/test_engine.log` |
| `python tests/test_gui_runtime.py` | **exit 0**, 41 checks | `logs/test_gui_runtime.log` |
| `python tests/test_breeding.py` | **exit 1**, 38 passed / 1 failed (pre-existing, §6) | `logs/test_breeding.log` |
| `python firefly_engine.py --selftest` | **exit 0** (`"selftest": "ok"`, 3 builds + lab) | `logs/selftest.log` |
| `python tools/acceptance_offline.py` | **exit 0**, 10/10 steps, 0 network attempts, 0 downloader calls | `logs/acceptance_offline.log` |
| `python tools/release_sweep.py --fast` | **exit 0**, 72/72 combinations, 48.2 s | `logs/release_sweep_fast.log` |
| `python tools/quality_benchmark.py --quick --out reports/production_readiness/quality_benchmark_quick.json` | **exit 0**, 28 runs | `logs/quality_benchmark_quick.log` |
| `python tools/audit_palette_accuracy.py --version survival-wool-1.8` | **exit 2** — refused, no simulator | `logs/palette_audit.log` |
| `python reports/production_readiness/probe_export.py` | **exit 0** — 4 exports + independent reload | `probe_export.json` |

Totals: the eight batteries contain **240 checks — 239 passed, 1 failed**.
Separately, acceptance ran 10/10 steps and the fast release sweep 72/72
combinations. The single failing check is reproduced at the committed HEAD in
a clean worktree (§6), i.e. it is not introduced by this audit.

### Skipped / blocked

- **Palette audit / real render comparison** — `tools/audit_palette_accuracy.py`
  refuses without `--simulator` / `MAPART_SIMULATOR`. No simulator is installed.
  This is a hard blocker for any in-game colour claim (F-05).
- **In-game / Litematica validation** — no Minecraft client, no Litematica, no
  simulator on this machine. The files were validated by the project's own
  independent NBT parsers only.
- **`tools/readiness_audit.py`** — not run: it rewrites the tracked
  `docs/evolution_lab_readiness.*` files and is a long real-experiment audit.
  Its engine/lab behaviours are already exercised by `test_engine`,
  `test_gui_runtime` and `acceptance_offline`. This is recorded as not-run, not
  as a pass.

## 3. Production pipeline (traced in the current code)

**Input → pixels**
1. `firefly_engine.py` / GUI / tools call `appcore.process`
   ([appcore.py:133](src/firefly/mapart/appcore.py#L133)).
2. `mapart.load_image_rgba` → `apply_transparency` (ignore/matte/binary) →
   `preprocess` (brightness, contrast, saturation, gamma) → `normalize` to
   `maps_w*128 × maps_h*128` (`cover`/`contain`/`stretch`/`crop`).
3. `load_palette` expands each base colour × 4 shade codes through the
   authoritative `rendermodel.shade_rgb` (integer truncation), and filters
   shades by the version rule ([mapart.py:86](src/firefly/mapart/mapart.py#L86),
   [mapart.py:429](src/firefly/mapart/mapart.py#L429)).
4. `mapart.match_and_dither` chooses one `(block, shade)` per pixel (RGB / Lab /
   CIEDE2000 metric, 11 dithering methods, suppression, block costs, optional
   neighbour polish).

**Plan → construction**
5. For every 128×128 tile, `rendermodel.solve_heights` converts the intended
   shade codes into a staircase heightmap and reports which pixels are
   realizable ([appcore.py:181](src/firefly/mapart/appcore.py#L181),
   [rendermodel.py:244](src/firefly/mapart/rendermodel.py#L244)).
6. The preview is generated from the **realized** shades × base colours, not
   from the intended ones. Quality metrics consume that preview.

**Export → validation**
7. `appcore._build_layers` places each pixel's block at its solved height
   (`frame`) and optionally fills every column below with the support block
   (`solid`) ([appcore.py:239](src/firefly/mapart/appcore.py#L239)).
8. `schem.export_schem` (Sponge v2), `export_litematic` (Litematica v6),
   `export_structure_nbt` (vanilla structure) write the files
   ([schem.py:21](src/firefly/mapart/schem.py#L21),
   [schem.py:182](src/firefly/mapart/schem.py#L182)).
9. `schem.validate_*` re-parse the NBT with an independent reader, check
   dimensions, palette prefix/range, decoded volume and block counts.
   `full_build` additionally compares the `.schem` placed-block count with the
   pixel plan ([appcore.py:400](src/firefly/mapart/appcore.py#L400)).

**Consumers of the authoritative model (VERIFIED by inspection):** palette
expansion, matcher reachability, construction solver, preview, quality metrics,
export heightmap and both report provenance blocks all call
`rendermodel`. The legacy `mapart.preview_rgb` / `layer_plan` paths still exist
but are not used by the exported plan (F-09).

## 4. Export validation probe

`reports/production_readiness/probe_export.py`, fixture
`fixtures_engine/real_e2e.png`, `bayer4`, strength 1.0, 1×1 map, both palettes
and both construction modes. Files were reloaded by the independent
`schem.validate_schem` / `validate_litematic` parsers; all four pairs passed
structural validation at the expected `[128, h, 128]` size.

| palette | mode | max height | infeasible px | intended≠realized | floating gravity blocks | `.schem` placed | `.litematic` |
|---|---|---|---|---|---|---|---|
| survival-wool-1.8 | frame | 90 | 2 / 16384 | 2 | 0 | 16384 | valid |
| survival-wool-1.8 | solid | 90 | 2 / 16384 | 2 | 0 | 263253 | valid |
| map-full-approx-1.15 | frame | 60 | **3447 / 16384 (21.04 %)** | **3444** | **1439** | 16384 | valid |
| map-full-approx-1.15 | solid | 60 | **3447 / 16384 (21.04 %)** | **3444** | 0 | 155586 | valid |

Observations:

- Structurally valid ≠ buildable: frame mode places gravity blocks (sand /
  red_sand) in mid-air; no validator rejects it.
- The construction solver cannot realize a large fraction of the
  `map-full-approx-1.15` plan; the export proceeds and only records the count.
- `.litematic` validation does not expose or compare a placed-block count
  against the plan (only non-air volume).
- Runtime: each 1×1 build including export and validation took 1.1–1.4 s.

## 5. Current quality benchmark (quick subset)

`python tools/quality_benchmark.py --quick` → exit 0, **28 runs**, palette
`survival-wool-1.8`, small tier, metrics rgb/lab, algorithms none/floyd-steinberg.
It measures the engine's own realized preview against the post-normalize source
using the engine's CIEDE2000/OKLab; it is not an in-game measurement.

| config | n | dE2000 mean | p95 | % ≤ dE10 | wall s | peak MB |
|---|---|---|---|---|---|---|
| lab \| floyd_steinberg | 7 | 12.7144 | 22.56 | 35.41 | 3.93 | 29.2 |
| lab \| none | 7 | 11.0180 | 19.00 | 40.64 | 0.26 | 29.2 |
| rgb \| floyd_steinberg | 7 | 12.5798 | 23.73 | 35.15 | 1.93 | 13.3 |
| rgb \| none | 7 | 11.5619 | 21.97 | 42.18 | 0.23 | 13.3 |

Exported artifact from the benchmark: `.schem` and `.litematic` both validate,
size `[128, 120, 128]`, 16384 blocks. On this pixel-error metric the
non-dithered variants score lower (dithering trades per-pixel error for
perceived texture); this is recorded, not treated as an optimisation target.
Full artifacts: `quality_benchmark_quick.json|md`, `logs/quality_benchmark_quick.log`.

## 6. Findings and blockers

Severity: **P0** blocks the primary objective, **P1** required before claiming
production readiness, **P2** cleanup/accuracy of documentation.

| id | sev | finding | evidence | affected |
|---|---|---|---|---|
| F-01 | P0 | **Palette base colours are unverified approximations.** | `assets/mc_palettes.json` declares `verification_status: approximation`; survival white wool is `[221,221,221]` (published 1.8.9 MapColor: 255,255,255) and magenta `[219,111,183]` (178,76,216); prior report §11 already flags contradictions. | palette, preview, metrics, all exports |
| F-02 | P0 | **Palette maps blocks to inconsistent/unavailable map colours.** | `map-full-approx-1.15` assigns map colours 18 `[140,224,231]` and 20 `[255,181,170]` both to `minecraft:red_sand`; ids 58/59 use `minecraft:warped_wart_block`, a 1.16 block, in a palette declaring 1.15.2. | palette, export validity |
| F-03 | P0 | **Default frame export floats gravity blocks.** | 1439 sand/red_sand blocks with air directly below in the 1×1 map-full probe; `Settings.construction` defaults to `frame`; validators accept the file. | construction, export |
| F-04 | P0 | **Infeasible shades are exported without a gate.** | 3447/16384 (21.04 %) infeasible pixels and 3444 intended-vs-realized mismatches in map-full; `Settings.strict_geometry` is accepted but never read. | solver, fidelity, export |
| F-05 | P0 | **No real renderer/client validation.** | palette audit exits 2 (no simulator); render validator requires `MAPART_SIMULATOR`; no client integration. | accuracy claims, palette, render model |
| F-06 | P1 | **Validation is structural, not geometry agreement.** | validators check size/prefix/counts; no block-for-block reload compare, no litematic count compare, no block-state properties. | export validation |
| F-07 | P1 | **No version manifest; hardcoded DataVersion.** | `schem.DATA_VERSION = 2578` for both palettes; 1.8 export carries 1.15.2 `MinecraftDataVersion`; no file pins MC/litematic/palette/model versions. | metadata, compatibility |
| F-08 | P1 | **Only block names are serialized.** | palette includes `minecraft:oak_leaves`; no `Properties` for persistent/decay state. | block states, build durability |
| F-09 | P1 | **Legacy idealized shade-layer paths remain.** | `mapart.layer_plan` / `preview_rgb` still populate `res.frames`; GUI says "Layers: 4 (one per shade depth)" while export is a staircase. | preview/UI, maintenance |
| F-10 | P2 | **Some tests do not represent production behaviour; docs stale.** | structural-only spec checks; stub-simulator tool test (documented); docs claim 57 spec checks / 39-of-39 breeding vs 56 / 38-of-39 now. | tests, docs |

## 7. Existing vs newly introduced failures

- **Pre-existing:** `tests/test_breeding.py::t06_offspring_can_outperform_both_parents`.
  Current scores: children `[0.670618, 0.671212, 0.602336, 0.663181]`,
  parents `[0.673148, 0.672022]`. Running the same test at the committed HEAD
  in a clean git worktree produced the **identical** numbers and failure.
  Not introduced by this audit. Optimizer tuning is out of scope for this
  milestone; it remains a known failure.
- **Blocked, not failures:** palette audit and in-game validation (no simulator
  or client).
- **Newly introduced by this audit:** none — no source files were changed.

## 8. Tests that do not prove production correctness

- `test_mapart_spec` verifies NBT structure, dimensions, counts and determinism;
  it does not verify the rendered colours or whether the volume can be built.
- `test_tools` uses a real helper script as a simulator (documented in the test
  header); it proves the audit plumbing, not Minecraft fidelity.
- `test_engine`/`test_gui_runtime` exercise the pipeline end-to-end but against
  locally generated fixtures and the same render model under test.
- No test checks gravity, fluids, unsupported blocks, block states, or the
  infeasible-pixel rate.
- Documentation in `docs/release_readiness_v1.md` and
  `docs/spec_requirements_report.md` records limitations honestly, but its
  counts and "ALL PASSED" claims are stale relative to this run.

## 9. Limitations of this baseline

- The working tree is dirty (prior milestone). Results describe this tree, not
  commit `9d8c272` alone.
- Validation is offline only; no Minecraft client or render simulator exists
  here, so no visual/in-game claim is made.
- The export probe covers one 1×1 fixture and one algorithm; it is a
  defect-discovery probe, not a statistical benchmark.
- Near-duplicate/asset findings from the previous milestone are unrelated and
  not repeated here.
- Peak memory and long-run stability were not re-measured in this milestone;
  the previous reports' numbers are treated as **UNVERIFIED** until re-run.

## 10. Files changed during Milestone 0

Only new evidence under `reports/production_readiness/`:

- `BASELINE_REPORT.md`, `BASELINE_RESULTS.json`, `env.json`,
  `probe_export.py`, `probe_export.json`, `build_baseline_json.py`,
  `probe_out/`, `logs/`.

No production, test or documentation source was edited. `reports/` is
git-ignored by the repository's `.gitignore`.

## 11. Consequence for the next milestones

Milestone 1 must establish a version-pinned, source-checked palette and
rendering model before any accuracy claim; F-01/F-02/F-05/F-07 are its direct
inputs. Milestone 2 must make construction feasibility a gate (F-03/F-04/F-06/
F-08) and compare reloaded geometry block-for-block. F-09/F-10 are cleanup
work that can follow without blocking correctness.
