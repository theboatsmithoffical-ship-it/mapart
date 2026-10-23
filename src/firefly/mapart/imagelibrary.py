"""Image Library & Test Datasets (feature: autonomous test-image discovery).

All heavy decisions made by real code; nothing is downloaded implicitly:
- Discovery/download runs ONLY on explicit user command (GUI button or CLI).
- Sources: Wikimedia Commons (no key, documented license field), local
  folders the user selects, and the built-in synthetic generator. Unsplash
  REQUIRES AN API KEY and is documented as such — never a basic requirement.
- Downloaded files land under <data>/imagelibrary/<id>.<ext> with a manifest
  entry (source, license, author, url, hash, analysis, datasets, revision).
- Images land in datasets with purpose: dev | stress | holdout | synthetic.
  The optimizer reads only 'dev' (+synthetic); holdout results surface as
  AGGREGATE-ONLY at checkpoints (enforced by benchmark/lab wiring, tested).
- Revisions: manifest bumps dataset_revision on membership change; older
  revisions preserved as imagelibrary/revisions/rev-N.json for fair compare.
- Offline readiness: every manifest entry must have a local readable file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

USER_AGENT = ("FireflyMapArtEngine/1.0 (local offline map-art tool; contact: local-user)")
WIKIMEDIA_ENDPOINT = "https://commons.wikimedia.org/w/api.php"
DEFAULT_PALETTE_KEY = "survival-wool-1.8"
MAX_DEFAULT_FILE_MB = 25

CAPABILITY_CATEGORIES = (
    "landscape", "sky", "forest", "water", "animal", "plant", "face",
    "architecture", "illustration", "pixel-art", "gradient", "high-contrast",
    "similar-colors", "out-of-palette", "fine-details", "flat-areas",
    "dark-image", "bright-image", "broad-range",
)


class LibraryError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# analysis (numpy + PIL only)
# --------------------------------------------------------------------------

def _load_small(path, max_px=256):
    from PIL import Image
    im = Image.open(path)
    im.load()
    w, h = im.size
    scale = 1.0
    if max(w, h) > max_px:
        scale = max_px / float(max(w, h))
    small = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    return im, np.asarray(small.convert("RGB"), dtype=np.float64)


def analyze_image(path: str, palette_arr: np.ndarray | None = None) -> dict:
    """Measured visual properties + capability tags. Sampling keeps memory low."""
    from PIL import Image
    im, arr = _load_small(path)
    w, h = im.size
    im.close()
    px = arr.reshape(-1, 3)
    # color diversity (sampled unique hues at 16-step quantization)
    q = (px // 16).astype(np.int32)
    uniq = np.unique(q, axis=0)
    color_diversity = round(min(1.0, len(uniq) / 256.0), 4)
    # brightness / contrast
    luma = px.mean(axis=1)
    brightness = round(float(luma.mean()) / 255.0, 4)
    contrast = round(float(luma.std()) / 128.0, 4)
    # edge density (gradient magnitude thr)
    gy, gx = np.gradient(arr.mean(axis=2))
    edges = np.sqrt(gx * gx + gy * gy)
    edge_density = round(float((edges > 12).mean()), 4)
    # flat area share
    flat_share = round(float((edges < 2).mean()), 4)
    # dominant colors (32-bin RGB histogram top clusters)
    q64 = (px // 32).astype(np.int32)
    vals, cnts = np.unique(q64, axis=0, return_counts=True)
    order = np.argsort(cnts)[::-1][:8]
    dominant = [int(c) for c in (cnts[order] / cnts.sum() * 10000)]  # basis points
    # similarity of many colors to each other (how hard matching is)
    if len(uniq) > 1:
        diffs = np.asarray(uniq, dtype=np.float64)[:, None, :]
        dists = np.linalg.norm(diffs - diffs[:, ::-1], axis=-1)
        close_pairs = float((dists < 48).sum()) / (len(uniq) * len(uniq))
        similar_color_score = round(min(1.0, close_pairs), 4)
    else:
        similar_color_score = 0.0
    # palette coverage estimate: fraction of pixels whose nearest survival
    # palette entry is within a small threshold (out-of-palette exposure)
    palette_ok = palette_arr is not None and len(palette_arr) > 0
    if palette_ok:
        d = np.linalg.norm(px[:, None, :] - palette_arr[None, :, :], axis=2)
        nearest = d.min(axis=1)
        oop = float((nearest > 48).mean())
        pal_coverage = round(1.0 - oop, 4)
        out_of_palette_share = round(oop, 4)
    else:
        pal_coverage, out_of_palette_share = None, None
    tags = []
    if brightness < 0.35:
        tags.append("dark-image")
    if brightness > 0.7:
        tags.append("bright-image")
    if contrast > 0.32:
        tags.append("high-contrast")
    elif contrast < 0.12:
        tags.append("flat-areas")
    if gradient_smoothness(arr) > 0.8 and edge_density < 0.06:
        tags.append("gradient")
    if edge_density > 0.18:
        tags.append("fine-details")
    if similar_color_score > 0.5:
        tags.append("similar-colors")
    if out_of_palette_share is not None and out_of_palette_share > 0.25:
        tags.append("out-of-palette")
    if color_diversity > 0.75:
        tags.append("broad-range")
    return {"width": int(w), "height": int(h), "aspect": round(w / max(1, h), 4),
            "color_diversity": color_diversity, "brightness": brightness,
            "contrast": contrast, "edge_density": edge_density,
            "flat_share": flat_share, "similar_color_score": similar_color_score,
            "palette_coverage": pal_coverage,
            "out_of_palette_share": out_of_palette_share,
            "dominant_color_basis_pts": dominant,
            "capability_tags": tags,
            "analyzed": time.time()}


def gradient_smoothness(arr: np.ndarray) -> float:
    g = np.gradient(arr.mean(axis=2))
    mag = np.sqrt(g[0] ** 2 + g[1] ** 2)
    return round(float((mag < 6).mean()), 4)


def palette_rgb(version_key: str = DEFAULT_PALETTE_KEY) -> np.ndarray:
    import sys as _sys
    from pathlib import Path as _P
    sys_path = _P(__file__).resolve().parents[1]
    if str(sys_path) not in _sys.path:
        _sys.path.insert(0, str(sys_path))
    from firefly.mapart.mapart import load_palette  # noqa
    pal = load_palette(version_key)
    return pal.arr


# --------------------------------------------------------------------------
# Wikimedia Commons discovery (no API key)
# --------------------------------------------------------------------------

def discover_wikimedia(query: str, limit: int = 12,
                       min_px: int = 256, max_file_mb: int = MAX_DEFAULT_FILE_MB) -> list:
    """Search Commons for free-licensed images. Returns candidate dicts with
    license metadata from the API (no assumptions about rights)."""
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"filetype:bitmap {query}", "gsrlimit": str(max(1, min(limit, 50))),
        "gsrnamespace": "6",
        "prop": "imageinfo",
        "iiprop": "url|size|extmetadata|mime",
        "iiurlwidth": "1280",
    }
    url = WIKIMEDIA_ENDPOINT + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as ex:  # noqa: BLE001
        raise LibraryError(f"wikimedia discovery failed: {type(ex).__name__}: {ex}")
    out = []
    for page in (data.get("query", {}).get("pages") or {}).values():
        ii = (page.get("imageinfo") or [{}])[0]
        title = page.get("title", "")
        mime = ii.get("mime", "")
        if not mime.startswith("image/") or mime in ("image/gif", "image/x-icon"):
            continue
        em = ii.get("extmetadata") or {}
        def _em(k):  # license metadata straight from the API
            return (em.get(k, {}).get("value") or "").strip()
        lic = _em("LicenseShortName") or _em("License") or "unknown"
        lic_url = _em("LicenseUrl") or ""
        artist = (em.get("Artist", {}).get("value") or "")[:200]
        w, h = ii.get("width", 0), ii.get("height", 0)
        size = ii.get("size", 0)
        if w < min_px or h < min_px:
            continue
        if size > max_file_mb * 1024 * 1024:
            continue
        dl = ii.get("thumburl") or ii.get("url")
        out.append({
            "source": "wikimedia_commons",
            "title": title, "url": dl, "page_url": ii.get("descriptionurl", ""),
            "license": lic, "license_url": lic_url,
            "author": artist, "mime": mime,
            "width": int(w), "height": int(h), "bytes": int(size),
            "requires_api_key": False,
        })
    return out


UNSPLASH_NOTICE = ("Unsplash requires an API key and its terms must be checked "
                   "by the user; basic functionality never depends on it. "
                   "Use Wikimedia Commons or local files instead.")


# --------------------------------------------------------------------------
# downloader
# --------------------------------------------------------------------------

def _safe_name(title: str, limit: int = 28) -> str:
    """Filesystem-safe compact name. Deep workspace roots on Windows make long
    names overflow MAX_PATH(260), so titles are aggressively shortened."""
    import re
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", title)
    return cleaned.strip("._")[:limit] or "img"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class ImageLibrary:
    def __init__(self, data_dir):
        self.dir = Path(data_dir) / "imagelibrary"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.rev_dir = self.dir / "revisions"
        self.rev_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.dir / "manifest.json"
        self.manifest = {"version": 1, "dataset_revision": 0, "images": [],
                         "updated": time.time()}
        self.load_manifest()

    # ---------------- manifest ----------------
    def load_manifest(self):
        if self.manifest_path.exists():
            try:
                blob = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if isinstance(blob, dict):
                    blob.setdefault("images", [])
                    blob.setdefault("dataset_revision", 0)
                    self.manifest = blob
            except json.JSONDecodeError:
                pass  # corrupt manifest -> start fresh, images remain on disk
        self._normalize_paths()

    def _normalize_paths(self):
        """Migrate legacy entries whose `path` was stored relative to a
        working directory: prefer the stable `file_name` inside this library
        dir, and rewrite `path` to a RESOLVED ABSOLUTE path only when that
        file can actually be located now. Entries whose file is genuinely
        absent keep their old value so verify() reports them honestly."""
        changed = False
        for e in self.manifest["images"]:
            if not isinstance(e, dict) or not e.get("id"):
                continue
            resolved = self._resolve_entry_path(e)
            if resolved is not None:
                if not e.get("file_name"):
                    e["file_name"] = Path(resolved).name
                    changed = True
                if str(resolved) != e.get("path", ""):
                    e["path"] = str(resolved)
                    changed = True
        if changed:
            self.save_manifest(bump_revision=False)

    def _resolve_entry_path(self, e: dict):
        """Locate one entry's image file on disk, or None.

        Resolution order (never depends on the caller's cwd):
        1. `file_name` inside this library dir (the canonical location);
        2. a legacy stored `path`, tried as absolute, then re-anchored at the
           install root this data dir belongs to (old manifests stored
           cwd-relative paths like `mapart-data\\imagelibrary\\img_x.jpg`).
        """
        fn = e.get("file_name")
        if fn:
            f = self.dir / str(fn)
            if f.is_file():
                return f
        legacy = e.get("path")
        if not legacy:
            return None
        p = Path(legacy)
        if p.is_absolute() and p.is_file():
            return p
        if self.dir.name in p.parts:  # re-anchor at the library's own root
            idx = len(p.parts) - 1 - p.parts[::-1].index(self.dir.name)
            anchored = Path(*p.parts[idx - 1:] if idx else p.parts)
            cand = self.dir.parent / anchored
            if cand.is_file():
                return cand
        return p if (p.is_file()) else None

    def file_path(self, entry: dict) -> str | None:
        """Canonical on-disk path for a manifest entry, or None.

        Consumers that need an image's real location MUST use this instead of
        trusting the stored `path` string, which may pre-date a move of the
        library (or even of the process cwd)."""
        if not isinstance(entry, dict):
            return None
        p = self._resolve_entry_path(entry)
        return str(p) if p is not None else None

    def save_manifest(self, bump_revision: bool = False):
        if bump_revision:
            self.manifest["dataset_revision"] = self.manifest.get("dataset_revision", 0) + 1
            rev = {"ts": time.time(),
                   "revision": self.manifest["dataset_revision"],
                   "ids": [i["id"] for i in self.manifest["images"]],
                   "membership": {i["id"]: sorted(i.get("datasets", []))
                                  for i in self.manifest["images"]}}
            self.rev_dir.joinpath(f"rev-{self.manifest['dataset_revision']}.json").write_text(
                json.dumps(rev, indent=1), encoding="utf-8")
        self.manifest["updated"] = time.time()
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.manifest, indent=1), encoding="utf-8")
        os.replace(tmp, self.manifest_path)

    def entry(self, image_id: str):
        for i in self.manifest["images"]:
            if i["id"] == image_id:
                return i
        return None

    def ids(self):
        return [i["id"] for i in self.manifest["images"]]

    # ---------------- add / download ----------------
    def add_local(self, path: str, source_label: str = "local-user-folder",
                  datasets: list | None = None) -> dict:
        """Import an explicitly user-selected local file."""
        src = Path(path)
        if not src.is_file():
            raise LibraryError(f"file not found: {src}")
        tmp = self.dir / f"_probe_{src.name}"
        shutil.copy2(src, tmp)
        entry = self._image_entry(tmp, source=source_label,
                                  source_url="", license_="user-owned",
                                  author="", title=src.stem, datasets=datasets)
        tmp.unlink(missing_ok=True)
        self.manifest["images"].append(entry)
        self.save_manifest(bump_revision=True)
        return entry

    def download(self, cand: dict, datasets: list | None = None,
                 progress=None) -> dict:
        """Download one candidate with resume, dup detection, verification."""
        url = cand.get("url")
        if not url:
            raise LibraryError("candidate has no url")
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        fname = f"{digest}_{_safe_name(cand.get('title', 'img'))}"
        ext = ".png" if "png" in cand.get("mime", "") else ".jpg"
        dest = self.dir / (fname + ext)
        part = self.dir / (dest.name + ".part")
        langd = 0
        if part.exists():
            langd = part.stat().st_size
        # duplicate detection: same source digest already downloaded?
        for e in self.manifest["images"]:
            if e.get("source", "").startswith("wikimedia") and \
               e.get("source_url") == cand.get("page_url", "") and \
               (self.dir / e.get("file_name", "")).exists():
                return {"id": e["id"], "duplicate": True}
        tmp = self.dir / f"_dl_{fname}{ext}"
        try:
            last_err: Exception | None = None
            for attempt in range(2):  # one polite retry (429s / transient 5xx)
                if attempt:
                    time.sleep(min(20.0, 5.0 * attempt))
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                    mode = "ab" if part.exists() and part.stat().st_size else "wb"
                    with urllib.request.urlopen(req, timeout=120) as r, open(part, mode) as f:
                        total = int(r.headers.get("Content-Length", 0)) or cand.get("bytes", 0)
                        got = f.tell() if mode == "ab" else 0
                        while True:
                            chunk = r.read(1 << 18)
                            if not chunk:
                                break
                            f.write(chunk)
                            got += len(chunk)
                            if progress and total:
                                progress(min(1.0, got / total),
                                         f"{fname}: {got // 1024} kB")
                    break
                except urllib.error.HTTPError as ex:
                    last_err = ex
                    if ex.code not in (429, 500, 502, 503, 504):
                        break
                except (urllib.error.URLError, OSError) as ex:
                    last_err = ex
                    break  # resume point kept in .part for the next call
            else:
                part.unlink(missing_ok=True)
                raise LibraryError(
                    f"download failed after retry: {type(last_err).__name__}: {last_err}")
            if last_err is not None:
                if isinstance(last_err, urllib.error.HTTPError):
                    part.unlink(missing_ok=True)
                    raise LibraryError(
                        f"download refused (HTTP {last_err.code}); not stored")
                raise LibraryError(
                    f"download failed (network): {type(last_err).__name__}: {last_err}")
            if progress:
                progress(1.0, f"{fname}: verifying")
            shutil.move(str(part), str(tmp))
        finally:
            part.unlink(missing_ok=True)
        entry = self._image_entry(tmp, source=cand.get("source", "wikimedia_commons"),
                                  source_url=cand.get("page_url", ""),
                                  license_=cand.get("license", "unknown"),
                                  author=cand.get("author", ""),
                                  title=cand.get("title", ""), datasets=datasets)
        tmp.unlink(missing_ok=True)
        self.manifest["images"].append(entry)
        self.save_manifest(bump_revision=True)
        return {"id": entry["id"], "path": self.file_path(entry) or entry.get("path"), "duplicate": False}

    def _image_entry(self, candidate_file: Path, source: str, source_url: str,
                     license_, author, title, datasets) -> dict:
        """Verify integrity + measure, then move into the library with stable id."""
        from PIL import Image, UnidentifiedImageError
        try:
            with Image.open(candidate_file) as im:
                im.verify()
            with Image.open(candidate_file) as im2:  # full decode check
                im2.load()
        except (UnidentifiedImageError, OSError, ValueError) as ex:
            raise LibraryError(f"corrupt or unreadable image: {type(ex).__name__}: {ex}")
        digest = _sha256(candidate_file)
        # strip sourced-html fragments (Commons API returns rich text)
        author = re.sub(r"<[^>]+>", "", author or "").strip()[:160]
        # identical content already present?
        for e in self.manifest["images"]:
            if e.get("sha256") == digest:
                return {"id": e["id"], "duplicate_of": e["id"]}
        iid = "img_" + digest[:12]
        ext = candidate_file.suffix.lower() or ".png"
        final = self.dir / f"{iid}{ext}"
        shutil.move(str(candidate_file), str(final))
        final_abs = final.resolve()
        pal_arr = palette_rgb()
        analysis = analyze_image(str(final), palette_arr=pal_arr)
        return {"id": iid, "file_name": final.name, "path": str(final_abs),
                "sha256": digest, "source": source, "source_url": source_url,
                "license": license_,
                "author": re.sub(r"<[^>]+>", "", author or "").strip()[:160],
                "title": title,
                "download_date": time.time(),
                "datasets": datasets or ["dev"],
                "analysis": analysis}

    # ---------------- verification / offline ----------------
    def verify(self) -> dict:
        ok, problems = 0, []
        for e in list(self.manifest["images"]):
            p = Path(self.file_path(e) or "")
            if not p.is_file():
                problems.append({"id": e["id"], "problem": "missing file"})
                continue
            try:
                digest = _sha256(p)
            except OSError as ex:
                problems.append({"id": e["id"], "problem": f"unreadable: {ex}"})
                continue
            if digest != e.get("sha256"):
                problems.append({"id": e["id"], "problem": "hash mismatch"})
                continue
            try:
                from PIL import Image
                with Image.open(p) as im:
                    im.verify()
            except Exception as ex:  # noqa: BLE001
                problems.append({"id": e["id"], "problem": f"corrupt: {type(ex).__name__}"})
                continue
            ok += 1
        return {"verified": ok, "total": len(self.manifest["images"]),
                "problems": problems, "offline_ready": not problems}

    def offline_ready(self) -> dict:
        missing = []
        for e in self.manifest["images"]:
            p = Path(self.file_path(e) or "")
            if not p.is_file():
                missing.append(e["id"])
        return {"ready": not missing, "missing": missing,
                "library_dir": str(self.dir)}

    # ---------------- dataset sets ----------------
    def set_membership(self, image_id: str, datasets: list):
        e = self.entry(image_id)
        if not e:
            raise LibraryError(f"unknown image id {image_id}")
        e["datasets"] = sorted(set(datasets))
        self.save_manifest(bump_revision=True)  # dataset revision history

    def start_set(self, which: str) -> dict:
        """Build quick/full/stress sets from the current library content.

        Returns what would CHANGE (ids) + estimated storage; the caller may
        ask for --apply to make membership edits real (bumps revision)."""
        if which == "quick":
            by = (["gradient", "high-contrast", "flat-areas"], 6, ["dev"])
        elif which == "full":
            by = (None, 18, ["dev"])
        elif which == "stress":
            by = (["similar-colors", "out-of-palette", "fine-details",
                   "high-contrast"], 12, ["stress"])
        else:
            raise LibraryError(f"unknown set {which!r}")
        wanted_tags, count, purpose = by
        chosen = self._diverse_select(wanted_tags, count)
        est = 0
        for iid in chosen:
            e = self.entry(iid)
            if e:
                try:
                    est += (Path(self.file_path(e) or "").stat().st_size + (1024 * 1024 - 1)) // (1024 * 1024)
                except OSError:
                    pass
        return {"set": which, "purpose": purpose, "ids": chosen,
                "estimated_size_mb": est, "count": len(chosen),
                "note": "membership applied by ImageLibrary.apply_set (bumps revision)"}

    def apply_set(self, which: str) -> dict:
        plan = self.start_set(which)
        purpose, ids = {"quick": "dev", "full": "dev",
                        "stress": "stress"}[which], plan["ids"]
        for iid in ids:
            self.set_membership(iid, [purpose])
        return plan | {"applied": True}

    def _diverse_select(self, wanted_tags, count: int) -> list:
        """Pick diverse images by measured properties (not first-N)."""
        rows = [e for e in self.manifest["images"] if self.file_path(e)]
        if wanted_tags:
            tagged = [e for e in rows
                      if set(e.get("analysis", {}).get("capability_tags", []))
                      & set(wanted_tags)]
            rows = tagged or rows
        # greedy diversity over (brightness, contrast, edge_density, diversity)
        picked, q = [], []
        for e in rows:
            a = e.get("analysis", {})
            q.append([a.get("brightness", 0.5), a.get("contrast", 0.2),
                      a.get("edge_density", 0.1), a.get("color_diversity", 0.5)])
        chosen_idx = []
        while len(chosen_idx) < min(count, len(rows)):
            best, best_score = None, -1.0
            for i, vec in enumerate(q):
                if i in chosen_idx:
                    continue
                score = 0.0
                for j in chosen_idx:
                    score += sum(abs(a - b) for a, b in zip(vec, q[j]))
                if len(chosen_idx) == 0:
                    score = 1.0  # start from best-scored area
                    if rows[i].get("analysis", {}).get("color_diversity", 0) > 0.4:
                        score += 0.2
                if score > best_score:
                    best, best_score = i, score
            chosen_idx.append(best)
        return [rows[i]["id"] for i in chosen_idx]

    def dev_files(self) -> list:
        """Local image paths the optimizer may train on (dev + synthetic)."""
        out = []
        for e in self.manifest["images"]:
            if any(p in ("dev", "synthetic") for p in e.get("datasets", [])) and \
               self.file_path(e):
                out.append(self.file_path(e))
        return out

    def holdout_files(self) -> list:
        return [p for e in self.manifest["images"]
                if "holdout" in e.get("datasets", []) and (p := self.file_path(e))]

    def revisions(self) -> list:
        return sorted(self.rev_dir.glob("rev-*.json"))

    def remove_dataset(self, dataset: str) -> list:
        """Drop every image whose membership includes `dataset` (files AND
        manifest entries). Experiment history lives elsewhere — lab/
        breeder checkpoints and champions are never touched."""
        keep, removed = [], []
        for e in self.manifest["images"]:
            if dataset in e.get("datasets", []):
                removed.append(e["id"])
                try:
                    Path(self.file_path(e) or "").unlink()
                except OSError:
                    pass  # missing file already; still drop the entry
            else:
                keep.append(e)
        self.manifest["images"] = keep
        if removed:
            self.save_manifest(bump_revision=True)
        return removed

    def export_manifest(self) -> dict:
        """Portable dataset manifest: relative file names only, plus a fresh
        integrity result so the receiving side knows what to expect."""
        v = self.verify()
        return {"format": "firefly-dataset-manifest/1",
                "exported": time.time(),
                "dataset_revision": self.manifest.get("dataset_revision", 0),
                "integrity": {"verified": v["verified"], "problems": v["problems"]},
                "count": len(self.manifest["images"]),
                "images": self.manifest["images"]}

    def import_manifest(self, entries: list) -> dict:
        """Merge a dataset manifest conservatively: entries are restored only
        when the image file already sits in this library dir with a matching
        sha256; entries without files are reported, never silently dropped."""
        added, skipped, missing_files = 0, [], []
        known = set(self.ids())
        for e in entries or []:
            if not isinstance(e, dict) or not e.get("id"):
                continue
            if e["id"] in known:
                skipped.append(e["id"])
                continue
            f = self.dir / str(e.get("file_name", ""))
            if not f.is_file() or _sha256(f) != e.get("sha256"):
                missing_files.append(e["id"])
                continue
            # re-anchor the entry to THIS library (never trust a foreign path)
            e["file_name"] = f.name
            e["path"] = str(f.resolve())
            self.manifest["images"].append(e)
            known.add(e["id"])
            added += 1
        if added:
            self.save_manifest(bump_revision=True)
        return {"added": added, "skipped": skipped, "missing_files": missing_files}

    def compare_revisions(self, a: int, b: int) -> dict:
        def load(n):
            blob = json.loads(self.rev_dir.joinpath(f"rev-{n}.json")
                              .read_text(encoding="utf-8"))
            return set(blob["ids"]), blob.get("membership", {})
        (sa, ma), (sb, mb) = load(a), load(b)
        moved = sorted(i for i in sa & sb if ma.get(i) != mb.get(i))
        return {"added": sorted(sb - sa), "removed": sorted(sa - sb),
                "membership_changed": moved, "unchanged": len(sa & sb) - len(moved)}

    def gaps_report(self, lab_history: list) -> dict:
        """Coverage gaps vs measured weaknesses (families that score poorly)."""
        fam_stats = _category_scores(lab_history)
        weak = [c for c, s in fam_stats.items() if s is not None and s < 0.90]
        present_tags = set()
        for e in self.manifest["images"]:
            present_tags |= set(e.get("analysis", {}).get("capability_tags", []))
        return {"weak_categories": weak,
                "library_tags_covered": sorted(present_tags),
                "recommendation": "generate synthetic tests for weak categories "
                                  "and search more tagged images"}

    # storage estimate for a candidate download batch
    def batch_estimate_bytes(self, cands: list) -> int:
        return sum(int(c.get("bytes", 0)) for c in cands)

    def free_space_mb(self) -> int:
        return disk_free_mb(self.dir)


def disk_free_mb(path) -> int:
    import shutil
    try:
        return shutil.disk_usage(str(path)).free // (1024 * 1024)
    except Exception:  # noqa: BLE001
        return 0


def _category_scores(_history_rows: list) -> dict:
    """Per-suite weak-category stats (suite ids are the lab's image families)."""
    by_suite = {}
    for r in _history_rows:
        suite = r.get("suite") or r.get("item_id")
        if suite and r.get("score") is not None:
            by_suite.setdefault(suite, []).append(r["score"])
    return {k: round(float(np.mean(v)), 4) for k, v in by_suite.items()}
