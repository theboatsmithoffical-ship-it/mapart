import sys, time
sys.path.insert(0, "src")
import numpy as np
from firefly.mapart import mapart

# profile the vectorized ciede2000 on a representative chunk
rng = np.random.default_rng(3)
n_pix, n_cand = 2048, 64
lab1 = mapart.srgb_to_lab(rng.integers(0,256,(n_pix,1,3)).astype(np.float32))
lab2 = mapart.srgb_to_lab(rng.integers(0,256,(1,n_cand,3)).astype(np.float32))

# total time for one chunk (32 chunks make up the full 65536 image)
t0=time.perf_counter()
for _ in range(20):
    mapart.ciede2000(lab1, lab2)
per = (time.perf_counter()-t0)/20
print(f"ciede2000 one chunk ({n_pix}x{n_cand}): {per*1000:.2f} ms")
print(f"x32 chunks (65536 px)          : {per*32*1000:.1f} ms  (measured full matrix was 1591 ms)")

# time the raw transcendentals that ciede2000 uses
a = rng.random((n_pix, n_cand)); b = rng.random((n_pix, n_cand))
def t(f, *args, n=50):
    t0=time.perf_counter()
    for _ in range(n): f(*args)
    return (time.perf_counter()-t0)/n*1000
print("\nindividual op cost (ms per call, {}x{}):".format(n_pix,n_cand))
print("  hypot          ", round(t(np.hypot, a, b),3))
print("  sqrt(a*a+b*b)  ", round(t(lambda x,y: np.sqrt(x*x+y*y), a, b),3))
print("  arctan2        ", round(t(np.arctan2, b, a),3))
print("  degrees(arctan2)", round(t(lambda x,y: np.degrees(np.arctan2(x,y)), b, a),3))
print("  cos            ", round(t(np.cos, a),3))
print("  x**7           ", round(t(lambda x: x**7, a),3))
print("  exp            ", round(t(np.exp, a),3))
print("  sqrt           ", round(t(np.sqrt, a),3))
print("  sin            ", round(t(np.sin, a),3))
