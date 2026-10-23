# Repository audit & commit workflow

One tool covers the whole loop: audit the tree, review the diff, run the
relevant checks, generate a specific Conventional-Commits message, and prepare
a clean commit or pull-request description — with safety gates and dry-run by
default.

```bash
python tools/repo_audit.py status                  # git state + pending journal + reports
python tools/repo_audit.py audit                   # full inventory, persisted JSON+MD report
python tools/repo_audit.py audit --checks quick    # ... and run the quick check preset
python tools/repo_audit.py review                  # staged vs unstaged vs untracked vs deleted
python tools/repo_audit.py prepare --files a.py b.py            # dry-run (default)
python tools/repo_audit.py prepare --files a.py --apply --yes   # commit exactly that list
python tools/repo_audit.py prepare --files a.py --checks quick --require-checks --apply --yes
python tools/repo_audit.py prepare --files a.py --branch fix/palette-matching --apply --yes
python tools/repo_audit.py pr --files a.py --title "fix(mapart): ..." --out PR.md
python tools/repo_audit.py abort                    # unstage a journaled plan, keep the tree
```

## What the audit reports

* git state: branch, HEAD, commit count, staged/unstaged/untracked/deleted/renamed/conflicts
* secrets: API-key patterns, private-key blocks, generic `key = "..."` assignments,
  `.env` files (templates allowed) — values are never printed
* large files (> 5 MB) and duplicate-content groups (SHA-256, ≤ 5 MB files)
* TODO/FIXME and placeholder markers
* runtime/generated directories present (`mapart-data/`, `acceptance_run/`, …)
* check results (PASS/FAIL, duration, tail) with a cache keyed by the working-tree
  digest so unchanged inputs are not re-tested (`--no-cache` to force)

Reports are written to `reports/repo_audit/` (the directory carries its own
`.gitignore`, so generated reports never enter a commit).

## Commit policy

* Conventional Commits: `feat|fix|test|docs|refactor|perf|build|ci|chore(scope): summary`,
  ≤ 72 characters, generated from the actual planned file list — never
  "Add files via upload". Override with `--type` / `--summary` when the
  heuristic cannot know intent.
* Group by purpose and use separate commits for application code, tests,
  documentation and build changes; `--files` is the plan, and only that plan is
  staged.
* Prefer a descriptive branch (`--branch feat/...`); never merge, force-push,
  delete branches, or push to a remote from this tool.

## Safety gates

`prepare --apply` refuses when:

* the enclosing git repository is **not** the project root (this workspace is
  inside a home-directory repository; staging there would add unrelated user
  files) — run `git init` in the project first;
* secret-like material or `.env` files are in the plan;
* merge conflicts exist;
* oversized binaries are planned (override with `--allow-large`);
* `--require-checks` was passed and a check failed;
* `--yes` is missing (dry-run is the default).

It never runs `git add -A`, never resets/stashes/discards changes, preserves
all unplanned modifications, and writes a journal before staging so an
interrupted run can be retried (`prepare --apply`) or rolled back (`abort`).

## Verification

```bash
python tests/test_repo_audit.py     # destructive workflows run in throw-away repos
python tests/test_tools.py          # existing tool battery
```

## Limitations

* Commit type/scope is heuristic; it describes the file plan, not intent.
* Secret scanning is pattern-based (no entropy modelling), so it can miss
  obfuscated secrets and can flag fixtures — review findings before overriding.
* The tool does not create GitHub PRs (no network); `pr` prints a ready
  description for the user to paste.
* Duplicate detection is content-exact and capped at 5 MB per file.
