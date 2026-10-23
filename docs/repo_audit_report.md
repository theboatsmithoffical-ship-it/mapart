# Repository audit report (FireFly / map-art engine)

Audit tool: `python tools/repo_audit.py audit` (persisted to
`reports/repo_audit/audit_*.json|md`). Manual review of source, tests, docs and
artefacts was combined with the automated findings.

## Blockers (must resolve before any commit)

| id | severity | evidence | action |
|---|---|---|---|
| `repo_root_mismatch` | **critical** | `git rev-parse --show-toplevel` → `C:/Users/cmlog` (the user's home directory); 0 tracked files under the project path | the project is **not** a git repository; committing would target unrelated home-directory files. `git init` in the project root, then re-run `prepare` |
| `runtime_data_present` | info | `mapart-data/` (~410k generated files), `acceptance_run/`, `measured_run/`, `breeding_cli_demo/`, `operator-ws/`, `optimizer-champion/` (18 duplicate-content groups, all inside these trees) | excluded from the commit plan; add to `.gitignore` before initialising a repository |

## Verified defects (fixed in this change set)

1. **Shade arithmetic diverged from vanilla.** The palette expanded shades as
   `round(channel * 0.71/0.86/1.0/0.53)`; Minecraft uses
   `channel * {180,220,255,135} / 255` with integer truncation (1.8.9
   `MapColor.getMapColor`, read verbatim). Fixed in `rendermodel.py`; palette
   entries now match the game arithmetic exactly (unit-tested against
   hand-computed values).
2. **The exported construction could not produce the predicted colors.**
   `_build_layers` placed each pixel at height = shade code, but the verified
   updater derives shade from the height step along each map column (flat → 220,
   rise → 255, drop → 180). Fixed: a backwards-lookahead staircase solver
   (`rendermodel.solve_heights`, 97.7 % of pixels feasible; only the
   boundary-constrained first pixel of a column and the unreachable shade 3 can
   fail) drives the export, and the preview/metrics use the **realized** render.
3. **Fidelity metrics never measured fidelity.** `metrics.compute(orig, arr,
   out_rgb, …)` was called with the *source* image in the reconstruction slot at
   all four call sites (breeding, benchmark, GUI, ensemble scorer), making
   `oklab_error` and `edge_preservation` constant. Fixed; measured screen scores
   are now fidelity-driven (~0.93 constant-ish → 0.70–0.72 meaningful range).
4. **Unreachable shade 3 was offered as a candidate.** The verified update loop
   emits only codes 0/1/2 (`MapColor` defines 135 for code 3 but nothing writes
   it). The matcher now rejects unreachable shades for source-verified versions
   and keeps them (labeled) for unverified ones.
5. **Broken duplicate colour helper** `_srgb_to_oklab` (returned a tuple,
   unused) removed from `metrics.py`.
6. **Offline guarantee violated by provenance URLs** embedded in shipped
   `src/` (the engine battery forbids network-looking literals). Source
   identifiers now live in code; full URLs live in the reports.

## Suspected issues (unverified — need a real renderer or game client)

1. **Palette base colors are unverified approximations.** The 1.8.9 `MapColor`
   table contains `clothColor = (199,199,199)` and `id 1 = grass`; the
   `survival-wool-1.8` asset claims "exact map colors" with
   `id 1 = white-wool (221,221,221)`. Either the id space is re-assigned or the
   RGBs come from dye/wool values rather than the map-color table. `palette_audit`
   exists for exactly this check but needs `MAPART_SIMULATOR`; nothing was
   claimed beyond that.
2. **1.15.2 shade rule is an assumption.** No 1.15.2/1.20 update-method source
   could be retrieved; the 1.8.9 rule is applied and labelled
   `unverified-assumption` in every report.
3. **`t02 pause froze evals` is load-sensitive** (failed once as
   `3 vs 2`, passed 3×): the lab pause assertion reads a counter that can
   advance between pause request and snapshot. Product behaviour is fine; the
   test should snapshot the count at pause request time.
4. **Docs drift:** `FEATURE-INVENTORY.md` cites "30-check battery", the spec
   report cites 56/57 checks; the heightmap export changes former geometry
   statements ("4-layer").

## Improvement backlog (optional)

* Solver height minimisation (current staircases pre-fund all remaining drops,
  reaching 110 blocks on a 128 px gradient; within the 1.8 build limit 256).
* `mapart-data/` is ~410k files of generated fixtures/previews — treat as
  cache, add a regeneration command and a `.gitignore` entry.
* Secret scanning is pattern-only; add entropy scoring if fixtures produce
  false positives.
* Duplicate detection is exact-content and capped at 5 MB.

## Verification status of this audit

Ran here: `tests/test_rendermodel.py` (15 checks), `tests/test_repo_audit.py`
(14 workflow tests), `tests/test_mapart_spec.py` (56), `tests/test_tools.py`,
`tests/test_breeding.py` (39), `tests/test_engine.py`, `tests/test_gui_runtime.py`
(one known load flake, see above). Repository-wide "correct because tests pass"
is explicitly **not** claimed: in-game rendering, real WorldEdit/Litematica
import, and the 1.15.2 rule remain unverified.
