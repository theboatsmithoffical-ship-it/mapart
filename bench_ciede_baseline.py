import sys, time
sys.path.insert(0, "src")
import numpy as np
from firefly.mapart import mapart

pal = mapart.load_palette("survival-wool-1.8", "blocks")
rng = np.random.default_rng(0)
# larger map image: 4 map-frames (256x256) of smooth-ish gradient + noise
def big_img(px):
    yy, xx = np.mgrid[0:px, 0:px]
    r = (xx * 220) // px + 15
    g = (yy * 220) // px + 15
    b = ((xx + yy) * 140) // px + 40
    arr = np.stack([r, g, b], -1).astype(np.float64)
    arr = arr + rng.normal(0, 18, arr.shape)
    return np.clip(arr, 0, 255).astype(np.float32)

for px, label in ((128, "1 frame 128^2"), (256, "4 frames 256^2")):
    img = big_img(px)
    for algo in ("none", "bayer4", "floyd_steinberg"):
        t0 = time.perf_counter()
        idx = mapart.match_and_dither(img, pal, algo, 1.0, set(), metric="ciede2000")
        dt = time.perf_counter() - t0
        print(f"{label:16s} {algo:16s} ciede2000  {dt*1000:9.1f} ms  (unique {len(np.unique(idx))})")
