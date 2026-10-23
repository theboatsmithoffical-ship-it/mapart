"""Central resource manager for the 8 GB runtime.

No new dependencies: on Windows system memory comes from ctypes
GlobalMemoryStatusEx and engine-process memory from GetProcessMemoryInfo;
otherwise values are reported as None ("unavailable") — never invented.

Modes (safe / balanced / performance) are STARTING TARGETS, not hard caps:
they are scheduling policy (worker count, checkpoint frequency, pause-on-
pressure). Reports always carry measured peaks.

Pressure contract (Emergency response):
- `pressure_event()` returns True below the configured free-memory floor
- lab/breeder loops call `ResourceManager.admission_check()` before each
  schedule: if pressured, they stop scheduling, pause at the next safe
  checkpoint and release disposable caches
- the champion, checkpoints and user files are never touched by cleanup

Cleanup rules: only managed cache dirs are pruned; champion exports,
checkpoints and lab/breeding state are explicitly protected paths.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

MODES = {
    "safe": {"workers": 1, "free_floor_mb": 1600, "cache_items": 8,
             "checkpoint_every_eval": 1, "max_image_px": 128 * 128},
    "balanced": {"workers": 1, "free_floor_mb": 1024, "cache_items": 24,
                 "checkpoint_every_eval": 1, "max_image_px": 256 * 256},
    "performance": {"workers": 2, "free_floor_mb": 700, "cache_items": 48,
                    "checkpoint_every_eval": 4, "max_image_px": 512 * 512},
}

# paths cleanup must NEVER delete
PROTECTED_NAMES = ("state.json", "breeder.json", "best.json", "lineage.jsonl",
                   "holdout.jsonl", "history.jsonl", "champion", "gate", "lab")


# --------------------------------------------------------------------------
# sysinfo (no psutil)
# --------------------------------------------------------------------------

def system_memory() -> dict:
    """{'total_mb', 'available_mb', 'free_mb', 'load_pct'} or honest None."""
    if sys.platform == "win32":
        try:
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return {"total_mb": m.ullTotalPhys // (1024 * 1024),
                    "available_mb": m.ullAvailPhys // (1024 * 1024),
                    "free_mb": m.ullAvailPhys // (1024 * 1024),
                    "load_pct": m.dwMemoryLoad,
                    "source": "GlobalMemoryStatusEx"}
        except Exception:  # noqa: BLE001
            return {"total_mb": None, "available_mb": None, "free_mb": None,
                    "load_pct": None, "source": "unavailable"}
    for field_ in ("/proc/meminfo",):
        try:
            d = {}
            for line in Path(field_).read_text().splitlines():
                k, v = line.split(":", 1)
                d[k.strip()] = int(v.strip().split()[0])  # kB
            return {"total_mb": d["MemTotal"] // 1024,
                    "available_mb": d["MemAvailable"] // 1024,
                    "free_mb": d["MemFree"] // 1024,
                    "load_pct": round(100.0 * (1 - d["MemAvailable"] / d["MemTotal"]), 1),
                    "source": "/proc/meminfo"}
        except Exception:  # noqa: BLE001
            continue
    return {"total_mb": None, "available_mb": None, "free_mb": None,
            "load_pct": None, "source": "unavailable"}


def process_memory(pid: int | None = None) -> int | None:
    """Working-set bytes of the engine process (Windows) or None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        h = ctypes.windll.kernel32.OpenProcess(0x0400, False, pid or os.getpid())
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
        ctypes.windll.kernel32.CloseHandle(h)
        if not ok:
            return None
        return {
            "working_set_mb": pmc.WorkingSetSize // (1024 * 1024),
            "peak_working_set_mb": pmc.PeakWorkingSetSize // (1024 * 1024),
            "pagefile_mb": pmc.PagefileUsage // (1024 * 1024),
        }
    except Exception:  # noqa: BLE001
        return None


def cpu_count() -> int:
    try:
        return os.cpu_count() or 1
    except Exception:  # noqa: BLE001
        return 1


def cpu_load_pct() -> float | None:
    """1-second delta of Windows process times; honest None off-Windows."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        ft = ctypes.wintypes.FILETIME  # type: ignore[attr-defined]
        creation, exit_, kernel, user = ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64()
        h = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.kernel32.GetProcessTimes(h, ctypes.byref(creation),
                                                  ctypes.byref(exit_),
                                                  ctypes.byref(kernel),
                                                  ctypes.byref(user)) == 0:
            return None
        t0 = kernel.value + user.value
        time.sleep(0.25)
        if ctypes.windll.kernel32.GetProcessTimes(h, ctypes.byref(creation),
                                                  ctypes.byref(exit_),
                                                  ctypes.byref(kernel),
                                                  ctypes.byref(user)) == 0:
            return None
        t1 = kernel.value + user.value
        return round(min(100.0, (t1 - t0) / 1e7 / 0.25 * 100.0), 1)
    except Exception:  # noqa: BLE001
        return None


def disk_usage(path) -> dict:
    try:
        u = __import__("shutil").disk_usage(str(path))
        return {"total_mb": u.total // (1024 * 1024),
                "used_mb": u.used // (1024 * 1024),
                "free_mb": u.free // (1024 * 1024)}
    except Exception:  # noqa: BLE001
        return {"total_mb": None, "used_mb": None, "free_mb": None}


def dir_size_mb(path) -> int:
    total = 0
    try:
        for p in Path(path).rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total // (1024 * 1024)


# --------------------------------------------------------------------------
# ResourceManager
# --------------------------------------------------------------------------

@dataclass
class Budget:
    mode: str = "safe"
    workers: int = 1
    free_floor_mb: int = MODES["safe"]["free_floor_mb"]
    engine_budget_mb: int = 2048      # starting target; scheduling policy only
    gui_budget_mb: int = 512          # visualization budget (policy)
    cache_items: int = 8
    checkpoint_every_eval: int = 1
    max_image_px: int = MODES["safe"]["max_image_px"]


class ResourceManager:
    """Owns budget state + pressure signalling. Thread-safe, cheap calls."""

    def __init__(self, data_dir, profile: dict | None = None):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.dir / "resource_profile.json"
        if profile is not None:
            saved = profile
        elif p.exists():
            try:
                saved = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                saved = {}
        else:
            saved = {}
        self.budget = Budget()
        self._apply_saved(saved)
        self.pressure_evt = threading.Event()
        self.peaks = []          # [{"ts","free_mb","engine_mb","event"}]
        self.cleanup_log = []    # released item descriptions
        self._watchdog_stop = threading.Event()

    def _apply_saved(self, saved: dict):
        mode = saved.get("mode", "safe")
        mode = mode if mode in MODES else "safe"
        self.budget.mode = mode
        self.budget.workers = int(saved.get("workers", MODES[mode]["workers"]))
        self.budget.free_floor_mb = int(
            saved.get("free_floor_mb", MODES[mode]["free_floor_mb"]))
        self.budget.engine_budget_mb = int(saved.get("engine_budget_mb", 2048))
        self.budget.gui_budget_mb = int(saved.get("gui_budget_mb", 512))
        self.budget.cache_items = int(saved.get("cache_items",
                                                MODES[mode]["cache_items"]))
        self.budget.checkpoint_every_eval = int(
            saved.get("checkpoint_every_eval", MODES[mode]["checkpoint_every_eval"]))
        self.budget.max_image_px = int(saved.get("max_image_px",
                                                 MODES[mode]["max_image_px"]))

    def save_profile(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dir.joinpath("resource_profile.json").write_text(json.dumps(
            {"mode": self.budget.mode, "workers": self.budget.workers,
             "free_floor_mb": self.budget.free_floor_mb,
             "engine_budget_mb": self.budget.engine_budget_mb,
             "gui_budget_mb": self.budget.gui_budget_mb,
             "cache_items": self.budget.cache_items,
             "checkpoint_every_eval": self.budget.checkpoint_every_eval,
             "max_image_px": self.budget.max_image_px}, indent=2),
            encoding="utf-8")

    def set_mode(self, mode: str) -> dict:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; MODES={sorted(MODES)}")
        self.budget.mode = mode
        self.budget.workers = MODES[mode]["workers"]
        self.budget.free_floor_mb = MODES[mode]["free_floor_mb"]
        self.budget.cache_items = MODES[mode]["cache_items"]
        self.budget.checkpoint_every_eval = MODES[mode]["checkpoint_every_eval"]
        self.budget.max_image_px = MODES[mode]["max_image_px"]
        self.save_profile()
        return self.budget.__dict__ | {"mode": mode}

    # ---------------- pressure ----------------
    def snapshot(self) -> dict:
        sm = system_memory()
        eng = process_memory()
        du = disk_usage(self.dir)
        return {"system": sm,
                "engine": eng if eng else None,
                "disk": du,
                "cpu_n": cpu_count(),
                "budget": self.budget.__dict__,
                "pressured": self.pressure_evt.is_set()}

    def admission_check(self, tag: str = "") -> bool:
        """Call before scheduling a new evaluation job.

        Returns True when scheduling may proceed; sets/clears the pressure
        event on transition and records peaks."""
        sm = system_memory()
        free = sm.get("free_mb")
        if free is None:
            self.pressure_evt.clear()
            return True
        was = self.pressure_evt.is_set()
        now_pressured = free < self.budget.free_floor_mb
        if now_pressured and not was:
            self.pressure_evt.set()
            self.peaks.append({"ts": time.time(), "free_mb": free,
                               "engine_mb": (process_memory() or {}).get(
                                   "working_set_mb"),
                               "event": f"pressure-on {tag}".strip()})
            self._logPressEvent("pressure-on", tag, free)
        elif not now_pressured and was:
            self.pressure_evt.clear()
            self._logPressEvent("pressure-off", tag, free)
        return not now_pressured

    def _logPressEvent(self, event: str, tag: str, free_mb):
        try:
            with open(self.dir / "pressure_log.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "event": event,
                                    "tag": tag, "free_mb": free_mb,
                                    "floor_mb": self.budget.free_floor_mb,
                                    "mode": self.budget.mode}) + "\n")
        except OSError:
            pass

    def release_disposable(self, dirs: list) -> list:
        """Release DISPOSABLE caches only: eval images, thumbnails, preview
        intermediates under the given dirs. Never touches protected paths
        (state.json, breeder.json, lineage files, champion exports, gates)."""
        released = []
        for d in dirs:
            d = Path(d)
            if not d.exists():
                continue
            for p in d.rglob("*"):
                if not p.is_file():
                    continue
                if p.name.lower() in PROTECTED_NAMES or p.suffix in (".json", ".jsonl"):
                    continue  # candidate/checkpoint data is never disposable
                try:
                    size = p.stat().st_size
                    p.unlink()
                    released.append({"path": str(p), "bytes": size})
                except OSError:
                    continue
        self.cleanup_log.append({"ts": time.time(), "released": len(released),
                                 "bytes": sum(x["bytes"] for x in released)})
        return released

    def cached_report(self) -> dict:
        return {"peaks": self.peaks[-20:], "cleanup_log": self.cleanup_log[-10:]}

    def save_peak_record(self, peak_mb: int, label: str) -> Path:
        """Persist a measured peak for the resource-usage report."""
        rec = {"ts": time.time(), "label": label, "peak_mb": int(peak_mb),
               "engine_budget_mb": self.budget.engine_budget_mb,
               "mode": self.budget.mode}
        out = self.dir / "memory_report.json"
        rows = []
        if out.exists():
            try:
                rows = json.loads(out.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        rows.append(rec)
        out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        return out


# module-level shared instance wired by the app
_MANAGER: ResourceManager | None = None


def init_manager(data_dir, profile: dict | None = None) -> ResourceManager:
    global _MANAGER
    _MANAGER = ResourceManager(data_dir, profile)
    return _MANAGER


def manager() -> ResourceManager:
    assert _MANAGER is not None, "call init_manager() first"
    return _MANAGER
