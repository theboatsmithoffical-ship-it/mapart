"""FireFly Map-Art Engine — local desktop GUI (tkinter, offline).

Launch: python firefly_engine.py   (or python -m firefly.mapart.gui)

Tabs:
  Map-Art    image import, palette/layout/dithering/suppression, preview,
             block counts, schematic export + validation
  TestSuite  the built-in deterministic test images (no downloads)
  Benchmark  quick/full discovered sweeps with CSV/JSON reports
  Lab        Evolution Lab: start/pause/resume/stop + best candidate export
  History    local experiment history files
  Settings   local data locations and defaults

Worker threads queue GUI updates through a message queue — the UI stays
responsive during long runs; Stop signals cooperative cancellation.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import numpy as np
from PIL import Image, ImageTk

from . import appcore, benchmark, breeding, imagelibrary, labcore, planner, resourcemanager, schem, testsuite
from . import mapart as _mapart
from .mapart import available_versions, load_palette
from .registry import all as reg_all

APP_DIR = Path(__file__).resolve().parents[3]
DATA_DIR = APP_DIR / "mapart-data"
DEFAULT_OUT = APP_DIR / "mapart-output"
DEFAULT_OUT.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

CATEGORIES = ("mapart", "suite", "benchmark", "lab", "history", "settings")


class GuiApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("FireFly Map-Art Engine + Evolution Lab (local, offline)")
        root.minsize(1080, 680)
        self.image_path = None
        self.result = None
        self.cancel_evt = threading.Event()
        self.msg: queue.Queue = queue.Queue()
        self.lab: labcore.LabController | None = None
        self.breeder: breeding.Breeder | None = None
        self.resman = resourcemanager.init_manager(DATA_DIR)
        self.planner = planner.Planner(DATA_DIR, library=imagelibrary.ImageLibrary(DATA_DIR))
        self.library = self.planner.library
        self._vs_images = {}          # visual studio on-demand image cache
        self._breed_file = None
        self._build()
        root.after(80, self._poll)

    # =================== build ===================
    def _build(self):
        # dark scientific theme (clam engine — real styling, not cosmetic-only;
        # BG/FG/ACCENT constants control every widget below)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        BG, FG, PANEL, ACCENT = "#14161b", "#e8eaf0", "#1d2027", "#4c8dd6"
        self.root.configure(bg=BG)
        style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL)
        style.configure("TNotebook", background=BG, foreground=FG)
        style.configure("TNotebook.Tab", background=PANEL, foreground=FG, padding=(10, 5))
        style.map("TNotebook.Tab", background=[("selected", ACCENT)])
        style.configure("TButton", background=PANEL, foreground=FG)
        style.map("TButton", background=[("active", "#2a2e38")])
        style.configure("TLabelframe", background=BG, foreground=FG)
        style.configure("TLabelframe.Label", background=BG, foreground="#9fd0ff")
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Treeview", background=PANEL, foreground=FG, fieldbackground=PANEL)
        style.configure("TProgressbar", troughcolor=PANEL, background=ACCENT)
        self._DARK = {"bg": BG, "fg": FG, "panel": PANEL, "accent": ACCENT}

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)
        self.parent = {}

        # ---- Control Center tab (overview) ----
        cc = ttk.Frame(nb, padding=8)
        nb.add(cc, text="Control Center")
        self._build_controlcenter(cc)
        # ---- Map-Art tab ----
        ma = ttk.Frame(nb, padding=8)
        nb.add(ma, text="Map-Art")
        self._build_mapart(ma)
        # ---- Test suite tab ----
        ts = ttk.Frame(nb, padding=8)
        nb.add(ts, text="Test Suite")
        self._build_suite(ts)
        # ---- Benchmark tab ----
        bm = ttk.Frame(nb, padding=8)
        nb.add(bm, text="Benchmark & Test Center")
        self._build_benchmark(bm)
        # ---- Lab tab ----
        lb = ttk.Frame(nb, padding=8)
        nb.add(lb, text="Evolution Lab")
        self._build_lab(lb)
        # ---- Visual comparison studio ----
        vs = ttk.Frame(nb, padding=8)
        nb.add(vs, text="Visual Studio")
        self._build_visual(vs)
        # ---- Image library & datasets ----
        dl = ttk.Frame(nb, padding=8)
        nb.add(dl, text="Image Library & Datasets")
        self._build_datasets(dl)
        # ---- Resources tab ----
        rs = ttk.Frame(nb, padding=8)
        nb.add(rs, text="Resources")
        self._build_resources(rs)
        # ---- History tab ----
        hi = ttk.Frame(nb, padding=8)
        nb.add(hi, text="History")
        self._build_history(hi)
        # ---- Configuration & recovery tab ----
        st = ttk.Frame(nb, padding=8)
        nb.add(st, text="Configuration & Recovery")
        self._build_settings(st)

    def _build_mapart(self, ma):
        left = ttk.Frame(ma); left.pack(side="left", fill="y", padx=(0, 8))
        ttk.Label(left, text="Start here (no terminal needed):\n1. Import image…\n"
                            "2. Auto = auto layout, defaults are fine\n"
                            "3. Process\n4. Export schematic…",
                  foreground="#445", justify="left").pack(anchor="w", pady=(0, 4))
        ttk.Button(left, text="Import image…", command=self.pick_image).pack(fill="x")
        self.lbl_img = ttk.Label(left, text="no image loaded", width=34, wraplength=260)
        self.lbl_img.pack(anchor="w", pady=(2, 8))
        ttk.Label(left, text="Palette / version").pack(anchor="w")
        self.cmb_ver = ttk.Combobox(left, values=sorted(available_versions()), state="readonly")
        self.cmb_ver.current(0); self.cmb_ver.pack(fill="x")
        self.ver_note = ttk.Label(left, text="", wraplength=260, foreground="#555")
        self.ver_note.pack(anchor="w")
        ttk.Label(left, text="Map layout (W × H, 128 px each)").pack(anchor="w", pady=(6, 0))
        row = ttk.Frame(left); row.pack(fill="x")
        self.spn_w = ttk.Spinbox(row, from_=1, to=8, width=5); self.spn_w.set(1)
        self.spn_h = ttk.Spinbox(row, from_=1, to=8, width=5); self.spn_h.set(1)
        self.spn_w.pack(side="left"); ttk.Label(row, text=" × ").pack(side="left"); self.spn_h.pack(side="left")
        ttk.Button(row, text="Auto", width=5, command=self.auto_layout).pack(side="left", padx=(4, 0))
        ttk.Label(left, text="Fit mode / transparency").pack(anchor="w", pady=(6, 0))
        self.cmb_fit = ttk.Combobox(left, values=list(appcore.FIT_MODES), state="readonly")
        self.cmb_fit.set("cover"); self.cmb_fit.pack(fill="x")
        self.cmb_tr = ttk.Combobox(left, values=list(appcore.TRANSPARENCY_MODES), state="readonly")
        self.cmb_tr.set("matte"); self.cmb_tr.pack(fill="x")
        ttk.Label(left, text="Color metric / palette kind / construction").pack(anchor="w", pady=(6, 0))
        self.cmb_metric = ttk.Combobox(left, values=list(appcore.METRICS), state="readonly")
        self.cmb_metric.set("rgb"); self.cmb_metric.pack(fill="x")
        self.cmb_kind = ttk.Combobox(left, values=list(appcore.PALETTE_KINDS), state="readonly")
        self.cmb_kind.set("blocks"); self.cmb_kind.pack(fill="x")
        self.cmb_cons = ttk.Combobox(left, values=list(appcore.CONSTRUCTION_MODES), state="readonly")
        self.cmb_cons.set("frame"); self.cmb_cons.pack(fill="x")
        nbr = ttk.Frame(left); nbr.pack(fill="x", pady=(2, 0))
        self.var_neighbors = tk.BooleanVar(value=False)
        ttk.Checkbutton(nbr, text="Neighbor-optimise choices", variable=self.var_neighbors).pack(side="left")
        fmt = ttk.Frame(left); fmt.pack(fill="x", pady=(2, 0))
        ttk.Label(fmt, text="Export:").pack(side="left")
        self.var_fmt_schem = tk.BooleanVar(value=True)
        self.var_fmt_litematic = tk.BooleanVar(value=False)
        self.var_fmt_structure = tk.BooleanVar(value=False)
        ttk.Checkbutton(fmt, text=".schem", variable=self.var_fmt_schem).pack(side="left")
        ttk.Checkbutton(fmt, text=".litematic", variable=self.var_fmt_litematic).pack(side="left")
        ttk.Checkbutton(fmt, text=".nbt", variable=self.var_fmt_structure).pack(side="left")
        ttk.Label(left, text="Dithering / matching").pack(anchor="w", pady=(6, 0))
        self.cmb_alg = ttk.Combobox(left, values=list(appcore.SUPPORTED_ALGORITHMS), state="readonly")
        self.cmb_alg.set("floyd_steinberg"); self.cmb_alg.pack(fill="x")
        ttk.Label(left, text="Strength").pack(anchor="w", pady=(6, 0))
        self.scl_strength = ttk.Scale(left, from_=0.0, to=2.0, value=1.0, command=self._strength_changed)
        self.scl_strength.pack(fill="x")
        self.lbl_strength = ttk.Label(left, text="1.00"); self.lbl_strength.pack(anchor="w")
        ttk.Label(left, text="Color suppression (select to drop)").pack(anchor="w", pady=(6, 0))
        self.lst_sup = tk.Listbox(left, selectmode="multiple", height=7, exportselection=False)
        self.lst_sup.pack(fill="x")
        self._sup_loaded_for = None
        btnbar = ttk.Frame(left); btnbar.pack(fill="x", pady=8)
        self.btn_process = ttk.Button(btnbar, text="Process", command=self.process)
        self.btn_process.pack(side="left")
        self.btn_stop = ttk.Button(btnbar, text="Stop", command=self.cancel, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        self.btn_export = ttk.Button(btnbar, text="Export schematic…", command=self.export, state="disabled")
        self.btn_export.pack(side="left")
        self.prg = ttk.Progressbar(left, mode="determinate", maximum=1.0); self.prg.pack(fill="x", pady=(4, 0))
        self.lbl_prog = ttk.Label(left, text="idle"); self.lbl_prog.pack(anchor="w")
        right = ttk.Frame(ma); right.pack(side="left", fill="both", expand=True)
        panes = ttk.Panedwindow(right, orient="vertical"); panes.pack(fill="both", expand=True)
        top_panes = ttk.Panedwindow(panes, orient="horizontal")
        bottom_panes = ttk.Panedwindow(panes, orient="horizontal")
        panes.add(top_panes, weight=1); panes.add(bottom_panes, weight=1)
        f_in = ttk.Labelframe(top_panes, text="Input image")
        f_proc = ttk.Labelframe(top_panes, text="Processed image (transparency + corrections)")
        top_panes.add(f_in, weight=1); top_panes.add(f_proc, weight=1)
        f_out = ttk.Labelframe(bottom_panes, text="Reconstruction (map colors)")
        f_layout = ttk.Labelframe(bottom_panes, text="Map-art layout (tile boundaries)")
        bottom_panes.add(f_out, weight=1); bottom_panes.add(f_layout, weight=1)
        self.cv_in = tk.Canvas(f_in, background="#202020", highlightthickness=0)
        self.cv_in.pack(fill="both", expand=True)
        self.cv_proc = tk.Canvas(f_proc, background="#202020", highlightthickness=0)
        self.cv_proc.pack(fill="both", expand=True)
        self.cv_out = tk.Canvas(f_out, background="#202020", highlightthickness=0)
        self.cv_out.pack(fill="both", expand=True)
        self.cv_layout = tk.Canvas(f_layout, background="#202020", highlightthickness=0)
        self.cv_layout.pack(fill="both", expand=True)
        counts = ttk.Labelframe(right, text="Planned dimensions, block counts, layers")
        counts.pack(fill="both", pady=(6, 0))
        self.txt_counts = tk.Text(counts, height=9, wrap="word"); self.txt_counts.pack(fill="both")

    def _build_suite(self, ts):
        top = ttk.Frame(ts); top.pack(fill="x")
        self.cmb_tier = ttk.Combobox(top, values=sorted(testsuite.TIERS), state="readonly")
        self.cmb_tier.set("small"); self.cmb_tier.pack(side="left")
        ttk.Label(top, text="tier").pack(side="left", padx=4)
        ttk.Button(top, text="Show selected", command=self.show_suite_item).pack(side="left", padx=4)
        ttk.Button(top, text="Render suite folder…", command=self.save_suite).pack(side="left", padx=4)
        body = ttk.Frame(ts); body.pack(fill="both", expand=True, pady=4)
        self.lst_items = tk.Listbox(body, width=34, exportselection=False)
        for i, (k, (_fn, purpose)) in enumerate(testsuite.GENERATORS.items()):
            self.lst_items.insert("end", f"{k}: {purpose}")
        self.lst_items.selection_set(0)
        self.lst_items.pack(side="left", fill="y")
        self.cv_suite = tk.Canvas(body, background="#202020", highlightthickness=0)
        self.cv_suite.pack(side="left", fill="both", expand=True)
        self.lbl_suite = ttk.Label(ts, text="14 deterministic built-in generators — nothing is downloaded")
        self.lbl_suite.pack(anchor="w")

    def _build_benchmark(self, bm):
        top = ttk.Frame(bm); top.pack(fill="x")
        self.cmb_bench_mode = ttk.Combobox(top, values=("quick", "full"), state="readonly")
        self.cmb_bench_mode.set("quick"); self.cmb_bench_mode.pack(side="left")
        ttk.Label(top, text="mode").pack(side="left", padx=4)
        self.btn_bench = ttk.Button(top, text="Run campaign", command=self.run_benchmark)
        self.btn_bench.pack(side="left", padx=6)
        self.btn_bench_stop = ttk.Button(top, text="Stop", command=self.stop_benchmark, state="disabled")
        self.btn_bench_stop.pack(side="left")
        self.prg_bench = ttk.Progressbar(bm, mode="determinate", maximum=1.0)
        self.prg_bench.pack(fill="x", pady=6)
        self.lbl_bench = ttk.Label(bm, text="idle — discovers all registered algorithms automatically")
        self.lbl_bench.pack(anchor="w")
        self.txt_bench = tk.Text(bm, height=22, wrap="word"); self.txt_bench.pack(fill="both", expand=True)

    def _build_lab(self, lb):
        left = ttk.Frame(lb); left.pack(side="left", fill="y", padx=(0, 8))
        ttk.Label(left, text="Search strategy").pack(anchor="w")
        self.cmb_lab_strategy = ttk.Combobox(left, values=("evolve", "random", "sweep"), state="readonly")
        self.cmb_lab_strategy.set("evolve"); self.cmb_lab_strategy.pack(fill="x")
        ttk.Label(left, text="Eval budget / time limit (s)").pack(anchor="w", pady=(6, 0))
        gr = ttk.Frame(left); gr.pack(fill="x")
        self.spn_lab_evals = ttk.Spinbox(gr, from_=2, to=20000, width=7); self.spn_lab_evals.set(40); self.spn_lab_evals.pack(side="left")
        self.spn_lab_time = ttk.Spinbox(gr, from_=30, to=86400, width=7); self.spn_lab_time.set(600); self.spn_lab_time.pack(side="left", padx=4)
        ttk.Label(left, text="Suite image list (comma-separated)").pack(anchor="w", pady=(6, 0))
        self.ent_lab_ids = ttk.Entry(left); self.ent_lab_ids.insert(0, "grad-smooth,faux-photo,checker-hf")
        self.ent_lab_ids.pack(fill="x")
        btns = ttk.Frame(left); btns.pack(fill="x", pady=8)
        ttk.Button(btns, text="Run", command=self.lab_start).pack(side="left")
        ttk.Button(btns, text="Pause", command=self.lab_pause).pack(side="left", padx=2)
        ttk.Button(btns, text="Resume", command=self.lab_resume).pack(side="left", padx=2)
        ttk.Button(btns, text="Stop", command=self.lab_stop).pack(side="left", padx=2)
        ttk.Button(left, text="Export best configuration…", command=self.lab_export_best).pack(fill="x", pady=8)
        self.lbl_lab = ttk.Label(left, text="idle — checkpoints in mapart-data/lab/")
        self.lbl_lab.pack(anchor="w")
        self.prg_lab = ttk.Progressbar(left, mode="determinate"); self.prg_lab.pack(fill="x", pady=(2, 6))
        right = ttk.Frame(lb); right.pack(side="left", fill="both", expand=True)
        self.txt_lab = tk.Text(right, height=16, wrap="word"); self.txt_lab.pack(fill="both", expand=True)
        frame_hist = ttk.Labelframe(right, text="Experiment history (this run)")
        frame_hist.pack(fill="both", pady=6)
        self.tree_hist = ttk.Treeview(frame_hist, columns=("score", "algorithm", "strength", "detail"),
                                      show="headings", height=8)
        for c, w in (("score", 70), ("algorithm", 120), ("strength", 70), ("detail", 380)):
            self.tree_hist.column(c, width=w); self.tree_hist.heading(c, text=c)
        self.tree_hist.pack(fill="both", expand=True)

        # ================= Breeding (feature §12) =================
        bf = ttk.Labelframe(right, text="Breeding — crossover, percentages, mutation rules")
        f_pack = bf.pack(fill="x", pady=6)
        fr1 = ttk.Frame(bf); fr1.pack(fill="x", pady=2)
        ttk.Label(fr1, text="Method").pack(side="left")
        self.cmb_breed_method = ttk.Combobox(
            fr1, values=sorted(breeding.REGISTRY), state="readonly", width=16)
        self.cmb_breed_method.set("weighted_params"); self.cmb_breed_method.pack(side="left", padx=4)
        ttk.Label(fr1, text="Preset").pack(side="left", padx=(10, 0))
        self.cmb_breed_preset = ttk.Combobox(
            fr1, values=sorted(breeding.PRESETS), state="readonly", width=26)
        self.cmb_breed_preset.set("50/50 crossover")
        self.cmb_breed_preset.pack(side="left", padx=4)
        self.cmb_breed_preset.bind("<<ComboboxSelected>>", self.breed_preset_changed)

        fr2 = ttk.Frame(bf); fr2.pack(fill="x", pady=2)
        ttk.Label(fr2, text="Contribution % (parent order = selection order)").pack(side="left")
        self.ent_breed_weights = ttk.Entry(fr2, width=14)
        self.ent_breed_weights.insert(0, "70,30"); self.ent_breed_weights.pack(side="left", padx=4)
        ttk.Label(fr2, text="Mutation prob / strength").pack(side="left", padx=(10, 0))
        self.spn_breed_mprob = ttk.Spinbox(fr2, from_=0.0, to=1.0, increment=0.05, width=5)
        self.spn_breed_mprob.set(0.7); self.spn_breed_mprob.pack(side="left", padx=2)
        self.spn_breed_mstr = ttk.Spinbox(fr2, from_=0.0, to=1.0, increment=0.05, width=5)
        self.spn_breed_mstr.set(0.5); self.spn_breed_mstr.pack(side="left", padx=2)

        fr3 = ttk.Frame(bf); fr3.pack(fill="x", pady=2)
        ttk.Label(fr3, text="Offspring/gens").pack(side="left")
        self.spn_breed_off = ttk.Spinbox(fr3, from_=1, to=12, width=4)
        self.spn_breed_off.set(2); self.spn_breed_off.pack(side="left", padx=2)
        self.spn_breed_gens = ttk.Spinbox(fr3, from_=1, to=50, width=4)
        self.spn_breed_gens.set(2); self.spn_breed_gens.pack(side="left", padx=2)
        ttk.Label(fr3, text="Elite/holdout every").pack(side="left", padx=(10, 0))
        self.spn_breed_elite = ttk.Spinbox(fr3, from_=1, to=32, width=4)
        self.spn_breed_elite.set(4); self.spn_breed_elite.pack(side="left", padx=2)
        self.spn_breed_hold = ttk.Spinbox(fr3, from_=0, to=20, width=4)
        self.spn_breed_hold.set(2); self.spn_breed_hold.pack(side="left", padx=2)

        fr4 = ttk.Frame(bf); fr4.pack(fill="x", pady=2)
        ttk.Label(fr4, text="Parents source").pack(side="left")
        self.cmb_breed_source = ttk.Combobox(
            fr4, values=("lab history top-K", "JSON file…"), state="readonly", width=18)
        self.cmb_breed_source.set("lab history top-K"); self.cmb_breed_source.pack(side="left", padx=4)
        ttk.Button(fr4, text="Browse…", command=self.breed_pick_file).pack(side="left", padx=2)
        bbtns = ttk.Frame(bf); bbtns.pack(fill="x", pady=4)
        ttk.Button(bbtns, text="Breed", command=self.breed_start).pack(side="left")
        ttk.Button(bbtns, text="Pause", command=self.breed_pause).pack(side="left", padx=2)
        ttk.Button(bbtns, text="Resume", command=self.breed_resume).pack(side="left", padx=2)
        ttk.Button(bbtns, text="Stop", command=self.breed_stop).pack(side="left", padx=2)
        ttk.Button(bbtns, text="Show lineage tree", command=self.breed_show_lineage).pack(side="left", padx=8)
        self.lbl_breed = ttk.Label(bf, text="idle — checkpoints in mapart-data/breeding/")
        self.lbl_breed.pack(anchor="w")
        self.lbl_breed_pct = ttk.Label(bf, text="effective parent percentages: —")
        self.lbl_breed_pct.pack(anchor="w")

    def _build_history(self, hi):
        ttk.Label(hi, text="Local run folders under mapart-data/ (checkpoints, results, caches)").pack(anchor="w")
        self.lst_hist = tk.Listbox(hi, height=18)
        for d in sorted(DATA_DIR.glob("*")):
            if d.is_dir():
                self.lst_hist.insert("end", d.name)
        self.lst_hist.pack(fill="both", expand=True)
        ttk.Button(hi, text="Refresh", command=self._refresh_history).pack(anchor="w", pady=6)

    def _build_settings(self, st):
        rec = ttk.Labelframe(st, text="Configuration profiles & recovery")
        rec.pack(fill="x", pady=4)
        row1 = ttk.Frame(rec); row1.pack(fill="x")
        ttk.Label(row1, text="Resource mode").pack(side="left")
        self.cmb_res_mode = ttk.Combobox(row1, values=("safe", "balanced", "performance"),
                                         state="readonly", width=12)
        self.cmb_res_mode.set(self.resman.budget.mode); self.cmb_res_mode.pack(side="left", padx=4)
        ttk.Button(row1, text="Apply mode", command=lambda: self.res_mode(
            self.cmb_res_mode.get())).pack(side="left", padx=4)
        row2 = ttk.Frame(rec); row2.pack(fill="x", pady=2)
        ttk.Button(row2, text="Restore run checkpoint (resume Lab)",
                   command=self.cfg_restore).pack(side="left")
        ttk.Button(row2, text="Restore champion → export",
                   command=self.cfg_restore_champion).pack(side="left", padx=4)
        ttk.Button(row2, text="Export experiment report",
                   command=self.cfg_export_report).pack(side="left", padx=4)
        lines = [
            f"Engine folder:        {APP_DIR}",
            f"Working data folder:  {DATA_DIR}   (presets, checkpoints, results, logs)",
            f"Export output folder: {DEFAULT_OUT}",
            f"Installed Python:     local; requires numpy + Pillow (tkinter included)",
            f"Network usage:        none in routine operation; explicit click only "
            f"downloads openly-licensed test images (Wikimedia Commons)",
            f"CPU policy:           single-worker evolution (8 GB profile)",
            f"Budgets:              engine {self.resman.budget.engine_budget_mb} MB "
            f"target, GUI {self.resman.budget.gui_budget_mb} MB target, mode "
            f"{self.resman.budget.mode} (policy + monitoring, not an OS-level cap)",
        ]
        txt = tk.Text(st, height=10, wrap="word")
        txt.insert("1.0", "\n".join(lines))
        txt.pack(fill="both", expand=True)
        logframe = ttk.Labelframe(st, text="Logs & recovery events")
        logframe.pack(fill="both", expand=True, pady=6)
        self.txt_logs = tk.Text(logframe, height=10, wrap="word")
        self.txt_logs.pack(fill="both", expand=True)
        ttk.Button(logframe, text="Refresh logs",
                   command=self.cfg_refresh_logs).pack(anchor="w", pady=2)

    def cfg_restore(self):
        try:
            if self.lab and self.lab.is_running():
                messagebox.showinfo("Recovery", "Stop the running lab first.")
                return
            if not (self.lab_dir() / "state.json").exists():
                messagebox.showinfo("Recovery", "No checkpoint found under mapart-data/lab.")
                return
            self.lab = labcore.LabController(self.lab_dir())  # resumes state.json
            self._set_status("checkpoint loaded — press Run/Resume to continue")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Recovery", f"{type(e).__name__}: {e}")

    def cfg_restore_champion(self):
        best = None
        bp = self.lab_dir() / "best.json"
        if bp.exists():
            try:
                best = json.loads(bp.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
        if not best and self.breeder:
            best = (self.breeder.state.get("champion") or None)
        if not best:
            messagebox.showinfo("Recovery", "No champion recorded yet.")
            return
        try:
            self.lab_export_best()
            self._set_status("champion restored to export folder")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Recovery", f"{type(e).__name__}: {e}")

    def cfg_export_report(self):
        try:
            rep = {
                "generated": time.time(),
                "resource_snapshot": self.resman.snapshot(),
                "labs": self._lab_status_snapshot(),
                "planner": self.planner.analyze(),
                "library": {"entries": len(self.library.manifest["images"]),
                            "verify": self.library.verify(),
                            "revisions": [p.name for p in self.library.revisions()]},
            }
            out = DATA_DIR / ("report_" + time.strftime("%Y%m%d_%H%M%S") + ".json")
            out.write_text(json.dumps(rep, indent=2, default=list), encoding="utf-8")
            self._set_status(f"report exported: {out}")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Report", f"{type(e).__name__}: {e}")

    def _lab_status_snapshot(self):
        out = {}
        for nm in ("lab",):
            d = DATA_DIR / nm
            if (d / "state.json").exists():
                try:
                    st = json.loads((d / "state.json").read_text(encoding="utf-8"))
                    out[nm] = {k: st.get(k) for k in ("evaluated", "invalid", "finished", "best")}
                except Exception:  # noqa: BLE001
                    pass
        bd = DATA_DIR / "breeding"
        if (bd / "breeder.json").exists():
            try:
                st = json.loads((bd / "breeder.json").read_text(encoding="utf-8"))
                out["breeding"] = {k: st.get(k) for k in ("generation", "evals", "champion")}
            except Exception:  # noqa: BLE001
                pass
        return out

    def cfg_refresh_logs(self):
        sections = []
        plog = DATA_DIR / "pressure_log.jsonl"
        if plog.exists():
            sections.append("pressure_log.jsonl (tail):\n" +
                            "\n".join(plog.read_text(encoding="utf-8").splitlines()[-12:]))
        hpath = self.lab_dir() / "history.jsonl"
        if hpath.exists():
            errs = [json.dumps(json.loads(l), default=list)[:220]
                    for l in hpath.read_text(encoding="utf-8").splitlines()
                    if l.strip() and json.loads(l).get("score") is None]
            sections.append(f"lab exceptions ({len(errs)}):\n" + "\n".join(errs[-8:]))
        self.txt_logs.delete("1.0", "end")
        self.txt_logs.insert("1.0", "\n\n".join(sections) or "no logs yet")
        ttk.Button(st, text="Open data folder (creates mapart-output)",
                   command=self.open_out).pack(anchor="w", pady=6)

    # =================== Control Center (page A) ===================
    def _build_controlcenter(self, cc):
        left = ttk.Frame(cc); left.pack(side="left", fill="y", padx=(0, 8))
        box = ttk.Labelframe(left, text="Run control")
        box.pack(fill="y")
        for txt, cmd in (("Lab: Start", self.lab_start), ("Lab: Pause", self.lab_pause),
                         ("Lab: Resume", self.lab_resume), ("Lab: Stop", self.lab_stop)):
            ttk.Button(box, text=txt, command=cmd, width=16).pack(fill="x", pady=2)
        for txt, cmd in (("Breed: Start", self.breed_start), ("Breed: Pause", self.breed_pause),
                         ("Breed: Resume", self.breed_resume), ("Breed: Stop", self.breed_stop)):
            ttk.Button(box, text=txt, command=cmd, width=16).pack(fill="x", pady=2)
        right = ttk.Frame(cc); right.pack(side="left", fill="both", expand=True)
        self.txt_cc = tk.Text(right, height=10, wrap="word")
        self.txt_cc.pack(fill="both", expand=True)
        pp = ttk.Labelframe(right, text="Planner — next experiments (from recorded weaknesses)")
        pp.pack(fill="both", pady=6)
        self.txt_plan = tk.Text(pp, height=10, wrap="word"); self.txt_plan.pack(fill="both", expand=True)
        pbtns = ttk.Frame(pp); pbtns.pack(fill="x")
        ttk.Button(pbtns, text="Plan next experiments", command=self.cc_plan).pack(side="left")
        ttk.Button(pbtns, text="Run proposal #1", command=self.cc_run_proposal).pack(side="left", padx=4)
        ttk.Button(pbtns, text="Show failure classes", command=self.cc_failures).pack(side="left", padx=4)
        self.lbl_cc_res = ttk.Label(right, text="")
        self.lbl_cc_res.pack(anchor="w")

    def _cc_line(self, st, name):
        if not st:
            return f"{name}: idle"
        return (f"{name}: gen {st.get('generation', st.get('evaluated', '-'))} "
                f"evals {st.get('evals', st.get('evaluated'))} "
                f"{'RUNNING' if st.get('running') else 'PAUSED' if st.get('paused') else 'idle'}")

    def _cc_poll(self):
        try:
            lab_st = self.lab.status() if self.lab else None
            br_st = self.breeder.status() if self.breeder else None
            snap = self.resman.snapshot()
            sm = snap["system"]
            eng = snap["engine"] or {}
            fails = 0
            if self.lab:
                fails = self.lab.state.get("invalid", 0)
            nodes = [self._cc_line(lab_st, "lab"), self._cc_line(br_st, "breeder")]
            champ = (br_st or {}).get("champion") or ((self.lab and self.lab.state.get("best")) or None)
            if champ:
                cand = champ.get("candidate", {})
                nodes.append(f"champion: {champ.get('score')} — {cand.get('algorithm')} "
                             f"strength {cand.get('strength')}")
            nodes.append(f"failures so far: {fails} (see Failure classes button)")
            nodes.append(f"system: free {sm.get('free_mb')} MB / {sm.get('total_mb')} MB "
                         f"({sm.get('source')})" + (f"; engine working set {eng.get('working_set_mb')} MB, "
                                                    f"peak {eng.get('peak_working_set_mb')} MB" if eng else "; engine memory: unavailable"))
            nodes.append("pressure: " + ("PAUSED-ON-PRESSURE floor " + str(snap['budget']['free_floor_mb']) + " MB"
                                          if snap["pressured"] else f"clear (floor {snap['budget']['free_floor_mb']} MB, mode {snap['budget']['mode']})"))
            # checkpoint status: real file timestamps
            cps = []
            for nm in ("state.json", "best.json"):
                p = DATA_DIR / "lab" / nm
                if p.exists():
                    cps.append(f"{nm} @ {time.strftime('%H:%M:%S', time.localtime(p.stat().st_mtime))}")
            for nm in ("breeder.json",):
                p = DATA_DIR / "breeding" / nm
                if p.exists():
                    cps.append(f"breeding/{nm} @ {time.strftime('%H:%M:%S', time.localtime(p.stat().st_mtime))}")
            nodes.append("checkpoints: " + (", ".join(cps) or "none yet"))
            txt = "\n".join(nodes)
            if self.txt_cc.get("1.0", "end-1c") != txt:
                self.txt_cc.delete("1.0", "end")
                self.txt_cc.insert("1.0", txt)
        except Exception:  # noqa: BLE001 — dashboard must never crash the pump
            pass

    def cc_plan(self):
        try:
            rows = []
            hpath = self.lab_dir() / "history.jsonl"
            if hpath.exists():
                for line in hpath.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        rows.append(json.loads(line))
            self.planner.history = rows
            props = self.planner.propose(3)
            self._props = props
            self.txt_plan.delete("1.0", "end")
            for i, p in enumerate(props, 1):
                self.txt_plan.insert("end",
                    f"#{i} [{p['factor']}] est {p['estimated_cost_s']}s "
                    f"rev {p.get('dataset_revision')}\n  H: {p['hypothesis']}\n"
                    f"  target: {', '.join(p['target_suites'])}\n")
            if not props:
                self.txt_plan.insert("1.0", "no uninformative-experiment repeats — "
                                            "already-run hypotheses are filtered\n")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Planner", f"{type(e).__name__}: {e}")

    def cc_run_proposal(self):
        props = getattr(self, "_props", None)
        if not props:
            messagebox.showinfo("Planner", "Click 'Plan next experiments' first.")
            return
        prop, rows = props[0], []
        hpath = self.lab_dir() / "history.jsonl"
        if hpath.exists():
            for line in hpath.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        champ = self.planner._current_champion()
        def work():
            try:
                cand = prop["candidate"]
                s = appcore.Settings(version=cand["version"], maps_w=1, maps_h=1,
                                     algorithm=cand["algorithm"], strength=cand.get("strength", 1.0),
                                     suppress=cand.get("suppress", ()), bias=cand.get("bias", 0))
                purpose, arr = testsuite.suite("small", ["faux-photo"])["faux-photo"]
                img = DATA_DIR / "planner_fixture.png"
                testsuite.rasterize(arr).save(img, format="PNG")
                import numpy as _np
                orig = _np.asarray(testsuite.rasterize(arr), dtype=_np.uint8)
                t0 = time.time()
                res = appcore.process(img, s)
                from . import metrics as M
                met = M.compute(orig, _np.asarray(res.pal.arr, dtype=_np.float64),
                                res.preview.astype(_np.float64), res.idx,
                                time.time() - t0, res.counts, _np.empty(res.idx.shape),
                                res.pal, schem_valid=True)
                after = M.weighted(True, met)
                before = champ["score"] if champ else None
                rid = self.planner.record_result(prop, {"before": before, "after": after,
                                                        "promoted": before is None or after > before})
                self.root.after(0, lambda: self.lbl_cc_res.config(text=
                    f"experiment {rid}: {before} -> {after} recorded with conclusion"))
            except Exception as e:  # noqa: BLE001
                self.root.after(0, lambda: self.lbl_cc_res.config(text=f"failed: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def cc_failures(self):
        rows = []
        hpath = self.lab_dir() / "history.jsonl"
        if hpath.exists():
            for line in hpath.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        fails = [r for r in rows if r.get("score") is None]
        classes = {}
        for f in fails:
            c = planner.classify_failure(f)
            classes[c["kind"]] = classes.get(c["kind"], 0) + 1
        if fails:
            out = planner.generate_regression_test(fails[-5:], self.lab_dir() / "regressions")
            msg = ("failure classes: " + json.dumps(classes) +
                   f"\nregression test generated: {out['paths'][1]}")
        else:
            msg = "no failures recorded yet"
        messagebox.showinfo("Failures", msg)

    # =================== Visual Studio (page D) ===================
    def _build_visual(self, vs):
        ctrl = ttk.Frame(vs); ctrl.pack(fill="x")
        ttk.Button(ctrl, text="Load source image…", command=self.pick_image).pack(side="left")
        ttk.Label(ctrl, text="Challenger candidate (JSON)").pack(side="left", padx=(10, 2))
        self.ent_vs_chall = ttk.Entry(ctrl, width=34)
        self.ent_vs_chall.pack(side="left")
        ttk.Label(ctrl, text="Zoom").pack(side="left", padx=(10, 2))
        self.scl_vs_zoom = ttk.Scale(ctrl, from_=1.0, to=8.0, orient="horizontal", length=120)
        self.scl_vs_zoom.set(3.0)
        self.scl_vs_zoom.pack(side="left")
        ttk.Button(ctrl, text="Compare", command=self.vs_compare).pack(side="left", padx=8)
        ttk.Button(ctrl, text="Release previews (free RAM)", command=self.vs_release).pack(side="left")
        self.lbl_vs = ttk.Label(vs, text="no comparison loaded")
        self.lbl_vs.pack(anchor="w")
        row = ttk.Frame(vs); row.pack(fill="both", expand=True, pady=4)
        self.canvases = {}
        for title in ("Source", "Champion", "Challenger", "Difference"):
            frame = ttk.Labelframe(row, text=title)
            frame.pack(side="left", fill="both", expand=True, padx=3)
            cnv = tk.Canvas(frame, height=300, bg="#0b0d11", highlightthickness=0)
            cnv.pack(fill="both", expand=True)
            cnv.bind("<Motion>", lambda e, t=title: self._vs_inspect(e, t))
            self.canvases[title] = cnv
        self.lbl_vs_pix = ttk.Label(vs, text="pixel inspector: hover over a panel")
        self.lbl_vs_pix.pack(anchor="w")
        self.txt_vs = tk.Text(vs, height=8, wrap="word"); self.txt_vs.pack(fill="both", expand=True)

    def vs_release(self):
        self._vs_images.clear()
        for c in self.canvases.values():
            c.delete("all")
        self.lbl_vs.config(text="previews released (memory freed)")

    def vs_compare(self):
        """Render champion + challenger candidates on demand (never at startup);
        caches at most 3 images at a time (small tier only)."""
        try:
            if self.image_path is None:
                messagebox.showinfo("Visual Studio", "Load a source image first (Map-Art tab or here).")
                return
            best_p = self.lab_dir() / "best.json"
            cand_c = json.loads(best_p.read_text(encoding="utf-8"))["candidate"] if best_p.exists() else None
            cpath = self.ent_vs_chall.get().strip()
            cand_c2 = None
            if cpath:
                blob = json.loads(Path(cpath).read_text(encoding="utf-8"))
                cand_c2 = blob.get("candidate", blob) if isinstance(blob, dict) else None
            if cand_c is None and cand_c2 is None:
                messagebox.showinfo("Visual Studio", "No champion (run the Lab) and no challenger file given.")
                return
            def render(cand):
                s = appcore.Settings(version=cand["version"], maps_w=1, maps_h=1,
                                     algorithm=cand["algorithm"], strength=cand.get("strength", 1.0),
                                     suppress=cand.get("suppress", ()), bias=cand.get("bias", 0))
                return appcore.process(self.image_path, s)
            src = ImageTk.Image.open(self.image_path).convert("RGB")
            self._vs_images["source"] = src
            champ_res = render(cand_c) if cand_c else None
            chal_res = render(cand_c2) if cand_c2 else champ_res
            if champ_res:
                self._vs_images["champion"] = Image.fromarray(champ_res.preview)
            if chal_res:
                self._vs_images["challenger"] = Image.fromarray(chal_res.preview)
            if champ_res is not None and chal_res is not None:
                import numpy as _np
                d = (_np.asarray(self._vs_images["champion"], dtype=_np.int16) -
                     _np.asarray(self._vs_images["challenger"], dtype=_np.int16))
                diff = (_np.abs(d) * 4).clip(0, 255).astype(_np.uint8)
                self._vs_images["difference"] = Image.fromarray(diff)
            self._vs_draw()
            if champ_res:
                blocks = {}
                pal = champ_res.pal
                for idx in sorted(set(champ_res.idx.flatten().tolist())):
                    blocks[pal.map_color_ids[idx]] = blocks.get(pal.map_color_ids[idx], 0) + int((champ_res.idx == idx).sum())
                self.txt_vs.delete("1.0", "end")
                self.txt_vs.insert("1.0", "block assignments (map_color -> px count): " +
                                   json.dumps(dict(sorted(blocks.items(), key=lambda kv: -kv[1])[:10])))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Visual Studio", f"{type(e).__name__}: {e}")

    def _vs_draw(self):
        zoom = float(self.scl_vs_zoom.get())
        cw = self.canvases["Source"].winfo_width() or 260
        for title, cnv in self.canvases.items():
            cnv.delete("all")
            img = self._vs_images.get(title.lower())
            if img is None:
                continue
            w, h = img.size
            scale = min(cw / w, 300 / h) * zoom
            small = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.NEAREST)
            self._vs_images[title + "_tk"] = ImageTk.PhotoImage(small)
            cnv.create_image(0, 0, anchor="nw", image=self._vs_images[title + "_tk"])
        self.lbl_vs.config(text=f"comparison rendered (zoom {zoom:.1f}x; images decoded on demand, "
                                f"{len(self._vs_images)} objects cached)")

    def _vs_inspect(self, event, title):
        cnv = event.widget
        try:
            img = self._vs_images.get(title.lower())
            if img is None:
                return
            zoom = float(self.scl_vs_zoom.get())
            w, h = img.size
            scale = min((cnv.winfo_width() or 260) / w, 300 / h) * zoom
            x, y = int(event.x / scale), int(event.y / scale)
            if 0 <= x < w and 0 <= y < h:
                px = img.getpixel((x, y))
                self.lbl_vs_pix.config(text=f"{title} pixel ({x},{y}): {px}")
        except Exception:  # noqa: BLE001
            pass

    # =================== Dataset builder (Image Library) ===================
    def _build_datasets(self, dl):
        self._dl_pause = threading.Event()
        top = ttk.Frame(dl); top.pack(fill="x")
        ttk.Button(top, text="Find Test Images", command=self.dl_find).pack(side="left")
        ttk.Button(top, text="Build My Test Library", command=self.dl_build).pack(side="left", padx=4)
        ttk.Button(top, text="Quick Test Set", command=lambda: self.dl_set("quick")).pack(side="left", padx=4)
        ttk.Button(top, text="Full Benchmark Set", command=lambda: self.dl_set("full")).pack(side="left", padx=4)
        ttk.Button(top, text="Stress-Test Set", command=lambda: self.dl_set("stress")).pack(side="left", padx=4)
        ttk.Button(top, text="Generate Synthetic Tests", command=self.dl_synth).pack(side="left", padx=4)
        mid = ttk.Frame(dl); mid.pack(fill="x", pady=2)
        ttk.Button(mid, text="Verify Image Library", command=self.dl_verify).pack(side="left")
        ttk.Button(mid, text="Check Offline Readiness", command=self.dl_offline).pack(side="left", padx=4)
        ttk.Button(mid, text="Add My Own Images", command=self.dl_import_local).pack(side="left", padx=4)
        ttk.Button(mid, text="Pause Downloads", command=self.dl_pause).pack(side="left", padx=4)
        ttk.Button(mid, text="Resume Downloads", command=self.dl_resume).pack(side="left", padx=4)
        low = ttk.Frame(dl); low.pack(fill="x", pady=(0, 2))
        ttk.Button(low, text="Remove Dataset…", command=self.dl_remove_dataset).pack(side="left")
        ttk.Button(low, text="Export Manifest…", command=self.dl_export_manifest).pack(side="left", padx=4)
        ttk.Button(low, text="Import Manifest…", command=self.dl_import_manifest).pack(side="left", padx=4)
        ttk.Label(mid, text="Source query:").pack(side="left", padx=(12, 2))
        self.ent_dl_query = ttk.Entry(mid, width=24)
        self.ent_dl_query.insert(0, "landscape gradient texture")
        self.ent_dl_query.pack(side="left")
        self.lbl_dl = ttk.Label(dl, text="idle — library at mapart-data/imagelibrary (offline-safe; "
                                          "nothing downloads unless you click Find/Build)")
        self.lbl_dl.pack(anchor="w", pady=2)
        self.prg_dl = ttk.Progressbar(dl, mode="determinate"); self.prg_dl.pack(fill="x")
        self.tree_dl = ttk.Treeview(dl, columns=("id", "source", "license", "tags", "datasets"),
                                    show="headings", height=12)
        for c, w in (("id", 110), ("source", 140), ("license", 120), ("tags", 220), ("datasets", 110)):
            self.tree_dl.column(c, width=w); self.tree_dl.heading(c, text=c)
        self.tree_dl.pack(fill="both", expand=True)

    def _dl_candidates(self):
        from firefly.mapart.imagelibrary import discover_wikimedia, UNSPLASH_NOTICE
        query = self.ent_dl_query.get().strip() or "landscape texture"
        cands = discover_wikimedia(query, limit=8)
        if not cands:
            raise LibraryError("wikimedia returned no candidates (offline? try Add My Own Images / synthetic)")
        est_mb = self.library.batch_estimate_bytes(cands) // (1024 * 1024)
        free = self.library.free_space_mb()
        if est_mb > max(1, free // 2):
            raise LibraryError(f"estimate {est_mb} MB exceeds safety (free {free} MB); "
                               "reduce limit or free space")
        self.lbl_dl.config(text=f"found {len(cands)} candidates, est {est_mb} MB "
                                f"(free {free} MB). {UNSPLASH_NOTICE}")
        return cands

    def dl_find(self):
        def work():
            try:
                cands = self._dl_candidates()
                self.root.after(0, lambda: self.tree_dl.delete(*self.tree_dl.get_children()) or
                                [self.tree_dl.insert("", "end", values=(
                                    c['title'][:40], c['source'], c['license'],
                                    "candidate", "not yet")) for c in cands])
            except Exception as e:  # noqa: BLE001
                self.root.after(0, lambda e=e: self.lbl_dl.config(text=f"discovery failed: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def dl_build(self):
        def work():
            try:
                cands = self._dl_candidates()
                added = []
                for i, c in enumerate(cands):
                    # spec: pause/resume — .part files make any pause point
                    # resumable; pause takes effect between images
                    while self._dl_pause.is_set():
                        time.sleep(0.2)
                    try:
                        r = self.library.download(c, datasets=["dev"],
                            progress=lambda p, m: self.root.after(0,
                                lambda p=p, m=m: (self.prg_dl.config(value=p * i / max(1, len(cands)) * 0),
                                                  self.lbl_dl.config(text=f"downloading {i + 1}/{len(cands)}: {m}"))))
                        if not r.get("duplicate"):
                            added.append(r["id"])
                    except LibraryError as le:
                        self.root.after(0, lambda le=le: self.lbl_dl.config(text=str(le)))
                self.root.after(0, lambda ia=added: (self._dl_refresh(),
                    self.lbl_dl.config(text=f"library built: +{len(ia)} verified images (duplicates skipped)")))
            except Exception as e:  # noqa: BLE001
                self.root.after(0, lambda e=e: self.lbl_dl.config(text=f"download failed: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def dl_set(self, which):
        try:
            out = self.library.start_set(which)
            messagebox.showinfo("Dataset set", f"{which}: {out['count']} images "
                                f"(purpose {out['purpose']}), est {out['estimated_size_mb']} MB —\n"
                                f"ids: {', '.join(out['ids'][:6])}{'…' if len(out['ids']) > 6 else ''}\n"
                                "Applying membership is done via ImageLibrary.apply_set (revision-bumped).")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Dataset set", f"{type(e).__name__}: {e}")

    def dl_synth(self):
        try:
            out_dir = DATA_DIR / "imagelibrary" / "synthetic"
            saved = testsuite.save_all("small", out_dir)
            self.lbl_dl.config(text=f"synthetic suite saved to {out_dir} ({len(saved)} images)")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Synthetic", f"{type(e).__name__}: {e}")

    def dl_verify(self):
        v = self.library.verify()
        messagebox.showinfo("Verify", f"verified {v['verified']}/{v['total']} "
                            f"offline_ready={v['offline_ready']}\nproblems: "
                            f"{json.dumps(v['problems'][:4])}")

    def dl_offline(self):
        o = self.library.offline_ready()
        messagebox.showinfo("Offline readiness", f"ready={o['ready']} missing={o['missing']}")

    def dl_import_local(self):
        folder = filedialog.askdirectory(title="Select a folder of OWN images")
        if not folder:
            return
        added, failed = 0, 0
        for f in Path(folder).glob("*"):
            if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                try:
                    self.library.add_local(str(f), datasets=["dev"])
                    added += 1
                except LibraryError:
                    failed += 1
        self._dl_refresh()
        self.lbl_dl.config(text=f"imported {added} (+{failed} rejected: corrupt/unsupported)")

    def dl_pause(self):
        self._dl_pause.set()
        self.lbl_dl.config(text="download paused at image boundary — resumable "
                                "via .part files (Resume Downloads)")

    def dl_resume(self):
        self._dl_pause.clear()
        self.lbl_dl.config(text="download resumed")

    def dl_remove_dataset(self):
        ds = simpledialog.askstring(
            "Remove optional dataset",
            "Which dataset to remove? (dev / stress / holdout / synthetic)\n"
            "Experiment history (lab/breeding checkpoints, champions) is NOT touched.")
        if not ds:
            return
        ds = ds.strip().lower()
        confirm, reason = False, ""
        if ds == imagelibrary.LOCKED_PURPOSE:
            reason = (simpledialog.askstring(
                "Confirm held-out removal",
                "Removing the held-out evaluation set needs a written reason:") or "").strip()
            confirm = bool(reason) and messagebox.askyesno(
                "Remove held-out set?",
                f"Delete every held-out image?\n\nReason: {reason}")
            if not confirm:
                self.lbl_dl.config(text="held-out removal cancelled (no confirmation)")
                return
        try:
            removed = self.library.remove_dataset(ds, confirm=confirm, reason=reason)
        except imagelibrary.HoldoutLocked as ex:
            self.lbl_dl.config(text=f"refused: {ex}")
            return
        self._dl_refresh()
        self.lbl_dl.config(text=f"removed '{ds}': {len(removed)} images "
                                f"({', '.join(removed[:4])}{'' if len(removed) <= 4 else '…'})")

    def dl_export_manifest(self):
        path = filedialog.asksaveasfilename(
            title="Export dataset manifest",
            defaultextension=".json", initialfile="dataset_manifest_export.json")
        if not path:
            return
        payload = self.library.export_manifest()
        Path(path).write_text(json.dumps(payload, indent=1), encoding="utf-8")
        self.lbl_dl.config(text=f"manifest exported: {payload['count']} images "
                                f"(rev {payload['dataset_revision']}) -> {path}")

    def dl_import_manifest(self):
        path = filedialog.askopenfilename(title="Import dataset manifest",
                                          filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            out = self.library.import_manifest(data.get("images", []))
        except (json.JSONDecodeError, OSError, LibraryError) as e:
            messagebox.showerror("Import manifest", f"{type(e).__name__}: {e}")
            return
        self._dl_refresh()
        self.lbl_dl.config(text=(f"manifest imported: +{out['added']} restored, "
                                 f"{len(out['skipped'])} already present, "
                                 f"{len(out['missing_files'])} entries without a "
                                 "local file (re-download those)"))

    def _dl_refresh(self):
        self.tree_dl.delete(*self.tree_dl.get_children())
        for e in self.library.manifest["images"]:
            self.tree_dl.insert("", "end", values=(
                e["id"], e.get("source"), e.get("license")[:28],
                ",".join(e.get("analysis", {}).get("capability_tags", []))[:40],
                ",".join(e.get("datasets", []))))

    # =================== Resources (page F) ===================
    def _build_resources(self, rs):
        self.lbl_res = tk.Text(rs, height=14, wrap="word")
        self.lbl_res.pack(fill="both", expand=True)
        btns = ttk.Frame(rs); btns.pack(fill="x", pady=4)
        for mode in ("safe", "balanced", "performance"):
            ttk.Button(btns, text=mode, command=lambda m=mode: self.res_mode(m)).pack(side="left", padx=2)
        ttk.Button(btns, text="Release disposable caches", command=self.res_cleanup).pack(side="left", padx=8)
        ttk.Label(rs, text="(budgets are scheduling policy + monitoring — a hard cap "
                           "would need OS isolation; peaks are measured, not assumed)").pack(anchor="w")

    def res_mode(self, mode):
        out = self.resman.set_mode(mode)
        self._set_status(f"resource mode -> {mode}: workers {out['workers']}, "
                         f"floor {out['free_floor_mb']} MB, cache {out['cache_items']} items")

    def res_cleanup(self):
        released = self.resman.release_disposable([DATA_DIR / "lab", DATA_DIR / "imagelibrary"])
        self._set_status(f"released {len(released)} disposable files "
                         f"({sum(r['bytes'] for r in released) // 1024} kB); state/checkpoints/champion untouched")

    def _res_poll(self):
        try:
            snap = self.resman.snapshot()
            eng = snap["engine"]
            sys_ = snap["system"]
            disk = snap["disk"]
            cache_mb = resourcemanager.dir_size_mb(DATA_DIR / "lab")
            lib_mb = resourcemanager.dir_size_mb(DATA_DIR / "imagelibrary")
            press_tail = ""
            plog = DATA_DIR / "pressure_log.jsonl"
            if plog.exists():
                lines = [l for l in plog.read_text(encoding="utf-8").splitlines() if l.strip()]
                press_tail = "\npressure log (last 3):\n" + "\n".join(lines[-3:])
            txt = (f"system memory: free {sys_.get('free_mb')} MB / total {sys_.get('total_mb')} MB "
                   f"(load {sys_.get('load_pct')}%) [{sys_.get('source')}]\n"
                   f"engine process: " + (f"working set {eng.get('working_set_mb')} MB, peak {eng.get('peak_working_set_mb')} MB"
                                          if eng else "unavailable on this platform") + "\n"
                   f"budgets (mode {snap['budget']['mode']}): engine {snap['budget']['engine_budget_mb']} MB, "
                   f"GUI {snap['budget']['gui_budget_mb']} MB, workers {snap['budget']['workers']}, "
                   f"pressure floor {snap['budget']['free_floor_mb']} MB\n"
                   f"CPU: {resourcemanager.cpu_count()} logical cores\n"
                   f"disk: free {disk.get('free_mb')} MB; caches: lab {cache_mb} MB, "
                   f"image library {lib_mb} MB\n"
                   f"peaks recorded: {len(self.resman.peaks)}" + press_tail)
            if self.lbl_res.get("1.0", "end-1c") != txt:
                self.lbl_res.delete("1.0", "end")
                self.lbl_res.insert("1.0", txt)
        except Exception:  # noqa: BLE001
            pass

    # =================== helpers ===================
    def _strength_changed(self, _):
        self.lbl_strength.config(text=f"{float(self.scl_strength.get()):.2f}")

    def _set_status(self, text):
        self.status.config(text=text)

    def _add_preview(self, canvas, pil_image):
        w = canvas.winfo_width() or 420
        h = canvas.winfo_height() or 380
        scale = min((w - 8) / pil_image.width, (h - 8) / pil_image.height, 1.0)
        im = pil_image.resize((max(1, int(pil_image.width * scale)),
                               max(1, int(pil_image.height * scale))), Image.NEAREST)
        photo = ImageTk.PhotoImage(im)
        canvas.create_image((w // 2, h // 2), image=photo, anchor="center")
        canvas.image = photo

    def _refresh_sup_list(self, version):
        if self._sup_loaded_for == version:
            return
        self._sup_loaded_for = version
        pal = load_palette(version)
        self.lst_sup.delete(0, "end")
        names = {c['map_color']: c['name'] for c in pal.colors}
        for i in sorted({c['map_color'] for c in pal.colors}):
            self.lst_sup.insert("end", f"{i}: {names[i]}")
        self.ver_note.config(text=pal.meta_note["note"][:240])

    # =================== Map-Art actions ===================
    def pick_image(self):
        path = filedialog.askopenfilename(title="Import image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")])
        if not path:
            return
        try:
            im = Image.open(path).convert("RGB")
            self.image_path = path
            self.lbl_img.config(text=path)
            self.root.after(60, lambda: self._add_preview(self.cv_in, im))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Import failed", f"{type(e).__name__}: {e}")

    def _collect_settings(self) -> appcore.Settings:
        suppress = {int(self.lst_sup.get(i).split(":", 1)[0])
                    for i in self.lst_sup.curselection()}
        formats = tuple(f for f, var in (("schem", self.var_fmt_schem),
                                         ("litematic", self.var_fmt_litematic),
                                         ("structure", self.var_fmt_structure))
                        if var.get())
        return appcore.Settings(
            version=self.cmb_ver.get(),
            maps_w=max(1, int(float(self.spn_w.get() or 1))),
            maps_h=max(1, int(float(self.spn_h.get() or 1))),
            algorithm=self.cmb_alg.get(),
            strength=float(self.scl_strength.get()),
            suppress=suppress,
            fit=self.cmb_fit.get(),
            metric=self.cmb_metric.get(),
            palette_kind=self.cmb_kind.get(),
            construction=self.cmb_cons.get(),
            transparency_mode=self.cmb_tr.get(),
            neighbor_optimize=self.var_neighbors.get(),
            formats=formats or ("schem",))

    def auto_layout(self):
        if not self.image_path:
            messagebox.showinfo("Import first", "Import an image before auto-layout.")
            return
        out = appcore.suggest_layout(self.image_path)
        self.spn_w.delete(0, "end"); self.spn_w.insert(0, str(out["maps_w"]))
        self.spn_h.delete(0, "end"); self.spn_h.insert(0, str(out["maps_h"]))
        self._set_status(f"auto layout: {out['maps_w']}x{out['maps_h']} maps for "
                         f"{out['size_px'][0]}x{out['size_px'][1]} px image")

    def process(self):
        if not self.image_path:
            messagebox.showinfo("Import first", "Import an image before processing.")
            return
        try:
            settings = self._collect_settings()
            self._refresh_sup_list(settings.version)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Invalid settings", f"{type(e).__name__}: {e}")
            return
        self.cancel_evt.clear()
        self.btn_process.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.prg.config(value=0.0); self.lbl_prog.config(text="processing…")

        def work():
            try:
                res = appcore.process(self.image_path, settings,
                                      cancel=self.cancel_evt,
                                      progress=lambda f, m: self.msg.put(("prog", f, m)))
                self.pending = res
                self.msg.put(("done", None, None))
            except appcore.Canceled:
                self.msg.put(("canceled", None, None))
            except Exception as e:  # noqa: BLE001
                self.msg.put(("error", str(e), type(e).__name__))

        threading.Thread(target=work, daemon=True).start()

    def export(self):
        if not getattr(self, "pending", None):
            return
        outdir = filedialog.askdirectory(initialdir=str(DEFAULT_OUT),
                                         title="Choose output directory") or str(DEFAULT_OUT)

        def work():
            try:
                out = appcore.export(self.pending, outdir, "mapart")
                s = self.pending.settings
                expect = {"size": (s.maps_w * 128,
                                   int(self.pending.heights.max()) + 1
                                   + (1 if s.combos else 0),
                                   s.maps_h * 128)}
                validation = {}
                if "schem" in s.formats:
                    validation["schem"] = schem.validate_schem(Path(out["schem"]), expect=expect)
                if "litematic" in s.formats:
                    validation["litematic"] = schem.validate_litematic(Path(out["litematic"]), expect=expect)
                if "structure" in s.formats:
                    validation["structure"] = schem.validate_structure(Path(out["structure"]), expect=expect)
                sizes = {k: list(v["size"]) for k, v in validation.items()}
                self.msg.put(("exported", json.dumps(sizes), str(out.get("report", ""))))
            except Exception as e:  # noqa: BLE001
                self.msg.put(("error", str(e), "export"))

        threading.Thread(target=work, daemon=True).start()

    def cancel(self):
        self.cancel_evt.set()
        self.lbl_prog.config(text="cancelling…")

    def _show_result(self, res):
        im = Image.fromarray(res.preview, "RGB")
        self.root.after(60, lambda: self._add_preview(self.cv_out, im))
        self.root.after(60, lambda: self._add_preview(
            self.cv_proc, Image.fromarray(res.processed, "RGB")))
        self.root.after(60, lambda: self._add_preview(
            self.cv_layout, Image.fromarray(
                _mapart.tile_overlay(res.preview, res.settings.maps_w,
                                     res.settings.maps_h), "RGB")))
        s = res.settings
        lines = [
            f"Palette: {s.version}  ({s.palette_kind}, metric {s.metric}, "
            f"construction {s.construction})",
            f"Maps: {s.maps_w} x {s.maps_h}  ({s.maps_w * 128} x {s.maps_h * 128} blocks)",
            f"Algorithm: {s.algorithm}  strength {s.strength:.2f}",
            f"Suppressed map colors: {sorted(s.suppress) or 'none'}",
            f"Processing: {res.elapsed:.2f} s.  Layers: 4 (one per shade depth)",
            "",
            "Block counts:",
        ]
        for blk, c in res.counts.items():
            lines.append(f"  {blk:<34} {c['count']:>8,}  ({c['item']})")
        self.txt_counts.delete("1.0", "end")
        self.txt_counts.insert("1.0", "\n".join(lines))
        self.btn_export.config(state="normal")
        self._set_status("preview ready — export writes .schem + preview + report")

    # =================== Test suite actions ===================
    def show_suite_item(self):
        tier = self.cmb_tier.get()
        name = self.lst_items.get(self.lst_items.curselection()[0]).split(":", 1)[0]
        purpose, arr = testsuite.suite(tier, [name])[name]
        self.root.after(40, lambda: self._add_preview(self.cv_suite, Image.fromarray(arr, "RGB")))
        self.lbl_suite.config(text=f"{tier}/{name}: {purpose} ({arr.shape[1]}x{arr.shape[0]}, deterministic)")

    def save_suite(self):
        tier = self.cmb_tier.get()
        out = filedialog.askdirectory(initialdir=str(DATA_DIR), title="Render suite to…") or str(DATA_DIR / f"suite-{tier}")
        def work():
            try:
                saved = testsuite.save_all(tier, out)
                self.msg.put(("suite_saved", str(out), f"{len(saved)} images"))
            except Exception as e:  # noqa: BLE001
                self.msg.put(("error", str(e), "suite"))
        threading.Thread(target=work, daemon=True).start()

    # =================== Benchmark actions ===================
    def run_benchmark(self):
        mode = self.cmb_bench_mode.get()
        tier = "small" if mode == "quick" else "medium"
        out = DATA_DIR / f"benchmark-{mode}"
        self.btn_bench.config(state="disabled")
        self.btn_bench_stop.config(state="normal")
        self.txt_bench.delete("1.0", "end")

        def work():
            try:
                camp = benchmark.Campaign(out, mode=mode, tier=tier,
                                          catalog_path=DATA_DIR / "assetcatalog.db")
                total = camp._total()
                rep = camp.run(progress=lambda f, m: self.msg.put(("bprog", f, m)))
                self.msg.put(("bdone", rep, str(out)))
            except Exception as e:  # noqa: BLE001
                self.msg.put(("error", str(e), "benchmark"))

        threading.Thread(target=work, daemon=True).start()

    def stop_benchmark(self):
        self.msg.put(("bench_stop", None, None))

    # =================== Lab actions ===================
    def lab_dir(self):
        return DATA_DIR / "lab"

    def lab_start(self):
        ids = [i.strip() for i in self.ent_lab_ids.get().split(",") if i.strip()]
        try:
            self.lab = labcore.LabController(self.lab_dir(),
                                             evals_limit=int(float(self.spn_lab_evals.get())),
                                             time_limit_s=float(self.spn_lab_time.get()),
                                             strategy=self.cmb_lab_strategy.get(),
                                             suite_ids=ids,
                                             library=self.library)
            self.lab.start()
        except RuntimeError as e:
            messagebox.showwarning("Lab", str(e)); return
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Lab", f"{type(e).__name__}: {e}"); return
        self.txt_lab.delete("1.0", "end")
        self.tree_hist.delete(*self.tree_hist.get_children())
        self.lbl_lab.config(text="running…")

    def lab_pause(self):
        if self.lab:
            self.lab.pause()

    def lab_resume(self):
        try:
            if self.lab:
                self.lab.resume()
        except RuntimeError as e:
            messagebox.showinfo("Lab", str(e))

    def lab_stop(self):
        if self.lab:
            self.lab.stop()

    def lab_export_best(self):
        best = (self.lab.state.get("best") if self.lab else None)
        if not best:
            messagebox.showinfo("Lab", "No best candidate yet."); return
        cand = best["candidate"]
        try:
            s = appcore.Settings(version=cand["version"], maps_w=1, maps_h=1,
                                 algorithm=cand["algorithm"], strength=cand["strength"],
                                 suppress=cand.get("suppress", ()), bias=cand.get("bias", 0),
                                 label=f"lab-best")
            img = DATA_DIR / "lab_eval_fixture.png"
            purpose, arr = testsuite.suite("small", ["faux-photo"])["faux-photo"]
            testsuite.rasterize(arr).save(img, format="PNG")
            out = appcore.export(appcore.process(img, s), DEFAULT_OUT, "lab_best")
            self._set_status(f"best exported: {out['schem']} (score {best['score']:.4f})")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Export best", f"{type(e).__name__}: {e}")

    def _open_out(self):
        import subprocess
        try:
            subprocess.Popen(["explorer", str(DEFAULT_OUT)])
        except Exception:  # noqa: BLE001
            self._set_status(str(DEFAULT_OUT))

    open_out = _open_out

    def _refresh_history(self):
        d = self.lab_dir()
        if d.exists():
            for p in d.glob("state.json"):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    print(json.dumps({k: data.get(k) for k in ("evaluated", "invalid", "finished")}))
                except Exception:  # noqa: BLE001
                    pass
        self.lst_hist.delete(0, "end")
        for x in sorted(DATA_DIR.glob("*")):
            if x.is_dir():
                self.lst_hist.insert("end", x.name)

    # =================== message pump ===================
    def _poll(self):
        try:
            while True:
                item = self.msg.get_nowait()
                kind, a, b = item[0], item[1], item[2]
                if kind == "prog":
                    self.prg.config(value=a); self.lbl_prog.config(text=b)
                elif kind == "done":
                    self.result = getattr(self, "pending", None)
                    self._show_result(self.result)
                    self.btn_stop.config(state="disabled"); self.btn_process.config(state="normal")
                elif kind == "canceled":
                    self.lbl_prog.config(text="canceled")
                    self.btn_stop.config(state="disabled"); self.btn_process.config(state="normal")
                elif kind == "exported":
                    self.lbl_prog.config(text="export+validation OK")
                    self._set_status(f"exported: {b} — validated size {a}")
                elif kind == "error":
                    self.lbl_prog.config(text="error")
                    messagebox.showerror("Failed", a)
                    self.btn_stop.config(state="disabled"); self.btn_process.config(state="normal")
                    self.btn_bench.config(state="normal"); self.btn_bench_stop.config(state="disabled")
                elif kind == "suite_saved":
                    self._set_status(f"suite rendered to {a} — {b}")
                elif kind == "bprog":
                    self.prg_bench.config(value=a); self.lbl_bench.config(text=b)
                elif kind == "bdone":
                    self.prg_bench.config(value=1.0)
                    self.lbl_bench.config(text=f"campaign finished — reports in {b}")
                    self.txt_bench.insert("1.0", json.dumps(a["scores_best"], indent=2, sort_keys=True))
                    self.btn_bench.config(state="normal"); self.btn_bench_stop.config(state="disabled")
                    self._set_status(f"benchmark done: {a['evaluated']} combos")
                elif kind == "bench_stop":
                    self.btn_bench_stop.config(state="disabled")
                    self.lbl_bench.config(text="stop requested (applies to next run)")
        except queue.Empty:
            pass
        # rate-limit heavy dashboard refreshes (~3 Hz instead of every pump)
        self._cc_poll()
        self._res_poll()
        self._lab_poll()
        self._breeder_poll()
        self.root.after(250, self._poll)

    # =================== Breeding actions (feature §12) ===================
    def _breed_dir(self):
        return DATA_DIR / "breeding"

    def breed_preset_changed(self, _evt=None):
        name = self.cmb_breed_preset.get()
        preset = breeding.PRESETS.get(name, {})
        if "method" in preset:
            self.cmb_breed_method.set(preset["method"])
        if "mutation_prob" in preset:
            self.spn_breed_mprob.set(preset["mutation_prob"])
        if "mutation_strength" in preset:
            self.spn_breed_mstr.set(preset["mutation_strength"])
        slider = breeding.PRESET_WEIGHTS_GUI.get(name)
        if slider:
            self.ent_breed_weights.delete(0, "end")
            self.ent_breed_weights.insert(0, slider)

    def breed_pick_file(self):
        p = filedialog.askopenfilename(title="Parent candidates JSON",
                                       filetypes=[("JSON", "*.json")])
        if p:
            self.cmb_breed_source.set("JSON file…")
            self._breed_file = p

    def _breed_parents(self):
        src = self.cmb_breed_source.get()
        if src.startswith("lab"):
            rows = []
            if self.lab is not None:
                st = self.lab.state
                if st.get("best"):
                    rows.append(st["best"])
                seen = {st["best"]["candidate"].get("algorithm")}
            else:
                seen = set()
            hpath = self.lab_dir() / "history.jsonl"
            if hpath.exists():
                hist = []
                for line in hpath.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        try:
                            r = json.loads(line)
                            if r.get("score") is not None and r.get("candidate"):
                                hist.append(r)
                        except json.JSONDecodeError:
                            pass
                hist.sort(key=lambda r: r["score"], reverse=True)
                fams = dict(seen)
                for r in hist:
                    f = r["candidate"].get("algorithm")
                    if f not in fams:
                        rows.append(r)
                        fams[f] = True
                    if len(rows) >= 3:
                        break
            if not rows:
                messagebox.showinfo(
                    "Breeding", "No lab history yet — run the Evolution Lab "
                    "first (or load parent candidates from a JSON file).")
                return None
            return [r["candidate"] for r in rows]
        p = getattr(self, "_breed_file", None)
        if not p:
            messagebox.showinfo("Breeding", "Pick a parent candidates JSON file first.")
            return None
        blob = json.loads(Path(p).read_text(encoding="utf-8"))
        parents = blob if isinstance(blob, list) else blob.get("parents", [])
        if not parents:
            messagebox.showwarning("Breeding", f"{p} contains no parents.")
            return None
        return parents

    def breed_start(self):
        if self.breeder is not None and self.breeder.is_running():
            messagebox.showinfo("Breeding", "A breeding run is already active.")
            return
        parents = self._breed_parents()
        if not parents:
            return
        weights = [float(x) for x in self.ent_breed_weights.get().split(",") if x.strip()]
        if len(weights) < 2:
            messagebox.showwarning("Breeding", "Give at least two contribution percentages.")
            return
        try:
            self.breeder = breeding.Breeder(
                self._breed_dir(), parents, weights=weights, normalize=True,
                method=self.cmb_breed_method.get(),
                mutation_prob=float(self.spn_breed_mprob.get()),
                mutation_strength=float(self.spn_breed_mstr.get()),
                offspring=int(float(self.spn_breed_off.get())),
                generations=int(float(self.spn_breed_gens.get())),
                population_size=8, elite_size=int(float(self.spn_breed_elite.get())),
                holdout_every=int(float(self.spn_breed_hold.get())), seed=0)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Breeding", f"{type(e).__name__}: {e}")
            return
        try:
            self.breeder.start()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Breeding", f"{type(e).__name__}: {e}")
            return
        self.lbl_breed.config(text="breeding…")
        self.txt_lab.insert("end", "\n— breeding started —\n")

    def breed_pause(self):
        if self.breeder:
            self.breeder.pause()

    def breed_resume(self):
        if self.breeder:
            try:
                self.breeder.resume()
            except Exception as e:  # noqa: BLE001
                messagebox.showinfo("Breeding", str(e))

    def breed_stop(self):
        if self.breeder:
            self.breeder.stop()
            self.lbl_breed.config(text="stopped")

    def breed_show_lineage(self):
        tree = breeding.lineage_tree(self._breed_dir() / "lineage.jsonl")
        self.txt_lab.insert("1.0", tree + "\n\n")

    def _breeder_poll(self):
        b = self.breeder
        if b is None:
            return
        try:
            st = b.status()
        except Exception:  # noqa: BLE001
            return
        eff = st.get("effective_parent_pcts") or []
        if eff:
            txt = "effective parent percentages: " + ", ".join(
                f"{p['pct']:.0f}%" for p in eff)
            if self.lbl_breed_pct["text"] != txt:
                self.lbl_breed_pct.config(text=txt)
        champ = st.get("champion")
        line = (f"gen {st['generation']} — evals {st['evals']}, "
                f"pop {st['population']} "
                f"({st['distinct_algorithms']} algorithms)"
                + ("  [PAUSED]" if st["paused"] else ""))
        if champ:
            line += (f" — champion {champ['score']:.4f} "
                     f"({champ['candidate'].get('algorithm')}, "
                     f"gen {champ['generation']})")
        if st["finished"]:
            line += " — finished"
        if self.lbl_breed["text"] != line:
            self.lbl_breed.config(text=line)
        if st["finished"] and not b.is_running():
            self.txt_lab.insert("end", breeding.lineage_tree(
                self._breed_dir() / "lineage.jsonl") + "\n")

    def _lab_poll(self):
        lab = self.lab
        if lab is None:
            return
        st = lab.status()
        if st["running"] or st["paused"]:
            rows = lab.history()
            self.tree_hist.delete(*self.tree_hist.get_children())
            for r in rows[-25:]:
                c = r.get("candidate") or {}
                self.tree_hist.insert("", "end", values=(
                    None if r.get("score") is None else f"{r['score']:.4f}",
                    c.get("algorithm"), c.get("strength"), r.get("detail", "")))
            self.prg_lab.config(value=min(1.0, (st["evaluated"] / max(1, st["evals_limit"]))))
            self.lbl_lab.config(text=f"evaluated {st['evaluated']}/{st['evals_limit']} "
                                     f"(invalid {st['invalid']}, cached {st['cached']})"
                                     + ("  [PAUSED]" if st["paused"] else ""))
        if st["finished"] and not st["running"] and not st["paused"]:
            best = lab.state.get("best")
            if best:
                cand, m = best["candidate"], best.get("metrics") or {}
                lines = [f"BEST  score {best['score']:.4f}",
                         f"config: {cand}",
                         f"oklab_error_norm {m.get('oklab_error_norm')} edge {m.get('edge_preservation')}",
                         f"coverage {m.get('palette_coverage')} cost {m.get('material_cost')}",
                         f"time {m.get('process_time_s')}s   valid: {m.get('schem_valid')}",
                         "", f"work dir: {self.lab_dir()}"]
            else:
                lines = ["no best candidate; work dir: %s" % self.lab_dir()]
            self.txt_lab.delete("1.0", "end")
            self.txt_lab.insert("1.0", "\n".join(lines))
            self.lbl_lab.config(text="finished — best preserved above")


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:  # noqa: BLE001
        pass
    GuiApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
