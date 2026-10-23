# Asset quality & reproducibility milestone

Date: 2026-10-23 · Scope: catalog, characteristics, benchmark provenance, set
separation, storage/parsing hardening, offline verification, health reporting.

No new map-art algorithms were added and core optimizer behavior was not tuned.

## Completion criteria (all satisfied)

| # | criterion | evidence |
|---|---|---|
| 1 | importer checks pass | `tests/test_assets.py` t05 (manifest import + traversal), t06 (quotas), t07 (malformed/oversize/disguised files), t09 (resume) — 4/4 PASS |
| 2 | licensing checks pass | `tools/verify_manifest.py` and the health report: 33/33 licenses recorded, 0 gaps; missing/unknown licenses are reported, never auto-filled |
| 3 | development / validation / held-out sets separated and locked | t04 (lock + journal), live library dev 7 · validation 3 · holdout 9 · stress 14; held-out membership change requires `confirm=True` + reason |
| 4 | benchmark history records revision, palette hash, algorithm definitions, MC version, settings | t11 + t12 PASS; recorded campaign run `36` evals, revision `9d8c272` (dirty), palette `b2f84c7c8296f2b9`, `dither-400c1fd900`, MC `1.15.2,1.8` |
| 5 | offline manifest verification + offline fixtures | t10 PASS; `tools/acceptance_offline.py` 10/10 steps, 0 network attempts; t14 asserts 0 non-loopback connections for the whole asset battery |
| 6 | regression batteries | spec 56/56, render model 15/15, engine, tools, repo-audit, GUI 41/41, assets 14/14; breeding 38/39 (see limitation 1) |

## What was added

- `src/firefly/mapart/provenance.py` — code revision (+dirty), palette content
  hash, dither-definition digest (`algorithm_version`), declared target
  Minecraft version, canonical settings digest.
- `src/firefly/mapart/assetcatalog.py` — SQLite (stdlib `sqlite3`, no new
  dependency) catalog: measured metadata, set membership + append-only event
  journal, benchmark history. Search by text, tag, set, width, detail,
  license status, source.
- `src/firefly/mapart/imagelibrary.py` — deterministic characteristics
  (`analysis_version = 2`), `StoragePolicy`, hardened parsing, thumbnail
  cache, resumable folder imports, held-out lock + membership journal,
  traversal-proof manifest import, verified range-resume downloads.
- `src/firefly/mapart/benchmark.py` — writes `run_meta.json` beside every
  campaign report and appends to the catalog when `catalog_path` is given
  (the GUI passes `mapart-data/assetcatalog.db`).
- `tools/verify_manifest.py` — offline manifest verification and digest.
- `tools/asset_health.py` — the asset-health report (see below).
- `tools/dataset_sets.py` — explicit membership management (`list`, `assign`,
  `events`); `--confirm` + `--reason` required to touch holdout.
- `tests/test_assets.py` — 14-check offline battery for all of the above.
- `tools/acceptance_offline.py` — new step 10 verifies the manifest, generates
  the health report and reads the catalog, still under the socket guard.

## Deterministic characteristics

`imagelibrary.characteristics(path)` returns, with fixed sampling (≤256 px
long edge) and stable tie-breaks:

`width`, `height`, `aspect`, `color_diversity`, `dominant_color_basis_pts`,
`dominant_colors_rgb` (mean RGB + share per top bin), `brightness`,
`contrast`, `edge_density`, `flat_share`, `similar_color_score`,
`palette_coverage`, `out_of_palette_share`, `transparency_share`,
`has_alpha`, `source_format`, `detail_complexity`, `capability_tags`,
`analysis_version`.

`characteristics_digest()` content-addresses the dict (no timestamps), so two
runs can be compared exactly. The live library was refreshed to version 2:
33/33 entries current, validated by t01 (repeat calls, renamed copy, RGBA
transparency).

## Set separation and held-out integrity

Purposes: `dev`, `validation`, `holdout` (locked), `stress`, `synthetic`.

- Changes touching `holdout` raise `HoldoutLocked` unless
  `confirm=True` and a non-empty reason are supplied — for `set_membership`,
  `assign_set`, `import_manifest` and `remove_dataset`.
- Every change is appended to `mapart-data/imagelibrary/membership_log.jsonl`
  (from → to, reason, confirmed, new revision), mirrored into the catalog's
  `set_events` table. Revision files (`revisions/rev-N.json`) are unchanged in
  format, so historical comparisons still work.
- `apply_set` (quick/full/stress) excludes held-out images.
- The live library received an explicit validation split (revision 115 → 118):
  `img_204e0061735b`, `img_2368009f47ea`, `img_57a9ffddd7df`, reason
  “milestone: explicit validation split for parameter selection (never trained
  on)”. Holdout was not touched.
- No benchmark or breeder path can train on holdout: the lab refuses
  `lib:<holdout>` with `PermissionError` (re-proven by acceptance step 8).

## Benchmark history

`benchmark.Campaign(..., catalog_path=...)` writes `run_meta.json`:

- `code_revision`, `code_dirty` (honest: `true` while this work is uncommitted)
- palette key(s), `palette_hash` (content hash of the color table; a sweep
  uses an order-independent combined hash; a single palette equals its plain
  hash), per-palette hashes and target MC versions
- `algorithm_version` (digest of algorithm list + error kernels + Bayer sizes)
- `settings` + `settings_digest` (mode, tier, suite, seed, strengths,
  algorithms, palettes, time limit)

History rows are keyed by that identity, so regenerating a report does not
duplicate a run. Recorded run in this workspace: 36 evals (2 algorithms ×
2 palettes × 3 strengths × 3 suite images), `tested_combos` 36, stored in
`mapart-data/assetcatalog.db` and `mapart-data/benchmark-milestone/`.

## Limits, resume and caching

`StoragePolicy` (persisted per library in `imagelibrary/policy.json`):

| limit | default |
|---|---|
| per-file | 25 MB |
| library total | 2048 MB |
| images | 2000 |
| decoded pixels | 64,000,000 |
| thumbnail | 128 px |
| allowed formats | PNG, JPEG, WEBP, BMP, TIFF |

Enforcement happens before any copy/move (`LibraryError` with the limit named),
so a refused import cannot consume quota. Parsing validates the detected
format (extension is not trusted), rejects empty/truncated/corrupt payloads,
GIF and disguised files, and refuses images whose header exceeds the pixel
budget before decoding.

Downloads: http(s) only, declared size pre-checked, streaming byte cap,
`Range: bytes=N-` resume (206 appends; a server that ignores Range and returns
200 restarts cleanly), `.part` retained for the next attempt, refused
downloads never stored. Folder imports are journalled per completed file
(`import_journal.json`); an interrupted run resumes without duplicates
(t09). Thumbnails are cached by id+size and reused unless the source is newer
(t08); the live cache holds 33 files / 131 kB.

## Measured results

- `python tests/test_assets.py` → **ALL PASSED** (14/14).
- `python tools/acceptance_offline.py` → exit 0, **10/10 steps**, 0 network
  attempts, 0 downloader calls.
- `python tools/verify_manifest.py --library mapart-data` → OK, 33/33 files,
  digest `c546447654a7e77a448c93eefae96e9ce1aa54ccf556c4666eb92fe540a5bd00`.
- `tools/asset_health.py` (see `docs/asset_health_report.md`): 33 images,
  33/33 verified, 0 missing, 0 stale hashes, 0 licensing gaps,
  1 near-duplicate candidate, 13.06 MB library (131 kB thumbnails),
  1 recorded benchmark run.
- Existing batteries re-run after the changes: spec 56/56, render model,
  engine, tools, repo-audit, GUI 41/41 — all exit 0.

## Honest limitations

1. `test_breeding` t06 (`crossover-only offspring outperforms both parents`)
   fails with the identical scores at the committed HEAD in a clean worktree,
   so it is pre-existing and unrelated to this milestone. Optimizer tuning is
   explicitly out of scope, so it was not changed; it is reported, not hidden.
2. The recorded benchmark run has `code_dirty: true` because these changes were
   not yet committed when it ran. Re-run after committing for a clean revision.
3. Near-duplicate detection is a coarse 16×16 grey fingerprint (<4/255 mean
   difference) and reports candidates only — nothing is merged or deleted.
4. Benchmark campaigns use the deterministic synthetic suite; the catalog does
   not claim per-image set coverage. Set-level evaluation evidence still comes
   from lab/breeder history.
5. The validation set is small (3 images) and no recorded selection run has
   used it yet; the infrastructure and the lock are in place, the experiment
   is not.
6. Network hardening is unit-tested with local files and mocked payloads; live
   Wikimedia downloads were not exercised in this milestone (the offline
   battery blocks them by design).
7. Palette base colors remain unverified against a real client (unchanged from
   the previous report); no simulator is configured.

## Reproduce

```
python tests/test_assets.py
python tools/verify_manifest.py --library mapart-data
python tools/asset_health.py --data-dir mapart-data --refresh-analysis \
    --out docs/asset_health_report.md --json reports/asset_health.json
python tools/dataset_sets.py list
python tools/acceptance_offline.py
```
