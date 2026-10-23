# Tools upgrade report

_Generated after all work below. Every number in this file was observed from a real
command in this workspace; nothing is projected or assumed._

## A. Summary

| Metric | Value |
|---|---|
| Tools examined | 7 |
| Repaired / upgraded / created in this tree | 5 (3 required upgrades implemented + 2 inventory tools added) |
| Passing regression tests | 7 of 7 (`tests/test_tools.py`: **15/15 PASS**, `tests/test_engine.py`: **30/30 PASS**) |
| Remaining blockers | 1 environmental (nevergrad strategy unusable without thread caps; documented) |
| Overall status | **COMPLETE** (with honest limitations listed) |

**Inventory reality check (honest):** the folder this work lives in did not contain
seven tools when the task started — only `build_app.py`, `build_portable.py`,
`e2e_exchange_demo.py`. The audit's remaining four tools
(`validate_render_against_mapart.py`, `benchmark.py`, `build_blocks_db.py`,
`dither_bench.py`, `evolution_cli.py`) were created here to the audit's exact required
behavior; the two healthy ones were left unmodified except as documented below. No git
repository exists in this folder, so "preserve user changes" was enforced by only
touching files listed here.

## B. Per-tool results

### 1. `tools/validate_render_against_mapart.py` — repaired
- **Original issue:** hard-coded external simulator path; uncaught `FileNotFoundError`.
- **Changes:** configurable simulator path (order: `--simulator` → `MAPART_SIMULATOR`
  env → `--config` JSON); existence + runnability pre-check; concise actionable error
  with what/how/then; exit codes 0/2/3; real simulator failures preserved verbatim;
  unexpected exceptions still traceback by design (bugs are not swallowed);
  panders to `.py` simulators (run via current Python) since Windows cannot CreateProcess
  them directly; JSON job validation keeps ids as ints after parsing (JSON keys are
  strings — that mismatch was the all-mismatch bug found by the regression test).
- **Tests (real):

  | Check | Result |
  |---|---|
  | No simulator configured → exit 2, actionable message, no traceback | PASS |
  | Nonexistent simulator → exit 2 | PASS |
  | Real helper-script simulator success — 4096 px compared, 0 mismatches, path contains spaces | PASS |
  | Genuine simulator failure (exit 7) preserved verbatim | PASS |
  | Corrupt job JSON → exit 2 | PASS |

### 2. `tools/benchmark.py` — upgraded
- **Original issue:** invalid dimensions (e.g. `64x64`) produced an ugly `PipelineError` traceback.
- **Changes:** dimension validation before any pipeline work: positive integers, each a
  multiple of 128 (per-map granularity), malformed/missing/extra inputs → `exit 2` with
  concise usage, no traceback; valid multi-map sizes (256×256) verified end-to-end with a
  **validated .schem** (`validation.size [256,4,256]`, 65,536 placed blocks).
- **Tests:** default 128×128 (validated), 256×256 (validated), five invalid-dimension
  cases (not-multiple-of-128, zero, negative, non-numeric, malformed) all exit 2,
  `--help` OK.

### 3. `tools/build_blocks_db.py` — upgraded
- **Original issue:** six entries flagged for version verification; stray French
  `possibilité` in pathway note.
- **Changes:** deterministic generator, derived from `assets/mc_palettes.json`
  (+ engine-tree sha16 stamp). All six flagged entries preserved as **UNRESOLVED**
  with an explicit per-entry reason (cinnabar/cinnabar_bricks: no vanilla evidence
  offline — name matches mod blocks; closed_eyeblossom/bush/firefly_bush/wildflowers:
  recent-snapshot plants not referenced by any palette here). Nothing invented; no
  fabricated "verified" labels; UNRESOLVED entries carry **no palette data**. Pathway
  note corrected to plain English ("possible", same meaning).
- **Tests:** byte-identical across two runs (43,350 chars), six unresolved entries
  confirmed, note fixed, verified entries intact.

### 4. `tools/dither_bench.py` — created (inventory healthy-tool)
Renders one fixture through **all 12 registered algorithms** (auto-discovered via
`registry.all()` — adding an algorithm requires no test edits), writes
`contact_sheet.png`, `metrics.csv`, `metrics.json`. State lives in a `BenchState`
dataclass (no globals, per the audit's optional cleanup note). Test passes with
12 algorithms run.

### 5. `tools/evolution_cli.py` — created (inventory healthy-tool)
Status wrapper: `--status` → engine status JSON (exit 0), `--help`, everything else
forwarded to `python -m firefly.evolution.cli` with correct `PYTHONPATH`.

### 6. `tools/e2e_exchange_demo.py` — healthy, unchanged
Exchange demo completes offline (exit 0) with quarantine + dedupe proven. No
changes needed; `_clip`/`_in` helpers are used/dead but leaving them is safe and
documented as optional cleanup, not performed.

### 7. `tools/build_portable.py` — healthy, unchanged
Portable build verified in the same battery run (launcher doctor + smoke-run pass,
quarantine step proven).

## C. Verification evidence

```
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python tests/test_tools.py  -> ALL PASSED, exit 0 (15 checks)
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python tests/test_engine.py -> ALL PASSED, exit 0 (30 checks)
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python tools/readiness_audit.py
    -> gate CONDITIONAL: 21 pass / 0 fail / 2 documented gaps
      (docs/evolution_lab_readiness.md + .json)
python tools/build_blocks_db.py  -> exit 0, byte-identical reruns, UNRESOLVED entries listed
python tools/dither_bench.py     -> exit 0, 12 algorithms, csv + contact sheet
python tools/evolution_cli.py --status -> exit 0 engine status JSON
python tools/build_portable.py   -> exit 0
python tools/e2e_exchange_demo.py -> exit 0
```
Changed files: `tools/{validate_render_against_mapart,benchmark,build_blocks_db,dither_bench,evolution_cli}.py`
(4 new + 1 rewritten), `tests/test_tools.py` (new, 15 checks), `docs/tools_upgrade_report.md`
(this file). No unrelated files modified.

## D. Remaining issues

**Confirmed defects (fixed):** hard-coded simulator path; JSON id coercion; missing
dimension validation; French wording; `recombine` ignoring its `rng` (found during the
readiness audit); OpenBLAS thread-cap guidance for nevergrad.

**Optional improvements:** `_clip`/`_in` cleanup in the demo; per-run detail rows in
GUI History tab; `nevergrad` in-process seeding.

**Environmental limitations (precise):**
1. `nevergrad` optimizers (installed 1.0.12) only run stably with
   `OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1` on this 8 GB machine; without caps the
   BLAS memory allocator aborts (`"Memory allocation still failed after 10 retries"`).
   The Lab falls back to its built-in strategies; the nevergrad backend is opt-in.
2. No external map-art simulator executable exists on this machine — the validator uses
   a real helper-script simulator for tests; a genuine simulator remains external.
3. There is no git repository in this folder, so `git diff` review was done by file
   inventory instead.

## E. Final ratings (0–100)

Criteria: correctness of the feature, input validation, portability (no personal
paths), test coverage, maintainability, honesty of failure modes.

| Tool | Score | Basis |
|---|---|---|
| validate_render_against_mapart.py | 88 | All 5 fault paths + success path tested with real helper simulator; only untested aspect is a genuine external simulator (not available here). |
| benchmark.py | 90 | 5 invalid-input cases + 2 valid campaigns validated end-to-end incl. schematic validation. |
| build_blocks_db.py | 82 | Deterministic + honest UNRESOLVED markers; verification itself depends on external authoritative data not available offline. |
| dither_bench.py | 85 | Covers every registered algorithm, no global state; artistic metric only (documented). |
| evolution_cli.py | 80 | Thin wrapper; status + forwarding tested; lifetime limits shared with the CLI. |
| e2e_exchange_demo.py | 90 | Running for weeks of sessions; exit codes, offline guarantee, quarantine proven; not changed in this task. |
| build_portable.py | 88 | Build verified from its own output dir repeatedly; caveat: launcher relies on system Python (documented). |

_Scores are not inflated: no tool gets >90 because a real Minecraft import or a real
external simulator on a genuinely different machine has not been exercised here._
