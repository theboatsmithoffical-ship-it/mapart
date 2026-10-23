import sys, time
sys.path.insert(0, "src")
import numpy as np
from firefly.mapart import mapart

# ---- 1. EQUIVALENCE: block form must pick the SAME candidates (argmin) as old per-pixel ----
rng = np.random.default_rng(1)
worst = 0.0
for metric in ("lab", "ciede2000"):
    n_cand = 150
    cand_rgb = rng.integers(0, 256, (n_cand, 3)).astype(np.float32)
    cand_lab = mapart.srgb_to_lab(cand_rgb)
    d = mapart.frame_distance(metric, cand_lab)
    # include near-tie / random colors to stress argmin stability
    block = rng.integers(0, 256, (8000, 3)).astype(np.float32)
    new = d(block, cand_rgb)
    old = np.stack([d(px, cand_rgb) for px in block], axis=0)
    ad = np.abs(new - old).max(); worst = max(worst, ad)
    arg = np.array_equal(np.argmin(new, axis=1), np.argmin(old, axis=1))
    print(f"{metric:9s} argmin-identical: {arg}  max cost diff: {ad:.3e}")
    assert arg, f"{metric} candidate selection changed!"

# ---- 2. TIMING on a larger map image (4 frames, 256x256 = 65536 px) ----
pal = mapart.load_palette("survival-wool-1.8", "blocks")
sub = pal.arr
yy, xx = np.mgrid[0:256, 0:256]
base = np.stack([(xx*220)//256+15, (yy*220)//256+15, ((xx+yy)*140)//256+40], -1).astype(np.float64)
base = np.clip(base + np.random.default_rng(0).normal(0, 18, base.shape), 0, 255).astype(np.float32)
flat = base.reshape(-1, 3)
cand_lab = mapart.srgb_to_lab(sub)
d = mapart.frame_distance("ciede2000", cand_lab)
t0=time.perf_counter(); cn=d(flat, sub); tn=time.perf_counter()-t0
t0=time.perf_counter(); co=np.stack([d(px, sub) for px in flat], axis=0); to=time.perf_counter()-t0
assert np.array_equal(np.argmin(cn,1), np.argmin(co,1))
print(f"\nCIEDE2000 cost matrix, {flat.shape[0]} px x {sub.shape[0]} cand:")
print(f"  old per-pixel : {to*1000:9.1f} ms")
print(f"  new vectorized: {tn*1000:9.1f} ms")
print(f"  SPEEDUP       : {to/tn:9.1f}x  (same candidate chosen for every pixel)")
print(f"\nworst cost diff across metrics: {worst:.3e}")
print("EQUIVALENCE_OK")
