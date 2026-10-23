# V1 Release-Readiness Report — PNG → Minecraft schematic engine

Audit date: 2026-10-21. All evidence below was generated in this workspace by
the exact repeatable commands listed. Statuses: **PASS** (verified here),
**FAIL** (found + fixed, re-verified), **BLOCKED** (cannot be verified here),
**NOT TESTED** (not exercised in this audit).

## Repeatable test commands

```
python tests/test_engine.py            # 38-check engine battery incl. app-dist selftest
python tests/test_gui_runtime.py       # 20-check desktop runtime battery
python tests/test_breeding.py          # 39-check breeding spec battery
python tests/test_tools.py             # 15-check tool battery
python tests/test_mapart_spec.py       # 57-check §1–§3 spec battery
python tools/release_sweep.py          # 139-combination pipeline sweep (validated exports)
python tools/release_sweep.py --fast   # 72-combination quick sweep
python tools/acceptance_offline.py     # §7 offline scenario under hard socket guard
PYTHONPATH=src python tools/build_app.py --out dist/FireflyEngine   # rebuild + selftest package
python firefly_engine.py --selftest    # headless verification of any installed copy
```

## Results at time of audit (all exit codes captured)

| command | result |
|---|---|
| test_engine.py | ALL PASSED, exit 0 (run twice, incl. after final edits) |
| test_gui_runtime.py | ALL PASSED, exit 0 |
| test_breeding.py | ALL PASSED (39 checks), exit 0 |
| test_tools.py | ALL PASSED, exit 0 |
| test_mapart_spec.py | ALL PASSED (57 checks), exit 0 |
| release_sweep.py | 139/139 combinations PASSED, exit 0 (90 s wall) |
| release_sweep.py --fast | 72/72 PASSED, exit 0 |
| acceptance_offline.py | 9/9 PASS, exit 0, **0 network attempts, 0 downloader calls** |
| build_app.py --out dist/FireflyEngine | built: true, exit 0, in-place selftest "ok" |

## Fixed this audit (real defects, root-caused, re-verified)

1. **`tools/build_app.py` relative `--out` doubled the build path** — the
   selftest probe ran with `cwd=out` while passing a relative launcher path,
   so a relative `--out dist/FireflyEngine` looked for
   `dist\FireflyEngine\dist\FireflyEngine\firefly_engine.py` and failed the
   build. Fixed by resolving the output dir to an absolute path at the entry
   of `build()`. **FAIL→PASS** (exit 1 → exit 0, in-repo rebuild verified).
2. **Stale shipped package** — `dist/FireflyEngine` predated the §1–§3 work.
   Rebuilt from current sources; selftest passes from its own directory
   (engine battery `app_dist_selftest_from_its_dir`).
3. **No low-disk guard on export** — a full disk previously produced a
   half-written schematic. Added a preflight in `appcore.export` that refuses
   cleanly with a typed error before any file is created. **Added spec
   battery t44** (disc full → refuses → no file written), PASSED.
4. **No no-terminal first-run guidance** — added a permanent "Start here"
   4-step block to the Map-Art tab; `test_gui_runtime` re-run, PASSED;
   package rebuilt afterward.

## Feature-status inventory (implemented / partial / broken / missing)

Implemented & verified (evidence):
- E2E pipeline: PNG(α) → transparency fold → color correction → fit/layout →
  match+dither → suppression/block plan → 4-layer construction plan →
  `.schem`/`.litematic`/`.nbt` export, each independently validated
  (release_sweep 139/139; test_mapart_spec t01–t44; acceptance step 6).
- Two-block combo mode (opt-in, HYPOTHESIS status): deterministic seeded
  per-pixel cover mask (`combos` + `combo_ratio` in Settings/lab/breeder
  candidates), colors and preview byte-identical to combos-off runs by
  construction, exported as a dedicated 5th layer with an explicit
  HYPOTHESIS note in every report; lab/breeder exploration of the combo
  space is opt-in via `allow_combos` and the cover blocks enter the
  material-cost objective (bat cb_colors_identical_off_on,
  cb_ratio_semantics, cb_export_structure_and_hypothesis,
  cb_lab_explore_combos).
- 12 dithering methods × 2 palettes × 5 map layouts; fit/metric/transparency/
  construction/palette-kind/exclusion/cost/neighbor modes
  (release_sweep full matrix).
- Evolution Lab: real evaluations, pause/stop/pause-resume, checkpoint resume
  by fresh controller with **no eval-count reset** (acceptance step 7), best
  candidate retained & reproducible (t17 reruns identically; determinism t16).
- Breeding: weighted crossover/mutation, buildability gate on a validated
  schematic, champion lineage, holdout isolation (holdout never read by
  selection; PermissionError on direct use; aggregate-only results).
- Offline dataset library: 33 verified images, byte-resolution relative-path
  fix (earlier audit), pause/resumable downloads, revisions, licenses.
- Resources: `safe`/`balanced`/`performance` profiles, default `safe` (1
  worker), admission denial under pressure, junk-cleanup preserving
  `state.json`, heap-peak budget assertions (test_gui_runtime t14/t15/t16).
- Diagnostics: `--selftest` headless verification, per-run report JSONs, pressure logs, persona-free error messages (typed, actionable).
- Desktop app: single double-click launcher (`FireflyEngine.cmd` / `.sh`),
  offline by construction (no network source lines outside the explicit
  downloader module), data fully contained in the app folder.

Partially implemented (documented, honest):
- **Two-block combos are HYPOTHESIS, never in-game-verified here.** The
  front cover block (default `minecraft:glass`, assumed to carry no map
  color) is modeled as color-neutral: the exported preview/report claim
  nothing visual beyond the unchanged palette plan, and every exported
  report carries a verification note stating what was NOT verified. A real
  audit specimen (render one covered pixel through the configured
  simulator / in-game) must pass before any combo output may be trusted
  in-game; until then treat the cover layer as unverified geometry.
- Palette color data is a **public-table approximation**; no in-game render
  confirmation on this machine. Verification status is now recorded per
  version and in every run report.
- Alternative block combinations beyond the 4-depth shade enumeration are
  NOT implemented (recorded in run reports).
- First-run sample project: written no-terminal guidance exists, but a real
  novice-user walkthrough on a fresh machine was not performed (NOT TESTED).

Missing / out of v1 scope (documented, not blocking):
- Optional Unsplash source (needs API key; GUI shows the notice).
- Render-protection GUI for the external simulator (CLI tool exists).

Broken & fixed during audits (history): manifest cwd-relative paths (fixed),
depth-derived-from-block-name placement collapse (fixed), litematic bit
packing (fixed + boundary-tested), NBT LongArray typing (fixed).

## Requirement-by-requirement statuses

| # | requirement | status | evidence |
|---|---|---|---|
| 1 | feature audit | PASS | this inventory; FEATURE-INVENTORY.md baseline |
| 2 | E2E pipeline + validation | PASS | release_sweep 139/139; acceptance steps 1–6 |
| 3 | every method/palette/layout/config combo | PASS | release_sweep.py full matrix |
| 4 | lab real/persistent/resumable/reproducible/best-retained | PASS | acceptance step 7, engine `lab_*`+`checkpoint_crash_recovery`, gui t09/t10/t17 |
| 5 | 8 GB Windows, memory limits/staging/cancel/recovery | PASS (automated) | default `safe` profile; heap-peak + pressure batteries; 8 GB host used throughout |
| 6 | offline after setup | PASS | acceptance 0 network attempts; dist selftest; offline import battery |
| 7 | install/first launch/save/reopen/export/diagnostics/uninstall | PASS | rebuilt package selftest from own dir; data contained in app folder; uninstall = delete folder |
| 8 | corrupted inputs/invalid settings/interrupted/low disk | PASS | spec t42–t44, tamper/manifest-corruption batteries, gate tests, new low-disk guard |
| 9 | backups/autosaves/diagnostics + privacy | PASS | lab checkpoints + history, resource pressure logs, revisions; user-visible JSONs contain local paths only, nothing transmitted |
| 10 | first-run without terminal | PARTIAL | GUI quick-start block added; novice walkthrough on a fresh machine NOT TESTED |
| 11 | release-readiness report | PASS | this document |

## Remaining limitations / NOT-BLOCKED items

- **Palette accuracy audit path now wired** (post-audit addition):
  `python tools/audit_palette_accuracy.py --version <v> --simulator <path>`
  renders every (map color, shade) through an external MAPART_SIMULATOR-contract
  simulator and writes `assets/palette_audit_<v>.json` with a per-color
  expected/observed/CIEDE2000 verdict. `load_palette` reads the sidecar back,
  upgrading the per-color status from "approximation" to "simulator-audited
  n/m verified" for exactly the audited colors, and every run report carries
  the summary. Without a configured simulator the tool refuses and records
  nothing. Battery coverage: `pa_refuse_no_sim`, `pa_stub_verified_sidecar`,
  `pa_stub_failing_verdicts`, `pa_sidecar_attach_upgrades_status`
  (tools battery, exit 0). Verdicts are as real as the simulator's own fidelity;
  a stub only validates the plumbing.

- **BLOCKED here, not release-blocking:** loading exported `.litematic`/`.nbt`
  inside Litematica/Minecraft and in-game palette verification require the
  actual game; structural validators fully cover the file format instead.
- **NOT TESTED:** physical Wi-Fi-disconnect run (documented runbook exists;
  the hard socket-guard equivalent is verified) — optional extra assurance.
- Map palette remains the 16-color survival-wool + 61-bit approx palette set;
  no exact-color claims are made anywhere.

## Recommendation

**READY FOR RELEASE** — with the recorded honest labels:

- Every automated acceptance, battery, sweep, and selftest passes with real
  evidence and captured exit codes; one release-blocking build tooling
  defect was found and fixed during this audit.
- The only knowingly inaccurate claim permitted in v1 materials is the
  documented palette-approximation status — the application itself reports
  it; release notes must repeat it.
- Post-release follow-ups (not blockers): in-game format/palette
  verification, optional two-block combos, novice-user observation session.
