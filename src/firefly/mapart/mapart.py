"""Minecraft map-art pipeline layer on top of the dithering engine.

Data path: image load -> normalize to layout -> per-map 128x128 match against
(version palette, shade-expanded) -> dithering (reuses firefly.evolution.dither
kernels adapted per-frame greedy) -> suppression -> block plan + counts.

Honesty notes live in the palette asset's _meta (approximation warning).
"""
from __future__ import annotations

import json
import math
import threading
from pathlib import Path

import numpy as np

ASSET = Path(__file__).resolve().parents[3] / "assets" / "mc_palettes.json"
if not ASSET.exists():  # portable layout fallback
    ASSET = Path(__file__).resolve().parents[2] / "assets" / "mc_palettes.json"
if not ASSET.exists():
    raise FileNotFoundError("mc_palettes.json asset not found")

MAP_SIZE = 128

# Shade math is owned by rendermodel (vanilla MapColor factors + integer
# truncation, source-verified for 1.8.9). Kept as a name for compatibility.
from . import rendermodel as _rendermodel
SHADE_MULTIPLIERS = _rendermodel.SHADE_MULTIPLIERS


class Canceled(Exception):
    pass


# ---------------- two-block combo mode (HYPOTHESIS — never verified in-game
# here; every output carries an explicit hypothesis label until an audit
# specimen passes through the real simulator) ----------------
DEFAULT_COMBO_FRONT_BLOCK = "minecraft:glass"  # classic no-map-color cover; ASSUMED


def compute_combo_cover(idx: np.ndarray, ratio: float, seed: int = 0,
                        front_block: str = DEFAULT_COMBO_FRONT_BLOCK):
    """Deterministic per-pixel cover decision for two-block mode.

    The cover is a color-NEUTRAL assumption in our model: the visible block
    keeps its palette color entry; a front cover block (assumed to carry no
    map color) is recorded as an EXTRA construction layer. Colors are byte-
    identical to non-combo runs by construction — nothing visual changes, so
    no visual claim is attached to combos. ratio controls what share of pixels
    gets a cover (seeded deterministic mask, identical runs reproduce it).
    Returns (cover_bool_grid, cover_block_string)."""
    if not 0.0 <= ratio <= 1.0:
        raise ValueError(f"combo_ratio must be within [0, 1]; got {ratio}")
    h, w = idx.shape
    if ratio == 0.0:
        return np.zeros((h, w), dtype=bool), front_block
    if ratio == 1.0:
        return np.ones((h, w), dtype=bool), front_block
    # deterministic per-pixel draws: frame-stable, reproducible, no RNG drift
    q = (np.arange(h * w, dtype=np.float64).reshape(h, w) * 0.618033988749895) % 1.0
    jitter = (stable_frame_seed(int(seed), 0, 0) % 1013) / 1013.0
    cover = ((q + jitter) % 1.0) < ratio
    return np.asarray(cover, dtype=bool), front_block


class Palette:
    def __init__(self, version_key: str, data: dict, kind: str = "blocks"):
        ver = data["versions"][version_key]
        if kind not in ("blocks", "carpets"):
            raise ValueError(f"unknown palette kind {kind!r}")
        self.version_key = version_key
        self.kind = kind
        self.description = ver.get("description", "")
        self.source = ver.get("source", data["_meta"].get("source", ""))
        self.target_mc_version = ver.get("target_mc_version", "")
        self.verification_status = ver.get("verification_status",
                                           data["_meta"].get("accuracy", ""))
        colors = []
        for c in ver["colors"]:
            if not c.get("survival", True):
                continue
            block = c["block"]
            if kind == "carpets" and not block.endswith("_wool"):
                continue  # carpet-only palettes keep only colors with a carpet twin
            for shade_index in range(len(_rendermodel.SHADE_FACTORS)):
                # exact vanilla arithmetic: (channel * factor) // 255
                rgb = _rendermodel.shade_rgb(c["rgb"], shade_index)
                if kind == "carpets":
                    block = c["block"][: -len("_wool")] + "_carpet"
                colors.append({"map_color": c["id"], "rgb": rgb, "shade": shade_index,
                               "block": block, "name": c["name"]})
        self.colors = colors
        arr = np.array([c["rgb"] for c in colors], dtype=np.float32)
        self.arr = arr
        self.map_color_ids = np.array([c["map_color"] for c in colors])
        self.shades = np.array([c["shade"] for c in colors])
        self.blocks = [c["block"] for c in colors]
        self.meta_note = data["_meta"]

    @property
    def n(self) -> int:
        return len(self.colors)


def available_versions() -> dict:
    data = json.loads(ASSET.read_text(encoding="utf-8"))
    return {k: v.get("description", "") for k, v in data["versions"].items()}


def load_palette(version_key: str, kind: str = "blocks") -> Palette:
    data = json.loads(ASSET.read_text(encoding="utf-8"))
    if version_key not in data["versions"]:
        raise ValueError(f"unknown palette version {version_key!r}; "
                         f"available: {sorted(data['versions'])}")
    pal = Palette(version_key, data, kind=kind)
    try:
        from . import palette_audit
        palette_audit.attach_audit(pal)  # sidecar verdicts -> per-color status
    except ImportError:  # audit module missing (trimmed builds): status stays as shipped
        pass
    return pal


def load_image_rgb(path) -> np.ndarray:
    from PIL import Image  # Pillow-only at call time
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"))


def normalize(img: np.ndarray, maps_w: int, maps_h: int, fit: str = "cover",
              bg_color=(15, 15, 15)) -> np.ndarray:
    """Resize/crop image to maps_w*128 x maps_h*128 RGB uint8.

    fit modes:
      cover   - scale up so the frame is fully covered, then center-crop
                (default; loses edges)
      contain - scale down to fit REMAINING fully inside the frame, padded
                with bg_color (letterbox; nothing lost, dead zones added)
      stretch - scale non-uniformly to exactly fill the frame (aspect distorted)
      crop    - center-crop the ORIGINAL resolution to the frame ratio first,
                then scale down to the exact frame (no upscaling if source is
                already large enough)
    """
    from PIL import Image

    tw, th = maps_w * MAP_SIZE, maps_h * MAP_SIZE
    im = Image.fromarray(img.astype(np.uint8), "RGB")
    if fit == "cover":
        scale = max(tw / im.width, th / im.height)
        im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
        left = (im.width - tw) // 2
        top = (im.height - th) // 2
        return np.asarray(im.crop((left, top, left + tw, top + th)))
    if fit == "contain":
        scale = min(tw / im.width, th / im.height)
        im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
        canvas = Image.new("RGB", (tw, th), tuple(bg_color))
        canvas.paste(im, ((tw - im.width) // 2, (th - im.height) // 2))
        return np.asarray(canvas)
    if fit == "stretch":
        im = im.resize((tw, th), Image.LANCZOS)
        return np.asarray(im)
    if fit == "crop":
        scale = max(1.0, tw / im.width, th / im.height)  # never upscale beyond source
        im2 = im.resize((max(1, min(im.width, round(im.width * scale))),
                         max(1, min(im.height, round(im.height * scale)))), Image.LANCZOS)
        left = max(0, (im2.width - tw) // 2)
        top = max(0, (im2.height - th) // 2)
        return np.asarray(im2.crop((left, top, min(im2.width, left + tw),
                                    min(im2.height, top + th))))[:th, :tw]
    raise ValueError(f"unknown fit mode {fit!r}")


def suggest_layout(img: np.ndarray, max_maps: int = 8) -> tuple[int, int]:
    """Automatic multi-map layout: one map per 128x128 px region of the source
    (ceil(w/128) x ceil(h/128), capped at max_maps per axis), preserving the
    native resolution. Cover-fit then crops at most 127 px per axis of
    overshoot. Returns (maps_w, maps_h).
    """
    h, w = img.shape[:2]
    maps_w = min(max(1, math.ceil(w / MAP_SIZE)), max_maps)
    maps_h = min(max(1, math.ceil(h / MAP_SIZE)), max_maps)
    return maps_w, maps_h


def load_image_rgba(path) -> np.ndarray:
    """Load as HxWx4 uint8 RGBA (alpha preserved for transparency handling)."""
    from PIL import Image  # Pillow-only at call time
    with Image.open(path) as im:
        return np.asarray(im.convert("RGBA"))


def apply_transparency(rgba: np.ndarray, mode: str = "matte",
                       bg_color=(15, 15, 15)) -> np.ndarray:
    """Fold alpha into an RGB frame.

    modes: 'matte'  - blend over bg_color by alpha (anti-aliased edges kept)
           'binary' - opaque alpha threshold 128: keep original color or bg
           'ignore' - drop alpha entirely (treated as fully opaque)
    """
    if mode == "ignore":
        return np.ascontiguousarray(rgba[..., :3])
    a = rgba[..., 3].astype(np.float32) / 255.0
    if mode == "matte":
        m = a[..., None]
        out = rgba[..., :3].astype(np.float32) * m + np.array(bg_color, np.float32) * (1.0 - m)
        return np.clip(out, 0, 255).astype(np.uint8)
    if mode == "binary":
        out = np.array(bg_color, np.uint8)[None, None, :].repeat(rgba.shape[0], axis=0).repeat(rgba.shape[1], axis=1)
        mask = rgba[..., 3] >= 128
        out[mask] = rgba[..., :3][mask]
        return np.ascontiguousarray(out)
    raise ValueError(f"unknown transparency mode {mode!r}")


def preprocess(img: np.ndarray, brightness: float = 0.0, contrast: float = 1.0,
               saturation: float = 1.0, gamma: float = 1.0) -> np.ndarray:
    """Deterministic color correction: brightness offset, contrast, saturation
    (via luma), gamma. All identity-neutral defaults; pure functions of the
    inputs (no RNG, no time) so identical settings reproduce byte-identical
    results.
    """
    w = img.astype(np.float32)
    if brightness:
        w = w + float(brightness) * 255.0
    if contrast != 1.0:
        w = (w - 127.5) * float(contrast) + 127.5
    if saturation != 1.0:
        luma = (0.299 * w[..., 0] + 0.587 * w[..., 1] + 0.114 * w[..., 2])[..., None]
        w = luma + (w - luma) * float(saturation)
    if gamma != 1.0 and gamma > 0:
        w = 255.0 * np.power(np.clip(w / 255.0, 0.0, 1.0), float(gamma))
    return np.clip(w, 0, 255).astype(np.uint8)


def tile_overlay(preview: np.ndarray, maps_w: int, maps_h: int) -> np.ndarray:
    """Final map-art layout view: the reconstruction with map-tile boundaries
    and per-map indices drawn in (white grid + blue tile ids at top-left).
    """
    w = preview.copy()
    for my in range(maps_h + 1):
        y = min(w.shape[0] - 1, my * MAP_SIZE)
        w[y, :, :] = (255, 255, 255)
    for mx in range(maps_w + 1):
        x = min(w.shape[1] - 1, mx * MAP_SIZE)
        w[:, x, :] = (255, 255, 255)
    for my in range(maps_h):
        for mx in range(maps_w):
            x0, y0 = mx * MAP_SIZE + 3, my * MAP_SIZE + 3
            idx = my * maps_w + mx
            _stamp_label(w, x0, y0, idx)
    return w


def _stamp_label(w: np.ndarray, x0: int, y0: int, idx: int) -> None:
    """Tiny 3x5 digit glyph stamp (digits 0-3 sufficient for 8x8 grids)."""
    glyphs = {
        "0": ((1, 1, 1), (1, 0, 1), (1, 0, 1), (1, 0, 1), (1, 1, 1)),
        "1": ((0, 1, 0), (1, 1, 0), (0, 1, 0), (0, 1, 0), (0, 1, 0)),
        "2": ((1, 1, 1), (0, 0, 1), (1, 1, 1), (1, 0, 0), (1, 1, 1)),
        "3": ((1, 1, 1), (0, 0, 1), (0, 1, 1), (0, 0, 1), (1, 1, 1)),
        "4": ((1, 0, 1), (1, 0, 1), (1, 1, 1), (0, 0, 1), (0, 0, 1)),
        "5": ((1, 1, 1), (1, 0, 0), (1, 1, 1), (0, 0, 1), (1, 1, 1)),
        "6": ((1, 1, 1), (1, 0, 0), (1, 1, 1), (1, 0, 1), (1, 1, 1)),
        "7": ((1, 1, 1), (0, 0, 1), (0, 0, 1), (0, 0, 1), (0, 0, 1)),
        "8": ((1, 1, 1), (1, 0, 1), (1, 1, 1), (1, 0, 1), (1, 1, 1)),
        "9": ((1, 1, 1), (1, 0, 1), (1, 1, 1), (0, 0, 1), (0, 0, 1)),
    }
    for ch_x, ch in enumerate(str(idx)):
        g = glyphs[ch]
        for gy, row in enumerate(g):
            for gx, on in enumerate(row):
                if on:
                    w[y0 + gy, x0 + ch_x * 4 + gx] = (60, 120, 255)


def _shade_palette(pal: Palette) -> np.ndarray:
    return pal.arr - 0.0  # already shade-expanded


# ---------------- color metrics ----------------
_M_LAB = np.array([[0.4124564, 0.3575761, 0.1804375],
                   [0.2126729, 0.7151522, 0.0721750],
                   [0.0193339, 0.1191920, 0.9503041]], dtype=np.float64)


def srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """CIE L*a*b* (D65) from 0..255 sRGB; vectorized, any leading shape."""
    c = np.clip(rgb.astype(np.float64) / 255.0, 0.0, 1.0)
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = lin @ _M_LAB.T
    xyz /= np.array([0.95047, 1.0, 1.08883])
    eps, kappa = 216.0 / 24389.0, 24389.0 / 27.0
    f = np.where(xyz > eps, np.cbrt(xyz), (kappa * xyz + 16.0) / 116.0)
    return np.stack([116.0 * f[..., 1] - 16.0,
                     500.0 * (f[..., 0] - f[..., 1]),
                     200.0 * (f[..., 1] - f[..., 2])], axis=-1)


def ciede2000(lab1: np.ndarray, lab2: np.ndarray) -> np.ndarray:
    """CIEDE2000 color difference, broadcast over leading axes (last dim = 3).

    Reference: Sharma, Wu, Dalal (2005) implementation notes.
    """
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]
    C1 = np.hypot(a1, b1)
    C2 = np.hypot(a2, b2)
    Cbar = (C1 + C2) / 2.0
    G = 0.5 * (1.0 - np.sqrt(Cbar ** 7 / (Cbar ** 7 + 25.0 ** 7)))
    ap1 = (1.0 + G) * a1
    ap2 = (1.0 + G) * a2
    Cp1 = np.hypot(ap1, b1)
    Cp2 = np.hypot(ap2, b2)
    hp1 = np.degrees(np.arctan2(b1, ap1)) % 360.0
    hp2 = np.degrees(np.arctan2(b2, ap2)) % 360.0
    dL = L2 - L1
    dC = Cp2 - Cp1
    dhp = np.where(Cp1 * Cp2 == 0, 0.0,
                   np.where(np.abs(hp2 - hp1) <= 180.0, hp2 - hp1,
                            np.where(hp2 - hp1 > 180.0, hp2 - hp1 - 360.0, hp2 - hp1 + 360.0)))
    dH = 2.0 * np.sqrt(Cp1 * Cp2) * np.sin(np.radians(dhp) / 2.0)
    Lbp = dL / 2.0
    Cpbar = (Cp1 + Cp2) / 2.0
    hpbar = np.where(Cp1 * Cp2 == 0, hp1 + hp2,
                     np.where(np.abs(hp1 - hp2) <= 180.0, (hp1 + hp2) / 2.0,
                              np.where(hp1 + hp2 < 360.0, (hp1 + hp2 + 360.0) / 2.0,
                                       (hp1 + hp2 - 360.0) / 2.0)))
    T = (1.0 - 0.17 * np.cos(np.radians(hpbar - 30.0))
         + 0.24 * np.cos(np.radians(2.0 * hpbar))
         + 0.32 * np.cos(np.radians(3.0 * hpbar + 6.0))
         - 0.20 * np.cos(np.radians(4.0 * hpbar - 63.0)))
    dtheta = 30.0 * np.exp(-(((hpbar - 275.0) / 25.0) ** 2))
    RC = 2.0 * np.sqrt(Cpbar ** 7 / (Cpbar ** 7 + 25.0 ** 7))
    SL = 1.0 + 0.015 * (Lbp ** 2) / np.sqrt(20.0 + Lbp ** 2)
    SC = 1.0 + 0.045 * Cpbar
    SH = 1.0 + 0.015 * Cpbar * T
    RT = -np.sin(np.radians(2.0 * dtheta)) * RC
    return np.sqrt((dL / SL) ** 2 + (dC / SC) ** 2 + (dH / SH) ** 2
                   + RT * (dC / SC) * (dH / SH))


def frame_distance(metric: str, cand_prev: np.ndarray):
    """Distance function d(px, cand_colors) -> costs.

    Accepts EITHER a single pixel ``px`` of shape (3,) -> returns (n_cand,)
    (used by the sequential error-diffusion path), OR a block of pixels of
    shape (n, 3) -> returns (n, n_cand) computed FULLY VECTORIZED (used by the
    non-sequential batched quantization path). The block form removes the
    per-pixel Python overhead; CIEDE2000 over a block is sub-chunked so the
    (chunk x n_cand x 3) broadcast intermediates stay within low-RAM budgets.
    Returned closure keeps the metric-specific candidate preprocessing
    (Lab / CIEDE2000 need cand_lab; rgb needs nothing).
    """
    if metric == "rgb":
        def d(px, cand_colors):
            if px.ndim == 1:
                dd = px[None, :] - cand_colors
                return np.sum(dd * dd, axis=1)
            dd = px[:, None, :] - cand_colors[None, :, :]
            return np.einsum("ijk,ijk->ij", dd, dd)
    elif metric == "lab":
        cand_lab = cand_prev
        def d(px, cand_colors):
            if px.ndim == 1:
                pl = srgb_to_lab(px[None, :])[0]
                return np.linalg.norm(cand_lab - pl, axis=1)  # (n_cand,)
            block_lab = srgb_to_lab(px)                        # (n,3)
            diff = block_lab[:, None, :] - cand_lab[None, :, :]
            return np.linalg.norm(diff, axis=2)                # (n,n_cand)
    elif metric == "ciede2000":
        cand_lab = cand_prev
        n_cand = int(cand_lab.shape[0])
        # Bound (chunk x n_cand) so the ~25 CIEDE2000 intermediates of the
        # (chunk, n_cand, 3) broadcast stay well under a few hundred MB.
        chunk = int(max(1, min(4096, 131072 // max(1, n_cand))))
        def d(px, cand_colors):
            if px.ndim == 1:
                pl = srgb_to_lab(px[None, :])[0]
                return ciede2000(pl[None, :], cand_lab)       # (n_cand,)
            block_lab = srgb_to_lab(px)                        # (n,3)
            n = block_lab.shape[0]
            out = np.empty((n, n_cand), dtype=np.float64)
            for s in range(0, n, chunk):
                e = min(s + chunk, n)
                out[s:e] = ciede2000(block_lab[s:e, None, :],
                                     cand_lab[None, :, :])     # (chunk,n_cand)
            return out
    else:
        raise ValueError(f"unknown metric {metric!r}")
    return d


def match_and_dither(img: np.ndarray, pal: Palette, algorithm: str,
                     strength: float, suppress: set, gradual: bool = False,
                     cancel: threading.Event | None = None,
                     bias: int = 0, seed: int = 0,
                     metric: str = "rgb", exclude_blocks: set | None = None,
                     block_costs: dict | None = None,
                     neighbor_optimize: bool = False) -> np.ndarray:
    """Return per-pixel index into pal.colors for the full (tw x th) image,
    processed map-frame by map-frame with error diffusion *inside* each frame
    (map art is per-map, so dither seeds reset at frame boundaries).

    metric: 'rgb' | 'lab' | 'ciede2000' — the color-distance used for the
      per-pixel decision (see make/frame distance helpers above).
    exclude_blocks: block id strings that must never be placed.
    block_costs: {block: cost} — a per-block construction/resource penalty
      added to the pixel's color distance * block_penalty weight (cost 1 =
      as expensive as 1 unit of distance).
    neighbor_optimize: post-pass that swaps the palette choices of adjacent
      pixels when doing so strictly lowers the total penalized distance.
    """
    from firefly.evolution import dither as d  # reuse kernels via our re-impl below

    h, w, _ = img.shape
    assert h % MAP_SIZE == 0 and w % MAP_SIZE == 0
    exclude_blocks = set(exclude_blocks or ())
    block_costs = dict(block_costs or {})
    out = np.empty((h, w), dtype=np.int16)
    pal_arr = pal.arr
    for my in range(h // MAP_SIZE):
        for mx in range(w // MAP_SIZE):
            if cancel is not None and cancel.is_set():
                raise Canceled()
            frame = img[my * MAP_SIZE:(my + 1) * MAP_SIZE,
                        mx * MAP_SIZE:(mx + 1) * MAP_SIZE].astype(np.float32)
            # construction constraint: candidate shades must be reachable
            # under the version's shade rule (source-verified 1.8.9 emits
            # only codes 0/1/2; unverified versions keep all shades eligible)
            reach = set(_rendermodel.rule_provenance(
                pal.target_mc_version).get("reachable_shades", (0, 1, 2, 3)))
            allowed = [i for i, c in enumerate(pal.colors)
                       if c["map_color"] not in suppress and c["block"] not in exclude_blocks
                       and int(c["shade"]) in reach]
            if not allowed:
                raise ValueError("every palette color is suppressed/excluded — empty palette")
            sub_arr = pal_arr[allowed]
            cand_bias = np.array([float(block_costs.get(pal.blocks[i], 0.0))
                                  for i in allowed], dtype=np.float32)
            metric_cand = srgb_to_lab(sub_arr) if metric in ("lab", "ciede2000") else None
            dist_fn = frame_distance(metric, metric_cand) if metric != "rgb" else None
            idx = _match_frame(frame, sub_arr, algorithm, strength, cancel,
                               bias=int(bias), seed=stable_frame_seed(seed, my, mx),
                               dist_fn=dist_fn, cand_bias=cand_bias,
                               allowed=np.array(allowed, dtype=np.int16))
            if neighbor_optimize:
                idx = _neighbor_polish(idx, frame, sub_arr, metric, metric_cand,
                                       cand_bias)
            out[my * MAP_SIZE:(my + 1) * MAP_SIZE, mx * MAP_SIZE:(mx + 1) * MAP_SIZE] = \
                np.array(allowed, dtype=np.int16)[idx]
    return out


def total_penalized_error(idx: np.ndarray, frame: np.ndarray, sub_arr: np.ndarray,
                          metric: str, metric_cand, cand_bias) -> float:
    """Sum of the penalized per-pixel color distance for a frame's choices."""
    if metric == "rgb":
        dd = frame.reshape(-1, 3) - sub_arr[idx.reshape(-1)]
        e = np.einsum("ij,ij->i", dd, dd)
    elif metric == "lab":
        e = np.linalg.norm(metric_cand[idx.reshape(-1)]
                           - srgb_to_lab(frame.reshape(-1, 3)), axis=1)
    else:
        e = ciede2000(srgb_to_lab(frame.reshape(-1, 3))[None, :],
                      metric_cand[idx.reshape(-1)])[0]
    return float(e.sum() + cand_bias[idx.reshape(-1)].sum())


def _neighbor_polish(idx: np.ndarray, frame: np.ndarray, sub_arr: np.ndarray,
                     metric: str, metric_cand, cand_bias) -> np.ndarray:
    """Bounded local search: repeatedly swap the palette choices of
    horizontally adjacent pixels when the swap strictly lowers the frame's
    total penalized distance (single-point swaps per pass; capped passes).
    """
    h, w = idx.shape
    if metric == "rgb":
        flat_phi = frame.reshape(-1, 3)
        E = np.einsum("ij,ij->i", flat_phi - sub_arr[idx.reshape(-1)],
                      flat_phi - sub_arr[idx.reshape(-1)]) + cand_bias[idx.reshape(-1)]
    else:
        flat_lab = srgb_to_lab(frame.reshape(-1, 3))
        if metric == "lab":
            E = np.linalg.norm(metric_cand[idx.reshape(-1)] - flat_lab, axis=1)
        else:
            E = ciede2000(flat_lab[None, :], metric_cand[idx.reshape(-1)])[0]
        E = E + cand_bias[idx.reshape(-1)]
    flat = idx.reshape(-1)
    for _pass in range(4):
        Improved_any = False
        for x in range(w - 1):
            for y in range(h):
                a = y * w + x
                b = a + 1
                ia, ib = flat[a], flat[b]
                if ia == ib:
                    continue
                # error of pixel a with ib's color, pixel b with ia's color
                if metric == "rgb":
                    da = np.einsum("ij,ij->i",
                                   flat_phi[a][None, :] - sub_arr[ib][None, :],
                                   flat_phi[a][None, :] - sub_arr[ib][None, :])[0] + cand_bias[ib]
                    db = np.einsum("ij,ij->i",
                                   flat_phi[b][None, :] - sub_arr[ia][None, :],
                                   flat_phi[b][None, :] - sub_arr[ia][None, :])[0] + cand_bias[ia]
                elif metric == "lab":
                    pa = metric_cand[ib] - flat_lab[a]; pb = metric_cand[ia] - flat_lab[b]
                    da = pa @ pa + cand_bias[ib]
                    db = pb @ pb + cand_bias[ia]
                else:
                    da = ciede2000(flat_lab[a][None, :], metric_cand[ib][None, :])[0] + cand_bias[ib]
                    db = ciede2000(flat_lab[b][None, :], metric_cand[ia][None, :])[0] + cand_bias[ia]
                gain = (E[a] + E[b]) - (da + db)
                if gain > 1e-9:
                    flat[a], flat[b] = ib, ia
                    E[a], E[b] = da, db
                    Improved_any = True
        if not Improved_any:
            break
    return flat.reshape(h, w)


def stable_frame_seed(seed: int, my: int, mx: int) -> int:
    import hashlib as _h
    b = _h.sha256(f"{seed}|{my}|{mx}".encode()).digest()
    return int.from_bytes(b[:4], "big") % (2 ** 31)


def _match_frame(frame: np.ndarray, sub_arr: np.ndarray, algorithm: str,
                 strength: float, cancel, bias: int = 0, seed: int = 0,
                 dist_fn=None, cand_bias=None, allowed=None) -> np.ndarray:
    h, w, _ = frame.shape
    n = sub_arr.shape[0]
    gamma = None if algorithm != "bayer8" else 1.0  # reserved
    if algorithm in ("none",):
        pass
    work = frame.copy()
    if algorithm == "threshold":
        work = work + float(bias)     # global shift then straight quantization
        work = np.clip(work, 0.0, 255.0)
    elif algorithm in ("floyd_steinberg", "atkinson", "burkes", "sierra_lite", "jjn", "stucki"):
        from firefly.evolution.dither import ERROR_KERNELS
        kernel, div = ERROR_KERNELS[algorithm]
        for y in range(h):
            if cancel is not None and cancel.is_set():
                raise Canceled()
            for x in range(w):
                v = np.clip(work[y, x], 0.0, 255.0)
                if dist_fn is not None:
                    costs = dist_fn(v, sub_arr)
                else:
                    dd = v[None, :] - sub_arr
                    costs = np.sum(dd * dd, axis=1)
                if cand_bias is not None:
                    costs = costs + cand_bias
                i = int(np.argmin(costs))
                p = sub_arr[i]
                err = (v - p) * strength
                for dx, dy, wk in kernel:
                    xx, yy = x + dx, y + dy
                    if 0 <= xx < w and 0 <= yy < h:
                        work[yy, xx] += err * (wk / div)
                work[y, x] = p
    else:
        from firefly.evolution.dither import BAYER_ALGORITHMS, _bayer_matrix
        work = frame.copy()
        if algorithm in BAYER_ALGORITHMS:
            n_cells = BAYER_ALGORITHMS[algorithm]
            m = _bayer_matrix(n_cells)
            tile = np.tile(m, ((h + n_cells - 1) // n_cells, (w + n_cells - 1) // n_cells))[:h, :w]
            work = work + (tile[..., None] - 0.5) * strength * 96.0
        if algorithm == "random":
            import numpy as _np
            rng = _np.random.default_rng(seed)
            u = rng.random(frame.shape[:2], dtype=np.float32)
            work = work + (u[..., None] - 0.5) * float(strength) * 255.0
        work = np.clip(work, 0.0, 255.0)
    flat = work.reshape(-1, 3)
    idx = np.empty(flat.shape[0], dtype=np.int64)
    step = 8192
    for s in range(0, flat.shape[0], step):
        if cancel is not None and cancel.is_set():
            raise Canceled()
        block = flat[s:s + step]
        if dist_fn is not None:
            # vectorized block form: (n, n_cand) in one call (no per-pixel loop)
            costs = dist_fn(block, sub_arr)
        else:
            dd = block[:, None, :] - sub_arr[None, :, :]
            costs = np.sum(dd * dd, axis=2)
        if cand_bias is not None:
            costs = costs + cand_bias[None, :]
        idx[s:s + step] = np.argmin(costs, axis=1)
    return idx.reshape(h, w)


def color_indices_to_blocks(idx: np.ndarray, pal: Palette) -> list:
    """idx (H,W) of palette color indices -> (H,W) block id strings."""
    blocks = np.array(pal.blocks, dtype=object)
    return blocks[idx.reshape(-1)].reshape(idx.shape).tolist()


def block_counts(idx: np.ndarray, pal: Palette) -> dict:
    counts: dict = {}
    names = {c["block"]: c["name"] for c in pal.colors}
    for color_index, count in zip(*np.unique(idx, return_counts=True)):
        blk = pal.blocks[int(color_index)]
        counts.setdefault(blk, {"item": names.get(blk, blk), "count": 0})
        counts[blk]["count"] += int(count)
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]["count"]))


def explain_plan(idx: np.ndarray, img: np.ndarray, pal: Palette, *,
                 metric: str = "ciede2000", suppress=frozenset(),
                 exclude_blocks=frozenset(), block_costs: dict | None = None,
                 algorithm: str | None = None, strength: float = 1.0,
                 sample: int = 1024, seed: int = 0) -> dict:
    """Machine-readable explanation of the plan's objective and choices.

    For a deterministic pixel sample: the predicted-color error of the chosen
    candidate, the best feasible candidate's error, the resulting dither
    penalty (chosen - best), and the constraint set that shaped the choice.
    Uses the same palette/render model as the matcher, preview and export.
    """
    reach = set(_rendermodel.rule_provenance(
        pal.target_mc_version).get("reachable_shades", (0, 1, 2, 3)))
    allowed = [i for i, c in enumerate(pal.colors)
               if c["map_color"] not in suppress
               and c["block"] not in exclude_blocks
               and int(c["shade"]) in reach]
    if not allowed:
        raise ValueError("explain_plan: every candidate is constrained out")
    allowed_arr = np.array(allowed, dtype=np.int64)
    sub = pal.arr[allowed_arr]
    bias = np.array([float((block_costs or {}).get(pal.blocks[i], 0.0))
                     for i in allowed], dtype=np.float32)
    flat_img = img.reshape(-1, 3).astype(np.float32)
    flat_idx = idx.reshape(-1).astype(np.int64)
    n_px = int(flat_idx.size)
    n = int(min(sample, n_px))
    rng = np.random.default_rng(int(seed))
    sel = np.sort(rng.choice(n_px, size=n, replace=False)) if n < n_px else np.arange(n_px)
    if metric == "rgb":
        dd = flat_img[sel][:, None, :] - sub[None, :, :]
        costs = np.einsum("ijk,ijk->ij", dd, dd)
    else:
        cand_lab = srgb_to_lab(sub)
        px_lab = srgb_to_lab(flat_img[sel])
        if metric == "lab":
            costs = np.linalg.norm(cand_lab[None, :, :] - px_lab[:, None, :], axis=2)
        else:
            chunk = int(max(1, min(4096, 131072 // max(1, len(allowed_arr)))))
            costs = np.empty((n, len(allowed_arr)), dtype=np.float64)
            for s in range(0, n, chunk):
                e = min(s + chunk, n)
                costs[s:e] = ciede2000(px_lab[s:e, None, :], cand_lab[None, :, :])
    costs = costs + bias[None, :]
    chosen_pos = np.searchsorted(allowed_arr, flat_idx[sel])
    rows = np.arange(n)
    err_chosen = costs[rows, chosen_pos]
    best = costs.min(axis=1)
    order = np.sort(costs, axis=1)
    margin = (order[:, 1] - order[:, 0]) if costs.shape[1] > 1 else np.zeros(n)
    gap = err_chosen - best
    shades = pal.shades[flat_idx[sel]]
    hist = {int(s): int((shades == s).sum()) for s in sorted(set(shades.tolist()))}
    used: dict = {}
    for i in flat_idx[sel]:
        b = pal.blocks[int(i)]
        used[b] = used.get(b, 0) + 1
    top_blocks = sorted(used.items(), key=lambda kv: -kv[1])[:8]
    return {
        "metric": metric,
        "objective": "minimize predicted map-color error vs the processed "
                     "source; per-pixel argmin over feasible (block, shade) "
                     "candidates; dithering perturbs decisions within each map "
                     "frame (strength scales the diffused error)",
        "dither": {"algorithm": algorithm, "strength": float(strength)},
        "constraints": {
            "suppressed_map_colors": sorted(int(x) for x in suppress),
            "excluded_blocks": sorted(exclude_blocks),
            "unreachable_shade_codes": sorted(
                {int(c["shade"]) for c in pal.colors} - reach),
            "shade_rule_status": _rendermodel.rule_provenance(
                pal.target_mc_version).get("status"),
        },
        "candidates_total": len(pal.colors),
        "candidates_allowed": len(allowed),
        "sample_pixels": n,
        "chosen_error_mean": round(float(err_chosen.mean()), 4),
        "chosen_error_p95": round(float(np.percentile(err_chosen, 95)), 4),
        "chosen_error_max": round(float(err_chosen.max()), 4),
        "optimal_error_mean": round(float(best.mean()), 4),
        "dither_gap_mean": round(float(gap.mean()), 4),
        "optimal_choice_rate": round(float((gap < 1e-9).mean()), 4),
        "runner_up_margin_mean": round(float(margin.mean()), 4),
        "chosen_shade_histogram": hist,
        "top_blocks": [{"block": b, "count": c} for b, c in top_blocks],
        "block_costs": dict(block_costs or {}),
        "honesty": "errors are predicted-color distances under the render "
                    "model on a pixel sample; not in-game measured output",
    }


def layer_plan(idx: np.ndarray, pal: Palette, maps_w: int, maps_h: int) -> list:
    """Layers 0..3 per frame: depth = shade index; returns per-frame rows of
    shade level ids so builders can lay floor blocks by depth."""
    layers = []
    frames = []
    h, w = idx.shape
    shades = pal.shades
    for my in range(maps_h):
        for mx in range(maps_w):
            sl = shades[idx[my * MAP_SIZE:(my + 1) * MAP_SIZE, mx * MAP_SIZE:(mx + 1) * MAP_SIZE]]
            frames.append(sl.astype(np.uint8).tolist())
    return frames


def preview_rgb(idx: np.ndarray, pal: Palette) -> np.ndarray:
    return pal.arr[idx.reshape(-1)].reshape(idx.shape[0], idx.shape[1], 3).astype(np.uint8)


class ProcessResult:
    def __init__(self, idx, pal, maps_w, maps_h, settings):
        self.idx = idx
        self.pal = pal
        self.maps_w, self.maps_h = maps_w, maps_h
        self.settings = settings
        self.blocks = color_indices_to_blocks(idx, pal)
        self.counts = block_counts(idx, pal)
        self.frames = layer_plan(idx, pal, maps_w, maps_h)
        self.preview = preview_rgb(idx, pal)

    @property
    def size_blocks(self):
        return (self.maps_w * MAP_SIZE, self.maps_h * MAP_SIZE)

    def summary(self) -> dict:
        return {
            "version": self.pal.version_key,
            "maps": f"{self.maps_w}x{self.maps_h}",
            "size_blocks": self.size_blocks,
            "blocks_total": sum(v["count"] for v in self.counts.values()),
            "blocks_unique": len(self.counts),
            "algorithm": self.settings["algorithm"],
            "suppress": sorted(self.settings["suppress"]),
            "note": self.pal.meta_note["note"],
        }
