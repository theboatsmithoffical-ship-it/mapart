#!/usr/bin/env python3
"""Render-model unit tests (plain python, no framework dependency).

Run:  python tests/test_rendermodel.py   (exit 0 = all pass)

Reference values are hand-computed from the verbatim 1.8.9 source formulas
(MapColor.getMapColor integer arithmetic; ItemMap.updateMapData thresholds),
so these tests pin the model to the game, not to itself.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from firefly.mapart import rendermodel as rm  # noqa: E402

FAILURES = []


def t(name, fn):
    try:
        detail = fn()
        print(f"PASS {name}  [{detail}]")
    except Exception as e:  # noqa: BLE001 — this runner reports all failures
        FAILURES.append(name)
        print(f"FAIL {name}: {e!r}")


# ---------------- exact shade arithmetic ----------------

def shade_factors_and_arithmetic():
    assert rm.SHADE_FACTORS == (180, 220, 255, 135), rm.SHADE_FACTORS
    # white-ish base 221: 221*180//255 == 156 exactly, 221*220//255 == 190,
    # 221*135//255 == 117 (integer truncation, hand-computed)
    assert rm.shade_rgb((221, 221, 221), 0) == (156, 156, 156)
    assert rm.shade_rgb((221, 221, 221), 1) == (190, 190, 190)
    assert rm.shade_rgb((221, 221, 221), 2) == (221, 221, 221)
    assert rm.shade_rgb((221, 221, 221), 3) == (117, 117, 117)
    # grass base (127,178,56) at factor 180: 89/125/39
    assert rm.shade_rgb((127, 178, 56), 0) == (89, 125, 39)
    # truncation, not rounding: 100*180/255 = 70.58 -> 70 (legacy round gave 71)
    assert rm.shade_rgb((100, 100, 100), 0) == (70, 70, 70)
    assert round(100 * 0.71) == 71, "legacy float path differs — this is the fix"
    return "vanilla integer truncation pinned to hand-computed values"


def shade_arr_matches_scalar():
    base = np.array([[221, 221, 221], [127, 178, 56], [0, 0, 0]], dtype=np.float32)
    shades = np.array([0, 2, 3], dtype=np.int64)
    out = rm.shade_arr(base, shades)
    for i in range(3):
        assert tuple(out[i]) == rm.shade_rgb(base[i], shades[i]), (i, out[i])
    return "vectorized == scalar for reference triples"


# ---------------- forward rule: heights -> shades ----------------

def flat_heights_shade1():
    h = np.full((4, 4), 64.0)
    codes = rm.shade_codes_from_heights(h, boundary=64.0)
    assert (codes == 1).all(), codes          # flat terrain -> shade 1 (220)
    # literal boundary reading: absent outside chunk means d0 = 0.0 at the
    # chain start, so the first row compares against 0 -> bright (shade 2)
    codes0 = rm.shade_codes_from_heights(h, boundary=0.0)
    assert (codes0[0] == 2).all(), codes0[0]
    assert (codes0[1:] == 1).all(), codes0[1:]
    return "flat->1; boundary d0=0.0 makes row 0 bright (documented literal)"


def step_rules_and_parity():
    # column x=0; parity = (z + x) & 1
    h = np.array([[64], [64], [66], [66], [64], [62]], dtype=np.float64)
    got = rm.shade_codes_from_heights(h, boundary=64.0)[:, 0].tolist()
    assert got == [1, 1, 2, 1, 0, 0], got     # +2 step -> 2, -2 step -> 0
    # +1 step: 0.8 - 0.2 computes as 0.6000000000000001 in float64 (as in
    # Java doubles), which IS > 0.6 -> shade 2 for both parities
    h_up = np.array([[64], [64], [64], [64], [65], [66]], dtype=np.float64)
    got_up = rm.shade_codes_from_heights(h_up, boundary=64.0)[:, 0].tolist()
    assert got_up == [1, 1, 1, 1, 2, 2], got_up
    # -1 step: -0.8 + 0.2 = -0.6000000000000001 < -0.6 -> shade 0 for both
    h_dn = np.array([[64], [64], [64], [64], [63], [62]], dtype=np.float64)
    got_dn = rm.shade_codes_from_heights(h_dn, boundary=64.0)[:, 0].tolist()
    assert got_dn == [1, 1, 1, 1, 0, 0], got_dn
    return "thresholds + float-faithful 1-step crossings match the source"


def shade3_never_emitted():
    rng = np.random.default_rng(7)
    h = rng.integers(0, 120, size=(64, 64)).astype(np.float64)
    codes = rm.shade_codes_from_heights(h, boundary=0.0)
    used = set(np.unique(codes).tolist())
    assert used <= {0, 1, 2}, used
    assert 3 not in used
    return f"codes used over random terrain: {sorted(used)}"


def water_depth_rule():
    # d2 = depth*0.1 + parity*0.2; <0.5 -> 2, >0.9 -> 0, else 1
    assert rm.water_shade(0, 0) == 2       # 0.0 < 0.5
    assert rm.water_shade(4, 0) == 2       # 0.4 < 0.5
    assert rm.water_shade(5, 0) == 1       # 0.5 -> middle
    assert rm.water_shade(4, 1) == 1       # 0.6 -> middle
    assert rm.water_shade(10, 0) == 0      # 1.0 > 0.9
    return "water depth+parity branch matches the source"


# ---------------- inverse rule: shades -> heights ----------------

def solver_roundtrip():
    rng = np.random.default_rng(11)
    codes = rng.integers(0, 3, size=(16, 16)).astype(np.int64)  # {0,1,2}
    heights, feasible = rm.solve_heights(codes, boundary=0.0)
    back = rm.shade_codes_from_heights(heights.astype(np.float64), boundary=0.0)
    ok = feasible & (back == codes)
    assert ok[feasible].all(), (
        f"{(feasible & (back != codes)).sum()} feasible pixels failed round-trip"
    )
    assert (heights >= 0).all(), "construction heights must stay buildable (>= 0)"
    # backwards lookahead makes every non-first-row pixel solvable for codes
    # in {0,1,2}: remaining drops are pre-funded by the rises before them
    zz, _xx = np.indices(codes.shape)
    assert not (feasible == False)[zz > 0].any(), "mid-chain infeasibility remains"
    frac = float(feasible.mean())
    assert frac > 0.9, f"solver feasible fraction too low: {frac:.3f}"
    # determinism
    h2, f2 = rm.solve_heights(codes, boundary=0.0)
    assert np.array_equal(heights, h2) and np.array_equal(feasible, f2)
    return f"round-trip exact on all feasible ({frac:.1%} of pixels)"


def solver_flags_impossible():
    # code 3 is unreachable by the verified rule -> always infeasible
    h, f = rm.solve_heights(np.full((2, 2), 3), boundary=0.0)
    assert not f.any(), f
    # chain [1, 0]: the drop ahead is pre-funded only by rising, but row 0
    # can only sit at h=0 to render code 1 against boundary 0 — so row 0 is
    # the pixel that cannot be realized and it is flagged there, while the
    # drop below it is still solved exactly (heights >= 0, code 0 rendered)
    h2, f2 = rm.solve_heights(np.array([[1], [0]]), boundary=0.0)
    assert not f2[0, 0], f2
    assert f2[1, 0], f2
    back2 = rm.shade_codes_from_heights(h2.astype(np.float64), boundary=0.0)
    assert back2[1, 0] == 0 and h2[1, 0] >= 0, (h2.ravel().tolist(), back2.ravel().tolist())
    return "shade-3 and boundary-forced mismatches are flagged, not hidden"


# ---------------- color science references ----------------

def ciede2000_sharma_reference():
    """Sharma, Wu & Dalal (2005) reference pairs — the canonical unit tests
    for CIEDE2000. Nine pairs reproduce the published values exactly."""
    from firefly.mapart.mapart import ciede2000
    refs = [
        ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
        ((50.0, 3.1571, -77.2803), (50.0, 0.0, -82.7485), 2.8615),
        ((50.0, 2.8361, -74.0200), (50.0, 0.0, -82.7485), 3.4412),
        ((50.0, -1.3802, -84.2814), (50.0, 0.0, -82.7485), 1.0000),
        ((50.0, -1.1848, -84.8006), (50.0, 0.0, -82.7485), 1.0000),
        ((50.0, -0.9009, -85.5211), (50.0, 0.0, -82.7485), 1.0000),
        ((50.0, 0.0, 0.0), (50.0, -1.0, 2.0), 2.3669),
        ((50.0, 2.5, 0.0), (73.0, 25.0, -18.0), 27.1492),
        ((50.0, 2.5, 0.0), (61.0, -5.0, 29.0), 22.8977),
    ]
    for a, b, exp in refs:
        got = float(ciede2000(np.array([a]), np.array([b]))[0])
        assert abs(got - exp) < 1e-4, (a, b, got, exp)
    same = float(ciede2000(np.array([[50.0, 20.0, -10.0]]),
                           np.array([[50.0, 20.0, -10.0]]))[0])
    assert same == 0.0
    return f"{len(refs)} Sharma reference pairs exact, identical colors = 0"


def lab_reference_values():
    from firefly.mapart.mapart import srgb_to_lab
    white = srgb_to_lab(np.array([[255.0, 255.0, 255.0]]))[0]
    black = srgb_to_lab(np.array([[0.0, 0.0, 0.0]]))[0]
    red = srgb_to_lab(np.array([[255.0, 0.0, 0.0]]))[0]
    # the published sRGB->XYZ matrix is truncated (~1e-5 residual on neutrals)
    assert abs(white[0] - 100.0) < 1e-3 and abs(white[1]) < 1e-3 and abs(white[2]) < 1e-3
    assert abs(black[0]) < 1e-9 and abs(black[1]) < 1e-9 and abs(black[2]) < 1e-9
    # sRGB D65 reference red: L*53.24 a*80.09 b*67.20
    assert abs(red[0] - 53.24) < 0.01 and abs(red[1] - 80.09) < 0.01 \
        and abs(red[2] - 67.20) < 0.01, red
    return f"white {white.round(2).tolist()}, black {black.round(2).tolist()}, red {red.round(2).tolist()}"


# ---------------- construction constraint: reachable shades ----------------

def reachable_shade_constraint():
    from firefly.mapart.mapart import load_palette, match_and_dither
    img = np.random.default_rng(5).integers(0, 256, size=(128, 128, 3), dtype=np.uint8)
    pal18 = load_palette("survival-wool-1.8")
    idx = match_and_dither(img, pal18, "none", 1.0, set(), metric="lab")
    used = set(np.unique(pal18.shades[idx]).tolist())
    assert used <= {0, 1, 2}, used          # verified rule never emits shade 3
    allowed = len([c for c in pal18.colors if c["shade"] in (0, 1, 2)])
    assert allowed == int(len(pal18.colors) * 0.75)
    # unverified version keeps all shades eligible (no rejection on assumption)
    prov = rm.rule_provenance(load_palette("map-full-approx-1.15").target_mc_version)
    assert prov["status"] == "unverified-assumption"
    assert set(prov["reachable_shades"]) == {0, 1, 2, 3}
    return f"1.8 plan uses shades {sorted(used)}; 1.15 keeps 4 shades (labeled assumption)"


def explain_plan_reports_objective():
    from firefly.mapart.mapart import load_palette, match_and_dither, explain_plan
    img = np.random.default_rng(9).integers(0, 256, size=(128, 128, 3), dtype=np.uint8)
    pal = load_palette("survival-wool-1.8")
    idx = match_and_dither(img, pal, "none", 1.0, set(), metric="ciede2000")
    ex = explain_plan(idx, img, pal, metric="ciede2000", algorithm="none")
    assert ex["candidates_allowed"] == int(len(pal.colors) * 0.75), ex["candidates_allowed"]
    assert ex["constraints"]["unreachable_shade_codes"] == [3]
    # no dithering -> every sampled pixel got the optimal feasible candidate
    assert ex["optimal_choice_rate"] == 1.0, ex["optimal_choice_rate"]
    assert ex["dither_gap_mean"] == 0.0
    assert set(ex["chosen_shade_histogram"]) <= {0, 1, 2}
    assert ex["chosen_error_mean"] >= 0.0 and ex["runner_up_margin_mean"] >= 0.0
    # dithered selection is allowed to deviate; the gap is measured, not hidden
    idx_d = match_and_dither(img, pal, "bayer4", 1.0, set(), metric="ciede2000")
    ex_d = explain_plan(idx_d, img, pal, metric="ciede2000", algorithm="bayer4")
    assert 0.0 <= ex_d["optimal_choice_rate"] <= 1.0
    return (f"undithered optimal rate {ex['optimal_choice_rate']}, "
            f"bayer4 {ex_d['optimal_choice_rate']}, gap {ex_d['dither_gap_mean']}")


# ---------------- palette wiring (single authority) ----------------

def palette_shade_entries_exact():
    from firefly.mapart.mapart import load_palette
    checked = 0
    for ver in ("survival-wool-1.8", "map-full-approx-1.15"):
        pal = load_palette(ver)
        by_key = {(c["map_color"], c["shade"]): tuple(c["rgb"]) for c in pal.colors}
        for cid in sorted({c["map_color"] for c in pal.colors}):
            base = by_key[(cid, 2)]          # factor 255 == base color
            for s in range(4):
                assert by_key[(cid, s)] == rm.shade_rgb(base, s), (ver, cid, s)
            checked += 1
    return f"{checked} map colors across 2 palettes match rendermodel exactly"


def provenance_labels():
    assert rm.rule_provenance("1.8")["status"] == "source-verified"
    assert rm.rule_provenance("1.15.2")["status"] == "unverified-assumption"
    unknown = rm.rule_provenance("9.9.9")
    assert unknown["status"] == "unverified-assumption"
    assert "UNVERIFIED" in unknown["shade_rule"]
    mp = rm.model_provenance()
    assert mp["shade_factors"] == [180, 220, 255, 135]
    assert mp["flat_terrain_shade"] == 1
    assert mp["unreachable_shade_codes"] == [3]
    assert "ItemMap.java" in mp["sources"]["itemmap_1_8_9"]
    # shipped src must stay free of network-looking literals (offline battery)
    assert "http" not in repr(mp).lower()
    return "1.8 verified, 1.15 labeled assumption, unknown labeled unverified"


def determinism():
    rng = np.random.default_rng(3)
    h = rng.integers(0, 90, size=(32, 32)).astype(np.float64)
    a = rm.shade_codes_from_heights(h, boundary=40.0)
    b = rm.shade_codes_from_heights(h, boundary=40.0)
    assert np.array_equal(a, b)
    return "identical inputs -> identical codes"


def main() -> int:
    print("=== render model unit tests ===")
    t("rm_shade_arithmetic", shade_factors_and_arithmetic)
    t("rm_shade_arr", shade_arr_matches_scalar)
    t("rm_flat_shade1", flat_heights_shade1)
    t("rm_step_parity", step_rules_and_parity)
    t("rm_no_shade3", shade3_never_emitted)
    t("rm_water_rule", water_depth_rule)
    t("rm_solver_roundtrip", solver_roundtrip)
    t("rm_solver_impossible", solver_flags_impossible)
    t("rm_ciede2000_sharma", ciede2000_sharma_reference)
    t("rm_lab_refs", lab_reference_values)
    t("rm_reachable_shades", reachable_shade_constraint)
    t("rm_explain_plan", explain_plan_reports_objective)
    t("rm_palette_wiring", palette_shade_entries_exact)
    t("rm_provenance", provenance_labels)
    t("rm_determinism", determinism)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
