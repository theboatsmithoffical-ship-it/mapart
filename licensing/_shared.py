"""Shared typed errors for the licensing package (no heavy imports)."""
from __future__ import annotations


class LicensingError(ValueError):
    """Typed, actionable licensing error (never a silent fallback)."""
