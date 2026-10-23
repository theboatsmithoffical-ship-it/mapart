#!/usr/bin/env python3
"""Repository audit, change review and Git commit preparation.

One reusable command for the workflow this project actually uses: inspect the
tree, review staged/unstaged/untracked changes, run the relevant checks,
generate a specific Conventional-Commits summary (never "Add files via
upload"), and prepare a clean commit -- with safety gates and a dry-run.

Subcommands
-----------
  audit     full repository inventory: git state, secrets, large files,
            duplicates, TODO markers, checks, and a persisted report
  review    change review only: staged vs unstaged vs untracked vs deleted,
            generated-file heuristics, secret/large-file gates
  prepare   generate commit summary/description/inventory and show the exact
            file list; dry-run by default. --apply --yes commits that exact
            list (never `git add -A`)
  pr        generate a pull-request title + Markdown description
  status    current git state, pending-commit journal, report locations

Safety guarantees
-----------------
* Refuses `prepare --apply` unless the git repository root IS the project
  root. (This workspace lives under a home-directory repository; staging
  there would touch unrelated user files.)
* Never stages files outside the explicitly planned list.
* Never discards, resets, or stashes user changes.
* Refuses to commit when secret-like material or oversized binaries are
  staged, or when requested checks failed.
* Dry-run is the default; it changes neither the index nor HEAD.
* Writes a journal before staging so an interrupted run can be resumed or
  aborted, and reports it via `status`.

Stdlib only; no network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

def paths(root: Path) -> tuple[Path, Path, Path]:
    """(report dir, pending-commit journal, check cache) anchored at the
    audited root — never at the process CWD."""
    d = root / "reports" / "repo_audit"
    return d, d / "pending_commit.json", d / "check_cache.json"


def _self_ignore(report_dir: Path) -> None:
    """Keep generated audit artefacts out of commits without touching the
    project's own .gitignore."""
    gi = report_dir / ".gitignore"
    try:
        if not gi.is_file():
            gi.write_text("*\n!.gitignore\n", encoding="utf-8")
    except OSError:
        pass

SECRET_PATTERNS = (
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{24,}")),
    ("private_key_block", re.compile(r"-----BEGIN (RSA|OPENSSH|EC|DSA|PGP) PRIVATE KEY-----")),
    ("generic_secret_assign", re.compile(
        r"(?i)(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*['\"][^'\"\s]{10,}['\"]")),
)
ENV_FILE_RE = re.compile(r"(^|/)\.env($|\.)")  # .env and .env.* ; .env.example allowed


def git(root: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    # utf-8 with replacement: filenames are bytes; the console locale must not
    # crash the audit (and must never print a filename as a decode error)
    return subprocess.run(["git", "-C", str(root), "--no-pager", *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=check)


def repo_root(root: Path) -> Path | None:
    p = git(root, "rev-parse", "--show-toplevel")
    if p.returncode != 0:
        return None
    return Path(p.stdout.strip()).resolve()


def head_info(root: Path) -> dict:
    br = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    sha = git(root, "rev-parse", "--short", "HEAD")
    cnt = git(root, "rev-list", "--count", "HEAD")
    return {"branch": br.stdout.strip() if br.returncode == 0 else None,
            "head": sha.stdout.strip() if sha.returncode == 0 else None,
            "commits": int(cnt.stdout.strip()) if cnt.returncode == 0 and cnt.stdout.strip().isdigit() else 0}


def parse_status(root: Path) -> dict:
    """Porcelain v1 -z -> staged/unstaged/untracked/deleted/renamed buckets."""
    p = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    out = {"staged": [], "unstaged": [], "untracked": [], "deleted": [],
           "renamed": [], "conflicted": []}
    if p.returncode != 0:
        return out
    parts = (p.stdout or "").split("\u0000")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if not entry or len(entry) < 3:
            continue
        x, y, path = entry[0], entry[1], entry[3:]
        if x == "R" or y == "R":
            old = parts[i] if i < len(parts) else ""
            i += 1
            out["renamed"].append({"path": path, "from": old})
            continue
        if "U" in (x + y) or (x == "A" and y == "A"):
            out["conflicted"].append(path)
            continue
        if x != " " and x != "?":
            out["staged"].append(path)
        if y != " " and y != "?":
            out["unstaged"].append(path)
        if x == "?" and y == "?":
            out["untracked"].append(path)
        if "D" in (x + y):
            out["deleted"].append(path)
    return out


def is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(4096)
    except OSError:
        return True


def scan_secrets(root: Path, paths: list[str]) -> list[dict]:
    hits = []
    for rel in paths:
        p = root / rel
        if not p.is_file():
            continue
        if ENV_FILE_RE.search(rel.replace("\\", "/")) and not rel.endswith((".example", ".sample", ".template")):
            hits.append({"file": rel, "kind": "env_file", "detail": "environment file must not be committed"})
            continue
        if is_binary(p) or p.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith(("#", "//")):
                continue
            for kind, pat in SECRET_PATTERNS:
                if pat.search(line):
                    hits.append({"file": rel, "line": lineno, "kind": kind,
                                 "detail": "possible secret material (value not echoed)"})
    return hits


# runtime output of the app/tests: never committed, never enumerated (they can
# hold hundreds of thousands of generated files); reported as a finding instead
RUNTIME_DIRS = ("mapart-data", "acceptance_run", "measured_run", "breeding_cli_demo",
                "operator-ws", "optimizer-champion", "screenshots")
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "dist", "build", "portable",
            "node_modules", ".venv", "venv", "reports", ".idea", ".vscode",
            *RUNTIME_DIRS}


def walk_files(root: Path) -> list[str]:
    """Project files on disk, excluding VCS/cache/build artefacts and the
    tool's own report directory (deterministic order)."""
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if set(rel.parts) & SKIP_DIRS:
            continue
        out.append(str(rel).replace("\\", "/"))
    return out


def inventory(root: Path, status: dict) -> dict:
    files = set(walk_files(root))
    files |= set(status["untracked"])
    large, missing = [], []
    for rel in sorted(files):
        p = root / rel
        if not p.is_file():
            missing.append(rel)
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size > 5 * 1024 * 1024:
            large.append({"file": rel, "bytes": size})
    dups = _duplicates(root, files)
    return {"files": len(files), "large": large, "duplicates": dups,
            "missing_on_disk": missing}


def _all_tracked(root: Path) -> list[str]:
    p = git(root, "ls-files", "-z")
    return [x for x in p.stdout.split("\u0000") if x] if p.returncode == 0 else []


def _duplicates(root: Path, files) -> list[dict]:
    by_hash: dict[str, list[tuple[str, int]]] = {}
    for rel in files:
        p = root / rel
        if not p.is_file():
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size == 0 or size > 5 * 1024 * 1024:
            continue
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        by_hash.setdefault(h, []).append((rel, size))
    return [{"sha256": h[:16], "files": [f for f, _ in grp], "bytes": grp[0][1]}
            for h, grp in by_hash.items() if len(grp) > 1]


TODO_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
PLACEHOLDER_RE = re.compile(r"(?i)\b(not implemented|placeholder|stub|coming soon)\b")


def scan_markers(root: Path, files) -> dict:
    todos, placeholders = [], []
    for rel in files:
        p = root / rel
        if not p.is_file() or p.suffix not in (".py", ".md", ".txt", ".json", ".cmd", ".toml"):
            continue
        if is_binary(p):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if TODO_RE.search(line):
                todos.append({"file": rel, "line": lineno, "text": line.strip()[:120]})
            elif PLACEHOLDER_RE.search(line):
                placeholders.append({"file": rel, "line": lineno, "text": line.strip()[:120]})
    return {"todos": todos, "placeholders": placeholders}


CHECK_PRESETS = {
    "quick": [["python", "tests/test_rendermodel.py"],
              ["python", "tests/test_mapart_spec.py"]],
    "full": [["python", "tests/test_rendermodel.py"],
             ["python", "tests/test_mapart_spec.py"],
             ["python", "tests/test_engine.py"],
             ["python", "tests/test_tools.py"],
             ["python", "tests/test_breeding.py"],
             ["python", "tests/test_gui_runtime.py"]],
}


def run_checks(root: Path, preset: str | None, explicit: list[str],
               use_cache: bool = True) -> list[dict]:
    cmds = list(CHECK_PRESETS.get(preset or "", [])) + [[c] if isinstance(c, str) else c
                                                       for c in explicit]
    if not cmds:
        return []
    state = git(root, "status", "--porcelain=v1", "-z").stdout
    digest = hashlib.sha256(state.encode() + repr(cmds).encode()).hexdigest()
    report_dir, _journal, cache_path = paths(root)
    cache = {}
    if use_cache and cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}
    if use_cache and cache.get("digest") == digest and cache.get("results"):
        return [dict(r, cached=True) for r in cache["results"]]
    results = []
    for cmd in cmds:
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=900)
            results.append({"command": " ".join(cmd), "exit": p.returncode,
                            "seconds": round(time.time() - t0, 1),
                            "tail": (p.stdout or p.stderr or "").strip().splitlines()[-1:] or [""],
                            "ok": p.returncode == 0})
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append({"command": " ".join(cmd), "exit": None,
                            "seconds": round(time.time() - t0, 1),
                            "tail": [f"{type(exc).__name__}: {exc}"], "ok": False})
    if use_cache:
        try:
            report_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({"digest": digest, "results": results},
                                             indent=1), encoding="utf-8")
        except OSError:
            pass
    return results


def classify(files: list[str], explicit_type: str | None) -> tuple[str, str]:
    """(conventional type, scope) from the planned file list."""
    def area(rel: str) -> str:
        parts = Path(rel).parts
        if len(parts) >= 3 and parts[0] == "src" and parts[1] == "firefly":
            return parts[2]
        if parts and parts[0] in ("tools", "tests", "docs", "assets"):
            return parts[0]
        return parts[0] if parts else "repo"

    areas = {area(f) for f in files}
    scope = sorted(areas)[0] if len(areas) == 1 else "repo"
    if explicit_type:
        return explicit_type, scope
    non_docs = [f for f in files if not f.startswith("docs/") and not f.endswith(".md")]
    only_tests = non_docs and all(f.startswith("tests/") for f in non_docs)
    only_docs = not non_docs
    only_tools = non_docs and all(f.startswith("tools/") for f in non_docs)
    if only_docs:
        return "docs", scope
    if only_tests:
        return "test", scope
    if only_tools:
        return "chore", scope
    return "feat", scope if scope != "repo" else "mapart"


def summarize(files: list[str], ctype: str, scope: str) -> str:
    """Specific, <=72-char summary built from the actual file list."""
    names = sorted({Path(f).name for f in files})
    shown = ", ".join(names[:3])
    extra = f" (+{len(names) - 3} files)" if len(names) > 3 else ""
    body = f"update {shown}{extra}" if ctype in ("feat", "fix", "refactor", "perf", "chore") \
        else f"{'add' if ctype == 'test' else 'update'} {shown}{extra}"
    msg = f"{ctype}({scope}): {body}"
    if len(msg) > 72:
        msg = msg[:69].rstrip(" ,;") + "..."
    return msg


def describe(files: list[str], status: dict, checks: list[dict], risks: list[str]) -> str:
    lines = ["## What changed", ""]
    if status["staged"]:
        lines.append(f"* staged: {len(status['staged'])} file(s)")
    for f in files[:40]:
        lines.append(f"* `{f}`")
    if len(files) > 40:
        lines.append(f"* ... and {len(files) - 40} more")
    lines += ["", "## Why", "", "Describe the motivation for this change.", "",
              "## Verification", ""]
    if checks:
        for c in checks:
            mark = "PASS" if c["ok"] else "FAIL"
            lines.append(f"* {mark} `{c['command']}` ({c['seconds']}s)"
                         + (" [cached]" if c.get("cached") else ""))
    else:
        lines.append("* No checks were run for this preview (pass `--checks quick|full`).")
    lines += ["", "## Risks / limitations", ""]
    lines += [f"* {r}" for r in risks] or ["* none identified by the automated gates"]
    return "\n".join(lines)


def plan_files(root: Path, args) -> list[str]:
    """Explicit file plan: --files, else all changed paths (staged+unstaged+untracked)."""
    if args.files:
        return [f.replace("\\", "/") for f in args.files]
    st = parse_status(root)
    changed = {*st["staged"], *st["unstaged"], *st["untracked"],
               *(r["path"] for r in st["renamed"])}
    return sorted(changed)


def gate_report(root: Path, files: list[str], status: dict) -> dict:
    secrets = scan_secrets(root, files)
    big = [f for f in files if (root / f).is_file()
           and (root / f).stat().st_size > 5 * 1024 * 1024]
    generated = [f for f in files
                 if any(part in f.replace("\\", "/").split("/")
                        for part in ("__pycache__", ".pytest_cache", "dist", "build",
                                     "node_modules", ".venv", "venv"))
                 or f.endswith((".pyc", ".log", ".tmp"))]
    return {"secrets": secrets, "large_files": big, "generated_files": generated,
            "staged": status["staged"], "unstaged": status["unstaged"],
            "untracked": status["untracked"], "deleted": status["deleted"],
            "conflicted": status["conflicted"]}


def do_audit(root: Path, args) -> dict:
    rr = repo_root(root)
    head = head_info(root) if rr else {}
    st = parse_status(root) if rr else {"staged": [], "unstaged": [], "untracked": [],
                                        "deleted": [], "renamed": [], "conflicted": []}
    inv = inventory(root, st) if rr else {"files": 0, "large": [], "duplicates": []}
    markers = scan_markers(root, walk_files(root))
    checks = run_checks(root, args.checks, args.check or [], use_cache=not args.no_cache)
    # gates only when the repository IS this project: scanning an enclosing
    # (home-directory) repository's status would be huge and irrelevant
    gates = (gate_report(root, plan_files(root, args), st)
             if rr is not None and rr == root.resolve() else {})
    findings = []
    if rr is None:
        findings.append({"severity": "info", "id": "no_git_repo",
                         "detail": "no git repository found at or above this directory"})
    elif rr != root.resolve():
        findings.append({"severity": "critical", "id": "repo_root_mismatch",
                         "detail": f"enclosing repository is {rr}, not the project root; "
                                   "commits here would target unrelated files — apply is refused"})
    if st["conflicted"]:
        findings.append({"severity": "high", "id": "merge_conflicts",
                         "files": st["conflicted"]})
    if gates.get("secrets"):
        findings.append({"severity": "critical", "id": "possible_secret",
                         "files": gates["secrets"]})
    if inv.get("large"):
        findings.append({"severity": "medium", "id": "large_files", "files": inv["large"]})
    if inv.get("duplicates"):
        findings.append({"severity": "low", "id": "duplicate_files",
                         "files": inv["duplicates"]})
    if markers["placeholders"]:
        findings.append({"severity": "low", "id": "placeholders",
                         "files": markers["placeholders"][:10]})
    present_runtime = [d for d in RUNTIME_DIRS if (root / d).is_dir()]
    if present_runtime:
        findings.append({"severity": "info", "id": "runtime_data_present",
                         "detail": "runtime/generated directories exist and are "
                                   "excluded from the commit plan: "
                                   + ", ".join(present_runtime)})
    report = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "root": str(root.resolve()), "repo_root": str(rr) if rr else None,
              "head": head, "status": st, "inventory": inv,
              "markers": {"todo_count": len(markers["todos"]),
                          "placeholders": markers["placeholders"][:20]},
              "gates": gates, "checks": checks, "findings": findings}
    report_dir, _journal, _cache = paths(root)
    report_dir.mkdir(parents=True, exist_ok=True)
    _self_ignore(report_dir)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    (report_dir / f"audit_{stamp}.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    (report_dir / f"audit_{stamp}.md").write_text(audit_markdown(report), encoding="utf-8")
    report["report_files"] = [str(report_dir / f"audit_{stamp}.json"),
                              str(report_dir / f"audit_{stamp}.md")]
    return report


def audit_markdown(r: dict) -> str:
    lines = [f"# Repository audit — {r['timestamp']}", "",
             f"* root: `{r['root']}`",
             f"* repo root: `{r['repo_root']}`",
             f"* branch: `{r['head'].get('branch')}` @ `{r['head'].get('head')}` "
             f"({r['head'].get('commits')} commits)", ""]
    st = r["status"]
    lines += ["## Working tree", "",
              f"* staged: {len(st['staged'])}; unstaged: {len(st['unstaged'])}; "
              f"untracked: {len(st['untracked'])}; deleted: {len(st['deleted'])}; "
              f"renamed: {len(st['renamed'])}; conflicts: {len(st['conflicted'])}", ""]
    lines += ["## Findings", ""]
    if not r["findings"]:
        lines.append("* no findings")
    for f in r["findings"]:
        lines.append(f"* **{f['severity']}** `{f['id']}` — {f.get('detail', '')}")
    inv = r["inventory"]
    lines += ["", f"## Files: {inv.get('files')} tracked/untracked; "
                  f"large: {len(inv.get('large', []))}; "
                  f"duplicate groups: {len(inv.get('duplicates', []))}", ""]
    for d in inv.get("duplicates", [])[:10]:
        lines.append(f"* duplicate ({d['bytes']} B, sha {d['sha256']}): "
                     + ", ".join(f"`{x}`" for x in d["files"][:4]))
    lines += ["", "## Checks", ""]
    if not r["checks"]:
        lines.append("* none requested")
    for c in r["checks"]:
        lines.append(f"* {'PASS' if c['ok'] else 'FAIL'} `{c['command']}` "
                     f"({c['seconds']}s){' [cached]' if c.get('cached') else ''}")
    lines += ["", f"## TODO markers: {r['markers']['todo_count']}", ""]
    return "\n".join(lines)


def do_prepare(root: Path, args) -> int:
    rr = repo_root(root)
    if rr is None:
        print("prepare: no git repository found — run `git init` in the project root first",
              file=sys.stderr)
        return 2
    if rr != root.resolve():
        # never touch an enclosing (e.g. home-directory) repository: staging
        # here would add unrelated user files to the index
        print(f"refused: the git repository root is {rr}, not the project root "
              f"{root.resolve()} — create a repository in the project first "
              "(git init) and re-run", file=sys.stderr)
        return 3
    st = parse_status(root)
    files = plan_files(root, args)
    if not files:
        print("prepare: nothing to commit (working tree clean for the planned paths)",
              file=sys.stderr)
        return 0
    gates = gate_report(root, files, st)
    checks = run_checks(root, args.checks, args.check or [], use_cache=not args.no_cache)
    ctype, scope = classify(files, args.type)
    summary = args.summary or summarize(files, ctype, scope)
    risks = []
    if gates["secrets"]:
        risks.append(f"secret-like material detected in {len(gates['secrets'])} file(s)")
    if gates["large_files"]:
        risks.append(f"large files planned: {gates['large_files']}")
    if gates["generated_files"]:
        risks.append(f"generated/cache paths planned: {gates['generated_files']}")
    if gates["conflicted"]:
        risks.append(f"merge conflicts present: {gates['conflicted']}")
    if rr != root.resolve():
        risks.append(f"repository root is {rr}, not the project — apply is refused")
    desc = describe(files, st, checks, risks)
    plan = {"root": str(root.resolve()), "repo_root": str(rr),
            "branch": head_info(root).get("branch"), "files": files,
            "summary": summary, "description": desc,
            "checks": checks, "gates": gates, "risks": risks,
            "dry_run": not args.apply}
    report_dir, journal, _cache = paths(root)
    report_dir.mkdir(parents=True, exist_ok=True)
    _self_ignore(report_dir)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    (report_dir / f"prepare_{stamp}.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
    print(f"commit summary: {summary}")
    print(f"planned files ({len(files)}):")
    for f in files:
        print(f"  - {f}")
    print(f"risks: {risks or 'none'}")
    for c in checks:
        print(f"check {'PASS' if c['ok'] else 'FAIL'}: {c['command']}")
    if not args.apply:
        print("dry-run: index and HEAD untouched (pass --apply --yes to commit)")
        return 0
    blockers = []
    if rr != root.resolve():
        blockers.append("repository root is not the project root")
    if gates["secrets"]:
        blockers.append("possible secrets detected")
    if gates["conflicted"]:
        blockers.append("merge conflicts present")
    if args.require_checks and any(not c["ok"] for c in checks):
        blockers.append("a requested check failed")
    if gates["large_files"] and not args.allow_large:
        blockers.append("large files planned (pass --allow-large to override)")
    if args.branch and git(root, "rev-parse", "--verify", args.branch).returncode == 0:
        cur = head_info(root).get("branch")
        if cur != args.branch:
            blockers.append(f"branch {args.branch} already exists and is not checked out")
    if blockers:
        print("refused: " + "; ".join(blockers), file=sys.stderr)
        return 3
    if args.branch:
        cur = head_info(root).get("branch")
        if cur != args.branch:
            cp = git(root, "checkout", "-b", args.branch)
            if cp.returncode != 0:
                print(f"branch creation failed: {cp.stderr.strip()}", file=sys.stderr)
                return 3
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({"files": files, "summary": summary,
                                   "created": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                  indent=1), encoding="utf-8")
    try:
        add = git(root, "add", "--", *files)
        if add.returncode != 0:
            raise RuntimeError(add.stderr.strip())
        body = f"{summary}\n\n{desc}\n"
        commit = subprocess.run(["git", "-C", str(root), "commit", "-F", "-"],
                                input=body, capture_output=True, text=True)
        if commit.returncode != 0:
            raise RuntimeError((commit.stdout + commit.stderr).strip())
    except Exception as exc:  # noqa: BLE001 — report and keep the journal for recovery
        print(f"commit failed: {exc}", file=sys.stderr)
        print(f"journal kept at {journal} — retry with --apply or clear with `abort`",
              file=sys.stderr)
        return 4
    journal.unlink(missing_ok=True)
    print(f"committed: {git(root, 'rev-parse', '--short', 'HEAD').stdout.strip()}")
    return 0


def do_review(root: Path, args) -> int:
    rr = repo_root(root)
    if rr is None:
        print(json.dumps({"ok": False, "error": "no git repository"}, indent=1))
        return 2
    if rr != root.resolve():
        print(json.dumps({"ok": False, "repo_root_mismatch": str(rr),
                          "error": "enclosing repository is not the project; "
                                   "create a project repository first"}, indent=1))
        return 3
    st = parse_status(root)
    files = plan_files(root, args)
    gates = gate_report(root, files, st)
    out = {"repo_root": str(rr), "status": st, "planned": files, "gates": gates}
    print(json.dumps(out, indent=1))
    return 0 if not gates["secrets"] else 3


def do_pr(root: Path, args) -> int:
    st = parse_status(root)
    files = plan_files(root, args)
    ctype, scope = classify(files, args.type)
    title = args.title or summarize(files, ctype, scope)
    desc = describe(files, st, [], [])
    text = f"# {title}\n\n{desc}\n"
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


def do_status(root: Path, args) -> int:
    rr = repo_root(root)
    st = parse_status(root)
    report_dir, journal, _cache = paths(root)
    out = {"root": str(root.resolve()), "repo_root": str(rr) if rr else None,
           "head": head_info(root) if rr else None, "status": st,
           "pending_journal": json.loads(journal.read_text(encoding="utf-8"))
           if journal.is_file() else None,
           "reports": sorted(str(p) for p in report_dir.glob("*"))[-5:]
           if report_dir.is_dir() else []}
    print(json.dumps(out, indent=1))
    return 0


def abort(root: Path, args) -> int:
    _report_dir, journal, _cache = paths(root)
    if not journal.is_file():
        print("no pending journal")
        return 0
    files = json.loads(journal.read_text(encoding="utf-8")).get("files", [])
    git(root, "reset", "--", *files)  # unstage exactly the planned files
    journal.unlink()
    print(f"aborted; unstaged {len(files)} planned file(s), working tree preserved")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["audit", "review", "prepare", "pr", "status", "abort"])
    ap.add_argument("--root", default=".")
    ap.add_argument("--files", nargs="*", help="explicit file plan (never the whole tree)")
    ap.add_argument("--checks", choices=sorted(CHECK_PRESETS), help="check preset")
    ap.add_argument("--check", action="append", help="extra check command (repeatable)")
    ap.add_argument("--type", choices=["feat", "fix", "test", "docs", "refactor",
                                      "perf", "build", "ci", "chore"])
    ap.add_argument("--summary")
    ap.add_argument("--branch")
    ap.add_argument("--apply", action="store_true", help="actually stage+commit the plan")
    ap.add_argument("--yes", action="store_true", help="required together with --apply")
    ap.add_argument("--require-checks", action="store_true")
    ap.add_argument("--allow-large", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--title")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    if args.apply and not args.yes:
        print("refusing --apply without --yes (dry-run is the default)", file=sys.stderr)
        return 2
    handlers = {"audit": lambda: (print(json.dumps(do_audit(root, args), indent=1)) or 0),
                "review": lambda: do_review(root, args),
                "prepare": lambda: do_prepare(root, args),
                "pr": lambda: do_pr(root, args),
                "status": lambda: do_status(root, args),
                "abort": lambda: abort(root, args)}
    return handlers[args.command]()


if __name__ == "__main__":
    sys.exit(main())
