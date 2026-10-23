#!/usr/bin/env python3
"""Repository audit / commit preparation workflow tests.

Run:  python tests/test_repo_audit.py   (exit 0 = all pass)

Every destructive workflow test runs against a throw-away git repository
created in a temp dir. The real project repository is never touched.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "repo_audit.py"
FAILURES = []


def t(name, fn):
    try:
        print(f"PASS {name}  [{fn()}]")
    except Exception as e:  # noqa: BLE001 — this runner reports all failures
        FAILURES.append(name)
        print(f"FAIL {name}: {e!r}")


def sh(cwd, *cmd, check=True):
    return subprocess.run(list(cmd), cwd=str(cwd), capture_output=True,
                          text=True, check=check)


def tool(repo, *args, expect=None):
    p = subprocess.run([sys.executable, str(TOOL), *args, "--root", str(repo)],
                       capture_output=True, text=True)
    if expect is not None and p.returncode != expect:
        raise AssertionError(f"exit {p.returncode} != {expect}\n{p.stdout[-800:]}\n{p.stderr[-800:]}")
    return p


def mkrepo() -> Path:
    d = Path(tempfile.mkdtemp(prefix="ra_"))
    sh(d, "git", "init", "-q")
    sh(d, "git", "config", "user.email", "t@example.invalid")
    sh(d, "git", "config", "user.name", "Repo Audit Test")
    (d / "a.py").write_text("print('a')\n", encoding="utf-8")
    sh(d, "git", "add", "a.py")
    sh(d, "git", "commit", "-q", "-m", "init")
    return d


def commits(repo) -> int:
    return int(sh(repo, "git", "rev-list", "--count", "HEAD").stdout.strip())


def audit(repo):
    p = tool(repo, "audit", expect=0)
    return json.loads(p.stdout)


# ---------------- tree states ----------------

def clean_tree():
    r = mkrepo()
    rep = audit(r)
    assert rep["head"]["commits"] == 1
    assert not rep["status"]["staged"] and not rep["status"]["unstaged"]
    assert not rep["status"]["untracked"]
    assert not [f for f in rep["findings"] if f["severity"] == "critical"]
    assert any(rep["report_files"])
    return f"{rep['inventory']['files']} files, clean, report written"


def staged_unstaged_untracked():
    r = mkrepo()
    (r / "a.py").write_text("print('a2')\n", encoding="utf-8")
    (r / "new.py").write_text("x = 1\n", encoding="utf-8")
    sh(r, "git", "add", "new.py")
    out = json.loads(tool(r, "review", expect=0).stdout)
    assert "new.py" in out["status"]["staged"], out["status"]
    assert "a.py" in out["status"]["unstaged"], out["status"]
    (r / "loose.txt").write_text("hi\n", encoding="utf-8")
    out = json.loads(tool(r, "review", expect=0).stdout)
    assert "loose.txt" in out["status"]["untracked"], out["status"]
    return "staged/unstaged/untracked separated"


def deleted_detected():
    r = mkrepo()
    (r / "a.py").unlink()
    out = json.loads(tool(r, "review", expect=0).stdout)
    assert "a.py" in out["status"]["deleted"], out["status"]
    return "deletion surfaced"


# ---------------- commit message generation ----------------

def message_categories():
    r = mkrepo()
    (r / "src" / "firefly" / "mapart").mkdir(parents=True)
    (r / "tests").mkdir(exist_ok=True)
    (r / "docs").mkdir(exist_ok=True)
    (r / "src" / "firefly" / "mapart" / "newmod.py").write_text("X = 1\n", encoding="utf-8")
    (r / "tests" / "test_newmod.py").write_text("def test_x():\n    assert 1\n", encoding="utf-8")
    (r / "docs" / "guide.md").write_text("# guide\n", encoding="utf-8")
    p = tool(r, "prepare", "--files", "src/firefly/mapart/newmod.py",
             "tests/test_newmod.py", expect=0)
    summ = [ln for ln in p.stdout.splitlines() if ln.startswith("commit summary:")][0]
    msg = summ.split(": ", 1)[1]
    assert msg.startswith("feat(mapart):"), msg
    assert len(msg) <= 72, len(msg)
    assert "upload" not in msg.lower()
    p = tool(r, "prepare", "--files", "docs/guide.md", expect=0)
    assert "docs(docs):" in p.stdout, p.stdout[:300]
    p = tool(r, "prepare", "--files", "tests/test_newmod.py", expect=0)
    assert "test(tests):" in p.stdout, p.stdout[:300]
    p = tool(r, "prepare", "--files", "tests/test_newmod.py", "--type", "refactor", expect=0)
    assert "refactor(" in p.stdout
    return "feat/docs/test classification from real paths"


# ---------------- safety gates ----------------

def secret_detection_and_refusal():
    r = mkrepo()
    (r / "settings.py").write_text(
        'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n', encoding="utf-8")
    rep = audit(r)
    crit = [f for f in rep["findings"] if f["id"] == "possible_secret"]
    assert crit and crit[0]["severity"] == "critical", rep["findings"]
    before = commits(r)
    p = tool(r, "prepare", "--files", "settings.py", "--apply", "--yes", expect=3)
    assert "secret" in (p.stdout + p.stderr).lower()
    assert commits(r) == before, "refused commit must not create a commit"
    # env files are refused too
    (r / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
    assert any(h["kind"] == "env_file" for h in audit(r)["gates"]["secrets"])
    return "secret + env file flagged; apply refused"


def large_file_gate():
    r = mkrepo()
    (r / "blob.bin").write_bytes(b"\x01" * (6 * 1024 * 1024))
    rep = audit(r)
    assert rep["inventory"]["large"], rep["inventory"]
    before = commits(r)
    p = tool(r, "prepare", "--files", "blob.bin", "--apply", "--yes", expect=3)
    assert "large" in (p.stdout + p.stderr).lower()
    assert commits(r) == before
    return "6 MB binary blocked without --allow-large"


def duplicate_detection():
    r = mkrepo()
    (r / "d1.txt").write_text("same\n", encoding="utf-8")
    (r / "d2.txt").write_text("same\n", encoding="utf-8")
    rep = audit(r)
    assert rep["inventory"]["duplicates"], rep["inventory"]
    assert {f for g in rep["inventory"]["duplicates"] for f in g["files"]} == {"d1.txt", "d2.txt"}
    return "identical files grouped"


# ---------------- dry-run and preservation ----------------

def dry_run_untouched():
    r = mkrepo()
    (r / "a.py").write_text("print('changed')\n", encoding="utf-8")
    before_index = sh(r, "git", "diff", "--cached", "--name-only").stdout
    before_commits = commits(r)
    p = tool(r, "prepare", "--files", "a.py", expect=0)
    assert "dry-run" in p.stdout
    assert sh(r, "git", "diff", "--cached", "--name-only").stdout == before_index
    assert commits(r) == before_commits
    return "index + HEAD unchanged"


def unrelated_changes_preserved():
    r = mkrepo()
    (r / "b.py").write_text("print('b')\n", encoding="utf-8")
    sh(r, "git", "add", "b.py")
    sh(r, "git", "commit", "-q", "-m", "add b")
    (r / "a.py").write_text("print('a-mod')\n", encoding="utf-8")
    (r / "b.py").write_text("print('b-mod')\n", encoding="utf-8")
    before = commits(r)
    tool(r, "prepare", "--files", "a.py", "--apply", "--yes",
         "--require-checks", expect=0)
    assert commits(r) == before + 1
    st = json.loads(tool(r, "review", expect=0).stdout)["status"]
    assert "b.py" in st["unstaged"], st
    assert "a.py" not in st["unstaged"], st
    return "only the planned file committed; b.py untouched"


def branch_creation():
    r = mkrepo()
    (r / "a.py").write_text("print('br')\n", encoding="utf-8")
    tool(r, "prepare", "--files", "a.py", "--apply", "--yes",
         "--branch", "feat/repo-audit", expect=0)
    br = sh(r, "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert br == "feat/repo-audit", br
    return f"branch {br} created for the commit"


def pr_description():
    r = mkrepo()
    (r / "a.py").write_text("print('pr')\n", encoding="utf-8")
    p = tool(r, "pr", "--files", "a.py", "--title", "feat(mapart): demo PR", expect=0)
    for section in ("# feat(mapart): demo PR", "## What changed", "## Verification"):
        assert section in p.stdout, p.stdout[:400]
    return "PR title + sections generated"


def interrupted_recovery():
    r = mkrepo()
    (r / "a.py").write_text("print('j')\n", encoding="utf-8")
    sh(r, "git", "add", "a.py")
    jdir = r / "reports" / "repo_audit"
    jdir.mkdir(parents=True)
    (jdir / "pending_commit.json").write_text(
        json.dumps({"files": ["a.py"], "summary": "feat(test): pending"}), encoding="utf-8")
    st = json.loads(tool(r, "status", expect=0).stdout)
    assert st["pending_journal"] and st["pending_journal"]["files"] == ["a.py"], st
    tool(r, "abort", expect=0)
    st = json.loads(tool(r, "status", expect=0).stdout)
    assert st["pending_journal"] is None
    assert not sh(r, "git", "diff", "--cached", "--name-only").stdout.strip()
    assert (r / "a.py").read_text() == "print('j')\n", "working tree must be preserved"
    return "journal reported, abort unstaged the plan, content kept"


def repo_root_mismatch_refused():
    outer = Path(tempfile.mkdtemp(prefix="ra_outer_"))
    sh(outer, "git", "init", "-q")
    sh(outer, "git", "config", "user.email", "t@example.invalid")
    sh(outer, "git", "config", "user.name", "Outer")
    (outer / "outer.txt").write_text("o\n", encoding="utf-8")
    sh(outer, "git", "add", "outer.txt")
    sh(outer, "git", "commit", "-q", "-m", "outer init")
    inner = outer / "project"
    inner.mkdir()
    (inner / "app.py").write_text("print('inner')\n", encoding="utf-8")
    rep = json.loads(tool(inner, "audit", expect=0).stdout)
    ids = [f["id"] for f in rep["findings"]]
    assert "repo_root_mismatch" in ids, ids
    p = tool(inner, "prepare", "--files", "app.py", "--apply", "--yes", expect=3)
    assert "repository root" in (p.stdout + p.stderr).lower()
    assert not any((outer / "reports").glob("repo_audit/pending_commit.json"))
    return "nested project under a foreign repo is refused, outer repo untouched"


def no_repo_is_reported():
    d = Path(tempfile.mkdtemp(prefix="ra_norepo_"))
    (d / "x.py").write_text("X = 1\n", encoding="utf-8")
    import os
    # the temp tree sits inside this machine's home-directory repository;
    # a ceiling keeps git from discovering it, so this really is "no repo"
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(d.parent))
    p = subprocess.run([sys.executable, str(TOOL), "prepare", "--root", str(d), "--files", "x.py"],
                       capture_output=True, text=True, env=env,
                       encoding="utf-8", errors="replace")
    assert p.returncode == 2 and "no git repository" in p.stderr, (p.returncode, p.stderr)
    return "clear typed error instead of touching anything"


def main() -> int:
    print("=== repository audit workflow tests ===")
    t("ra_clean_tree", clean_tree)
    t("ra_states", staged_unstaged_untracked)
    t("ra_deleted", deleted_detected)
    t("ra_messages", message_categories)
    t("ra_secrets", secret_detection_and_refusal)
    t("ra_large", large_file_gate)
    t("ra_duplicates", duplicate_detection)
    t("ra_dry_run", dry_run_untouched)
    t("ra_preserve", unrelated_changes_preserved)
    t("ra_branch", branch_creation)
    t("ra_pr", pr_description)
    t("ra_recovery", interrupted_recovery)
    t("ra_root_mismatch", repo_root_mismatch_refused)
    t("ra_no_repo", no_repo_is_reported)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
