#!/usr/bin/env python3
"""FireFly Map-Art Engine — local entry point (Windows/POSIX, offline).

  python firefly_engine.py                -> desktop GUI (tkinter)
  python firefly_engine.py --selftest     -> headless end-to-end verification
  python firefly_engine.py --process IMAGE [--maps WxH] [--pal NAME]
                        [--algorithm N] [--strength S] [--suppress IDS]
                        [--out DIR]       -> build + validate + export files

Normal operation is 100% local: local files, installed Python + numpy +
Pillow + tkinter. Nothing here talks to GitHub, a server, or the cloud.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _selftest() -> int:
    """Headless Priority-1/2 verification: import -> process -> preview ->
    export -> open+validate .schem -> counts agreement."""
    import numpy as np
    from PIL import Image

    from firefly.mapart import appcore, schem

    def fixture(path: Path, size: tuple = (128, 128)) -> Path:
        x = np.linspace(20, 235, size[0], dtype=np.float64)
        y = np.linspace(235, 20, size[1], dtype=np.float64)
        a = np.broadcast_to(x[None, :], size)
        b = np.broadcast_to(y[:, None], size)
        g = 0.5 * (a + b)
        w = np.sin(a / 9.0) * 18 + np.cos(b / 6.0) * 12
        arr = np.clip(np.stack([a + w, g + w * 0.6, b], axis=-1), 0, 255).astype(np.uint8)
        Image.fromarray(arr, "RGB").save(path, format="PNG")
        return path

    td = Path(tempfile.mkdtemp(prefix="firefly_engine_selftest_"))
    img = fixture(td / "fixture.png")
    checks = {}

    for i, (ver, alg, maps) in enumerate([
            ("survival-wool-1.8", "floyd_steinberg", "1x1"),
            ("map-full-approx-1.15", "bayer4", "1x1"),
            ("survival-wool-1.8", "atkinson", "2x2")]):
        mw, mh = (1, 1) if maps == "1x1" else (2, 2)
        s = appcore.Settings(version=ver, maps_w=mw, maps_h=mh, algorithm=alg,
                             strength=1.0, suppress=(1,) if i == 1 else ())
        cancel = threading.Event()
        out = appcore.full_build(img, s, td / f"out{i}", cancel=cancel,
                                 progress=lambda f, m: None)
        v = out["files"]["validation"]["schem"]
        px = mw * mh * 128 * 128
        assert v["blocks_placed"] == px, (v, px)
        checks[f"{ver}|{alg}|{maps}"] = {"placed": v["blocks_placed"],
                                         "palette": v["palette_entries"],
                                         "size": v["size"]}
        if i == 1:
            rep = json.loads((Path(out["files"]["report"])).read_text(encoding="utf-8"))
            assert all(c["block"] != "minecraft:oak_leaves" for c in rep["counts"]), rep

    # Evolution Lab smoke: 4 real evaluations, best must be preserved
    import shutil
    labdir = td / "lab_min"
    from firefly.mapart import labcore
    lab = labcore.LabController(labdir, evals_limit=4, time_limit_s=120,
                                suite_ids=["grad-smooth"])
    lab.start()
    t_end = time.time() + 120
    while lab.is_running() and time.time() < t_end:
        time.sleep(0.3)
    st = lab.status()
    assert st["finished"] and st["evaluated"] == 4, st
    assert st["best"] is not None and st["best"]["score"] is not None, st
    checks["lab_min"] = {"evaluated": st["evaluated"],
                         "best_score": st["best"]["score"]}
    checks["offline"] = True
    print(json.dumps({"selftest": "ok", "runs": checks, "work": str(td)}, sort_keys=True))
    return 0


def _process_args(argv):
    import argparse

    from firefly.mapart import appcore

    ap = argparse.ArgumentParser(prog="firefly_engine.py process",
                                 description="Process an image to Minecraft map art and export a validated .schem")
    p = ap
    p.add_argument("--image", required=True)
    p.add_argument("--maps", default="1x1")
    p.add_argument("--pal", default="survival-wool-1.8")
    p.add_argument("--algorithm", default="floyd_steinberg")
    p.add_argument("--strength", type=float, default=1.0)
    p.add_argument("--suppress", default="")
    p.add_argument("--out", default=str(HERE / "mapart-output"))
    a = ap.parse_args(argv)
    mw, mh = a.maps.lower().split("x")
    suppress = {int(x) for x in a.suppress.split(",") if x.strip()}
    settings = appcore.Settings(version=a.pal, maps_w=int(mw), maps_h=int(mh),
                                algorithm=a.algorithm, strength=a.strength,
                                suppress=suppress)
    out = appcore.full_build(a.image, settings, a.out)
    files = out["files"]
    v = dict(files.pop("validation"))
    print(json.dumps({"ok": True, **files, "validation": v}, sort_keys=True))
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--selftest":
        return _selftest()
    if argv and argv[0] == "process":
        return _process_args(argv[1:])
    if argv and argv[0] in ("--help", "-h"):
        print(__doc__)
        return 0
    from firefly.mapart.gui import main as gui_main
    return gui_main() or 0


if __name__ == "__main__":
    sys.exit(main())
