"""Ed25519 signature verification in plain Python (RFC 8032, section 5.1.7).

`volunteer.py` prefers the `cryptography` package for checking the signature
on a release manifest.  This module is the fallback for a build that does not
carry it, so that a missing wheel never turns into "install without checking".
It only *verifies*: there is no signing and no key generation here, so nothing
in it handles a secret, and its speed (a few milliseconds of big-integer
arithmetic, once per update) and lack of constant-time behaviour do not matter.

It follows the RFC's reference code: points in extended homogeneous
coordinates, a strict decode of the public key and of R, and the check that
S is below the group order (so a signature cannot be malleated by adding L).
The cofactorless equation [S]B = R + [h]A is used, as the RFC allows.
"""

from __future__ import annotations

import hashlib

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)
_GY = 4 * pow(5, _P - 2, _P) % _P

Point = tuple[int, int, int, int]


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GX = _recover_x(_GY, 0)
_G: Point = (_GX, _GY, 1, _GX * _GY % _P)          # extended coordinates (X, Y, Z, T)
_ZERO: Point = (0, 1, 1, 0)


def _add(p: Point, q: Point) -> Point:
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(scalar: int, point: Point) -> Point:
    result = _ZERO
    while scalar > 0:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _equal(p: Point, q: Point) -> bool:
    return ((p[0] * q[2] - q[0] * p[2]) % _P == 0
            and (p[1] * q[2] - q[1] * p[2]) % _P == 0)


def _decode(data: bytes) -> Point | None:
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """True only for a valid signature of `message` by `public_key`."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a_point = _decode(public_key)
    r_point = _decode(signature[:32])
    if a_point is None or r_point is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(),
                       "little") % _L
    return _equal(_mul(s, _G), _add(r_point, _mul(h, a_point)))
