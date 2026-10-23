# Map-art quality benchmark

Generated 2026-10-23T05:54:50.710047+00:00  ·  tier small  ·  81 runs

## By configuration (metric | algorithm)

| config | n | dE2000 mean | dE2000 p95 | dE2000 max | %≤dE10 | oklab RMS | time s | peak MB |
|---|---|---|---|---|---|---|---|---|
| ciede2000|bayer4 | 9 | 12.9362 | 21.2099 | 32.9956 | 25.74 | 0.081648 | 0.397 | 34.8 |
| ciede2000|floyd_steinberg | 9 | 12.3088 | 22.0878 | 54.0643 | 36.75 | 0.068804 | 9.121 | 34.5 |
| ciede2000|none | 9 | 10.121 | 17.3114 | 26.672 | 51.06 | 0.060598 | 0.398 | 34.5 |
| lab|bayer4 | 9 | 13.0869 | 22.4008 | 35.4545 | 26.41 | 0.08314 | 0.144 | 29.5 |
| lab|floyd_steinberg | 9 | 12.7225 | 23.3294 | 55.4716 | 35.72 | 0.074268 | 2.121 | 29.2 |
| lab|none | 9 | 10.81 | 18.2231 | 30.2462 | 42.88 | 0.062585 | 0.142 | 29.2 |
| rgb|bayer4 | 9 | 14.4381 | 25.119 | 43.3631 | 20.93 | 0.085885 | 0.14 | 26.2 |
| rgb|floyd_steinberg | 9 | 12.4823 | 23.7471 | 49.9015 | 35.74 | 0.069484 | 1.051 | 13.3 |
| rgb|none | 9 | 11.2662 | 20.4771 | 43.2163 | 43.56 | 0.059566 | 0.133 | 13.3 |

## By category (averaged over configs)

| category | n | dE2000 mean | dE2000 p95 | %≤dE10 | oklab RMS |
|---|---|---|---|---|---|
| gradient | 18 | 11.4183 | 22.3161 | 46.51 | 0.063748 |
| high-frequency | 18 | 12.8796 | 21.7471 | 15.28 | 0.056642 |
| noisy-photo | 9 | 6.703 | 12.2373 | 81.25 | 0.036155 |
| out-of-palette | 9 | 15.4761 | 19.8531 | 0.0 | 0.13225 |
| photograph | 9 | 12.8105 | 22.2278 | 37.4 | 0.061577 |
| pixel-art | 9 | 14.9251 | 30.435 | 32.07 | 0.105209 |
| transparency | 9 | 11.6615 | 21.0259 | 44.48 | 0.070006 |

## Export validation (representative)

- schem valid: True (size [128, 120, 128], placed 16384)
- litematic valid: True
