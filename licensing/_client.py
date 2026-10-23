"""Desktop-side purchase verification client (HTTP to the creator-run service).

The app NEVER holds BMC tokens or signing keys. It only needs the verifier's
URL (and, during the unconfigured first-install period, honest error messages).
Config resolution order for each key:
  1. environment override (FIREFLY_VERIFIER_URL, FIREFLY_BUY_URL,
     FIREFLY_SUPPORT_URL),
  2. `mapart-data/config/licensing.json` (plain config, no secrets),
  3. missing -> the GUI reports "integration not configured" instead of faking.

Provider-unavailable handling encodes the spec exactly:
- HTTP failure / outage -> PENDING state; last verified entitlement keeps the
  offline grace window; projects are never touched.
- verifier 404 -> VERIFICATION_FAILED with the service's actionable message.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from ._shared import LicensingError

DEFAULT_TIMEOUT = 15.0


class VerifierClient:
    def __init__(self, base_url: str | None, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = float(timeout)

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        if not self.base_url:
            raise NotConfiguredError()
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return resp.status, body
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except ValueError:
                body = {"error": f"verifier answered HTTP {e.code} without JSON"}
            return e.code, body
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise UnreachableError(str(e)) from e

    def activate(self, purchase_ref: str, ref_kind: str, product: str,
                 device_id: str, device_name: str) -> dict:
        code, body = self._post("activate", {
            "purchase_ref": purchase_ref, "ref_kind": ref_kind,
            "product": product, "device_id": device_id,
            "device_name": device_name})
        return {"http": code, **body}

    def recheck(self, purchase_ref: str, ref_kind: str, device_id: str) -> dict:
        code, body = self._post("recheck", {
            "purchase_ref": purchase_ref, "ref_kind": ref_kind,
            "device_id": device_id})
        return {"http": code, **body}

    def deactivate(self, purchase_ref: str, ref_kind: str, device_id: str) -> dict:
        code, body = self._post("deactivate", {
            "purchase_ref": purchase_ref, "ref_kind": ref_kind,
            "device_id": device_id})
        return {"http": code, **body}


class NotConfiguredError(LicensingError):
    def __init__(self):
        super().__init__("license verifier not configured — see Settings > Purchase & "
                         "Support for instructions (no fake activation happens)")


class UnreachableError(LicensingError):
    """Network-layer failure only; the entitlement just stays at its last legal state."""
