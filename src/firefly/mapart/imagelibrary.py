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
- Purpose sets are explicit: dev | validation | holdout | stress | synthetic.
  The held-out evaluation set is LOCKED: adding to or removing from it, or
  moving an image out of it, needs `confirm=True` and a written reason, and
  every change is appended to imagelibrary/membership_log.jsonl. Nothing is
  promoted between sets silently.
- Resources are bounded by a StoragePolicy: per-file and whole-library
  quotas, image count, decoded-pixel ceiling, allowed formats. Parsing is
  hardened (extension is not trusted; format/dimensions are checked before
  decode) and downloads resume from a verified byte offset.
- Thumbnails are cached under imagelibrary/thumbs/ and reused by hash.
- Offline readiness: every manifest entry must have a local readable file;
  `manifest_digest()` content-addresses the manifest for offline comparison.
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
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

USER_AGENT = ("FireflyMapArtEngine/1.0 (local offline map-art tool; contact: local-user)")
WIKIMEDIA_ENDPOINT = "https://commons.wikimedia.org/w/api.php"
DEFAULT_PALETTE_KEY = "survival-wool-1.8"
MAX_DEFAULT_FILE_MB = 25
ANALYSIS_VERSION = 2  # bump when measured fields/tags change meaning

# Purpose sets. `holdout` is the final-evaluation set and is locked against
# silent membership changes; `validation` is for parameter selection and is
# kept separate from both training data and the final held-out set.
PURPOSES = ("dev", "validation", "holdout", "stress", "synthetic")
LOCKED_PURPOSE = "holdout"

DEFAULT_ALLOWED_FORMATS = ("PNG", "JPEG", "WEBP", "BMP", "TIFF")


def license_status(value: str | None) -> str:
    """'missing' (no license), 'unknown', or 'recorded'."""
    text = (value or "").strip()
    if not text:
        return "missing"
    if text.lower() in ("unknown", "n/a", "none", "?"):
        return "unknown"
    return "recorded"


@dataclass(frozen=True)
class StoragePolicy:
    """Configurable resource limits for the image library.

    Defaults are generous enough for the existing library but bounded:
    no single download may be unbounded, no library may grow forever, and no
    image may be decoded beyond `max_pixels` (decompression-bomb guard).
    """
    max_file_mb: int = MAX_DEFAULT_FILE_MB
    max_library_mb: int = 2048
    max_images: int = 2000
    max_pixels: int = 64_000_000
    thumb_px: int = 128
    allowed_formats: tuple = DEFAULT_ALLOWED_FORMATS

    def to_dict(self) -> dict:
        return {"max_file_mb": self.max_file_mb,
                "max_library_mb": self.max_library_mb,
                "max_images": self.max_images,
                "max_pixels": self.max_pixels,
                "thumb_px": self.thumb_px,
                "allowed_formats": list(self.allowed_formats)}

    @classmethod
    def from_dict(cls, blob: dict | None) -> "StoragePolicy":
        blob = blob or {}
        base = cls()
        allowed = blob.get("allowed_formats")
        return cls(
            max_file_mb=int(blob.get("max_file_mb", base.max_file_mb)),
            max_library_mb=int(blob.get("max_library_mb", base.max_library_mb)),
            max_images=int(blob.get("max_images", base.max_images)),
            max_pixels=int(blob.get("max_pixels", base.max_pixels)),
            thumb_px=int(blob.get("thumb_px", base.thumb_px)),
            allowed_formats=tuple(str(x).upper() for x in allowed)
            if allowed else base.allowed_formats)

CAPABILITY_CATEGORIES = (
    "landscape", "sky", "forest", "water", "animal", "plant", "face",
    "architecture", "illustration", "pixel-art", "gradient", "high-contrast",
    "similar-colors", "out-of-palette", "fine-details", "flat-areas",
    "dark-image", "bright-image", "broad-range",
)


class LibraryError(RuntimeError):
    pass


class HoldoutLocked(LibraryError):
    """Held-out evaluation membership changed without explicit confirmation."""


# --------------------------------------------------------------------------
# analysis (numpy + PIL only)
# --------------------------------------------------------------------------

def safe_image_info(path, policy: "StoragePolicy | None" = None) -> dict:
    """Validate a candidate image WITHOUT trusting its file extension.

    Checks (in order, all before a full decode): extension and detected
    format are allowed, the file is within the per-file byte cap, the header
    is readable, and width*height is within the pixel ceiling (the
    decompression-bomb guard). Corruption surfaces as LibraryError, never as
    a traceback from PIL.
    """
    from PIL import Image, UnidentifiedImageError
    policy = policy or StoragePolicy()
    p = Path(path)
    if not p.is_file():
        raise LibraryError(f"file not found: {p}")
    size = p.stat().st_size
    if size <= 0:
        raise LibraryError(f"empty file: {p.name}")
    if size > policy.max_file_mb * 1024 * 1024:
        raise LibraryError(
            f"file too large: {p.name} is {size / 1048576:.1f} MB "
            f"(limit {policy.max_file_mb} MB)")
    ext = p.suffix.lower().lstrip(".")
    if ext and ext not in ("png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"):
        raise LibraryError(f"unsupported file type: .{ext} ({p.name})")
    try:
        with Image.open(p) as im:
            fmt = (im.format or "").upper()
            w, h = im.size
            mode = im.mode
            has_alpha = mode in ("RGBA", "LA") or (
                mode == "P" and "transparency" in (im.info or {}))
            if fmt not in policy.allowed_formats:
                raise LibraryError(
                    f"unsupported image format {fmt or 'unknown'} for {p.name}; "
                    f"allowed: {', '.join(policy.allowed_formats)}")
            if w <= 0 or h <= 0:
                raise LibraryError(f"invalid dimensions {w}x{h} for {p.name}")
            if w * h > policy.max_pixels:
                raise LibraryError(
                    f"image too large to decode safely: {w}x{h} = {w * h} px "
                    f"(limit {policy.max_pixels} px)")
            im.verify()
        with Image.open(p) as im2:  # full decode proves the payload is intact
            im2.load()
    except LibraryError:
        raise
    except Image.DecompressionBombError as ex:
        raise LibraryError(f"decompression bomb rejected: {p.name}: {ex}")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as ex:
        raise LibraryError(
            f"corrupt or unreadable image: {p.name}: {type(ex).__name__}: {ex}")
    return {"format": fmt, "width": int(w), "height": int(h), "mode": mode,
            "has_alpha": bool(has_alpha), "bytes": int(size)}


def _load_small(path, max_px=256, policy: "StoragePolicy | None" = None):
    """Decode a bounded downsample. Returns (rgb float array, alpha share, info)."""
    from PIL import Image
    info = safe_image_info(path, policy)
    with Image.open(path) as im:
        w, h = im.size
        scale = 1.0
        if max(w, h) > max_px:
            scale = max_px / float(max(w, h))
        small = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        arr = np.asarray(small.convert("RGB"), dtype=np.float64)
        alpha_share = 0.0
        if info["has_alpha"]:
            alpha = np.asarray(small.convert("RGBA"))[..., 3]
            alpha_share = round(float((alpha < 250).mean()), 4)
    return arr, alpha_share, info


def characteristics(path: str, palette_arr: np.ndarray | None = None,
                    policy: "StoragePolicy | None" = None) -> dict:
    """Deterministic measured properties used for benchmark balancing.

    Same bytes in -> identical dict out (no timestamps, no unordered sets,
    fixed sample size, stable tie-breaks), so two runs can be compared
    exactly and the result can be content-addressed (`characteristics_digest`).
    """
    arr, alpha_share, info = _load_small(path, policy=policy)
    w, h = info["width"], info["height"]
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
    # dominant colors (32-bin RGB histogram; stable order by count then bin)
    q64 = (px // 32).astype(np.int32)
    vals, cnts = np.unique(q64, axis=0, return_counts=True)
    grouped = sorted(range(len(cnts)),
                     key=lambda i: (-int(cnts[i]), tuple(int(x) for x in vals[i])))
    order = grouped[:8]
    dominant = [int(c) for c in (cnts[order] / cnts.sum() * 10000)]  # basis points
    dominant_rgb = []
    for i in order:
        bin_rows = np.all(q64 == vals[i], axis=1)
        mean = np.rint(px[bin_rows].mean(axis=0)).astype(int)
        dominant_rgb.append({"rgb": [int(x) for x in mean],
                             "share_basis_pts": int(cnts[i] / cnts.sum() * 10000)})
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
    # detail complexity: edge energy, with a small bonus for non-flat area and
    # colour spread; bounded to 1.0 so it can be used for balancing directly.
    detail_complexity = round(min(1.0, edge_density * 2.0
                                  + (1.0 - flat_share) * 0.25
                                  + color_diversity * 0.1), 4)
    return {"width": int(w), "height": int(h), "aspect": round(w / max(1, h), 4),
            "color_diversity": color_diversity, "brightness": brightness,
            "contrast": contrast, "edge_density": edge_density,
            "flat_share": flat_share, "similar_color_score": similar_color_score,
            "palette_coverage": pal_coverage,
            "out_of_palette_share": out_of_palette_share,
            "dominant_color_basis_pts": dominant,
            "dominant_colors_rgb": dominant_rgb,
            "transparency_share": alpha_share,
            "has_alpha": info["has_alpha"],
            "source_format": info["format"],
            "detail_complexity": detail_complexity,
            "capability_tags": tags,
            "analysis_version": ANALYSIS_VERSION}


def analyze_image(path: str, palette_arr: np.ndarray | None = None,
                  policy: "StoragePolicy | None" = None) -> dict:
    """`characteristics` plus the observation time (manifest storage)."""
    out = characteristics(path, palette_arr=palette_arr, policy=policy)
    out["analyzed"] = time.time()
    return out


def characteristics_digest(analysis: dict) -> str:
    """Content address of a characteristics dict (timestamps excluded)."""
    blob = {k: v for k, v in analysis.items() if k != "analyzed"}
    return hashlib.sha256(json.dumps(blob, sort_keys=True, separators=(",", ":"),
                                     default=str).encode("utf-8")).hexdigest()[:16]


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
    def __init__(self, data_dir, policy: "StoragePolicy | None" = None):
        self.dir = Path(data_dir) / "imagelibrary"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.rev_dir = self.dir / "revisions"
        self.rev_dir.mkdir(parents=True, exist_ok=True)
        self.thumbs_dir = self.dir / "thumbs"
        self.thumbs_dir.mkdir(parents=True, exist_ok=True)
        self.membership_log = self.dir / "membership_log.jsonl"
        self.import_journal = self.dir / "import_journal.json"
        self.policy_path = self.dir / "policy.json"
        if policy is None:
            policy = self._load_policy()
        self.policy = policy
        self.manifest_path = self.dir / "manifest.json"
        self.manifest = {"version": 1, "dataset_revision": 0, "images": [],
                         "updated": time.time()}
        self.load_manifest()

    # ---------------- configurable policy ----------------
    def _load_policy(self) -> "StoragePolicy":
        if self.policy_path.is_file():
            try:
                return StoragePolicy.from_dict(
                    json.loads(self.policy_path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                pass  # corrupt policy -> documented defaults, never crash
        return StoragePolicy()

    def save_policy(self) -> dict:
        """Persist the active limits so they survive restarts."""
        blob = self.policy.to_dict()
        tmp = self.policy_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob, indent=1), encoding="utf-8")
        os.replace(tmp, self.policy_path)
        return blob

    def set_policy(self, **changes) -> dict:
        """Update individual limits (e.g. max_library_mb=500) and persist."""
        self.policy = replace(self.policy, **changes)
        return self.save_policy()

    # ---------------- resource accounting ----------------
    def library_bytes(self) -> int:
        total = 0
        for e in self.manifest["images"]:
            p = self.file_path(e)
            if p:
                try:
                    total += Path(p).stat().st_size
                except OSError:
                    pass
        try:
            for t in self.thumbs_dir.glob("*.jpg"):
                total += t.stat().st_size
        except OSError:
            pass
        return total

    def quota_status(self) -> dict:
        used = self.library_bytes()
        limit = self.policy.max_library_mb * 1024 * 1024
        return {"used_bytes": used, "used_mb": round(used / 1048576, 2),
                "limit_mb": self.policy.max_library_mb,
                "used_pct": round(100.0 * used / limit, 1) if limit else None,
                "images": len(self.manifest["images"]),
                "image_limit": self.policy.max_images,
                "free_disk_mb": self.free_space_mb()}

    def _check_capacity(self, add_bytes: int):
        """Refuse BEFORE touching the library; limits are configurable but
        always enforced (no silent overrun, no truncation)."""
        if len(self.manifest["images"]) >= self.policy.max_images:
            raise LibraryError(
                f"image limit reached: {len(self.manifest['images'])}/"
                f"{self.policy.max_images} (raise max_images in "
                f"{self.policy_path.name})")
        used = self.library_bytes()
        limit = self.policy.max_library_mb * 1024 * 1024
        if used + add_bytes > limit:
            raise LibraryError(
                f"library storage limit would be exceeded: "
                f"{(used + add_bytes) / 1048576:.1f} MB > {self.policy.max_library_mb} MB "
                f"(raise max_library_mb in {self.policy_path.name})")
        if self.free_space_mb() * 1024 * 1024 < add_bytes:
            raise LibraryError("not enough free disk space for this import")

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

    def manifest_digest(self) -> str:
        """Content address of the manifest (membership + hashes + metadata),
        stable across machines and observation timestamps, for offline compare."""
        return manifest_content_digest(self.manifest["images"],
                                       self.manifest.get("dataset_revision", 0))

    # ---------------- add / download ----------------
    def add_local(self, path: str, source_label: str = "local-user-folder",
                  datasets: list | None = None) -> dict:
        """Import an explicitly user-selected local file."""
        src = Path(path)
        if not src.is_file():
            raise LibraryError(f"file not found: {src}")
        # validate the bytes BEFORE copying them into the library, so a
        # malformed/huge file never consumes quota even temporarily
        safe_image_info(src, self.policy)
        self._check_capacity(src.stat().st_size)
        tmp = self.dir / f"_probe_{_safe_name(src.stem, 60)}{src.suffix.lower()}"
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
        scheme = urllib.parse.urlsplit(str(url)).scheme.lower()
        if scheme not in ("http", "https"):
            raise LibraryError(f"refusing to download non-http(s) url: {scheme or '?'}")
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        fname = f"{digest}_{_safe_name(cand.get('title', 'img'))}"
        ext = ".png" if "png" in cand.get("mime", "") else ".jpg"
        dest = self.dir / (fname + ext)
        part = self.dir / (dest.name + ".part")
        # duplicate detection: same source page already downloaded?
        for e in self.manifest["images"]:
            if e.get("source", "").startswith("wikimedia") and \
               e.get("source_url") == cand.get("page_url", "") and \
               (self.dir / e.get("file_name", "")).exists():
                return {"id": e["id"], "duplicate": True}
        cap = self.policy.max_file_mb * 1024 * 1024
        declared = int(cand.get("bytes") or 0)
        if declared > cap:
            raise LibraryError(
                f"candidate is {declared / 1048576:.1f} MB, above the "
                f"{self.policy.max_file_mb} MB per-file limit; not downloaded")
        self._check_capacity(declared or 1)
        tmp = self.dir / f"_dl_{fname}{ext}"
        try:
            last_err: Exception | None = None
            for attempt in range(2):  # one polite retry (429s / transient 5xx)
                if attempt:
                    time.sleep(min(20.0, 5.0 * attempt))
                try:
                    have = part.stat().st_size if part.exists() else 0
                    headers = {"User-Agent": USER_AGENT}
                    if have:
                        headers["Range"] = f"bytes={have}-"
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=120) as r:
                        status = int(getattr(r, "status", 200) or 200)
                        # a server that ignores Range sends 200 + full body
                        if have and status != 206:
                            have = 0
                        remaining = int(r.headers.get("Content-Length", 0)) or 0
                        full_total = (have + remaining) if remaining else declared
                        mode = "ab" if have else "wb"
                        got = have
                        with open(part, mode) as f:
                            while True:
                                chunk = r.read(1 << 18)
                                if not chunk:
                                    break
                                got += len(chunk)
                                if got > cap:
                                    raise LibraryError(
                                        f"download exceeded the "
                                        f"{self.policy.max_file_mb} MB limit; "
                                        f"partial file discarded")
                                f.write(chunk)
                                if progress and full_total:
                                    progress(min(1.0, got / full_total),
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
        except LibraryError:
            part.unlink(missing_ok=True)
            raise
        finally:
            if not tmp.exists():
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
        info = safe_image_info(candidate_file, self.policy)  # extension untrusted
        self._check_capacity(candidate_file.stat().st_size)
        digest = _sha256(candidate_file)
        # strip sourced-html fragments (Commons API returns rich text)
        author = re.sub(r"<[^>]+>", "", author or "").strip()[:160]
        # identical content already present?
        for e in self.manifest["images"]:
            if e.get("sha256") == digest:
                return {"id": e["id"], "duplicate_of": e["id"]}
        iid = "img_" + digest[:12]
        # extension follows the DETECTED format, not the source file's name
        ext = {"JPEG": ".jpg", "TIFF": ".tif"}.get(info["format"],
                                                    "." + info["format"].lower())
        final = self.dir / f"{iid}{ext}"
        shutil.move(str(candidate_file), str(final))
        final_abs = final.resolve()
        pal_arr = palette_rgb()
        analysis = analyze_image(str(final), palette_arr=pal_arr,
                                 policy=self.policy)
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
    def _log_membership(self, image_id: str, before: list, after: list,
                        reason: str, confirmed: bool):
        """Append-only audit trail: set changes are never silent."""
        row = {"ts": time.time(), "id": image_id,
               "from": sorted(before), "to": sorted(after),
               "reason": reason, "confirmed": bool(confirmed),
               "dataset_revision": self.manifest.get("dataset_revision", 0) + 1}
        with open(self.membership_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
        return row

    def set_membership(self, image_id: str, datasets: list, *,
                       reason: str = "", confirm: bool = False) -> dict:
        """Change an image's purpose sets, enforcing the held-out lock.

        Touching the held-out evaluation set (adding to it, moving an image
        out of it, or removing it) requires an explicit `confirm=True` and a
        non-empty `reason`; otherwise this raises `HoldoutLocked`. Every
        change is written to membership_log.jsonl and bumps the revision, so
        the history of any evaluation set is reconstructable.
        """
        e = self.entry(image_id)
        if not e:
            raise LibraryError(f"unknown image id {image_id}")
        before = list(e.get("datasets", []))
        after = sorted(set(datasets))
        unknown = [p for p in after if p not in PURPOSES]
        if unknown:
            raise LibraryError(f"unknown dataset purpose(s) {unknown}; "
                               f"valid: {list(PURPOSES)}")
        if after == sorted(before):
            return {"changed": False, "id": image_id, "datasets": after}
        touches_locked = (LOCKED_PURPOSE in before) or (LOCKED_PURPOSE in after)
        if touches_locked and not (confirm and reason.strip()):
            raise HoldoutLocked(
                f"{image_id}: changing membership of the held-out evaluation "
                f"set requires confirm=True and a written reason "
                f"({before} -> {after})")
        e["datasets"] = after
        self._log_membership(image_id, before, after, reason.strip(), confirm)
        self.save_manifest(bump_revision=True)  # dataset revision history
        return {"changed": True, "id": image_id, "from": sorted(before),
                "to": after, "reason": reason.strip()}

    def assign_set(self, image_id: str, purpose: str, *, reason: str = "",
                   confirm: bool = False) -> dict:
        """Assign ONE purpose set, replacing current membership."""
        if purpose not in PURPOSES:
            raise LibraryError(f"unknown purpose {purpose!r}; valid: {list(PURPOSES)}")
        return self.set_membership(image_id, [purpose], reason=reason,
                                   confirm=confirm)

    def membership_log_rows(self) -> list:
        if not self.membership_log.is_file():
            return []
        rows = []
        for line in self.membership_log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows

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
        """Pick diverse images by measured properties (not first-N).

        Held-out members are excluded: quick/full/stress sets must never
        silently pull an image out of the locked evaluation set."""
        rows = [e for e in self.manifest["images"]
                if self.file_path(e) and LOCKED_PURPOSE not in e.get("datasets", [])]
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

    def remove_dataset(self, dataset: str, *, confirm: bool = False,
                       reason: str = "") -> list:
        """Drop every image whose membership includes `dataset` (files AND
        manifest entries). Experiment history lives elsewhere — lab/
        breeder checkpoints and champions are never touched.

        Deleting the held-out evaluation set is a membership change of the
        locked set: it requires `confirm=True` and a written reason and is
        journalled before the files are removed."""
        if dataset == LOCKED_PURPOSE:
            if not (confirm and reason.strip()):
                raise HoldoutLocked(
                    "removing the held-out evaluation set requires "
                    "confirm=True and a written reason")
        keep, removed = [], []
        previous: dict = {}
        for e in self.manifest["images"]:
            if dataset in e.get("datasets", []):
                removed.append(e["id"])
                previous[e["id"]] = list(e.get("datasets", []))
                try:
                    Path(self.file_path(e) or "").unlink()
                except OSError:
                    pass  # missing file already; still drop the entry
            else:
                keep.append(e)
        self.manifest["images"] = keep
        if removed:
            for iid in removed:
                self._log_membership(iid, previous.get(iid, []), [],
                                     reason.strip() or f"removed set {dataset}",
                                     confirm)
            self.save_manifest(bump_revision=True)
        return removed

    def export_manifest(self) -> dict:
        """Portable dataset manifest: relative file names only, plus a fresh
        integrity result and a content digest so the receiving side can verify
        it offline without the sender."""
        v = self.verify()
        sets: dict = {p: 0 for p in PURPOSES}
        for e in self.manifest["images"]:
            for p in e.get("datasets", []):
                sets[p] = sets.get(p, 0) + 1
        return {"format": "firefly-dataset-manifest/1",
                "exported": time.time(),
                "dataset_revision": self.manifest.get("dataset_revision", 0),
                "manifest_digest": self.manifest_digest(),
                "policy": self.policy.to_dict(),
                "set_counts": sets,
                "integrity": {"verified": v["verified"], "problems": v["problems"]},
                "count": len(self.manifest["images"]),
                "images": self.manifest["images"]}

    @staticmethod
    def _safe_entry_name(name) -> str | None:
        """Bare filename inside the library dir; rejects traversal/absolute
        paths, drive letters and nested names from foreign manifests."""
        text = str(name or "")
        if not text or text in (".", ".."):
            return None
        if any(sep in text for sep in ("/", "\\")) or ":" in text:
            return None
        p = Path(text)
        if p.is_absolute() or p.name != text:
            return None
        return text

    def import_manifest(self, entries: list, *, confirm: bool = False,
                        reason: str = "") -> dict:
        """Merge a dataset manifest conservatively: entries are restored only
        when the image file already sits in this library dir with a matching
        sha256; entries without files are reported, never silently dropped.

        Foreign paths and traversal names are rejected outright. Entries
        claiming held-out membership need explicit confirmation; nothing is
        promoted between sets silently.
        """
        added, skipped, missing_files = 0, [], []
        rejected, locked_refused = [], []
        known = set(self.ids())
        for e in entries or []:
            if not isinstance(e, dict) or not e.get("id"):
                continue
            if e["id"] in known:
                skipped.append(e["id"])
                continue
            name = self._safe_entry_name(e.get("file_name"))
            if name is None:
                rejected.append({"id": e["id"], "reason": "unsafe file_name"})
                continue
            f = self.dir / name
            if not f.is_file() or _sha256(f) != e.get("sha256"):
                missing_files.append(e["id"])
                continue
            sets = sorted(set(x for x in (e.get("datasets") or ["dev"])
                              if x in PURPOSES))
            if LOCKED_PURPOSE in sets and not (confirm and reason.strip()):
                locked_refused.append(e["id"])
                continue
            # re-anchor the entry to THIS library (never trust a foreign path)
            e["file_name"] = f.name
            e["path"] = str(f.resolve())
            e["datasets"] = sets or ["dev"]
            if not e.get("analysis"):
                e["analysis"] = analyze_image(str(f), palette_arr=palette_rgb(),
                                              policy=self.policy)
            self.manifest["images"].append(e)
            known.add(e["id"])
            if LOCKED_PURPOSE in sets:
                self._log_membership(e["id"], [], set(e["datasets"]),
                                     reason.strip() or "explicit import", confirm)
            added += 1
        if added:
            self.save_manifest(bump_revision=True)
        return {"added": added, "skipped": skipped, "missing_files": missing_files,
                "rejected": rejected, "holdout_requires_confirmation": locked_refused}

    # ---------------- thumbnails ----------------
    def thumbnail(self, entry_or_id, max_px: int | None = None) -> str:
        """Return a cached thumbnail path, generating it only when missing or
        older than the source file. Thumbnails live beside the library so a
        rerun never re-decodes full images to draw a grid."""
        e = entry_or_id if isinstance(entry_or_id, dict) else self.entry(str(entry_or_id))
        if not e:
            raise LibraryError(f"unknown image id {entry_or_id}")
        src = self.file_path(e)
        if not src or not Path(src).is_file():
            raise LibraryError(f"source file missing for {e.get('id')}")
        px = int(max_px or self.policy.thumb_px)
        out = self.thumbs_dir / f"{e['id']}_{px}.jpg"
        if out.is_file() and out.stat().st_mtime >= Path(src).stat().st_mtime:
            return str(out)
        from PIL import Image
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((px, px))
            tmp = out.with_suffix(".tmp.jpg")
            im.save(tmp, format="JPEG", quality=82, optimize=False)
        os.replace(tmp, out)
        return str(out)

    def thumbnail_stats(self) -> dict:
        files = list(self.thumbs_dir.glob("*.jpg"))
        total = 0
        for f in files:
            try:
                total += f.stat().st_size
            except OSError:
                pass
        return {"count": len(files), "bytes": total,
                "dir": str(self.thumbs_dir)}

    # ---------------- resumable folder import ----------------
    def import_paths(self, paths, datasets: list | None = None,
                     source_label: str = "local-user-folder",
                     journal: Path | None = None) -> dict:
        """Import many local files with a checkpoint journal.

        The journal records each completed source path after its file is
        safely in the library, so an interrupted run resumes where it stopped
        instead of re-copying or double-adding. Journal writes are atomic.
        """
        jpath = Path(journal) if journal else self.import_journal
        state = {"started": time.time(), "done": {}, "dataset": datasets or ["dev"]}
        if jpath.is_file():
            try:
                loaded = json.loads(jpath.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("done"), dict):
                    state.update(loaded)
            except (json.JSONDecodeError, OSError):
                pass  # corrupt journal -> start over; files dedupe by hash
        added, skipped, errors, resumed = [], [], [], 0
        for raw in paths:
            key = str(Path(raw))
            if key in state["done"]:
                eid = state["done"][key]
                if self.entry(str(eid)):
                    resumed += 1
                    skipped.append(str(eid))
                    continue
            try:
                entry = self.add_local(raw, source_label=source_label,
                                       datasets=datasets)
            except (LibraryError, OSError) as ex:
                errors.append({"path": key, "error": f"{type(ex).__name__}: {ex}"})
                continue
            eid = entry.get("duplicate_of") or entry["id"]
            state["done"][key] = eid
            tmp = jpath.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
            os.replace(tmp, jpath)
            if entry.get("duplicate_of"):
                skipped.append(str(eid))
            else:
                added.append(str(eid))
        return {"added": added, "skipped": skipped, "errors": errors,
                "resumed": resumed, "journal": str(jpath),
                "remaining": [str(p) for p in paths if str(Path(p)) not in state["done"]]}

    # ---------------- analysis refresh ----------------
    def analysis_status(self) -> dict:
        stale = [e["id"] for e in self.manifest["images"]
                 if int((e.get("analysis") or {}).get("analysis_version", 0))
                 < ANALYSIS_VERSION]
        return {"current_version": ANALYSIS_VERSION, "stale": stale,
                "stale_count": len(stale),
                "total": len(self.manifest["images"])}

    def refresh_analysis(self, only_stale: bool = True) -> dict:
        """Recompute deterministic characteristics in place (versioned)."""
        updated, failed = 0, []
        pal_arr = palette_rgb()
        for e in self.manifest["images"]:
            if only_stale and int((e.get("analysis") or {})
                                  .get("analysis_version", 0)) >= ANALYSIS_VERSION:
                continue
            p = self.file_path(e)
            if not p:
                failed.append({"id": e["id"], "error": "missing file"})
                continue
            try:
                e["analysis"] = analyze_image(p, palette_arr=pal_arr,
                                              policy=self.policy)
                updated += 1
            except LibraryError as ex:
                failed.append({"id": e["id"], "error": str(ex)})
        if updated:
            self.save_manifest(bump_revision=False)
        return {"updated": updated, "failed": failed,
                "analysis_version": ANALYSIS_VERSION}

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


def _canonical_entry(entry: dict) -> dict:
    """Entry with machine-specific path and observation time removed, so the
    same dataset exported on two machines has the same digest."""
    e = {k: v for k, v in entry.items() if k != "path"}
    analysis = dict(e.get("analysis") or {})
    analysis.pop("analyzed", None)
    e["analysis"] = analysis
    return e


def manifest_content_digest(images, dataset_revision) -> str:
    """Portable content address over sorted canonical entries + revision."""
    canon = sorted((_canonical_entry(e) for e in images if isinstance(e, dict)),
                   key=lambda e: str(e.get("id", "")))
    blob = {"dataset_revision": int(dataset_revision or 0), "images": canon}
    return hashlib.sha256(json.dumps(blob, sort_keys=True, default=str)
                          .encode("utf-8")).hexdigest()


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
