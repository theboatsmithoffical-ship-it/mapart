"""Headless application core — the single data path used by the GUI, the
launcher selftest and the test battery. No tkinter imports here, no network.

Workflow: load image -> (optionally normalize) -> palette -> match+dither ->
suppression -> plan/counts -> preview PNG -> .schem export + report JSON.
Every stage validates its inputs and raises typed errors the UI translates.
"""
from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image

from . import mapart, rendermodel, schem
from .mapart import MAP_SIZE, Canceled, available_versions, load_image_rgb, load_palette

SUPPORTED_ALGORITHMS = ("floyd_steinberg", "atkinson", "burkes", "sierra_lite",
                        "jjn", "stucki", "bayer2", "bayer4", "bayer8",
                        "threshold", "random", "none")


METRICS = ("rgb", "lab", "ciede2000")
FIT_MODES = ("cover", "contain", "stretch", "crop")
TRANSPARENCY_MODES = ("ignore", "matte", "binary")
CONSTRUCTION_MODES = ("frame", "solid")
PALETTE_KINDS = ("blocks", "carpets")
EXPORT_FORMATS = ("schem", "litematic", "structure")


class Settings:
    """Validated settings bag shared by preview + export (prevents drift).

    Every field is reproducible: identical settings + identical source image
    produce identical outputs, down to the exported file bytes.
    """

    def __init__(self, version="survival-wool-1.8", maps_w=1, maps_h=1,
                 algorithm="floyd_steinberg", strength=1.0, suppress=(),
                 fit="cover", label="artwork", bias=0,
                 metric="rgb", exclude_blocks=(), block_costs=None,
                 neighbor_optimize=False, palette_kind="blocks",
                 transparency_mode="ignore", bg_color=(15, 15, 15),
                 brightness=0.0, contrast=1.0, saturation=1.0, gamma=1.0,
                 construction="frame", support_block="minecraft:white_wool",
                 formats=("schem",), strict_geometry=True,
                 combos=False, combo_front_block=mapart.DEFAULT_COMBO_FRONT_BLOCK,
                 combo_ratio=1.0):
        if version not in available_versions():
            raise ValueError(f"unknown palette {version!r}")
        if not (1 <= maps_w <= 8 and 1 <= maps_h <= 8):
            raise ValueError("layout maps must be within 1..8 each axis")
        if algorithm not in SUPPORTED_ALGORITHMS:
            raise ValueError(f"unknown algorithm {algorithm!r}")
        if not 0.0 <= float(strength) <= 2.0:
            raise ValueError("strength must be within [0, 2]")
        if metric not in METRICS:
            raise ValueError(f"unknown metric {metric!r}")
        if fit not in FIT_MODES:
            raise ValueError(f"unknown fit mode {fit!r}")
        if transparency_mode not in TRANSPARENCY_MODES:
            raise ValueError(f"unknown transparency mode {transparency_mode!r}")
        if palette_kind not in PALETTE_KINDS:
            raise ValueError(f"unknown palette kind {palette_kind!r}")
        if construction not in CONSTRUCTION_MODES:
            raise ValueError(f"unknown construction mode {construction!r}")
        if not isinstance(formats, (tuple, list)) or not formats or \
                any(f not in EXPORT_FORMATS for f in formats):
            raise ValueError(f"formats must be a non-empty subset of {EXPORT_FORMATS}")
        self.version, self.maps_w, self.maps_h = version, int(maps_w), int(maps_h)
        self.algorithm, self.strength = algorithm, float(strength)
        self.suppress = set(int(c) for c in suppress)
        bias = int(bias)
        if not -64 <= bias <= 64:
            raise ValueError("bias must be in [-64, 64]")
        self.bias = bias
        self.fit, self.label = fit, str(label)
        self.metric = metric
        self.exclude_blocks = set(exclude_blocks)
        self.block_costs = {str(k): float(v) for k, v in (block_costs or {}).items()}
        if any(c < 0 for c in self.block_costs.values()):
            raise ValueError("block costs must be non-negative")
        self.neighbor_optimize = bool(neighbor_optimize)
        self.palette_kind = palette_kind
        self.transparency_mode = transparency_mode
        self.bg_color = (int(bg_color[0]), int(bg_color[1]), int(bg_color[2]))
        self.brightness, self.contrast = float(brightness), float(contrast)
        self.saturation, self.gamma = float(saturation), float(gamma)
        self.construction = construction
        self.support_block = str(support_block)
        self.formats = tuple(formats)
        # two-block combo mode (HYPOTHESIS): front cover block + visible block
        # per pixel. Colors are computationally unaffected (the visible block
        # keeps its palette entry; the cover is a recorded extra layer), so no
        # visual claim is ever attached; outputs carry an explicit hypothesis
        # label until an audit specimen verifies the cover's no-map-color
        # behavior through the real simulator/in-game render.
        self.combos = bool(combos)
        self.combo_front_block = str(combo_front_block)
        if not 0.0 <= float(combo_ratio) <= 1.0:
            raise ValueError("combo_ratio must be within [0, 1]")
        self.combo_ratio = float(combo_ratio)

    def as_dict(self) -> dict:
        return {"version": self.version, "maps": [self.maps_w, self.maps_h],
                "algorithm": self.algorithm, "strength": self.strength,
                "suppress": sorted(self.suppress), "fit": self.fit, "label": self.label,
                "bias": self.bias, "metric": self.metric,
                "exclude_blocks": sorted(self.exclude_blocks),
                "block_costs": self.block_costs,
                "neighbor_optimize": self.neighbor_optimize,
                "palette_kind": self.palette_kind,
                "transparency_mode": self.transparency_mode,
                "bg_color": list(self.bg_color),
                "brightness": self.brightness, "contrast": self.contrast,
                "saturation": self.saturation, "gamma": self.gamma,
                "construction": self.construction,
                "support_block": self.support_block,
                "formats": list(self.formats),
                "combos": self.combos,
                "combo_front_block": self.combo_front_block if self.combos else None,
                "combo_ratio": self.combo_ratio if self.combos else 0.0}


class Result:
    pass


def process(image_path, settings: Settings, cancel: threading.Event | None = None,
            progress=None, explain: bool = False) -> Result:
    """Run the full plan; returns a Result carrying preview, counts, frames.

    Pipeline: load (alpha-aware) -> transparency fold -> color correction ->
    normalize to layout -> match+dither -> plan. `progress(frac, message)`
    reports real stage progress only."""
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"image not found: {path}")
    img = mapart.load_image_rgba(path)
    t0 = time.perf_counter()
    if progress:
        progress(0.05, "transparency + color correction")
    img = mapart.apply_transparency(img, settings.transparency_mode,
                                    bg_color=settings.bg_color)
    img = mapart.preprocess(img, brightness=settings.brightness,
                            contrast=settings.contrast,
                            saturation=settings.saturation,
                            gamma=settings.gamma)
    tw, th = settings.maps_w * MAP_SIZE, settings.maps_h * MAP_SIZE
    if (img.shape[0], img.shape[1]) != (th, tw):
        img = mapart.normalize(img, settings.maps_w, settings.maps_h, settings.fit,
                               bg_color=settings.bg_color)
    res_processed = img.copy()
    if progress:
        progress(0.15, f"matching+dithering {tw}x{th}")
    pal = load_palette(settings.version, kind=settings.palette_kind)
    idx = mapart.match_and_dither(img, pal, settings.algorithm, settings.strength,
                                  settings.suppress, cancel=cancel, bias=settings.bias,
                                  metric=settings.metric,
                                  exclude_blocks=settings.exclude_blocks,
                                  block_costs=settings.block_costs,
                                  neighbor_optimize=settings.neighbor_optimize)
    if progress:
        progress(0.95, "planning construction")
    res = Result()
    res.idx = idx
    res.pal = pal
    res.settings = settings
    res.processed = res_processed
    res.blocks = mapart.color_indices_to_blocks(idx, pal)
    res.counts = mapart.block_counts(idx, pal)
    res.frames = mapart.layer_plan(idx, pal, settings.maps_w, settings.maps_h)
    # ---- construction realization: solve the staircase heightmap whose render
    # under the shade rule equals the intended shade map, then predict the map
    # from THAT construction. Preview, metrics and export all consume this.
    base_by_id: dict = {}
    for c in pal.colors:
        if int(c["shade"]) == 2:      # factor 255 -> untouched base map color
            base_by_id[int(c["map_color"])] = np.array(c["rgb"], dtype=np.int64)
    heights = np.zeros(idx.shape, dtype=np.int32)
    feasible = np.ones(idx.shape, dtype=bool)
    realized = np.zeros(idx.shape, dtype=np.uint8)
    for my in range(settings.maps_h):
        for mx in range(settings.maps_w):
            sl = (slice(my * MAP_SIZE, (my + 1) * MAP_SIZE),
                  slice(mx * MAP_SIZE, (mx + 1) * MAP_SIZE))
            frame_codes = pal.shades[idx[sl]].astype(np.int64)
            h, ok = rendermodel.solve_heights(frame_codes)
            heights[sl] = h
            feasible[sl] = ok
            realized[sl] = rendermodel.shade_codes_from_heights(h.astype(np.float64))
    id_grid = pal.map_color_ids[idx].astype(np.int64)
    base_rgb = np.zeros(idx.shape + (3,), dtype=np.int64)
    for cid, rgb in base_by_id.items():
        base_rgb[id_grid == cid] = rgb
    fac = np.asarray(rendermodel.SHADE_FACTORS, dtype=np.int64)[realized]
    res.preview = np.clip((base_rgb * fac[..., None]) // 255, 0, 255).astype(np.uint8)
    res.heights, res.feasible, res.realized = heights, feasible, realized
    key_to_idx = {(int(c["map_color"]), int(c["shade"])): i
                  for i, c in enumerate(pal.colors)}
    res.realized_idx = np.array(
        [key_to_idx[(int(cid), int(s))]
         for cid, s in zip(id_grid.ravel(), realized.ravel())],
        dtype=np.int32).reshape(idx.shape)
    if settings.combos:
        cover, front_block = mapart.compute_combo_cover(
            idx, settings.combo_ratio, seed=7,
            front_block=settings.combo_front_block)
        res.combo_cover, res.combo_block = cover, front_block
    else:
        res.combo_cover, res.combo_block = None, None
    res.elapsed = time.perf_counter() - t0
    res.image_path = str(path)
    # objective/constraint explanation (why these candidates were chosen) —
    # optional because it costs an extra candidate scan; full_build enables it
    res.explain = None
    if explain:
        res.explain = mapart.explain_plan(
            res.realized_idx, res_processed, pal, metric=settings.metric,
            suppress=settings.suppress, exclude_blocks=settings.exclude_blocks,
            block_costs=settings.block_costs, algorithm=settings.algorithm,
            strength=settings.strength)
    if progress:
        progress(1.0, "done")
    return res


def preview_png(result: Result) -> bytes:
    with Image.fromarray(result.preview, "RGB") as im:
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()


def _build_layers(result: Result, mode: str = "frame") -> list:
    """Construction layers from the SOLVED staircase heightmap.

    layer[y][z][x] = block placed at height y. The visible block of each map
    pixel sits at its solved height (rendermodel.solve_heights), i.e. the
    staircase that makes the shade rule render the intended shades. Schematic
    height = tallest column + 1. Support mode additionally fills every column
    BELOW its visible block so floating structures become buildable.
    """
    pal = result.pal
    width = result.settings.maps_w * MAP_SIZE
    length_ = result.settings.maps_h * MAP_SIZE
    height = int(result.heights.max()) + 1
    layers = [[["minecraft:air"] * width for _ in range(length_)] for _ in range(height)]
    if mode not in ("frame", "solid"):
        raise ValueError(f"unknown construction mode {mode!r}")
    for yi in range(length_):
        for xi in range(width):
            ci = int(result.idx[yi][xi])
            blk = pal.blocks[ci]  # same block as result.blocks[yi][xi]
            h = int(result.heights[yi][xi])
            layers[h][yi][xi] = blk
            if mode == "solid":
                for y in range(h):
                    layers[y][yi][xi] = result.settings.support_block
    return layers


def export(result: Result, out_dir, base_name: str = "mapart") -> dict:
    """Write validated deliverables into out_dir (created if needed).

    Exports every format selected in settings.formats ('schem' = Sponge v2,
    'litematic' = Litematica v6, 'structure' = vanilla structure NBT), then
    independently validates each file. The three formats are NOT treated as
    interchangeable: each gets its own exporter and its own validator.

    Preflight: refuses to start writing when the target disk cannot plausibly
    hold the deliverables — a half-written schematic is worse than a clean
    typed error."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    from .imagelibrary import disk_free_mb  # local import: heavy module, no cycle
    plan_blocks = result.settings.maps_w * result.settings.maps_h * MAP_SIZE ** 2
    needed_mb = max(4, (plan_blocks * 4 * len(result.settings.formats)) // (1024 * 1024))
    free_mb = disk_free_mb(out_dir)
    if free_mb < needed_mb:
        raise ValueError(f"insufficient disk space at {out_dir}: need ~{needed_mb} MB, "
                         f"free {free_mb} MB — free space and retry")
    s = result.settings
    width = s.maps_w * MAP_SIZE
    length_ = s.maps_h * MAP_SIZE
    meta = {
        "palette": s.version,
        "palette_kind": s.palette_kind,
        "layout": [s.maps_w, s.maps_h],
        "algorithm": s.algorithm,
        "strength": s.strength,
        "metric": s.metric,
        "label": s.label,
        "transparency": s.transparency_mode,
        "construction": s.construction,
        "source_fixture": result.image_path,
    }
    if s.combos:
        # combo keys only when active: the vanilla-NBT writer cannot encode
        # null tags, so combos-off exports must never carry None values in meta
        meta["combos_on"] = True
        meta["combo_front_block"] = s.combo_front_block
    layers = _build_layers(result, mode=s.construction)
    total_height = len(layers)
    if s.combos:
        # dedicated extra layer so the validated 4-shade geometry is untouched:
        # the cover plane holds the front combo block where the deterministic
        # cover mask says so, air elsewhere.
        cover_plane = [[(s.combo_front_block if covered else "minecraft:air")
                        for covered in row] for row in result.combo_cover]
        layers = layers + [cover_plane]
        total_height = len(layers)
    files = {"preview": str(out_dir / f"{base_name}_preview.png"), "formats": {}}
    (out_dir / f"{base_name}_preview.png").write_bytes(preview_png(result))
    if "schem" in s.formats:
        schem_path = out_dir / f"{base_name}_{width}x{length_}.schem"
        info = schem.export_schem(schem_path, layers, width, total_height, length_, meta)
        files["schem"] = str(schem_path)
        files["formats"]["schem"] = info
    if "litematic" in s.formats:
        lit_path = out_dir / f"{base_name}_{width}x{length_}.litematic"
        info = schem.export_litematic(lit_path, layers, width, total_height, length_,
                                      metadata={k: str(v) for k, v in meta.items()})
        files["litematic"] = str(lit_path)
        files["formats"]["litematic"] = info
    if "structure" in s.formats:
        st_path = out_dir / f"{base_name}_{width}x{length_}.nbt"
        info = schem.export_structure_nbt(st_path, layers, width, total_height, length_)
        files["structure"] = str(st_path)
        files["formats"]["structure"] = info
    report = {
        "summary": {
            "version": s.version, "maps": [s.maps_w, s.maps_h],
            "size_blocks": [width, length_], "map_pixels": width * length_,
            "algorithm": s.algorithm, "strength": s.strength,
            "suppress": sorted(s.suppress), "label": s.label,
            "metric": s.metric, "construction": s.construction,
            "elapsed_s": round(result.elapsed, 3),
        },
        "counts": [{"item": v["item"], "block": blk, "count": v["count"]}
                   for blk, v in result.counts.items()],
        "construction_counts": ({"note": "full construction incl. combo covers",
                                 "totals": schem.counts_from_plan(layers)}
                                if s.combos else None),
        "combos": ({
            "enabled": True,
            "front_block": s.combo_front_block,
            "covered_pixels": int(sum(sum(1 for c in row if c) for row in result.combo_cover)),
            "ratio_requested": s.combo_ratio,
            "colors_unchanged_vs_frame": True,
            "verification": "HYPOTHESIS — the front cover's no-map-color behavior and "
                            "any real in-game color interaction have NOT been verified "
                            "here; run an audit specimen through the real simulator "
                            "(audit path) before trusting combo construction in-game",
        } if s.combos else {"enabled": False}),
        "resource_cost": {
            "note": "sum(count * settings.block_costs) — cost table is user policy, "
                    "not verified market data",
            "total": round(sum(v["count"] * s.block_costs.get(blk, 0.0)
                               for blk, v in result.counts.items()), 3),
        },
        "frames": len(result.frames),
        "construction": {
            "model": "staircase heightmap solved against the render model",
            "height": int(result.heights.max()) + 1,
            "height_min": int(result.heights.min()),
            "height_max": int(result.heights.max()),
            "feasible_pixels": int(result.feasible.sum()),
            "infeasible_pixels": int((~result.feasible).sum()),
            "feasible_pct": round(100.0 * float(result.feasible.mean()), 2),
            "intended_vs_realized_equal_pct": round(
                100.0 * float((result.realized_idx == result.idx).mean()), 2),
            "boundary": "0.0 (literal source reading: no outside column loaded)",
            "shade_rule": rendermodel.rule_provenance(result.pal.target_mc_version),
            "honesty": "pixels marked infeasible cannot render their intended "
                        "shade under the stated rule; the preview shows the "
                        "REALIZED render, never the intended one",
        },
        "export": files["formats"],
        "objective": result.explain,
        "render_model": rendermodel.model_provenance(),
        "palette_note": result.pal.meta_note["note"],
        "palette_source": result.pal.source,
        "palette_target_mc_version": result.pal.target_mc_version,
        "palette_verification": result.pal.verification_status,
        "palette_audit": result.pal.verification if getattr(result.pal, "verification", None) else None,
        "alternating_combinations": "1 pixel = 1 block; the combo space is the "
                                    "4-depth shade enumeration of the palette",
    }
    report_path = out_dir / f"{base_name}_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    files["report"] = str(report_path)
    return files


def full_build(image_path, settings: Settings, out_dir, base_name=None,
               cancel: threading.Event | None = None, progress=None) -> dict:
    """One-call workflow: process -> export -> validate EVERY produced format
    against the plan -> report. Validation compares each file's decoded
    geometry with the processed plan, not with a second exporter."""
    base = base_name or Path(image_path).stem or "mapart"
    result = process(image_path, settings, cancel=cancel, progress=progress,
                     explain=True)
    if progress:
        progress(0.97, "exporting + validating")
    files = export(result, out_dir, base_name=base)
    combos_on = settings.combos
    construction_is_frame = settings.construction == "frame"
    # the exported schematic is the solved staircase heightmap, so its height
    # comes from the result (tallest column + 1), not a fixed 4-layer stack
    expect = {"size": (settings.maps_w * MAP_SIZE,
                       int(result.heights.max()) + 1 + (1 if settings.combos else 0),
                       settings.maps_h * MAP_SIZE)}
    validation = {}
    if "schem" in settings.formats:
        v = schem.validate_schem(Path(files["schem"]), expect=expect)
        validation["schem"] = v
        # construction agreement: in frame mode exactly one block per map pixel
        # must be placed; solid mode additionally packs support blocks, so the
        # count may only be >= the pixel count there.
        pixels = settings.maps_w * settings.maps_h * MAP_SIZE ** 2
        covered = int(result.combo_cover.sum()) if result.combo_cover is not None else 0
        if combos_on:
            # frame: exactly visible + covers; solid: additionally support blocks
            if settings.construction == "frame":
                if v["blocks_placed"] != pixels + covered:
                    raise ValueError("export validation failed: placed blocks disagree "
                                     f"with the combo plan ({v['blocks_placed']} vs "
                                     f"{pixels + covered})")
            elif v["blocks_placed"] < pixels + covered:
                raise ValueError("export validation failed: solid+combo under-filled")
        elif (v["blocks_placed"] != pixels) if settings.construction == "frame" else \
                (v["blocks_placed"] < pixels):
            raise ValueError("export validation failed: placed blocks disagree "
                             f"with the plan ({v['blocks_placed']} vs {pixels} pixels, "
                             f"mode {settings.construction!r})")
    if "litematic" in settings.formats:
        validation["litematic"] = schem.validate_litematic(Path(files["litematic"]), expect=expect)
    if "structure" in settings.formats:
        validation["structure"] = schem.validate_structure(Path(files["structure"]), expect=expect)
    files["validation"] = validation
    if progress:
        progress(1.0, "done")
    return {"result": result, "files": files}


def suggest_layout(image_path, max_maps: int = 8) -> dict:
    """Headless auto-layout helper: reads the image and returns the map grid
    that preserves its native resolution (capped at max_maps per axis)."""
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"image not found: {path}")
    img = mapart.load_image_rgba(path)
    mw, mh = mapart.suggest_layout(img, max_maps=max_maps)
    return {"image": str(path), "maps_w": mw, "maps_h": mh,
            "size_px": [img.shape[1], img.shape[0]],
            "canvas_blocks": [mw * MAP_SIZE, mh * MAP_SIZE]}
