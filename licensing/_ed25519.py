"""Vendored Ed25519 (RFC 8032) — pure Python, stdlib-only, no dependencies.

Adapted from the public-domain Ed25519 design (D. J. Bernstein et al.).
Vendored because desktop use must verify the creator's signatures OFFLINE
without a binary dependency (Python's stdlib has no Ed25519).

Arithmetic implemented in TIGHTLY AFFINE coordinates on the twisted Edwards
curve -x^2 + y^2 = 1 + d x^2 y^2, d = -121665/121666 (values mod p) with the
complete unified addition law
    x3 = (x1 y2 + x2 y1) / (1 + d x1 x2 y1 y2)
    y3 = (y1 y2 + x1 x2) / (1 - d x1 x2 y1 y2)
  TWO modular inversions per point operation — deliberately simple and
  exactly the equation pair; two inverses/operation cost ~0.5 s per 251-bit
  scalar multiplication, which is entirely adequate for license messages.
  DO NOT optimize to projective/extended formulas without re-pinning the
  RFC 8032 TEST 1 vector in tests/test_licensing.py (a previous projective
  rewrite here ERRONEOUSLY produced valid-looking but wrong points).

Wire format (RFC 8032): compressed point = y (255 bits) | parity(x) << 255,
little-endian 32 bytes; x recovered from x^2 = (y^2 - 1)/(1 + d y^2).

public_key/sign/verify implement RFC 8032 Ed25519 exactly; the sign path
derivation was verified byte-for-byte against the published TEST 1 vector.
"""
from __future__ import annotations

import hashlib

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493

_I = pow(2, (P - 1) // 4, P)                    # I^2 = -1 (mod p)
_D = (-121665 * pow(121666, P - 2, P)) % P      # authoritative curve parameter
IDENT = (0 % P, 1 % P, 1 % P)

_INV_CACHE: dict[int, int] = {}


def _inv(x: int) -> int:
    v = _INV_CACHE.get(x)
    if v is None:
        v = pow(x, P - 2, P)
        if len(_INV_CACHE) > 64:
            _INV_CACHE.clear()
        _INV_CACHE[x] = v
    return v


def _sqrt(xx: int) -> int | None:
    """Square root mod p (None if xx is a non-residue). Validated input."""
    x = pow(xx, (P + 3) // 8, P)
    if (x * x - xx) % P == 0:
        return x
    x2 = (x * _I) % P
    if (x2 * x2 - xx) % P == 0:
        return x2
    return None


def _add(p1, p2):
    """Affine unified addition law (see module docstring). Returns (x, y, 1)."""
    x1, y1, _ = p1
    x2, y2, _ = p2
    nx = (x1 * y2 + x2 * y1) % P
    ny = (y1 * y2 + x1 * x2) % P
    c = (x1 * x2 % P) * (y1 * y2 % P) % P
    x3 = nx * _inv((1 + _D * c) % P) % P
    y3 = ny * _inv((1 - _D * c) % P) % P
    return (x3, y3, 1 % P)


def _mul(s: int, p0):
    q = IDENT
    while s > 0:
        if s & 1:
            q = _add(q, p0)
        p0 = _add(p0, p0)
        s >>= 1
    return q


_BY = (4 * _inv(5)) % P
_BX2 = ((_BY * _BY - 1) % P) * _inv((1 + _D * _BY * _BY) % P) % P   # x^2
_BX = _sqrt(_BX2)
assert _BX is not None
# RFC-pinned canonical base representative has EVEN x parity (verified:
# clamp(sha512(TEST1 seed)) * B encodes to the published public key).
if _BX % 2 != 0:
    _BX = (P - _BX) % P
B = (_BX, _BY, 1 % P)
assert ((-_BX * _BX % P) + _BY * _BY - 1 - _D * _BX * _BX % P * (_BY * _BY % P)) % P == 0


def _compress(p) -> bytes:
    x, y, _ = p
    return int.to_bytes((y % P) | ((x % P & 1) << 255), 32, "little")


def _decompress(b: bytes):
    v = int.from_bytes(b, "little")
    y = v & ((1 << 255) - 1)
    par = v >> 255
    xx = ((y * y - 1) % P) * _inv((1 + _D * y * y) % P) % P
    x = _sqrt(xx)
    if x is None:
        raise ValueError("invalid point encoding")
    if (x & 1) != par:
        x = P - x
    # true-curve membership: -x^2 + y^2 - 1 - d x^2 y^2 == 0 (mod p)
    x2 = x * x % P
    y2 = y * y % P
    if (-x2 + y2 - 1 - _D * x2 * y2) % P != 0:
        raise ValueError("invalid point encoding")
    return (x % P, y % P, 1 % P)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _clamp_scalar(h32: bytes) -> int:
    # RFC 8032 §5.1.2: clear the top bit and low 3 bits, set bit 254
    a = int.from_bytes(h32, "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a


def public_key(seed: bytes) -> bytes:
    return _compress(_mul(_clamp_scalar(_sha512(seed)[:32]), B))


def sign(seed: bytes, msg: bytes) -> bytes:
    h = _sha512(seed)
    a = _clamp_scalar(h[:32])
    pub = _compress(_mul(a, B))
    r = int.from_bytes(_sha512(h[32:] + msg), "little") % L
    rp = _compress(_mul(r, B))
    k = int.from_bytes(_sha512(rp + pub + msg), "little") % L
    s = (r + k * a) % L
    return rp + int.to_bytes(s, 32, "little")


def verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    if len(pub) != 32 or len(sig) != 64:
        return False
    try:
        a_point = _decompress(pub)
        _decompress(sig[:32])          # R must be a valid curve point too
    except ValueError:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= L:                          # RFC 8032 §5.1.7: reject s >= L
        return False
    k = int.from_bytes(_sha512(sig[:32] + pub + msg), "little") % L
    sb = _mul(s, B)                      # s*B
    ka = _mul(k % L, a_point)            # k*A
    # s*B - k*A: negate A-scalar product on the twisted curve = (-x, y)
    neg_ka = ((P - ka[0]) % P, ka[1], 1 % P)
    return _compress(_add(sb, neg_ka)) == sig[:32]
