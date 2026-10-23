"""Authoritative Minecraft map-render prediction model.

Single source of truth for how a (base map color, shade code) becomes a
rendered pixel and how construction heights become shade codes. Palette
expansion, the matcher, the preview, quality metrics and export validation
all consume this module instead of re-deriving the math.

Source basis (verbatim decompiled Minecraft 1.8.9, MCP-919 mapping set,
read in full; full URLs are recorded outside src/ in the engineering report):

* MapColor.getMapColor(int shade)
  MCP-919: net/minecraft/block/material/MapColor.java
      factor = {0: 180, 1: 220, 2: 255, 3: 135}[shade]
      channel = base_channel * factor / 255     (int arithmetic, TRUNCATED)
* ItemMap.updateMapData(...) per-pixel shade selection
  MCP-919: net/minecraft/item/ItemMap.java
      d2 = (d1 - d0) * 4.0 / (scale_size + 4) + (((x + z) & 1) - 0.5) * 0.4
      shade = 1; if d2 > 0.6 -> 2; if d2 < -0.6 -> 0
      d1 = height of the column (scan down past air-mapcolor blocks to the
           first non-air map color; averaged over the scale sample area)
      d0 = the previous pixel's height ALONG THE CHAIN (the inner scan),
           reset to 0.0 at the start of every chain. The first written pixel
           of a chain therefore compares against 0.0 when the outside chunk
           is absent, or against the height just outside the map edge when
           it is loaded — modeled by the `boundary` parameter.
      chains run along z for each x (from colors[x + z * 128] with x as the
           outer loop): the shade of (x, z) depends on h(x,z) - h(x,z-1) and
           the checkerboard parity (x + z) & 1. Flat terrain -> shade 1.
      IEEE note (float-faithful): the game compares doubles exactly like
           this module. A one-block step gives 0.8 - 0.2 = 0.6000000000000001
           (> 0.6) and -0.8 + 0.2 = -0.6000000000000001 (< -0.6), so at art
           scale (integer heights) a rise renders shade 2 and a drop renders
           shade 0 for BOTH parities; parity is inert for integer steps and
           only matters for fractional (averaged) heights at scale > 0.
           Verified shade table at scale 0: h even/flat -> 1, rise -> 2,
           drop -> 0.
      the update loop writes shade codes ONLY in {0, 1, 2}: code 3 (factor
           135) is defined by MapColor but never reachable from terrain.
      water overrides the rule with a depth+parity formula (water_shade).

Verification status by target version (VERSION_RULES):
  * 1.8    — SOURCE-VERIFIED for both arithmetic and shade rule.
  * 1.15.2 — NOT SOURCE-VERIFIED. No update-method source for this version
             could be retrieved; the 1.8 rule is applied as an explicit,
             labeled assumption everywhere it is used.
  * Shade-3 palette entries are flagged unverified-constructible: the
             verified rule can never emit code 3.

Honesty rules for callers: a prediction from this module is only as true as
the verification status attached to the palette's target version — surface
`rule_provenance()` / `model_provenance()` output alongside any claim.
Nothing here touches the network or game binaries; all functions are pure
numpy/python and deterministic.
"""
from __future__ import annotations

import numpy as np

# index = shade code; vanilla MapColor.getMapColor factors (verbatim 1.8.9)
SHADE_FACTORS = (180, 220, 255, 135)
# display floats (f / 255) kept for backward-compatible callers; the
# AUTHORITY is the integer factor above, applied with truncation.
SHADE_MULTIPLIERS = tuple(f / 255.0 for f in SHADE_FACTORS)

# shade codes the verified update loop can actually emit for terrain
REACHABLE_TERRAIN_SHADES = (0, 1, 2)

# Source identifiers only: shipped code must stay free of network-looking
# literals (test_engine.offline_no_network_imports). Full URLs live in the
# engineering report / docs, not in src/.
SOURCES = {
    "mapcolor_1_8_9": "MCP-919 decompiled: net/minecraft/block/material/MapColor.java",
    "itemmap_1_8_9": "MCP-919 decompiled: net/minecraft/item/ItemMap.java",
}

# target_mc_version (as found in assets/mc_palettes.json) -> rule status.
VERSION_RULES = {
    "1.8": {
        "arithmetic": "source-verified (MapColor.getMapColor, integer truncation)",
        "shade_rule": "source-verified (ItemMap.updateMapData scan-derivative + parity)",
        "reachable_shades": list(REACHABLE_TERRAIN_SHADES),
        "status": "source-verified",
    },
    "1.15.2": {
        "arithmetic": "assumed identical to 1.8.9 (API docs still describe "
                      "base color + brightness; update-method source NOT retrieved)",
        "shade_rule": "ASSUMPTION — 1.8.9 scan-derivative rule applied; "
                      "no 1.15.2 update-method source could be verified",
        # Unverified: do NOT drop shade 3 on an assumption. All four shades
        # stay candidate-eligible; the report labels the rule assumption.
        "reachable_shades": [0, 1, 2, 3],
        "reachable_shades_note": "unknown for this version — all shades kept "
                                 "eligible (shade 3 may also be unreachable, "
                                 "as in 1.8.9)",
        "status": "unverified-assumption",
    },
}


def rule_provenance(target_mc_version: str) -> dict:
    """Verification labels for the render rule of one target version.

    Unknown versions are labeled fully unverified — never silently assumed.
    """
    known = VERSION_RULES.get(target_mc_version)
    if known:
        return dict(known, target_version=target_mc_version, sources=dict(SOURCES))
    return {"target_version": target_mc_version,
            "arithmetic": "UNVERIFIED — version not in VERSION_RULES; "
                          "1.8.9 integer arithmetic applied as assumption",
            "shade_rule": "UNVERIFIED — 1.8.9 scan-derivative rule applied as assumption",
            "reachable_shades": list(REACHABLE_TERRAIN_SHADES),
            "status": "unverified-assumption",
            "sources": dict(SOURCES)}


def model_provenance() -> dict:
    """Consolidated provenance block for run reports / audits."""
    return {
        "model": "firefly-rendermodel/1",
        "arithmetic": "channel_out = (channel_base * SHADE_FACTORS[shade]) // 255 "
                      "(integer truncation, verbatim vanilla)",
        "shade_factors": list(SHADE_FACTORS),
        "shade_rule": "d2 = (h - h_prev) * 4/(scale_size+4) + ((x+z)&1 - 0.5)*0.4; "
                      "d2 > 0.6 -> shade 2; d2 < -0.6 -> shade 0; else shade 1; "
                      "chain resets per map column with boundary height",
        "flat_terrain_shade": 1,
        "unreachable_shade_codes": [3],
        "versions": {v: rule_provenance(v) for v in sorted(VERSION_RULES)},
        "sources": dict(SOURCES),
        "honesty": "prediction accuracy equals the verification status of the "
                   "target version; 1.8.9 arithmetic+rule are source-verified, "
                   "1.15.2 is a labeled assumption, palette base colors are a "
                   "separate approximation (asset _meta / palette audit)",
    }


# ---------------- exact shade arithmetic (source-verified) ----------------

def shade_rgb(base_rgb, shade) -> tuple[int, int, int]:
    """Rendered RGB of one base map color at one shade code.

    Vanilla arithmetic: (channel * factor) / 255 with INTEGER TRUNCATION.
    """
    f = SHADE_FACTORS[int(shade)]
    return tuple((int(c) * f) // 255 for c in base_rgb)


def shade_arr(base_arr: np.ndarray, shades: np.ndarray) -> np.ndarray:
    """Vectorized shade_rgb: base (N,3) + shades (N,) -> (N,3) uint8."""
    b = np.asarray(base_arr)
    s = np.asarray(shades, dtype=np.int64)
    f = np.asarray(SHADE_FACTORS, dtype=np.int64)[s]           # (N,)
    out = (b.astype(np.int64) * f[:, None]) // 255
    return np.clip(out, 0, 255).astype(np.uint8)


# ---------------- forward rule: heights -> shade codes ----------------

def shade_codes_from_heights(heights: np.ndarray, boundary: float = 0.0,
                             scale_bits: int = 0) -> np.ndarray:
    """Predict the shade code the verified update loop writes per pixel.

    heights : (H, W) top-of-column heights (world y of the visible block).
              Rows are map z, columns are map x.
    boundary: d0 at each chain's first pixel (z = 0). 0.0 is the literal
              source reading for an absent outside chunk; pass the height of
              the terrain just outside the map's south/north edge to model a
              loaded surrounding chunk (chains reset per x-column).
    scale_bits: map scale (0 = one block per map pixel, vanilla art scale).

    Returns (H, W) uint8 codes in {0, 1, 2} (code 3 is never emitted).
    """
    h = np.asarray(heights, dtype=np.float64)
    if h.ndim != 2:
        raise ValueError(f"heights must be 2-D (z, x); got shape {h.shape}")
    zz, xx = np.indices(h.shape, dtype=np.int64)
    parity = (zz + xx) & 1                       # (x + z) & 1, verbatim
    par_term = (parity - 0.5) * 0.4
    k = 4.0 / (float(1 << int(scale_bits)) + 4.0)
    codes = np.empty(h.shape, dtype=np.uint8)
    d0 = np.full(h.shape[1], float(boundary), dtype=np.float64)
    for z in range(h.shape[0]):
        d2 = (h[z] - d0) * k + par_term[z]
        codes[z] = np.where(d2 > 0.6, 2, np.where(d2 < -0.6, 0, 1))
        d0 = h[z]
    return codes


def water_shade(depth_blocks, parity) -> np.ndarray | int:
    """Shade code for water pixels (verbatim ItemMap water branch).

    d2 = depth * 0.1 + parity * 0.2; default 1; d2 < 0.5 -> 2; d2 > 0.9 -> 0.
    The verified palette candidates contain no water block, so this exists
    for model completeness and future water-capable palettes.
    """
    d2 = np.asarray(depth_blocks, dtype=np.float64) * 0.1 + \
        np.asarray(parity, dtype=np.float64) * 0.2
    out = np.where(d2 < 0.5, 2, np.where(d2 > 0.9, 0, 1)).astype(np.uint8)
    return int(out) if out.ndim == 0 else out


# ---------------- inverse rule: intended shades -> construction heights ----------------

def _dh_candidates(code: int, parity: int, max_step: int = 4):
    """Height deltas realizing `code` at a pixel (scale 0), ordered by
    |delta| (smallest staircase step first). Derived from the verified d2
    thresholds: 0.8 * dh +/- 0.2 with float64 comparisons, where a 1-block
    step crosses the threshold for both parities (see module docstring).
    `parity` is accepted for signature stability; it does not change the
    integer-step outcomes at art scale."""
    if code == 1:      # |d2| <= 0.6 -> only dh == 0
        return [0]
    if code == 2:      # d2 > 0.6 -> any rise >= 1
        return list(range(1, max_step + 1))
    if code == 0:      # d2 < -0.6 -> any drop <= -1
        return [-d for d in range(1, max_step + 1)]
    return []          # code 3: never reachable by the verified rule


def _chain_requirement(codes_col: np.ndarray) -> np.ndarray:
    """Backwards minimum height needed at each chain position so the REST of
    the chain stays buildable (>= y 0).

    Achievable per-code deltas at art scale: code 1 -> exactly 0; code 0 ->
    any drop (minimal -1); code 2 -> any rise (minimal +1, unbounded). Only
    drops consume height, and rises can always rebuild it, so the backwards
    requirement is: r[i] = r[i+1] (+1 if the next pixel drops) and reset to
    0 wherever the next pixel is a rise.
    """
    n = len(codes_col)
    req = np.zeros(n, dtype=np.int64)
    for i in range(n - 2, -1, -1):
        nxt = int(codes_col[i + 1])
        if nxt == 2:
            req[i] = 0                      # a rise can recover any deficit
        elif nxt == 0:
            req[i] = req[i + 1] + 1         # this drop costs one level
        else:
            req[i] = req[i + 1]             # code 1 cannot move
    return req


def solve_heights(codes: np.ndarray, boundary: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """Inverse of shade_codes_from_heights: construction heights whose forward
    evaluation realizes the intended shade map.

    Returns (heights int32 (H, W), feasible bool (H, W)).

    * `heights` is a valid build (all values >= 0) whenever feasible allows
      it; infeasible pixels keep chain continuity (delta 0 / best effort) and
      are flagged.
    * per-chain constructive solve WITH backwards lookahead: the cumulative
      requirement of all remaining drops is known before walking, so code-2
      pixels rise by as much as the rest of the chain needs. This makes the
      only remaining infeasibilities genuine: the first pixel of a chain
      cannot realize a drop against `boundary`, and shade code 3 never exists.
    * Code 3 is infeasible by construction under the verified rule.
    """
    c = np.asarray(codes, dtype=np.int64)
    if c.ndim != 2:
        raise ValueError(f"codes must be 2-D (z, x); got shape {c.shape}")
    h_w = np.zeros(c.shape, dtype=np.int32)
    feasible = np.ones(c.shape, dtype=bool)
    zz, xx = np.indices(c.shape, dtype=np.int64)
    parity = (zz + xx) & 1
    k = 4.0 / (1 + 4.0)  # scale 0: 4/(i+4) with i = 1  (art scale)
    for x in range(c.shape[1]):
        b = float(boundary)
        col = c[:, x]
        req = _chain_requirement(col)
        h_prev = None
        for z in range(c.shape[0]):
            p = int(parity[z, x])
            target = int(col[z])
            if z == 0:
                # absolute solve: candidates whose d2 hits the target against
                # the boundary; prefer the smallest that also meets req[0]
                picks = [h0 for h0 in range(0, int(abs(b)) + 9)
                         if _matches((h0 - b) * k + (p - 0.5) * 0.4, target)]
                good = [h0 for h0 in picks if h0 >= int(req[0])]
                if good:
                    h0 = min(good)
                else:
                    feasible[z, x] = False           # boundary-forced mismatch
                    h0 = max([0] + picks + [int(req[0])])
                h_w[z, x] = h0
                h_prev = h0
                continue
            need = int(req[z])
            if target == 2:
                dh = max(1, need - int(h_prev))       # rise as far as needed
                picked, ok = int(h_prev) + dh, True
            elif target == 1:
                picked, ok = int(h_prev), int(h_prev) >= need
            elif target == 0:
                picked, ok = int(h_prev) - 1, (int(h_prev) - 1) >= need
            else:                                     # code 3: impossible
                picked, ok = int(h_prev), False
            if not ok:
                feasible[z, x] = False
                picked = max(0, picked, need) if target != 0 else max(0, int(h_prev))
            h_w[z, x] = picked
            h_prev = picked
    return h_w, feasible


def _matches(d2: float, target: int) -> bool:
    """d2 -> shade code mapping, identical to shade_codes_from_heights.
    (0.8*1 - 0.2 evaluates to 0.6000000000000001 > 0.6 in float64.)"""
    code = 2 if d2 > 0.6 else (0 if d2 < -0.6 else 1)
    return code == target


def construction_report(codes: np.ndarray, heights: np.ndarray,
                        feasible: np.ndarray, target_mc_version: str) -> dict:
    """Honest per-frame construction summary for run reports (Phase 5/7)."""
    total = int(codes.size)
    infeasible = int((~feasible).sum())
    return {
        "pixels": total,
        "feasible": total - infeasible,
        "infeasible": infeasible,
        "infeasible_pct": round(100.0 * infeasible / max(1, total), 3),
        "shade3_intended": int((codes == 3).sum()),
        "height_min": int(heights.min()) if total else 0,
        "height_max": int(heights.max()) if total else 0,
        "rule": rule_provenance(target_mc_version),
        "honesty": "feasibility is computed against the shade rule stated in "
                   "'rule'; for unverified versions this is an assumption, "
                   "not an in-game fact",
    }
