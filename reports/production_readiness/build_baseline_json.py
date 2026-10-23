"""Assemble the machine-readable Milestone 0 baseline record from real run logs."""
import json
import re
import subprocess
from pathlib import Path

out = Path("reports/production_readiness")
logs = out / "logs"


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout.strip()


status = [line for line in git("status", "--porcelain").splitlines() if line.strip()]
battery = [json.loads(line) for line in
           (logs / "battery_results.jsonl").read_text().splitlines() if line.strip()]


def counts(name):
    text = (logs / f"{name}.log").read_text(encoding="utf-8", errors="replace")
    return {
        "passed": len(re.findall(r"^PASS", text, re.M)),
        "failed": len(re.findall(r"^FAIL ", text, re.M)),
        "summary": next((line for line in text.splitlines()
                         if line.startswith("ALL PASSED") or line.startswith("FAILED:")), ""),
    }


batteries = []
for row in battery:
    c = counts(row["battery"])
    batteries.append({**row, "checks_passed": c["passed"],
                      "checks_failed": c["failed"], "summary": c["summary"],
                      "log": f"reports/production_readiness/logs/{row['battery']}.log"})

probe = json.loads((out / "probe_export.json").read_text())
env = json.loads((out / "env.json").read_text())
bench = json.loads((out / "quality_benchmark_quick.json").read_text())

findings = [
    {"id": "F-01", "severity": "P0", "area": "palette accuracy",
     "title": "Palette base colors are unverified approximations",
     "evidence": ("assets/mc_palettes.json _meta.verification_status = 'approximation'; "
                  "survival-wool white 221,221,221 vs published 1.8.9 MapColor 255,255,255; "
                  "magenta 219,111,183 vs 178,76,216; docs/engineering_report_v2.md section 11 "
                  "already flags contradictions"),
     "impact": ("preview, quality metrics and exports can be internally consistent yet wrong "
                "in-game"),
     "status": "open (Milestone 1)"},
    {"id": "F-02", "severity": "P0", "area": "palette data / version compatibility",
     "title": "Palette maps blocks to inconsistent or unavailable map colors",
     "evidence": ("map-full-approx-1.15 maps map_color 18 rgb [140,224,231] and 20 "
                  "rgb [255,181,170] both to minecraft:red_sand; ids 58/59 use "
                  "minecraft:warped_wart_block, a 1.16 block, in a version labeled "
                  "target_mc_version 1.15.2"),
     "impact": ("exported blocks cannot render the intended color; warped_wart_block does not "
                "exist in the declared target"),
     "status": "open (Milestones 1-2)"},
    {"id": "F-03", "severity": "P0", "area": "construction feasibility",
     "title": "Default frame construction exports floating gravity blocks",
     "evidence": ("probe_export.json: map-full-approx-1.15 frame has 1439 sand/red_sand with "
                  "air directly below; solid mode 0; Settings.construction defaults to frame"),
     "impact": "the exported volume collapses when built; structural validators accept it",
     "status": "open (Milestone 2)"},
    {"id": "F-04", "severity": "P0", "area": "fidelity / construction solver",
     "title": "Infeasible staircase pixels are exported without a gate",
     "evidence": ("probe_export.json: map-full frame 3447/16384 infeasible (21.04%), 3444 "
                  "intended-vs-realized shade mismatches; survival 2/16384; "
                  "Settings.strict_geometry is accepted but never read"),
     "impact": ("up to a fifth of the image renders a different shade than requested; delivery "
                "proceeds with only a report counter"),
     "status": "open (Milestones 1-2)"},
    {"id": "F-05", "severity": "P0", "area": "authoritative validation",
     "title": "No real renderer / client validation exists",
     "evidence": ("tools/audit_palette_accuracy.py exits 2 (no simulator configured); "
                  "tools/validate_render_against_mapart.py requires MAPART_SIMULATOR; no client "
                  "integration in repo"),
     "impact": "no in-game color or map-render claim is currently supported",
     "status": "blocked (external), documented"},
    {"id": "F-06", "severity": "P1", "area": "export validation",
     "title": "Validation is structural only, not geometry agreement",
     "evidence": ("schem.validate_schem checks sizes/palette prefix/block count; "
                  "validate_litematic checks size/volume/non-air; full_build compares only schem "
                  "block count, no block-for-block compare, no litematic count compare, no "
                  "block-state properties"),
     "impact": ("a structurally valid file can differ from the planned construction or be "
                "unbuildable"),
     "status": "open (Milestone 2)"},
    {"id": "F-07", "severity": "P1", "area": "version metadata",
     "title": "No version manifest; export DataVersion is hardcoded",
     "evidence": ("schem.DATA_VERSION = 2578 used for every palette; litematic writes "
                  "MinecraftDataVersion 2578 even for survival-wool-1.8; no manifest file ties "
                  "Minecraft version, litematic version, palette revision and model version "
                  "together"),
     "impact": "consumers cannot tell which game/format a file targets; 1.8 exports carry 1.15.2 metadata",
     "status": "open (Milestone 1)"},
    {"id": "F-08", "severity": "P1", "area": "block states",
     "title": "Only block names are serialized",
     "evidence": ("schem.export_litematic/export_schem/export_structure_nbt write Name/id only; "
                  "oak_leaves is in the palette and will take default persistent=false/distance=7 "
                  "state"),
     "impact": ("leaves may decay after placement; future blocks needing properties cannot be "
                "represented faithfully"),
     "status": "open (Milestone 2)"},
    {"id": "F-09", "severity": "P1", "area": "legacy/duplicated paths",
     "title": "Old shade-layer and idealized-preview paths remain",
     "evidence": ("mapart.layer_plan and mapart.preview_rgb are still computed into "
                  "res.frames/ProcessResult; gui.py:1141 tells the user 'Layers: 4 (one per shade "
                  "depth)'; export is now a solved staircase"),
     "impact": "conflicting mental model and future drift; result.frames in report json is misleading",
     "status": "open (Milestone 1 cleanup)"},
    {"id": "F-10", "severity": "P2", "area": "tests/docs",
     "title": "Some checks do not represent production behaviour; docs are stale",
     "evidence": ("test_mapart_spec asserts structure/counts, not colour or buildability; "
                  "test_tools uses a stub simulator (documented); docs/release_readiness_v1.md "
                  "claims 57 spec checks and 39/39 breeding, current run is 56 checks and 38/39"),
     "impact": "passing batteries overstate production readiness",
     "status": "open (Milestone 3+)"},
]

accept_lines = (logs / "acceptance_offline.log").read_text(
    encoding="utf-8", errors="replace").splitlines()
accept_pass = [line for line in accept_lines if line.startswith("PASS")]

results = {
    "format": "firefly-production-baseline/1",
    "date": "2026-10-23",
    "repository": {
        "revision": git("rev-parse", "HEAD"),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(status),
        "dirty_entries": status,
        "note": ("Working tree carries uncommitted asset-quality milestone changes; source "
                 "changes during Milestone 0: none."),
    },
    "environment": env,
    "commands": [
        {"command": "python tests/<battery>.py (8 batteries, sequential)",
         "result": "see batteries"},
        {"command": "python firefly_engine.py --selftest", "exit": 0,
         "log": "reports/production_readiness/logs/selftest.log"},
        {"command": "python tools/acceptance_offline.py", "exit": 0,
         "steps_passed": len(accept_pass),
         "log": "reports/production_readiness/logs/acceptance_offline.log"},
        {"command": "python tools/release_sweep.py --fast", "exit": 0,
         "combinations": 72, "failed": 0,
         "log": "reports/production_readiness/logs/release_sweep_fast.log"},
        {"command": "python tools/audit_palette_accuracy.py --version survival-wool-1.8",
         "exit": 2, "result": "refused: no render simulator configured",
         "log": "reports/production_readiness/logs/palette_audit.log"},
        {"command": "python tools/quality_benchmark.py --quick --out reports/production_readiness/quality_benchmark_quick.json",
         "exit": 0, "result": "28 runs; see quality_benchmark_quick",
         "log": "reports/production_readiness/logs/quality_benchmark_quick.log"},
        {"command": "python reports/production_readiness/probe_export.py", "exit": 0,
         "result": "see export_probe"},
    ],
    "batteries": batteries,
    "acceptance_offline": {"steps_passed": len(accept_pass), "exit": 0,
                            "network_attempts": 0, "downloader_calls": 0},
    "release_sweep_fast": {"combinations": 72, "passed": 72, "failed": 0,
                            "wall_time_s": 48.2},
    "export_probe": probe,
    "quality_benchmark_quick": {
        "n_runs": bench["n_runs"],
        "palette": bench["palette"],
        "tier": bench["tier"],
        "by_config": bench["aggregate"]["by_config"],
        "by_category": bench["aggregate"]["by_category"],
        "export": bench["export"],
        "note": bench["note"],
        "artifact": "reports/production_readiness/quality_benchmark_quick.json",
    },
    "known_preexisting_failures": [
        {"test": "tests/test_breeding.py t06",
         "evidence": ("children=[0.670618, 0.671212, 0.602336, 0.663181] "
                      "parents=[0.673148, 0.672022]; identical failure reproduced at HEAD in a "
                      "clean git worktree"),
         "introduced_here": False},
    ],
    "blocked": [
        {"check": "palette audit / render comparison",
         "reason": "no MAPART_SIMULATOR executable or script configured"},
        {"check": "in-game / Litematica client validation",
         "reason": "no Minecraft client or simulator available in this environment"},
        {"check": "tools/readiness_audit.py",
         "reason": ("not run in Milestone 0: it rewrites tracked "
                    "docs/evolution_lab_readiness.* and is a long real-experiment audit; its "
                    "engine/lab behaviours are covered by test_engine, test_gui_runtime and "
                    "acceptance_offline")},
    ],
    "findings": findings,
    "files_changed_milestone_0": [
        "reports/production_readiness/ (new evidence only; no source edits)"],
}

(out / "BASELINE_RESULTS.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
print(json.dumps({"batteries": batteries, "acceptance_steps": len(accept_pass),
                  "findings": len(findings)}, indent=1))
