"""FireFly licensing & entitlements — deliberately SEPARATE from the engine.

Architecture in one paragraph (details: docs/LICENSING_ARCHITECTURE.md):
The desktop app never holds secrets and never talks to the payment platform
directly. A tiny creator-run verification service (service.py) holds the
Buy Me a Coffee developer token and signs entitlement records. The app sends
its purchase reference there, stores a NON-AUTHORITATIVE, tamper-evident local
record, and honors a documented offline grace window._payment-card details are
never seen by any component of this application.

Data protection guarantees enforced here, on purpose:
- License failure NEVER touches user data. This module contains no code that
  deletes, encrypts or relocates projects; it only reports entitlements.
- The local record is a convenience cache only. Editing it locally can never
  unlock Pro: the verifier is the only authority (checked online, with the
  machine fingerprint bound at activation time).
- Supporter membership and the Pro license are two separate entitlements that
  can be granted and lost independently; a cancelled membership never removes
  a previously verified Pro license.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from ._shared import LicensingError

__all__ = ["Entitlements", "Status", "LicenseStore", "LicensingError",
           "FEATURE_GATES", "allows", "STATE_FREE", "STATE_PRO_VERIFIED",
           "STATE_PRO_GRACE", "STATE_PENDING", "STATE_FAILED",
           "STATE_SUPPORTER_ACTIVE", "STATE_SUPPORTER_EXPIRED"]


# ---------------------------------------------------------------- states ----
GRACE_DAYS_DEFAULT = 30.0

STATE_FREE = "free"
STATE_PRO_VERIFIED = "pro_verified"
STATE_PRO_GRACE = "pro_grace"          # verified once; re-check temporarily impossible/unavailable
STATE_PENDING = "purchase_pending"     # verification in progress / unreachable; not an error verdict
STATE_FAILED = "verification_failed"   # verifier answered: no such purchase
STATE_SUPPORTER_ACTIVE = "supporter_active"
STATE_SUPPORTER_EXPIRED = "supporter_expired"

PRODUCTS = {"pro": "FireFly Pro (full desktop application, permanent $5 license)",            "supporter": "FireFly Supporter ($1/month development membership)"}


@dataclass
class Status:
    """Single source of truth for entitlement reporting (GUI + services)."""
    state: str = STATE_FREE
    supporter: str = ""                # STATE_SUPPORTER_ACTIVE / STATE_SUPPORTER_EXPIRED / ""
    label: str = "Free Edition"
    pro: bool = False
    supporter_badge: str = ""          # short badge text, empty = no badge
    can_upgrade: bool = True
    needs_recheck: bool = False        # grace running out / unavailable recently
    detail: str = ""                   # honest human-readable note
    record: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in
                ("state", "supporter", "label", "pro", "supporter_badge",
                 "can_upgrade", "needs_recheck", "detail")}


# ------------------------------------------------------------ machine id ----
def device_id(data_dir: Path | str | None = None) -> str:
    """Stable per-machine identifier (privacy: one-way hash, no MAC address)."""
    raw = platform.platform() + "|" + platform.node() + "|" + \
        uuid.getnode().__format__("x")
    salt = ""
    if data_dir is not None:
        salt_file = Path(data_dir) / "device_salt"
        # keep a local salt so identical machines do not share an id, but do NOT
        # treat the file as authoritative (the verifier re-binds at activation)
        if not salt_file.exists():
            salt_file.parent.mkdir(parents=True, exist_ok=True)
            salt_file.write_text(uuid.uuid4().hex, encoding="utf-8")
        salt = "|" + salt_file.read_text(encoding="utf-8").strip()
    return hashlib.sha256((raw + salt).encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------- store ----
def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _integrity_cache_key(record: dict, did: str) -> str:
    """HMAC keyed by the machine fingerprint: casual edits to license.json do
    not reproduce this. NOT cryptographic proof of purchase (the local file is
    never authoritative) — purely tamper-evident convenience caching."""
    h = hashlib.sha256()      # stdlib only; key material = device fingerprint, not a secret
    h.update((did + "|firefly-license-cache|").encode())
    h.update(_canonical({k: v for k, v in record.items() if k != "integrity"}).encode())
    return h.hexdigest()


class LicenseStore:
    """Stores the latest verifier-signed record + local cache state.

    Never stores: payment card data, BMC tokens, keys, or web secrets.
    Only: the signed entitlement record the verifier returned and local bookkeeping."""

    CACHE_VERSION = 1

    def __init__(self, data_dir: Path | str):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "license.json"

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            blob = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        return blob if isinstance(blob, dict) else None

    def save_record(self, record: dict, verifier: str = "") -> None:
        did = device_id(self.dir)
        blob = {"cache_version": self.CACHE_VERSION,
                "record": dict(record),
                "verified_at": time.time(),
                "verifier_url": verifier,
                "verified_devices": {did: {"name": platform.node(), "ts": time.time()}},
                "integrity": _integrity_cache_key(record, did)}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(blob, indent=2, default=list), encoding="utf-8")
        try:
            os.replace(tmp, self.path)
        except OSError as e:
            raise LicensingError(f"cannot write license record: {e}") from e

    def save_cache_only(self, blob: dict) -> None:
        did = device_id(self.dir)
        blob["integrity"] = _integrity_cache_key(blob.get("record", {}), did)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(blob, indent=2, default=list), encoding="utf-8")
        os.replace(tmp, self.path)

    def record_intact(self, blob: dict, did: str | None = None) -> bool:
        did = did or device_id(self.dir)
        rec = blob.get("record", {})
        if blob.get("cache_version") != self.CACHE_VERSION:
            return False
        return blob.get("integrity") == _integrity_cache_key(rec, did)


# ----------------------------------------------------------- entitlements ---
class Entitlements:
    """Reads the store and answers: what may this copy do right now?

    The verifier is authoritative; the local record only enables the
    documented offline grace window (default 30 days). A tampered or missing
    record simply means Free — never a crash, never project damage."""

    def __init__(self, data_dir: Path | str, grace_days: float = GRACE_DAYS_DEFAULT,
                 now: Callable[[], float] = time.time):
        self.store = LicenseStore(data_dir)
        self.grace_days = float(grace_days)
        self._now = now

    def status(self) -> Status:
        blob = self.store.load()
        s = Status()
        if blob is None:
            s.detail = "no license record — Free Edition features apply"
            return s
        did = device_id(self.store.dir)
        if not self.store.record_intact(blob, did):
            # tampered/corrupt cache: stay Free, keep the file for diagnostics,
            # and flag it honestly instead of silently resetting
            s.detail = ("local license cache failed its integrity check — treated as "
                        "Free; re-verify once online to restore Pro")
            s.needs_recheck = True
            return s
        rec = blob.get("record", {})
        verified_at = float(blob.get("verified_at", 0))
        age_days = (self._now() - verified_at) / 86400.0
        if rec.get("format") != "firefly-license/1":
            unknown = rec.get("format", "<none>")
            raise LicensingError(f"unsupported license record format {unknown!r} "
                                 "(outdated or foreign file; remove it and re-verify)")
        if rec.get("edition") == "pro" and rec.get("expires_at") in (None, ""):
            if age_days <= self.grace_days:
                s.state = STATE_PRO_VERIFIED if age_days <= 7 else STATE_PRO_GRACE
                s.pro = True
                s.label = "Pro Edition ($5 license — permanent)"
                if age_days > 7:
                    s.detail = (f"verified {age_days:.0f} days ago; next online re-check "
                                f"due within {self.grace_days - age_days:.0f} days "
                                "(offline use stays fully allowed)")
                else:
                    s.detail = "purchase verified through the license verifier"
            else:
                s.state = STATE_PENDING
                s.needs_recheck = True
                s.detail = (f"last verification {age_days:.0f} days ago — beyond the "
                            f"{int(self.grace_days)}-day offline grace window. Your data "
                            "and projects are untouched; run 'Verify purchase' once "
                            "online to restore Pro.")
        else:
            s.state = STATE_FREE
        sup = rec.get("supporter") or {}
        if sup.get("status") == "active":
            period_end = str(sup.get("current_period_end", ""))
            if period_end:
                try:
                    from datetime import datetime
                    end = datetime.fromisoformat(period_end.replace("Z", "+00:00"))
                    if end.timestamp() < self._now():
                        s.supporter = STATE_SUPPORTER_EXPIRED
                        s.detail = (s.detail + "\n" if s.detail else "") + \
                            "supporter membership ended at its paid period end — thank you " \
                            "for supporting development; your Pro license (if verified) " \
                            "is permanent and unaffected."
                    else:
                        s.supporter = STATE_SUPPORTER_ACTIVE
                        s.supporter_badge = "Supporter ♥"
                except ValueError:
                    s.supporter = STATE_SUPPORTER_ACTIVE
                    s.supporter_badge = "Supporter ♥"
            else:
                s.supporter = STATE_SUPPORTER_ACTIVE
                s.supporter_badge = "Supporter ♥"
        elif sup.get("status") in ("cancelled", "expired"):
            s.supporter = STATE_SUPPORTER_EXPIRED
            if not s.detail:
                s.detail = ("supporter membership cancelled — Pro rights (if verified) "
                            "unaffected; projects untouched")
        return s


# ---------------------------------------------------------- feature gates ---
# The gating table is DATA, not scattered conditionals. The engine core library
# stays ungated (see LICENSING_ARCHITECTURE.md for the honest reason: local
# Python source cannot form real DRM, so Pro value = updates, services,
# convenience; enforcement is advisory at the GUI level and documented).
FEATURE_GATES: dict[str, str] = {
    # feature key -> required edition ("free"| "pro" etc.)
    "import": "free",
    "preview": "free",
    "single_map": "free",
    "multi_map": "pro",
    "advanced_dither": "pro",       # all 12 algorithms + metric modes beyond rgb
    "block_costs": "pro",
    "neighbor_optimize": "pro",
    "carpet_palettes": "pro",
    "solid_construction": "pro",
    "export_schem": "free",
    "export_other": "pro",          # litematic + vanilla structure NBT
    "batch_convert": "pro",
    "evolution_lab": "pro",
    "breeding": "pro",
    "visual_studio": "pro",
    "image_library": "pro",
    "benchmark": "pro",
    "engine_transfer": "pro",
    "project_save": "free",
    "project_export": "free",       # users can always export their own data
    "docs": "free",
}


def allows(status: Status, feature: str) -> bool:
    need = FEATURE_GATES.get(feature)
    if need is None:
        raise LicensingError(f"unknown feature gate {feature!r}")
    if need == "free":
        return True
    return bool(status.pro)


def edition_of(status: Status, feature: str) -> str:
    return FEATURE_GATES.get(feature, "free")
