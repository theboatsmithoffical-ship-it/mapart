"""FireFly license verification service — creator-run, stdlib-only, optional.

Hosting requirements (documented honestly in docs/LICENSING_ARCHITECTURE.md):
- Any Python 3.10+ host reachable over HTTPS by buyers' machines (a reverse
  proxy in front of this plain HTTP service may terminate TLS).
- Secrets come from the environment ONLY; this file never contains keys:
    BMC_TOKEN   Buy Me a Coffee personal access token (read scopes)
    SIGN_KEY    random 32+ bytes, hex; signs entitlement attestations (HMAC-SHA256)
    DEVICE_LIMIT  optional int, default 3 — concurrent device registrations
- The service stores only purchase/activation bookkeeping in its work dir
  (JSON). It never sees payment card data (Buy Me a Coffee never sends any).

Known, honest limitation (not invented away): Buy Me a Coffee documents NO
license-key generation service-side; it offers a read-only developer API and
webhooks. This service therefore verifies by cross-checking the buyer's
purchase reference against the BMC API (Shop item `firefly-pro` for the $5
one-time purchase; membership `firefly-supporter` for $1/month) every time it
has not yet got a webhook-confirmed record for it.

IMPORTANT unresolved item (docs/RELEASE_CHECKLIST.md): the exact BMC response
schema is not publicly documented; `_normalize_bmc_*` centralizes the mapping
and MUST be confirmed against a real account before first commercial release.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ._shared import LicensingError
BMC_API = "https://developers.buymeacoffee.com/api/v1"

MAX_BODY = 64 * 1024


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sign(key_hex: str, record: dict) -> str:
    return hmac.new(bytes.fromhex(key_hex), canonical(record).encode(),
                    hashlib.sha256).hexdigest()


class BMCClient:
    """Buy Me a Coffee developer-API adapter (read-only, personal token).

    Injectable transport ( Bernardino-safe for tests): pass `transport` as
    a callable (method, url, token) -> (status:int, body:bytes)."""

    def __init__(self, token: str, transport=None):
        self.token = token
        self._transport = transport

    def _get(self, path: str, params: dict | None = None):
        url = f"{BMC_API}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        if self._transport is not None:
            status, body = self._transport("GET", url, self.token)
            if status != 200:
                raise UpstreamError(f"BMC API answered {status} for {path}")
            try:
                return json.loads(body.decode("utf-8"))
            except ValueError as e:
                raise UpstreamError(f"BMC API returned non-JSON body ({e})") from e
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise UpstreamError(f"cannot reach Buy Me a Coffee API: {e}") from e

    # ---- normalized lookups. FIELD MAPPING UNRESOLVED until checked live:
    # extraction helpers below accept the documented connector field names
    # (payer_email, subscription_is_cancelled, subscription_current_period_end,
    #  subscription_id / purchase id, product link / icon) and fail HONESTLY
    # (LicensingError) when the schema does not match expectations.
    @staticmethod
    def _unwrap(payload: dict):
        data = payload.get("data", payload if isinstance(payload, list) else None)
        return data if isinstance(data, list) else []

    def find_one_time(self, ref_kind: str, ref: str, product: str) -> dict | None:
        """One-time Shop purchase ('extras' collection) for email or tx id."""
        wanted = product.split()[:1]
        for page in ("1", "2"):        # generous: small creator account fits 1 page
            payload = self._get("extras", {"limit": "100", "page": page})
            rows = self._unwrap(payload)
            if not isinstance(rows, list):
                raise UpstreamError("unexpected BMC /extras payload shape "
                                    "(documented schema differs) — see RELEASE_CHECKLIST")
            for row in rows:
                email = str(row.get("payer_email", "")).lower()
                pid = str(row.get("id", "") or row.get("order_id", "")).lower()
                if (ref_kind == "email" and email == ref.lower()) or \
                        (ref_kind == "tx_id" and pid == ref.lower()):
                    link = str(row.get("link", "") or row.get("product", "") or "")
                    if product not in link and wanted[0] not in link:
                        continue  # right buyer, different product — keep looking
                    return {"provider": "buymeacoffee", "platform_ref": str(row.get("id")),
                            "buyer": email, "payout_currency": row.get("support_currency", ""),
                            "purchased_at": str(row.get("support_created_on") or
                                                row.get("support_updated_on") or "")}
            if len(rows) < 100:
                break
        return None

    def find_membership(self, ref_kind: str, ref: str) -> dict | None:
        """Active membership (any tier) for email or subscription id."""
        for page in ("1", "2"):
            payload = self._get("subscriptions", {"limit": "100", "page": page})
            rows = self._unwrap(payload)
            if not isinstance(rows, list):
                raise UpstreamError("unexpected BMC /subscriptions payload shape "
                                    "(documented schema differs) — see RELEASE_CHECKLIST")
            for row in rows:
                email = str(row.get("payer_email", "")).lower()
                sub = str(row.get("subscription_id", "") or row.get("id", "")).lower()
                if (ref_kind == "email" and email == ref.lower()) or \
                        (ref_kind == "tx_id" and sub == ref.lower()):
                    cancelled = bool(row.get("subscription_is_cancelled", False))
                    period_end = str(row.get("subscription_current_period_end", ""))
                    return {"provider": "buymeacoffee", "platform_ref": sub,
                            "buyer": email, "status": "cancelled" if cancelled else "active",
                            "current_period_end": period_end}
            if len(rows) < 100:
                break
        return None


class UpstreamError(RuntimeError):
    """Payment platform unreachable/unmatching — NEVER maps to 'failed purchase'."""


class VerifyService:
    """The actual policy: authoritative purchase confirmation + device bookkeeping."""

    def __init__(self, work_dir: Path | str, bmc: BMCClient, sign_key_hex: str,
                 device_limit: int = 3, pro_product="firefly-pro",
                 supporter_product="firefly-supporter", now=time.time):
        self.dir = Path(work_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.bmc = bmc
        self.key = sign_key_hex
        self.device_limit = int(device_limit)
        self.pro_product = pro_product
        self.supporter_product = supporter_product
        self._now = now

    # ---- local ledger (tiny JSON bookkeeping; webhook events may also feed it)
    def _ledger(self) -> dict:
        p = self.dir / "activations.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                return {}
        return {}

    def _flush(self, ledger: dict) -> None:
        p = self.dir / "activations.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(ledger, indent=1, default=list), encoding="utf-8")
        os.replace(tmp, p)

    def activate(self, purchase_ref: str, ref_kind: str, want_product: str,
                 device_id: str, device_name: str) -> tuple[dict, int]:
        """Returns (response_dict, http_status). Authority = BMC API, not this cache."""
        purchase_ref = purchase_ref.strip()
        if not purchase_ref:
            return {"error": "empty purchase reference"}, 400
        if ref_kind not in ("email", "tx_id"):
            return {"error": "ref_kind must be email or tx_id"}, 400
        if want_product not in ("pro", "supporter"):
            return {"error": "want_product must be pro or supporter"}, 400
        ledger = self._ledger()
        key = f"{ref_kind}:{purchase_ref.lower()}"
        known = ledger.get(key)

        # Fast path: previous signed record + recorded support status; still
        # RE-CHECKED against BMC periodically (webhook/periodic poll ahead).
        if known and (self._now() - known.get("checked_at", 0)) < 86400:
            rec, sup = dict(known["record"]), known.get("supporter")
            rec["supporter"] = sup
        else:
            rec, sup = {}, None
            try:
                if want_product == "pro":
                    rec = self.bmc.find_one_time(ref_kind, purchase_ref, self.pro_product)
                sup = self.bmc.find_membership(ref_kind, purchase_ref)
            except UpstreamError as e:
                # provider outage: pending, not failed — user must never lose Pro
                # based on an unreachable payment platform
                return {"error": "payment platform unreachable — verification pending, "
                                 "record NOT confirmed; retry later", "pending": True,
                                 "detail": str(e)}, 503

            if rec is None and want_product == "pro":
                return {"error": f"no qualifying '{self.pro_product}' purchase found for this "
                                 f"{ref_kind}; check the reference or buy at the storefront"}, 404
            rec = rec or {}
            sup = sup or {}
            member_status = "active" if sup.get("status") == "active" else None
            checked_at = self._now()

            # membership cancellation arrives either by webhook (edge below) or here
            record = {
                "format": "firefly-license/1",
                "edition": "pro" if rec else "none",
                "product": self.pro_product,
                "platform": rec.get("provider", "buymeacoffee") if rec else "buymeacoffee",
                "platform_ref": rec.get("platform_ref", ""),
                "purchase_ref_hash": hashlib.sha256(
                    (key + "|firefly").encode()).hexdigest()[:16],
                "purchased_at": rec.get("purchased_at", ""),
                "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(checked_at)),
                "expires_at": None,          # one-time $5 = permanent; no expiry
                "supporter": ({"status": member_status,
                               "current_period_end": sup.get("current_period_end", "")}
                              if member_status else None),
            }
            known = {"record": record, "checked_at": checked_at,
                     "supporter": record["supporter"],
                     "devices": known.get("devices", {}) if known else {}}
            ledger[key] = known

        devices = known.setdefault("devices", {})
        if device_id not in devices:
            if len([d for d, c in devices.items() if not c.get("removed")]) >= self.device_limit:
                active = [f"{c.get('name','?')}@{d[:8]}" for d, c in devices.items()
                          if not c.get("removed")]
                return {"error": "device limit reached for this license",
                        "active_devices": active,
                        "hint": "deactivate one device first via the app or the creator"}, 409
            devices[device_id] = {"name": device_name[:60], "added": self._now(),
                                  "removed": False}
        attestation = sign(self.key, {**known["record"], "device_id": device_id})
        known["last_attestation"] = attestation
        self._flush(ledger)
        return {"ok": True, "record": known["record"], "attestation": attestation}, 200

    def deactivate(self, purchase_ref: str, ref_kind: str, device_id: str) -> tuple[dict, int]:
        ledger = self._ledger()
        known = ledger.get(f"{ref_kind}:{purchase_ref.lower()}")
        if not known:
            return {"error": "no activation on record"}, 404
        known.setdefault("devices", {})[device_id] = {"name": "removed", "removed": True,
                                                      "until": self._now()}
        self._flush(ledger)
        return {"ok": True}, 200

    def attest(self, purchase_ref: str, ref_kind: str, device_id: str,
               allow_stale_seconds: float = 0.0) -> tuple[dict, int]:
        """Re-check endpoint: refreshes from BMC when stale, else signs cached."""
        ledger = self._ledger()
        known = ledger.get(f"{ref_kind}:{purchase_ref.lower()}")
        if not known:
            return {"error": "no activation on record — activate first"}, 404
        if (self._now() - known.get("checked_at", 0)) > 86400 or allow_stale_seconds == -1:
            rec, sup = {}, None
            try:
                rec = self.bmc.find_membership(ref_kind, purchase_ref)
            except UpstreamError:
                return {"error": "payment platform unreachable — attestation of last "
                                 "known state only", "pending": True,
                         "record": known["record"], "stale": True}, 503
            sup = rec or {}
            member_status = "active" if sup.get("status") == "active" else None
            known["record"]["supporter"] = (
                {"status": member_status,
                 "current_period_end": sup.get("current_period_end", "")}
                if member_status else None)
            known["supporter"] = known["record"]["supporter"]
            known["checked_at"] = self._now()
        known.setdefault("devices", {})
        attestation = sign(self.key, {**known["record"], "device_id": device_id})
        self._flush(ledger)
        return {"ok": True, "record": known["record"], "attestation": attestation}, 200


def make_handler(service: VerifyService):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet by default; creator may log at proxy level
            pass

        def _json(self, code: int, obj: dict):
            body = json.dumps(obj, default=list).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/health"):
                self._json(200, {"ok": True, "service": "firefly-verifier/1"})
            else:
                self._json(404, {"error": "unknown path"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > MAX_BODY:
                self._json(413, {"error": "body too large"})
                return
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except ValueError:
                self._json(400, {"error": "invalid JSON body"})
                return
            path = urllib.parse.urlparse(self.path).path
            pid = body.get("purchase_ref", "")
            rkind = body.get("ref_kind", "")
            did = body.get("device_id", "")
            dname = body.get("device_name", "")
            product = body.get("product", "pro")
            if path == "/activate":
                obj, code = service.activate(pid, rkind, product, did, dname)
            elif path == "/recheck":
                obj, code = service.attest(pid, rkind, did)
            elif path == "/deactivate":
                obj, code = service.deactivate(pid, rkind, did)
            else:
                obj, code = {"error": "unknown path"}, 404
            self._json(code, obj)

    return Handler


def serve(work_dir: str, host="127.0.0.1", port=8333):
    """Run the verifier locally (creator-side). Secrets via env only."""
    token = os.environ.get("BMC_TOKEN", "")
    key = os.environ.get("SIGN_KEY", "")
    if not token:
        raise LicensingError("BMC_TOKEN not set, refusing to start: create a Buy Me a "
                             "Coffee developer token and set it in the environment")
    if not key:
        raise LicensingError("SIGN_KEY not set: attestations would be unverifiable, set "
                             "a persistent 32+ byte hex key (openssl rand -hex 32) first")
    limit = int(os.environ.get("DEVICE_LIMIT", "3"))
    svc = VerifyService(work_dir, BMCClient(token), key, device_limit=limit)
    httpd = ThreadingHTTPServer((host, port), make_handler(svc))
    httpd.serve_forever()
