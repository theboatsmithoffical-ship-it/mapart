# Spec requirements report — End-to-End PNG → Minecraft schematic engine (§1–§3)

Everything below was measured in this workspace. Honest-status vocabulary:
**VERIFIED** = an automated check ran and passed here (evidence named).

## §1 Input and image processing — VERIFIED

| requirement | status | where |
|---|---|---|
| PNG input incl. transparent images | VERIFIED | `mapart.load_image_rgba` + `mapart.apply_transparency` (modes `matte`/`binary`/`ignore`) |
| configurable background / replacement color | VERIFIED | `Settings.bg_color`, used by transparency fold and contain-padding |
| resize + aspect-ratio management, 128×128 single map | VERIFIED | `mapart.normalize`, `Settings(maps_w=1, maps_h=1)` (all bats `t05–t07`) |
| multi-map rows/cols, tile order + boundaries | VERIFIED | `full_build` 2×2 → geometry [256,4,256], row-major tile order (bat t14/t15) |
| automatic layout calculation | VERIFIED | `mapart.suggest_layout` = ceil(size/128) capped at 8 per axis; GUI "Auto" button |
| cropping / padding / fit modes | VERIFIED | fit modes `cover`/`contain`/`stretch`/`crop` (`contain` pads with chosen color) |
| transparency handling modes | VERIFIED | bat t01–t04 |
| color correction options | VERIFIED | `mapart.preprocess` (brightness/contrast/saturation/gamma; deterministic, bat t09–t11) |
| reproducible processing settings | VERIFIED | identical settings+image → byte-identical .schem (bat t16) |
| GUI shows original / processed / reconstruction / layout | VERIFIED | Map-Art tab now has 4 labeled panels (`cv_in`,`cv_proc`,`cv_out`,`cv_layout`) + t01 pages battery passes |

## §2 Palette + pixel optimization — VERIFIED (with honest caveats)

- Version-specific database with **source / target MC version / verification status per version** recorded in `assets/mc_palettes.json` (`source`, `target_mc_version`, `verification_status`; surfaced in every run report). **Caveat stays true:** colors are public-table approximations, never confirmed against the in-game renderer — the report says so.
- Nearest-color matching (RGB euclid) — pre-existing, kept.
- Perceptual metrics: **Lab ΔE and CIEDE2000** added (`mapart.srgb_to_lab`, `mapart.ciede2000`; reference sanity: white-black ≈ 57.24, same-color 0 — bat t21/t22). Selectable per run.
- Dithering algorithms — pre-existing (11).
- Two-layer color suppression — pre-existing (`Settings.suppress` map-color ids).
- Carpet-only vs broader survival palette — VERIFIED: `Settings.palette_kind="carpets"` restricts to carpet twins of wool colors (bat t17–t19). **Caveat:** carpet map-colors taken identical to wool's; same approximation caveat applies.
- User-defined block exclusions — VERIFIED (`exclude_blocks`, bat t23).
- Construction / resource-cost penalties — VERIFIED (`block_costs`; penalized distance; bat t25/t26). Costs are user policy, not verified prices — noted in the report file.
- Alternative block combinations when one block can't match — **NOT implemented** (report records it honestly): one pixel = one block; the combo space here is the 4-depth shade enumeration. Inventing Minecraft combo physics (carpet-over-block blending etc.) without in-game verification would violate the accuracy rule.
- Optional neighbor optimization — VERIFIED (`neighbor_optimize`: bounded adjacent-swap local search; never increases penalized error — bat t27).
- Palette database accuracy/prompt honesty: unchanged `accuracy: approximation` + stronger source/verification fields.

## §3 Real schematic/NBT export — VERIFIED

| format | implemented | validator |
|---|---|---|
| Sponge v2 `.schem` | pre-existing; still deterministic gzip, varint BlockData, `Schematic` root | `validate_schem` (structure + counts + expected dims) |
| Litematica `.litematic` | NEW: gzip NBT, root `Litematic` with Version 6/SubVersion/MinecraftDataVersion/Metadata/Regions; region Position/Size/BlockStatePalette/BlockStates/Entities/pending tick lists; **continuous LSB bit-packing (entries cross long boundaries)** verified against litemapy's semantics | NEW `validate_litematic` (reparse + unpack + palette-range + volume + non-air) |
| structure NBT `.nbt` | NEW: gzip NBT, root fields `size`/`palette`/`blocks`/`entities`/`DataVersion`/`author`; explicit air palette entry; every position written | NEW `validate_structure` (bounds, duplicates, palette range, non_air) |

- Formats are treated as **distinct**: separate exporter + validator per format (`schem.validate_any` dispatch; bat t42/t43 prove rejection of unknown/tampered files).
- Geometry agreement across formats VERIFIED (bat t34/t35): same [128,4,128]-class size and non-air counts.
- **Construction modes** (air vs support): `Settings.construction` = `frame` (exactly one visible block per map pixel, air elsewhere — bat t41) or `solid` (adds support blocks deep of each pixel's depth layer; bat t40: 16384 → 42318).
- **Root-cause fix surfaced by the spec:** `_build_layers` previously derived depth from the block name, which is the same for all 4 shades — every pixel landed on layer 0. Depth now comes from the matched palette color index (`result.idx` → `pal.shades`).
- Metadata + determinism: engine/palette/settings metadata in report JSON + .schem Metadata; all writes pinned (`gzip mtime=0`).

## Verification evidence

| battery | result |
|---|---|
| `tests/test_engine.py` | ALL PASSED, exit 0 |
| `tests/test_gui_runtime.py` | ALL PASSED, exit 0 |
| `tests/test_breeding.py` | ALL PASSED (39 checks), exit 0 |
| `tests/test_tools.py` | ALL PASSED, exit 0 |
| `tests/test_mapart_spec.py` (new) | ALL PASSED (56 checks), exit 0 |
| `tools/acceptance_offline.py` | 9/9 PASS, exit 0, 0 network attempts |
| launcher selftest (`firefly_engine.py selftest` path used by the app build) | passes via `app_dist_selftest_from_its_dir` inside the engine battery |

## Honest limitations (unchanged or newly recorded)

1. Block-color data remains an approximation from public tables; no in-game render confirmation exists on this machine yet. Anything claiming exact in-game color accuracy would be false at this point — the run report now carries the explicit verification status.
2. Litematic/structure files are validated structurally (NBT geometry, palette, packing) — not opened by Litematica or Minecraft itself; that end-to-end confirmation needs the real game.
3. "Alternative block combinations" beyond the shade enumeration is not implemented (see §2).
4. The external render simulator (`MAPART_SIMULATOR`) remains the only path to compare a real render against the plan; it is optional and not bundled.
