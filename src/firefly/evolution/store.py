"""Exchange store: inbox scan, quarantine, dedupe, local re-benchmarking,
seed candidates. Sender-scores stay claims — locals are always recomputed."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import pack, synth, VERSION
from .evolve import evaluate


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atom(path: Path, obj) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _registry(root: Path):
    reg = root / "store" / "registry.json"
    entries = []
    if reg.exists():
        entries = json.loads(reg.read_text(encoding="utf-8")).get("entries", [])
    return reg, entries


def _file_sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def import_review(root, tolerance: float = 0.05) -> dict:
    """Shared-champion import review joining EVERY candidate claim to its
    local verification result.

    Verdicts per candidate row:
    - claim_confirmed ... local deterministic rerun reproduced the sender's
      score within `tolerance` (both engines evaluated the same local suite)
    - score_differs_from_claim ... candidate runs fine locally but the sender's
      number was not reproduced (different fixtures/claim inflation) — the
      operator trusts the LOCAL number, the claim stays a claim
    - local_run_not_deterministic ... two identical local runs disagreed
    - not_locally_evaluated ... run evaluate-imported first

    Writes a readable report to <root>/import_review.md and returns machine
    JSON with the same rows. In this engine lower score = better.
    """
    root = Path(root)
    reg, entries = _registry(root)
    packages = []
    dirty = False
    for e in entries:
        if e.get("kind") != "package":
            continue
        local = e.get("local_results")
        records = local.get("list", []) if isinstance(local, dict) else (local or [])
        by_id = {r["id"]: r for r in records} if isinstance(records, list) else {}
        # the sender's measurements lived on ITS fixture suite; local numbers
        # come from the local suite — a delta between them is a fixture gap,
        # so verdicts must not accuse the claim when suites differ
        sender_suite = e.get("sender_suite")
        if sender_suite is None:
            try:
                pkg = pack.read_package(e["path"])
                su = pkg["manifest"].get("suite") or {}
                sender_suite = su.get("id") if isinstance(su, dict) else su
                e["sender_suite"] = sender_suite
                dirty = True
            except pack.PackageError:
                sender_suite = "unknown"
        local_suite = (e.get("evaluated_with") or {}).get("suite")
        # collapse exact repeated (id, claim) entries from the sender package
        grouped = {}
        for claim in e.get("claims", []):
            key = (claim.get("id"), claim.get("score"), claim.get("verified_by"))
            grouped.setdefault(key, 0)
            grouped[key] += 1
        rows = []
        for (cid, csc, cby), n in grouped.items():
            r = by_id.get(cid)
            if r is None:
                verdict, loc, det, delta = "not_locally_evaluated", None, None, None
                note = "run evaluate-imported first"
            else:
                loc = r.get("local_score")
                det = r.get("local_verified")
                delta = r.get("claim_delta")
                if not det:
                    verdict, note = "local_run_not_deterministic", "two identical local runs disagreed"
                elif isinstance(delta, (int, float)) and delta <= tolerance:
                    verdict, note = "claim_confirmed", ""
                elif sender_suite != "unknown" and sender_suite != local_suite:
                    verdict = "expected_fixture_gap"
                    note = (f"claim was measured on the sender suite "
                            f"{sender_suite}; local numbers are from {local_suite} "
                            "— the LOCAL score is what this review trusts")
                else:
                    verdict, note = "not_reproduced", "same suite, score not reproduced locally"
            rows.append({"id": cid, "claim": csc,
                         "claim_verified_by_sender": cby,
                         "occurrences_in_package": n,
                         "local_score": loc,
                         "locally_verified": det,
                         "claim_delta": delta,
                         "verdict": verdict, "note": note})
        # champion pointers: lower score = better (evolution engine convention)
        def _best(rows_, key):
            nums = [x for x in rows_ if isinstance(x.get(key), (int, float))]
            return min(nums, key=lambda x: x[key]) if nums else None
        best_claim_row = _best(rows, "claim")
        best_local_row = _best(rows, "local_score")
        for row in rows:
            row["sender_champion"] = row is best_claim_row
            row["best_locally"] = row is best_local_row
        packages.append({
            "label": e.get("label"),
            "sender_suite": sender_suite,
            "sender": e.get("sender"),
            "package_sha16": (e.get("hashes") or {}).get("package", "")[:16],
            "imported_at": e.get("imported_at"),
            "locally_verified_all": e.get("local_verified"),
            "local_suite": (e.get("evaluated_with") or {}).get("suite"),
            "evaluated_at": e.get("evaluated_at"),
            "verdicts": {v: sum(1 for r in rows if r["verdict"] == v)
                         for v in sorted({r["verdict"] for r in rows})},
            "rows": rows,
        })
    # readable report for the receiving operator
    lines = ["# Shared-champion import review", ""]
    lines.append(f"Engine: firefly.evolution {VERSION}; convention: lower score "
                 "= better. Claims are the sender's numbers; local results are "
                 "re-benchmarked here and always take precedence.")
    lines.append("")
    for p in packages:
        lines.append(f"## {p['label']} (package {p['package_sha16']}, "
                     f"sender {p['sender'].get('engine_version', 'unknown')}, "
                     f"sender suite: {p.get('sender_suite')})")
        lines.append("")
        lines.append("| candidate | n | claim (sender) | local score | claim delta | verdict |")
        lines.append("|---|---|---|---|---|---|")
        for r in p["rows"]:
            mark = ("*" if r["sender_champion"] else "") + ("+" if r["best_locally"] else "")
            lines.append("| {m} {id} | {n} | {c} | {l} | {d} | {v} |".format(
                m=mark, id=r["id"][:12], n=r["occurrences_in_package"],
                c=r["claim"] if r["claim"] is not None else "—",
                l=r["local_score"] if r["local_score"] is not None else "—",
                d=r["claim_delta"], v=r["verdict"]))
        lines.append("")
        notes = {r["note"] for r in p["rows"] if r.get("note")}
        for t in notes:
            lines.append(f"- {t}")
        if notes:
            lines.append("")
        lines.append(f"(* = sender's champion; + = best locally. "
                     f"verdicts: {p['verdicts']})")
        lines.append("")
    if dirty:
        _atom(reg, {"entries": entries})
    text = "\n".join(lines)
    report_path = root / "import_review.md"
    report_path.write_text(text, encoding="utf-8")
    totals = {}
    for p in packages:
        for v, n in p["verdicts"].items():
            totals[v] = totals.get(v, 0) + n
    return {"ok": True, "root": str(root), "report": str(report_path),
            "packages": packages, "totals": totals, "readable": text}


def inbox_scan(root) -> dict:
    """Consume root/optimizer-inbox: valid packages -> store, bad -> quarantine,
    duplicates -> skipped. Returns an operation report."""
    root = Path(root)
    inbox_dir = root / "optimizer-inbox"
    store_dir = root / "store"
    quarantine_dir = root / "quarantine"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    store_dir.mkdir(parents=True, exist_ok=True)
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    reg, entries = _registry(root)

    imported, duplicates, quarantined = [], [], []
    known = {e["hashes"]["package"] for e in entries
             if e.get("kind") == "package" and e.get("hashes", {}).get("package")}

    for f in sorted(inbox_dir.glob("*.ffevopack")):
        try:
            pkg = pack.read_package(f)
        except pack.PackageError as e:
            dest = quarantine_dir / f.name
            k = 1
            while dest.exists():
                dest = quarantine_dir / f"{f.stem}-{k}{f.suffix}"
                k += 1
            shutil.move(str(f), str(dest))
            reason = str(e)
            # the log lives in the store dir: the quarantine folder must hold
            # ONLY rejected packages (report tooling counts that folder 1:1)
            log = store_dir / "reject_log.json"
            lines = []
            if log.exists():
                try:
                    lines = json.loads(log.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    lines = []
            lines.append({"file": dest.name, "reason": reason, "ts": _now()})
            _atom(log, lines)
            quarantined.append({"file": dest.name, "reason": reason})
            continue

        phash = _file_sha(f)
        entry = {
            "kind": "package",
            "hashes": {"package": phash},
            "label": pkg["manifest"]["label"],
            "sender": pkg["manifest"]["sender"],
            "created": pkg["manifest"]["created"],
            # store file names use 24-hex of the content hash: keeps paths
            # short enough for Windows MAX_PATH on deep nests; the full hash
            # is recorded in hashes.package and is what dedupe compares.
            "path": str(store_dir / f"{phash[:24]}.ffevopack"),
            "imported_at": _now(),
            "claims": [{"id": c["id"], "score": c["score"],
                        "verified_by": c.get("verified_by")} for c in pkg["candidates"]],
            "local_results": None,
            "local_verified": False,
        }
        if phash in known:
            duplicates.append(f.name)
            continue
        shutil.copy2(f, entry["path"])
        entries.append(entry)
        known.add(phash)
        try:
            f.unlink()
        except OSError:
            pass
        imported.append(f.name)

    _atom(reg, {"entries": entries})
    return {"ok": True, "root": str(root), "imported": imported,
            "duplicates": duplicates, "quarantined": quarantined}


def evaluate_imported(root) -> dict:
    """Re-benchmark every unevaluated package locally. Sender score claims are
    preserved untouched; local results are recorded alongside them."""
    root = Path(root)
    reg, entries = _registry(root)
    env = synth.builtin_env()
    evaluated = rechecked = locally_verified = 0
    for e in entries:
        if e.get("kind") != "package" or e.get("local_results") is not None:
            continue
        try:
            pkg = pack.read_package(e["path"])
        except pack.PackageError as ex:
            e["local_results"] = {"error": str(ex)}
            e["local_verified"] = False
            continue
        results = []
        for c in pkg["candidates"]:
            s1, per = evaluate(env, c["genome"])
            s2, _ = evaluate(env, c["genome"])
            det = s1 == s2
            claimed = c.get("score")
            delta = None
            if isinstance(claimed, (int, float)):
                delta = round(abs(s1 - claimed), 6)
            results.append({"id": c["id"], "genome": c["genome"],
                            "claimed_score": claimed, "local_score": s1,
                            "local_fixture_scores": per, "local_verified": det,
                            "claim_delta": delta})
        e["local_results"] = results
        e["local_verified"] = bool(results) and all(r["local_verified"] for r in results)
        e["evaluated_at"] = _now()
        e["evaluated_with"] = {"engine_version": VERSION, "suite": env.suite_id}
        evaluated += 1
        rechecked += len(results)
        locally_verified += sum(1 for r in results if r["local_verified"])
    _atom(reg, {"entries": entries})
    return {"ok": True, "packages_evaluated": evaluated, "candidates": rechecked,
            "locally_verified": locally_verified, "suite": env.suite_id}


def seed_candidates(root, limit: int = 16) -> list:
    """Locally-confirmed candidates across the store, best local score first."""
    root = Path(root)
    _, entries = _registry(root)
    out = []
    for e in entries:
        if e.get("kind") != "package":
            continue
        local = e.get("local_results")
        records = local.get("list", []) if isinstance(local, dict) else (local or [])
        for r in records:
            if r.get("local_verified"):
                g, s = r["genome"], r["local_score"]
                if isinstance(s, (int, float)):
                    out.append({"genome": dict(g), "score": s})
    out.sort(key=lambda c: c["score"])
    return out[:limit]
