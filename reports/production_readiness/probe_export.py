"""Baseline probe: export each config, reload independently, inspect buildability."""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path('src').resolve()))
from firefly.mapart import appcore, schem, rendermodel
import numpy as np

FIXTURE = Path('fixtures_engine/real_e2e.png')
OUT = Path('reports/production_readiness/probe_out')
GRAVITY = {'minecraft:sand', 'minecraft:red_sand', 'minecraft:gravel',
           'minecraft:anvil', 'minecraft:dragon_egg'}
rows = []
for pal in ('survival-wool-1.8', 'map-full-approx-1.15'):
    for construction in ('frame', 'solid'):
        t0 = time.perf_counter()
        s = appcore.Settings(version=pal, algorithm='bayer4', strength=1.0,
                             construction=construction,
                             formats=('schem', 'litematic'))
        res = appcore.process(FIXTURE, s)
        d = OUT / f'{pal}_{construction}'
        files = appcore.export(res, d, 'probe')
        layers = appcore._build_layers(res, mode=construction)
        height = len(layers)
        air_below = 0
        for y in range(1, height):
            for z in range(len(layers[y])):
                row = layers[y][z]
                for x, blk in enumerate(row):
                    if blk in GRAVITY and layers[y-1][z][x] == 'minecraft:air':
                        air_below += 1
        intended = res.pal.shades[res.idx].astype(int)
        realized = res.realized.astype(int)
        mismatch = int((intended != realized).sum())
        val = {}
        for fmt, key in (('schem', 'schem'), ('litematic', 'litematic')):
            p = files.get(key)
            if p:
                v = schem.validate_any(Path(p), fmt,
                                       expect={'size': (128, height, 128)})
                val[fmt] = {k: v.get(k) for k in ('ok', 'size', 'blocks_placed',
                                                  'volume', 'palette_size', 'error')}
        rows.append({
            'palette': pal, 'construction': construction,
            'max_height': int(res.heights.max()), 'schem_height': height,
            'feasible_pixels': int(res.feasible.sum()),
            'infeasible_pixels': int((~res.feasible).sum()),
            'shade_mismatch_realized_vs_intended': mismatch,
            'floating_gravity_blocks': air_below,
            'export_validation': val,
            'seconds': round(time.perf_counter() - t0, 2),
        })
print(json.dumps(rows, indent=1))
Path('reports/production_readiness/probe_export.json').write_text(
    json.dumps(rows, indent=1), encoding='utf-8')
