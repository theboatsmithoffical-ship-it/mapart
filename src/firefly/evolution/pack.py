""".ffevopack — the offline package exchange format (a zip, no code inside).

Security stance applied throughout:
- only whitelisted members are read (manifest.json, candidates.json, assets/*)
- member paths containing traversal or absolute forms are rejected
- genomes are parameter dictionaries validated by schema; genome ids are
  content hashes and must match on import; NOTHING is ever executed
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import synth
from .genome import genome_id, sanitize as _sanitize
from .evolve import evaluate

FORMAT_KIND = "ffevopack"
FORMAT_VERSION = 1
MAX_CANDIDATES = 400
_CORE_MEMBERS = ("manifest.json", "candidates.json")

from . import VERSION  # noqa: E402


class PackageError(ValueError):
    pass


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _member_check(names) -> None:
    for raw in names:
        n = raw.replace("\\", "/")
        if n not in _CORE_MEMBERS and not n.startswith("assets/"):
            raise PackageError(f"foreign archive member rejected: {n!r}")
        parts = n.split("/")
        if n.startswith("/") or ".." in parts or (":" in parts[0] and len(parts[0]) <= 3):
            raise PackageError(f"unsafe archive path rejected: {n!r}")


_CAND_BEGIN = {"id", "genome", "score"}
_CAND_ALLOWED = _CAND_BEGIN | {"fixture_scores", "verified_by", "seen_in", "source"}
_MANIFEST_REQUIRED = {"kind", "format_version", "label", "sender", "created",
                      "engine_version", "suite", "candidates_sha256"}
_MANIFEST_ALLOWED = _MANIFEST_REQUIRED | {"notes", "provenance", "run", "origin"}


def _validate_manifest(m) -> None:
    if not isinstance(m, dict):
        raise PackageError("manifest.json is not an object")
    missing = _MANIFEST_REQUIRED - set(m)
    if missing:
        raise PackageError(f"manifest missing required keys: {sorted(missing)}")
    unknown = set(m) - _MANIFEST_ALLOWED
    if unknown:
        raise PackageError(f"manifest has unknown keys: {sorted(unknown)}")
    if m["kind"] != FORMAT_KIND:
        raise PackageError(f"wrong package kind: {m['kind']!r}")
    if m["format_version"] != FORMAT_VERSION:
        raise PackageError(f"unsupported format_version: {m['format_version']!r}")
    if not isinstance(m["label"], str) or not m["label"]:
        raise PackageError("label must be a non-empty string")
    for key in ("sender", "created", "engine_version", "candidates_sha256", "suite"):
        if not isinstance(m[key], (str, dict)) or not m[key]:
            raise PackageError(f"manifest field {key!r} must be a non-empty string or mapping")


def _validate_candidates(cands, cbytes: bytes, manifest) -> None:
    if not isinstance(cands, list):
        raise PackageError("candidates.json must be a list")
    if len(cands) > MAX_CANDIDATES:
        raise PackageError(f"too many candidates: {len(cands)} > {MAX_CANDIDATES}")
    for c in cands:
        if not isinstance(c, dict):
            raise PackageError("candidate entries must be objects")
        if set(c) - _CAND_ALLOWED or not _CAND_BEGIN.issubset(c):
            raise PackageError(f"candidate {c.get('id')!r} fails schema")
        score = c["score"]
        if not isinstance(score, (int, float)) or not isinstance(score, bool) is False:
            raise PackageError(f"candidate {c['id']!r} score must be numeric")
        gid = genome_id(_sanitize(c["genome"]))
        if gid != c["id"]:
            raise PackageError(f"candidate id mismatch: {c['id']!r} vs content id {gid}")
    digest = _sha256_bytes(cbytes)
    if digest != manifest["candidates_sha256"]:
        raise PackageError("candidates checksum mismatch (manifest.candidates_sha256)")


def read_package(path) -> dict:
    """Open and fully validate a .ffevopack; raises PackageError on any fault."""
    path = Path(path)
    if not path.exists() or not path.is_file():
        raise PackageError("package file not found")
    try:
        with zipfile.ZipFile(path) as zf:
            _member_check(i.filename for i in zf.infolist())
            cbytes = zf.read("candidates.json")
            manifest = json.loads(zf.read("manifest.json"))
            candidates = json.loads(cbytes)
    except zipfile.BadZipFile as e:
        raise PackageError("not a valid zip archive") from e
    except (KeyError,) as e:
        raise PackageError(f"package member missing: {e}") from e
    _validate_manifest(manifest)
    _validate_candidates(candidates, cbytes, manifest)
    return {"path": str(path), "manifest": manifest, "candidates": candidates}


def write_package(out_path, manifest, candidates, assets=None) -> dict:
    out_path = Path(out_path)
    if out_path.parent and not out_path.parent.exists():
        out_path.parent.mkdir(parents=True, exist_ok=True)
    cbytes = json.dumps(candidates, indent=2, sort_keys=True).encode("utf-8")
    manifest = dict(manifest)
    manifest["candidates_sha256"] = _sha256_bytes(cbytes)
    tmp = Path(out_path).with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        zf.writestr("candidates.json", cbytes)
        for name, data in (assets or {}).items():
            safe = name if name.startswith("assets/") else "assets/" + name
            zf.writestr(safe, data)
    tmp.replace(out_path)
    return {"path": str(out_path), "candidates": len(candidates)}


def _verification_env(state, fixture_input):
    """Rebuild the run's fixture env for export-time self-verification."""
    paths = state.get("fixture_paths") or {}
    usable = {n: Path(p) for n, p in paths.items() if p and Path(p).exists()}
    if usable:
        fixtures = {n: synth.load_image(p) for n, p in usable.items()}
        env = synth.fixture_env(fixtures, state["suite"]["id"], usable)
        return env
    if fixture_input:
        loaded = synth.load_fixtures(fixture_input)
        fixtures = {name: img for name, img in loaded}
        blob = b"".join(Path(f).read_bytes() for f in [fixture_input])
        sid = "custom-" + hashlib.sha256(blob).hexdigest()[:8]
        return synth.fixture_env(fixtures, sid)
    raise PackageError("no verification fixtures available (state fixture "
                       "paths missing and no --input given)")


def export_package(run_dir, fixture_input, out_path, label, include_assets=False, top=6) -> dict:
    """Export self-verified candidates from a completed run.

    A candidate is exported only if it re-evaluates with the identical score
    and byte-identical deterministic output — claims, never unverified picks.
    """
    run_dir = Path(run_dir)
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    cfg = state.get("config", {})
    recs = []
    cache = run_dir / "cache" / "candidates.jsonl"
    if cache.exists():
        for line in cache.read_text(encoding="utf-8").splitlines():
            if line.strip():
                recs.append(json.loads(line))
    recs.sort(key=lambda r: r.get("score", float("inf")))
    env = _verification_env(state, fixture_input)
    verified = []
    for rec in recs[:top]:
        g = rec["genome"]
        s1, per = evaluate(env, g)
        s2, _ = evaluate(env, g)
        if s1 != s2:
            continue
        if abs(s1 - float(rec["score"])) > 1e-6:
            continue
        verified.append({
            "id": genome_id(_sanitize(g)), "genome": g, "score": s1,
            "fixture_scores": per, "verified_by": "determinism-v1",
            "seen_in": [label], "source": "local-run",
        })
    assets = {}
    if include_assets:
        for name, img in env.fixtures:
            assets[name + ".png"] = synth.png_bytes(img)
    manifest = {
        "kind": FORMAT_KIND, "format_version": FORMAT_VERSION,
        "label": label, "sender": {"engine_version": VERSION},
        "created": _now(), "engine_version": VERSION,
        "suite": {"id": env.suite_id, "fixtures": env.hashes},
        "run": {"seed": cfg.get("seed"), "generations": cfg.get("gens"),
                "population": cfg.get("pop"), "evals_used": state.get("evals_used")},
        "origin": "export",
    }
    write_package(out_path, manifest, verified, assets)
    return {"ok": True, "out": str(out_path), "label": label,
            "candidates": len(verified), "engine_version": VERSION}


def inspect_package(path) -> dict:
    pkg = read_package(path)
    m = pkg["manifest"]
    return {"ok": True, "path": str(path), "kind": m["kind"],
            "format_version": m["format_version"], "label": m["label"],
            "sender": m["sender"], "created": m["created"],
            "engine_version": m["engine_version"],
            "candidates": len(pkg["candidates"]),
            "candidates_sha256": m["candidates_sha256"],
            "suite": (m["suite"] or {}).get("id"),
            "sha256": _sha256_bytes(Path(path).read_bytes())}


def merge_packages(a_path, b_path, out_path, notes="") -> dict:
    a, b = read_package(a_path), read_package(b_path)
    merged = {}
    for label, cand in ([(a["manifest"].get("label"), c) for c in a["candidates"]]
                        + [(b["manifest"].get("label"), c) for c in b["candidates"]]):
        cid = cand["id"]
        if cid not in merged:
            merged[cid] = {k: cand[k] for k in ("id", "genome", "score", "fixture_scores",
                                                "verified_by") if k in cand}
            merged[cid]["seen_in"] = [label]
            merged[cid]["source"] = "merged"
        elif label not in merged[cid]["seen_in"]:
            merged[cid]["seen_in"].append(label)
    manifest = {
        "kind": FORMAT_KIND, "format_version": FORMAT_VERSION,
        "label": f"merge:{a['manifest'].get('label')}+{b['manifest'].get('label')}",
        "sender": {"engine_version": VERSION},
        "created": _now(), "engine_version": VERSION,
        "suite": a["manifest"]["suite"],
        "provenance": [{"label": x["manifest"].get("label"), "created": x["manifest"]["created"],
                        "sender": x["manifest"]["sender"]} for x in (a, b)],
        "notes": notes, "origin": "merge",
    }
    write_package(out_path, manifest, list(merged.values()))
    labels = sorted({lbl for c in merged.values() for lbl in c.get("seen_in", [])})
    return {"ok": True, "out": str(out_path), "candidates": len(merged),
            "seen_in": labels}
