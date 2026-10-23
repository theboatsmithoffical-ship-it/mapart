"""Candidate genome schema and evolutionary operators.

A genome describes *parameters only* — never code — so packages exchange
data, not programs. Ids are content hashes; a mismatch on import is a hard
error (forge protection).
"""
from __future__ import annotations

import hashlib
import json
import random

from .dither import ALGORITHMS

GENOME_KEYS = ("algorithm", "strength", "bias", "serpentine", "gamma", "mapping")


def default_genome() -> dict:
    return {
        "algorithm": "floyd_steinberg",
        "strength": 1.0,
        "bias": 0,
        "serpentine": True,
        "gamma": 1.0,
        "mapping": "nearest",
    }


def sanitize(g) -> dict:
    """Validate a genome; returns a normalized copy. Raises ValueError."""
    if not isinstance(g, dict):
        raise ValueError("genome must be a JSON object")
    if set(g) != set(GENOME_KEYS):
        diff = sorted(set(g) ^ set(GENOME_KEYS))
        raise ValueError(f"genome keys may not change: mismatch on {diff}")
    out = dict(g)
    if out["algorithm"] not in ALGORITHMS:
        raise ValueError(f"unknown algorithm {out['algorithm']!r}")
    if out["mapping"] != "nearest":
        raise ValueError(f"unsupported mapping {out['mapping']!r}")
    if not isinstance(out["serpentine"], bool):
        raise ValueError("serpentine must be boolean")
    strength = float(out["strength"])
    if not 0.0 <= strength <= 2.0:
        raise ValueError("strength must be within [0, 2]")
    out["strength"] = round(strength, 6)
    bias = out["bias"]
    if not isinstance(bias, int) or not -64 <= bias <= 64:
        raise ValueError("bias must be an integer in [-64, 64]")
    gamma = float(out["gamma"])
    if not 0.25 <= gamma <= 2.5:
        raise ValueError("gamma must be within [0.25, 2.5]")
    out["gamma"] = round(gamma, 6)
    if isinstance(out["strength"], int):
        pass  # float() in normalization below
    out["strength"] = float(out["strength"])
    out["gamma"] = float(out["gamma"])
    out["bias"] = int(out["bias"])
    return out


def genome_id(g: dict) -> str:
    """Stable content id (first 16 hex of sha256 of the canonical genome)."""
    payload = json.dumps(sanitize(g), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def random_genome(rng: random.Random) -> dict:
    return sanitize({
        "algorithm": rng.choice(ALGORITHMS),
        "strength": round(rng.uniform(0.3, 1.8), 4),
        "bias": rng.randint(-24, 24),
        "serpentine": bool(rng.getrandbits(1)),
        "gamma": round(rng.uniform(0.8, 1.4), 4),
        "mapping": "nearest",
    })


def mutate(g: dict, rng: random.Random) -> dict:
    n = dict(g)
    field = rng.choice(tuple(GENOME_KEYS))
    if field == "algorithm":
        alts = [a for a in ALGORITHMS if a != n["algorithm"]]
        n["algorithm"] = rng.choice(alts)
    elif field == "strength":
        n["strength"] = float(n["strength"]) + rng.uniform(-0.4, 0.4)
    elif field == "bias":
        n["bias"] = int(n["bias"]) + rng.randint(-12, 12)
    elif field == "serpentine":
        n["serpentine"] = not n["serpentine"]
    elif field == "gamma":
        n["gamma"] = float(n["gamma"]) * rng.uniform(0.8, 1.25)
    else:  # mapping is fixed; nudge strength instead
        n["strength"] = float(n["strength"]) + rng.uniform(-0.2, 0.2)
    return sanitize(n)


def crossover(a: dict, b: dict, rng: random.Random) -> dict:
    child = {k: (a[k] if rng.getrandbits(1) else b[k]) for k in GENOME_KEYS}
    return sanitize(child)
