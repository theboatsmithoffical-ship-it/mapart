# Desktop GUI + 8 GB runtime — integration plan

## Framework decision (Part 1)

The established GUI framework in this repository is **tkinter** (`src/firefly/mapart/gui.py`,
single-file, `tkinter.ttk`, zero added dependencies, already used for Map-Art / Benchmark /
Lab / History tabs and covered by `tests/test_engine.py` GUI-construction checks).

PySide6 was evaluated and **not** adopted:

- PySide6 adds ~200 MB of wheels and a Qt runtime DLL set to an 8 GB machine whose
  installed Python is deliberately minimal (numpy + Pillow only, per Settings tab facts).
- The repository GUI is already structured around ttk notebooks and real controllers
  (`labcore.LabController`, `mapart.breeding.Breeder`, `mapart.benchmark`); converting to
  Qt would be a rewrite with regression risk against the known-good baseline, not a safer
  or more maintainable choice for this codebase.
- tkinter qualifies for every required behavior (page tabs, threaded workers via the
  existing message-queue pump, cancellation, dark styling).

**Decision: reuse tkinter.** The GUI keeps controlling the engine through the existing
controller interfaces — it duplicates no business logic.

## Page map (Part 2)

| Requested page | Implementation |
|---|---|
| A Overview & control center | new **Control Center** tab: live lab/breeder status, champion, improvements, failures, resources, checkpoint info |
| B Evolution laboratory | existing **Evolution Lab** tab (contrrolls `LabController`) + lineage inspection via breeding tree |
| C Genetic breeding studio | existing **Evolution Lab → Breeding frame** (`Breeder`, percentages validation + presets + method status + mutation controls) — plus parentage shown per offspring in the lineage output |
| D Visual comparison studio | new **Visual Studio** tab: source vs champion vs challenger vs difference, zoom, pixel inspector, block assignment list, per-image metrics |
| E Benchmark & test center | existing **Benchmark** tab (`benchmark.py` engine) + schematic-validation viewer |
| F Resource & performance center | new **Resources** tab backed by `resourcemanager.py`: system vs engine memory, CPU, disk, cache size, budgets, cleanup, pressure log |
| G Configuration & recovery | existing **Settings** tab extended: profiles (safe/balanced/performance), checkpoint restore, champion restore, report/log viewer |

## Memory-bounded runtime (Part 3/4)

New module `src/firefly/mapart/resourcemanager.py`:

- Windows `GlobalMemoryStatusEx` via ctypes (no psutil dependency); engine-process memory
  via `GetProcessMemoryInfo` — and honest `None` ("unavailable") on unsupported platforms,
  never invented values.
- Budget model in `mapart-data/resource_profile.json`: engine worker budget ~2 GB, GUI
  visualization budget ~512 MB, concurrency 1 by default, mode = safe/balanced/performance.
- Emergency pressure: when free system memory falls below the configured floor, a shared
  `pressure` event stops new job scheduling in lab/breeder loops, they pause at the next
  safe checkpoint, disposable caches are released; the champion and checkpoints are never
  touched. Resume only when headroom recovers or the user intervenes.
- No unbounded pools: evaluation remains single-worker by default everywhere (Lab,
  Breeder, benchmark engine are all one-eval-at-a-time; measured, not assumed).
- Disk-backed caches are already the norm (eval fixture PNGs written per eval, deleted
  after; candidate configs stored as JSON, not images in RAM).

## Honest scope notes

- "Hard memory cap" cannot be promised without a real isolation mechanism; budgets are
  enforced as scheduling policy + monitoring, and the report shows measured peaks instead.
- Screenshots are captured from a real running window via PIL `ImageGrab` where the
  environment permits; if it is not possible, that is reported as a limitation.
