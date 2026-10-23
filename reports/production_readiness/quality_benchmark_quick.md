# Map-art quality benchmark

Generated 2026-10-23T06:41:40.755154+00:00  ·  tier small  ·  28 runs

## By configuration (metric | algorithm)

| config | n | dE2000 mean | dE2000 p95 | dE2000 max | %≤dE10 | oklab RMS | time s | peak MB |
|---|---|---|---|---|---|---|---|---|
| lab|floyd_steinberg | 7 | 12.7144 | 22.559 | 55.4716 | 35.41 | 0.077426 | 3.93 | 29.2 |
| lab|none | 7 | 11.018 | 19.0016 | 30.2462 | 40.64 | 0.068555 | 0.26 | 29.2 |
| rgb|floyd_steinberg | 7 | 12.5798 | 23.7275 | 49.9015 | 35.15 | 0.072977 | 1.928 | 13.3 |
| rgb|none | 7 | 11.5619 | 21.9691 | 43.2163 | 42.18 | 0.064522 | 0.23 | 13.3 |

## By category (averaged over configs)

| category | n | dE2000 mean | dE2000 p95 | %≤dE10 | oklab RMS |
|---|---|---|---|---|---|
| gradient | 4 | 11.5526 | 21.6956 | 43.36 | 0.063741 |
| high-frequency | 4 | 12.9155 | 24.1972 | 2.59 | 0.056067 |
| noisy-photo | 4 | 4.9167 | 9.066 | 97.08 | 0.018797 |
| out-of-palette | 4 | 15.3525 | 19.6204 | 0.0 | 0.132242 |
| photograph | 4 | 12.5259 | 22.3712 | 40.25 | 0.057539 |
| pixel-art | 4 | 15.4291 | 34.5779 | 32.86 | 0.10526 |
| transparency | 4 | 11.0873 | 21.1719 | 52.29 | 0.062446 |

## Export validation (representative)

- schem valid: True (size [128, 120, 128], placed 16384)
- litematic valid: True
