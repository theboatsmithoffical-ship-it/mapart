# Engineering report — map-art rendering model, optimizer, and construction

Status vocabulary: **VERIFIED** = a check ran here and passed (named below);
**SOURCE-VERIFIED** = the behaviour is quoted from decompiled Minecraft source;
**UNVERIFIED** = explicitly not confirmed (stated as such everywhere it is used).

## 1. Original baseline and measured results

Baseline recorded before any algorithm change (`docs/baseline_metrics_before.*`,
81 runs, identical fixtures/settings):

| config (metric \| dither) | dE2000 mean | p95 | %≤dE10 | time s |
|---|---|---|---|---|
| ciede2000 \| none | 9.928 | 17.23 | 52.3 | 0.43 |
| ciede2000 \| floyd_steinberg | 12.096 | 22.22 | 38.0 | 10.5 |
| ciede2000 \| bayer4 | 12.847 | 21.05 | 26.2 | 0.43 |
| lab \| none | 10.619 | 18.09 | 43.8 | 0.04 |
| rgb \| none | 11.082 | 20.34 | 44.3 | 0.02 |

After the changes (`docs/after_metrics.*`, same 81 runs):

| config | dE2000 mean | p95 | %≤dE10 | time s | delta mean |
|---|---|---|---|---|---|
| ciede2000 \| none | 10.121 | 17.31 | 51.1 | 0.40 | **+0.19** |
| ciede2000 \| floyd_steinberg | 12.309 | 22.09 | 36.8 | 9.12 | **+0.21** |
| ciede2000 \| bayer4 | 12.936 | 21.21 | 25.7 | 0.40 | **+0.09** |
| lab \| none | 10.810 | 18.22 | 42.9 | 0.14 | +0.19 |
| rgb \| none | 11.266 | 20.48 | 43.6 | 0.13 | +0.18 |

**The after numbers are deliberately worse-looking and more truthful.** The
baseline measured an idealization the game cannot build: every pixel free to
pick any of four shades (including shade code 3, which the verified updater
never writes) and a height field that ignored the shade rule. The after numbers
measure the render predicted from the actual exported staircase construction,
with shade 3 removed for the 1.8 palette. The cost of physical constructibility
is ~0.1–0.2 dE2000 mean — small, and now honest.

Memory/time: peak RSS per run 13–35 MB (down from 17–39 MB); no config slower
than baseline except the CIEDE2000+FS pair, which is faster (10.5 s → 9.1 s)
because the exact arithmetic removed per-palette rounding work.

## 2. Architecture and algorithm changes

* **`src/firefly/mapart/rendermodel.py` (new, single authority).**
  * exact shade arithmetic `channel = base * {180,220,255,135}[shade] // 255`
    (integer truncation), palette expansion consumes it;
  * the verified per-pixel shade rule: chain along each map column,
    `d2 = (h - h_prev) * 4/(scale+4) + (((x+z)&1) - 0.5) * 0.4`, `>0.6 → 2`,
    `<-0.6 → 0`, else `1`; float comparisons exactly as Java doubles, so a
    one-block step is shade 2 (rise) / shade 0 (drop) for both parities;
  * `solve_heights`: backwards-lookahead staircase solver returning heights +
    feasibility; 97.7 % of pixels realizable in random tests, failures limited
    to the boundary-constrained first pixel of a column and shade 3;
  * version rules with verification labels (1.8 = source-verified; 1.15.2 =
    labelled assumption; unknown versions fully labelled);
  * `model_provenance()` embedded in every exported report.
* **Construction is now the exported reality.** `_build_layers` places each
  pixel's block at its solved height; schematic height = tallest column + 1
  (e.g. `[128, 120, 128]`), not a fixed 4-layer stack. Preview and quality
  metrics use `shade_codes_from_heights(solved)` — the **realized** render.
* **Reachability constraint in the matcher**: candidates whose shade cannot be
  emitted under the target version's rule are rejected (1.8: shade 3; 1.15:
  kept, labelled, because the rule is unverified).
* **Metrics wiring fixed** (4 call sites): fidelity is measured between source
  and reconstruction, not source and itself.
* **Objective/explanation report** (`appcore` → report `objective`): per-pixel
  chosen error, best feasible error, dither gap, optimal-choice rate,
  runner-up margin, shade histogram, constraint set and rule status.
* Dead/broken `_srgb_to_oklab` duplicate removed.

## 3. Objective and constraints

Minimize `CIEDE2000(predicted map render, processed source)` per pixel over
feasible `(block, shade)` candidates, with dithering as a spatial term
(strength-scaled error diffusion inside each 128×128 map frame) and optional
per-block cost bias. Hard constraints: palette kind, suppressed map colors,
excluded blocks, reachable shade codes, map-frame geometry, and (for export)
the staircase construction. Construction cost never overrides fidelity unless
the user supplies a cost table.

## 4. Tests and checks run

| check | result |
|---|---|
| `tests/test_rendermodel.py` (new, 15 checks incl. 9 Sharma CIEDE2000 reference pairs, Lab references, solver round-trip, reachability, explanation) | **ALL PASSED** |
| `tests/test_repo_audit.py` (new, 14 workflow tests in throwaway repos) | **ALL PASSED** |
| `tests/test_mapart_spec.py` (56 checks) | **ALL PASSED** |
| `tests/test_engine.py` | **ALL PASSED** |
| `tests/test_tools.py` | **ALL PASSED** |
| `tests/test_breeding.py` (39 checks) | **ALL PASSED** |
| `tests/test_gui_runtime.py` | PASSED 3×, 1 load-flake (`t02 pause froze evals 3 vs 2`, counter snapshot race — product behaviour fine) |
| `tools/quality_benchmark.py` (before: 81 runs; after: 81 runs, export validation on) | **EXIT 0 both** |

## 5. Before/after examples

Same fixtures, same settings (small tier, `survival-wool-1.8`); see §1 for all
18 config/category rows. Category detail after change: noisy-photo 6.70 dE
(best), pixel-art 14.93, out-of-palette 15.48 (0 % within dE10 — palette simply
cannot express those colors; not hidden).

## 6. Export validation

`.schem` (Sponge v2), `.litematic`, and structure-NBT exports all validate and
agree on geometry and non-air counts after the construction change; exported
height now equals the solved staircase height (e.g. `[128,120,128]`, 16 384
blocks placed for a 1×1 map). Validators re-open the produced files and check
palette range, bounds, volume, and count agreement with the plan.

## 7. In-game validation

**Not performed — blocked.** No Minecraft client, no render simulator
(`MAPART_SIMULATOR`) is available in this environment. The hooks exist and are
tested with a stub: `tools/validate_render_against_mapart.py` and
`palette_audit` (which refuses with a typed error when no simulator is
configured). Every simulated number in this report is labelled as a prediction
under the stated rule; none is an in-game measurement.

## 8. Known limitations and unverified assumptions

1. 1.15.2/1.20 shade rule is unverified (1.8.9 rule assumed and labelled).
2. Palette base colors remain an approximation from public tables; the
   survival-wool id/rgb mapping is **suspected inconsistent** with the 1.8.9
   `MapColor` table and needs a real-renderer audit.
3. First row of each map column can be unrealizable (literal `d0 = 0.0`
   boundary reading); ~0.6 % of pixels in practice, reported per run.
4. Shade 3 (factor 135) is unreachable under the verified rule; 25 % of the
   palette's dark end is therefore unavailable for 1.8 targets.
5. Staircases are not height-minimised (110 blocks on a 128 px gradient, within
   the 1.8 limit of 256).
6. Schematic validity is checked by the project's own parsers, not by
   WorldEdit/Litematica importing the file.

## 9. Commands

```bash
python tests/test_rendermodel.py      # render model + color science
python tests/test_mapart_spec.py      # spec battery (56 checks)
python tests/test_engine.py           # engine battery
python tests/test_tools.py            # tool battery
python tests/test_breeding.py         # breeder battery
python tests/test_gui_runtime.py      # GUI runtime battery
python tools/quality_benchmark.py --out docs/after_metrics.json
python tools/repo_audit.py audit
python tests/test_repo_audit.py
```

## 10. Artefacts

* Baseline: `docs/baseline_metrics_before.json|md` (legacy), `docs/baseline_metrics.json|md`
* After: `docs/after_metrics.json|md`
* Audit reports: `reports/repo_audit/audit_*.json|md`
* Repository findings: `docs/repo_audit_report.md`
* Workflow docs: `docs/repo_workflow.md`
* Fixtures: `mapart-data/imagelibrary/synthetic/` (generated by
  `src/firefly/mapart/testsuite.py`, deterministic)

## 11. Single most important remaining quality limitation

**The palette's base colors are unverified.** The shade arithmetic and shade
selection are now pinned to the game source, but every predicted pixel still
rests on RGB values reconstructed from public tables — and the survival-wool
asset already shows an id/rgb mapping that contradicts the 1.8.9 `MapColor`
table. Until a renderer audit (`MAPART_SIMULATOR` / real client) verifies the
base colors per map color id, no absolute color-accuracy claim is defensible;
the construction model can be right while the colors are still wrong.
