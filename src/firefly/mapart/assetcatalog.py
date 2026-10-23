"""Searchable local metadata catalog for the image library (stdlib SQLite).

The manifest stays the source of truth for *files*; this catalog is a queryable
index over them plus the long-lived records the milestone needs:

- deterministic measured characteristics per image (tag, size, aspect,
  detail complexity, transparency, palette exposure, dominant colors);
- explicit set membership and an append-only set-event journal, so
  development / validation / held-out assignment changes are never silent;
- benchmark history keyed by code revision, palette hash, algorithm
  definitions, target Minecraft version and processing-settings digest.

No new dependency: `sqlite3` ships with Python. The database is opened per
call, so background lab/benchmark threads cannot share a stale connection.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .imagelibrary import license_status

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS images (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    file_name TEXT,
    source TEXT,
    source_url TEXT,
    license TEXT,
    author TEXT,
    title TEXT,
    bytes INTEGER,
    width INTEGER,
    height INTEGER,
    aspect REAL,
    color_diversity REAL,
    brightness REAL,
    contrast REAL,
    edge_density REAL,
    flat_share REAL,
    detail_complexity REAL,
    transparency_share REAL,
    has_alpha INTEGER,
    dominant_colors TEXT,
    capability_tags TEXT,
    analysis_digest TEXT,
    analysis_version INTEGER,
    imported REAL,
    indexed REAL
);
CREATE TABLE IF NOT EXISTS image_sets (
    image_id TEXT NOT NULL,
    set_name TEXT NOT NULL,
    assigned_at REAL,
    reason TEXT,
    PRIMARY KEY (image_id, set_name)
);
CREATE TABLE IF NOT EXISTS set_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    image_id TEXT NOT NULL,
    from_sets TEXT,
    to_sets TEXT,
    reason TEXT,
    confirmed INTEGER,
    origin TEXT
);
CREATE TABLE IF NOT EXISTS benchmark_runs (
    run_id TEXT PRIMARY KEY,
    created REAL NOT NULL,
    code_revision TEXT,
    code_dirty INTEGER,
    palette_key TEXT,
    palette_hash TEXT,
    algorithm_version TEXT,
    target_mc_version TEXT,
    settings_digest TEXT,
    settings TEXT,
    mode TEXT,
    suite TEXT,
    results_path TEXT,
    n_evals INTEGER,
    summary TEXT,
    source TEXT
);
CREATE INDEX IF NOT EXISTS idx_images_tags ON images(capability_tags);
CREATE INDEX IF NOT EXISTS idx_sets_name ON image_sets(set_name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_dedupe
    ON set_events(image_id, ts, origin);
CREATE INDEX IF NOT EXISTS idx_runs_palette ON benchmark_runs(palette_hash);
CREATE INDEX IF NOT EXISTS idx_runs_revision ON benchmark_runs(code_revision);
"""

_COLUMNS = ("id", "sha256", "file_name", "source", "source_url", "license",
            "author", "title", "bytes", "width", "height", "aspect",
            "color_diversity", "brightness", "contrast", "edge_density",
            "flat_share", "detail_complexity", "transparency_share",
            "has_alpha", "dominant_colors", "capability_tags",
            "analysis_digest", "analysis_version", "imported", "indexed")


def _like(text: str) -> str:
    """Escape a user term for a literal LIKE comparison."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class AssetCatalog:
    def __init__(self, path):
        self.path = Path(path)
        if self.path.parent and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                      (str(SCHEMA_VERSION),))

    # ---------------- plumbing ----------------
    @contextmanager
    def _conn(self):
        """One short-lived connection per call: committed and CLOSED on exit,
        so background threads never share state and Windows releases the file."""
        conn = sqlite3.connect(str(self.path), timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def meta(self) -> dict:
        with self._conn() as c:
            return {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM meta")}

    def count(self) -> dict:
        with self._conn() as c:
            return {
                "images": c.execute("SELECT COUNT(*) FROM images").fetchone()[0],
                "sets": c.execute("SELECT COUNT(*) FROM image_sets").fetchone()[0],
                "set_events": c.execute("SELECT COUNT(*) FROM set_events").fetchone()[0],
                "benchmark_runs": c.execute("SELECT COUNT(*) FROM benchmark_runs").fetchone()[0],
            }

    # ---------------- indexing ----------------
    def index_library(self, lib) -> dict:
        """Mirror the library manifest into the catalog (idempotent).

        Files that vanished are reported by `stale_entries`, not deleted here:
        the manifest remains authoritative for membership.
        """
        now = time.time()
        rows = []
        for e in lib.manifest["images"]:
            a = e.get("analysis") or {}
            try:
                size = Path(lib.file_path(e) or "").stat().st_size
            except (OSError, TypeError):
                size = 0
            rows.append((
                e["id"], e.get("sha256", ""), e.get("file_name", ""),
                e.get("source", ""), e.get("source_url", ""),
                e.get("license", ""), e.get("author", ""), e.get("title", ""),
                size, int(a.get("width") or 0), int(a.get("height") or 0),
                float(a.get("aspect") or 0.0),
                float(a.get("color_diversity") or 0.0),
                float(a.get("brightness") or 0.0),
                float(a.get("contrast") or 0.0),
                float(a.get("edge_density") or 0.0),
                float(a.get("flat_share") or 0.0),
                float(a.get("detail_complexity") or 0.0),
                float(a.get("transparency_share") or 0.0),
                1 if a.get("has_alpha") else 0,
                json.dumps(a.get("dominant_colors_rgb") or [], sort_keys=True),
                json.dumps(sorted(a.get("capability_tags") or [])),
                _digest(a), int(a.get("analysis_version") or 0),
                float(e.get("download_date") or 0.0), now))
        with self._conn() as c:
            c.executemany(
                f"INSERT OR REPLACE INTO images ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _COLUMNS)})", rows)
            c.execute("DELETE FROM image_sets")
            set_rows = []
            for e in lib.manifest["images"]:
                for purpose in e.get("datasets", []):
                    set_rows.append((e["id"], purpose, now, "manifest"))
            c.executemany("INSERT OR REPLACE INTO image_sets"
                          "(image_id, set_name, assigned_at, reason) VALUES (?,?,?,?)",
                          set_rows)
        return {"indexed": len(rows), "sets": len(set_rows), "at": now}

    def stale_entries(self, lib) -> dict:
        """Manifest entries whose file is missing, changed, or unindexed."""
        missing, changed, unindexed, analysis_stale = [], [], [], []
        with self._conn() as c:
            known = {r["id"]: dict(r) for r in c.execute("SELECT * FROM images")}
        for e in lib.manifest["images"]:
            row = known.get(e["id"])
            if row is None:
                unindexed.append(e["id"])
                continue
            if row["sha256"] != e.get("sha256"):
                changed.append({"id": e["id"], "kind": "content hash"})
            a = e.get("analysis") or {}
            if int(a.get("analysis_version") or 0) != int(row["analysis_version"] or 0):
                analysis_stale.append(e["id"])
            p = lib.file_path(e)
            if not p or not Path(p).is_file():
                missing.append(e["id"])
        return {"missing_files": missing, "hash_changes": changed,
                "unindexed": unindexed, "analysis_stale": analysis_stale,
                "known_in_catalog": len(known),
                "known_in_manifest": len(lib.manifest["images"])}

    # ---------------- search ----------------
    def search(self, text: str = "", tag: str = "", set_name: str = "",
               min_width: int | None = None, max_width: int | None = None,
               min_detail: float | None = None, max_detail: float | None = None,
               license_filter: str = "any", source: str = "",
               order: str = "id", limit: int = 100) -> list:
        """Query measured characteristics. All filters combine with AND."""
        where, args = [], []
        if text:
            args += [_like(text)] * 4
            where.append("(title LIKE ? ESCAPE '\\' OR author LIKE ? ESCAPE '\\' "
                         "OR capability_tags LIKE ? ESCAPE '\\' "
                         "OR source LIKE ? ESCAPE '\\')")
        if tag:
            where.append("capability_tags LIKE ? ESCAPE '\\'")
            args.append("%\"" + tag + "\"%")
        if set_name:
            where.append("id IN (SELECT image_id FROM image_sets WHERE set_name = ?)")
            args.append(set_name)
        if min_width is not None:
            where.append("width >= ?")
            args.append(int(min_width))
        if max_width is not None:
            where.append("width <= ?")
            args.append(int(max_width))
        if min_detail is not None:
            where.append("detail_complexity >= ?")
            args.append(float(min_detail))
        if max_detail is not None:
            where.append("detail_complexity <= ?")
            args.append(float(max_detail))
        if source:
            where.append("source = ?")
            args.append(source)
        if license_filter != "any":
            if license_filter == "missing":
                where.append("(license IS NULL OR TRIM(license) = '')")
            elif license_filter == "unknown":
                where.append("LOWER(TRIM(license)) IN ('unknown', 'n/a', 'none', '?')")
            elif license_filter == "recorded":
                where.append("TRIM(license) <> '' AND LOWER(TRIM(license)) "
                             "NOT IN ('unknown', 'n/a', 'none', '?')")
        order_sql = {"id": "id ASC", "detail": "detail_complexity DESC, id ASC",
                     "width": "width DESC, id ASC",
                     "size": "bytes DESC, id ASC"}.get(order, "id ASC")
        sql = ("SELECT * FROM images"
               + (" WHERE " + " AND ".join(where) if where else "")
               + f" ORDER BY {order_sql} LIMIT ?")
        args.append(max(1, int(limit)))
        with self._conn() as c:
            out = []
            for r in c.execute(sql, args):
                d = dict(r)
                d["capability_tags"] = json.loads(d["capability_tags"] or "[]")
                d["dominant_colors"] = json.loads(d["dominant_colors"] or "[]")
                d["license_status"] = license_status(d.get("license"))
                out.append(d)
            return out

    # ---------------- set membership journal ----------------
    def record_set_event(self, image_id: str, from_sets, to_sets, reason: str,
                         confirmed: bool, origin: str = "library",
                         ts: float | None = None) -> int:
        """Append a membership event; identical (image, time, origin) rows
        are ignored, so re-importing a library journal is idempotent."""
        stamp = float(ts if ts is not None else time.time())
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO set_events(ts, image_id, from_sets, to_sets, "
                "reason, confirmed, origin) VALUES (?,?,?,?,?,?,?)",
                (stamp, image_id, json.dumps(sorted(from_sets or [])),
                 json.dumps(sorted(to_sets or [])), reason or "",
                 1 if confirmed else 0, origin))
            return int(cur.lastrowid or 0)

    def set_events(self, image_id: str = "", limit: int = 200) -> list:
        sql = "SELECT * FROM set_events"
        args: list = []
        if image_id:
            sql += " WHERE image_id = ?"
            args.append(image_id)
        sql += " ORDER BY event_id DESC LIMIT ?"
        args.append(max(1, int(limit)))
        with self._conn() as c:
            out = []
            for r in c.execute(sql, args):
                d = dict(r)
                d["from_sets"] = json.loads(d["from_sets"] or "[]")
                d["to_sets"] = json.loads(d["to_sets"] or "[]")
                out.append(d)
            return out

    def set_summary(self) -> dict:
        with self._conn() as c:
            rows = c.execute("SELECT set_name, COUNT(*) n FROM image_sets "
                             "GROUP BY set_name ORDER BY set_name").fetchall()
            return {r["set_name"]: r["n"] for r in rows}

    # ---------------- benchmark history ----------------
    def record_benchmark(self, context: dict, *, mode: str = "", suite=(),
                         results_path: str = "", n_evals: int = 0,
                         summary: dict | None = None, source: str = "campaign") -> str:
        """Append one benchmark run. Idempotent for the same run identity, so
        regenerating reports never duplicates history."""
        import hashlib
        suite_list = sorted(str(x) for x in (suite or ()))
        ident = json.dumps({"revision": context.get("code_revision"),
                            "palette_hash": context.get("palette_hash"),
                            "algorithm_version": context.get("algorithm_version"),
                            "settings_digest": context.get("settings_digest"),
                            "target_mc_version": context.get("target_mc_version"),
                            "suite": suite_list, "mode": mode,
                            "results_path": str(results_path)},
                           sort_keys=True)
        run_id = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]
        with self._conn() as c:
            existing = c.execute("SELECT run_id FROM benchmark_runs WHERE run_id=?",
                                 (run_id,)).fetchone()
            if existing:
                # same run identity (e.g. a resumed campaign's regenerated
                # report): refresh the progress counters, keep one history row
                c.execute("UPDATE benchmark_runs SET n_evals=?, summary=?, "
                          "results_path=? WHERE run_id=?",
                          (int(n_evals),
                           json.dumps(summary or {}, sort_keys=True, default=str),
                           str(results_path), run_id))
                return run_id
            dirty = context.get("code_dirty")
            c.execute(
                "INSERT INTO benchmark_runs(run_id, created, code_revision, code_dirty, "
                "palette_key, palette_hash, algorithm_version, target_mc_version, "
                "settings_digest, settings, mode, suite, results_path, n_evals, "
                "summary, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, time.time(), context.get("code_revision"),
                 None if dirty is None else int(bool(dirty)),
                 context.get("palette_key"), context.get("palette_hash"),
                 context.get("algorithm_version"), context.get("target_mc_version"),
                 context.get("settings_digest"),
                 json.dumps(context.get("settings") or {}, sort_keys=True, default=str),
                 mode, json.dumps(suite_list), str(results_path), int(n_evals),
                 json.dumps(summary or {}, sort_keys=True, default=str), source))
        return run_id

    def benchmarks(self, palette_hash: str = "", code_revision: str = "",
                   limit: int = 100) -> list:
        where, args = [], []
        if palette_hash:
            where.append("palette_hash = ?")
            args.append(palette_hash)
        if code_revision:
            where.append("code_revision = ?")
            args.append(code_revision)
        sql = "SELECT * FROM benchmark_runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created DESC LIMIT ?"
        args.append(max(1, int(limit)))
        with self._conn() as c:
            out = []
            for r in c.execute(sql, args):
                d = dict(r)
                d["suite"] = json.loads(d["suite"] or "[]")
                d["settings"] = json.loads(d["settings"] or "{}")
                d["summary"] = json.loads(d["summary"] or "{}")
                out.append(d)
            return out

    def benchmark_coverage(self) -> dict:
        """What history exists, grouped so gaps are visible."""
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM benchmark_runs").fetchone()[0]
            by_palette = [dict(r) for r in c.execute(
                "SELECT palette_key, palette_hash, COUNT(*) runs, "
                "MAX(created) last_created FROM benchmark_runs "
                "GROUP BY palette_key, palette_hash ORDER BY runs DESC")]
            by_algorithm = [dict(r) for r in c.execute(
                "SELECT algorithm_version, COUNT(*) runs FROM benchmark_runs "
                "GROUP BY algorithm_version ORDER BY runs DESC")]
            by_revision = [dict(r) for r in c.execute(
                "SELECT code_revision, COUNT(*) runs FROM benchmark_runs "
                "GROUP BY code_revision ORDER BY runs DESC")]
            last = c.execute("SELECT * FROM benchmark_runs ORDER BY created DESC "
                             "LIMIT 1").fetchone()
        latest = dict(last) if last else None
        if latest:
            latest["suite"] = json.loads(latest["suite"] or "[]")
            latest["settings"] = json.loads(latest["settings"] or "{}")
            latest["summary"] = json.loads(latest["summary"] or "{}")
        return {"runs": total, "by_palette": by_palette,
                "by_algorithm": by_algorithm, "by_revision": by_revision,
                "latest": latest, "set_counts": self.set_summary()}

    def close(self):
        pass  # connections are per-call; kept for API symmetry


def _digest(analysis: dict) -> str:
    from .imagelibrary import characteristics_digest
    return characteristics_digest(analysis) if analysis else ""
