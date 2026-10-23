# Dataset library + offline acceptance report (spec §1–§3, §7)

Everything below was measured in this workspace; commands and exit codes come
from real runs (see `acceptance_run/report.json` and `bat_*.log`).
Honest-status vocabulary throughout: VERIFIED means an automated check ran
and passed here.

## 1. Dataset on disk (real files, verified hashes)

Location: `mapart-data/imagelibrary/` — the app's configured data dir
(`DATA_DIR = APP_DIR / "mapart-data"` in `src/firefly/mapart/gui.py`).
The library was rebuilt this session (`tools/rebuild_dataset.py`); members
were fetched through the same production code path the GUI uses.

| metric | value (acceptance run) |
|---|---|
| downloaded images | 33, all from Wikimedia Commons (no API key) |
| integrity | **33/33 sha256 + PIL-decode verified** (`ImageLibrary.verify()`) |
| disk usage | 12 MB images (plus suite caches, well under any ceiling) |
| manifest revision | 115 (bumped on every membership change; 38+ revisions preserved in `revisions/`) |
| purposes | dev 10 · stress 14 · **holdout 9** |
| locally generated synthetic | 14 deterministic PNGs + suite manifest (`imagelibrary/synthetic/`) |
| licenses recorded | Public domain 12 · CC0 2 · CC BY 2.0 2 · CC BY-SA 2.5 1 · 3.0 7 · 4.0 9 (per-image `license`, `license_url`, `author`, `source_url`, sha256, dimensions, analysis tags in `manifest.json`) |

Categories covered (by discovery query + measured capability tags):
landscapes, architecture, animals, plants, portraits, illustrations,
pixel art, gradients, night skylines, high-detail textures — plus difficult
cases auto-assigned to `stress` (similar-colors / out-of-palette /
fine-details / high-contrast tags).

## 2. What changed this session

- `src/firefly/mapart/imagelibrary.py` (download robustness — root-cause
  fixes, no suppressions):
  - raw `HTTPError` (e.g. Wikimedia 429 rate limit) used to crash the caller;
    now wrapped as `LibraryError` with **one polite retry** (429/5xx backoff),
    partial `.part` files preserved for resume, refused downloads never stored;
  - fixed `part.stat()` on a nonexistent part file (FileNotFoundError instead
    of a clean start).
- `tools/rebuild_dataset.py` (new): explicit, measured rebuild — disk-space
  preflight, per-query candidate estimates, politeness delay, dedupe,
  holdout/stress assignment on measured tags, synthetic generation,
  integrity verification + JSON report. Exit 0 requires `offline_ready`.
- `src/firefly/mapart/labcore.py` (dataset → optimizer bridge):
  - suite ids may now be `lib:<image-id>` — ONE local image loads on demand
    at suite resolution from the manifest index (never the whole library; §5),
  - **holdout entries raise PermissionError in the lab** — the optimizer can
    never train on the final-evaluation set (§4; holdout evaluation itself
    stays aggregate-only in the breeder's separate `holdout.jsonl`),
  - `LabController(..., library=ImageLibrary(dir))` wired in the GUI lab.
- `tools/acceptance_offline.py` (new): spec §7 scenario under a hard
  network guard.

## 3. Spec §7 acceptance test — VERIFIED, exit 0

Run with network blocked at the socket layer
(`socket.connect` + `getaddrinfo` raise and RECORD any non-loopback attempt;
`ImageLibrary.download` / `discover_wikimedia` instrumented to fail loudly):

| # | step | result |
|---|---|---|
| 1 | local dataset preflight | PASS — 33/33 verified, rev 115 |
| 2 | synthetic benchmark generated locally | PASS — 14 deterministic images |
| 3 | screening experiment (fixtures + real dev images) | PASS — 5 evals, best 0.9259 |
| 4 | development experiment (real dev + stress images) | PASS — 5 evals, best 0.9362 |
| 5 | breed + evaluate candidates | PASS — 10 evals, 2 generations, champion 0.9272, holdout row aggregate-only |
| 6 | generate + validate schematic | PASS — `acceptance_128x128.schem`, size (128,4,128) validated, from real dataset image |
| 7 | restart + resume | PASS — checkpoint resumed by fresh controller (4 evals, ≥ stop time), finished at 8, no reset |
| 8 | holdout never trains the optimizer | PASS — lab refused holdout item (PermissionError) |
| 9 | no network required | PASS — **0 network attempts, 0 downloader calls** |

Engine batteries after these changes: **133 PASS / 0 FAIL, exit 0**
(`test_breeding` 39, `test_gui_runtime` 41, `test_engine` 38, `test_tools` 15).

## 4. Honest limitations

- The network guard is a socket-level kill switch (Wi-Fi-disconnect
  equivalent). A literal hardware disconnect was not performed on this box.
- §2's example folder taxonomy is realized as manifest purposes
  (dev/stress/holdout/synthetic) + capability tags rather than directory
  names; categories and membership are queryable from `manifest.json` and
  revisions.
- Unsplash requires an API key and was not exercised (documented in the GUI;
  basic functionality never depends on it). COCO/Kodak are not wired; the
  spec lists them as example sources, and Commons already covers every
  required category with recorded licenses.
- Cache size cap and images-per-run are configurable via the resource
  profiles (`safe`/`balanced`/`performance`) — no OS-level hard memory cap
  exists; budgets are policy + measured peaks (8 GB profile is the default).
