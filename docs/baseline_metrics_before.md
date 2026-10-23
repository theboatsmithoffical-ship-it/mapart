# Map-art quality benchmark

Generated 2026-10-23T04:04:05.685324+00:00  ·  tier small  ·  81 runs

## By configuration (metric | algorithm)

| config | n | dE2000 mean | dE2000 p95 | dE2000 max | %≤dE10 | oklab RMS | time s | peak MB |
|---|---|---|---|---|---|---|---|---|
| ciede2000|bayer4 | 9 | 12.8473 | 21.0476 | 32.9956 | 26.21 | 0.081299 | 0.433 | 36.9 |
| ciede2000|floyd_steinberg | 9 | 12.0963 | 22.2163 | 54.6714 | 38.01 | 0.06705 | 10.523 | 36.6 |
| ciede2000|none | 9 | 9.9283 | 17.2276 | 25.2989 | 52.3 | 0.058905 | 0.431 | 36.6 |
| lab|bayer4 | 9 | 13.0502 | 22.3712 | 35.4545 | 26.73 | 0.083564 | 0.037 | 39.0 |
| lab|floyd_steinberg | 9 | 12.5541 | 23.2814 | 55.2808 | 36.5 | 0.072933 | 2.21 | 38.6 |
| lab|none | 9 | 10.619 | 18.0926 | 30.2462 | 43.75 | 0.061258 | 0.037 | 38.6 |
| rgb|bayer4 | 9 | 14.3413 | 25.0605 | 43.3631 | 21.39 | 0.085627 | 0.032 | 34.6 |
| rgb|floyd_steinberg | 9 | 12.3127 | 23.7522 | 49.9015 | 36.78 | 0.067686 | 1.041 | 17.5 |
| rgb|none | 9 | 11.0821 | 20.3404 | 43.2163 | 44.33 | 0.057728 | 0.023 | 17.5 |

## By category (averaged over configs)

| category | n | dE2000 mean | dE2000 p95 | %≤dE10 | oklab RMS |
|---|---|---|---|---|---|
| gradient | 18 | 11.3494 | 22.2031 | 46.83 | 0.062722 |
| high-frequency | 18 | 12.894 | 21.8991 | 15.32 | 0.056452 |
| noisy-photo | 9 | 6.523 | 11.9249 | 81.37 | 0.034358 |
| out-of-palette | 9 | 15.4638 | 19.8531 | 0.0 | 0.132015 |
| photograph | 9 | 12.6837 | 21.9806 | 36.93 | 0.061538 |
| pixel-art | 9 | 14.6351 | 30.5267 | 34.18 | 0.102652 |
| transparency | 9 | 11.0389 | 20.9003 | 49.21 | 0.067141 |

## Export validation (representative)

- schem valid: True (size [128, 4, 128], placed 16384)
- litematic valid: True
