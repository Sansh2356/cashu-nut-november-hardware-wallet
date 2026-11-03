"""Minimal secp256k1 point arithmetic in pure Python.

Used for Cashu blinding (NUT-00): hash_to_curve, B_ = Y + rG, C = C_ - rK.
It is deliberately dependency-free so it runs unchanged on a Pi Zero (ARMv6).
It is NOT constant-time. That is acceptable for blinding factors in a prototype,
but signing keys never touch this module: Schnorr signing goes through embit's
libsecp256k1 bindings (see crypto.py).
"""

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)
INF = None  # point at infinity


def is_on_curve(pt) -> bool:
    if pt is INF:
        return True
    x, y = pt
    return (y * y - x * x * x - 7) % P == 0


def add(p1, p2):
    if p1 is INF:
        return p2
    if p2 is INF:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return INF
        lam = (3 * x1 * x1) * pow(2 * y1, -1, P) % P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (lam * lam - x1 - x2) % P
    y3 = (lam * (x1 - x3) - y1) % P
    return (x3, y3)


def neg(pt):
    if pt is INF:
        return INF
    return (pt[0], (-pt[1]) % P)


def mul(k: int, pt=G):
    k %= N
    result = INF
    addend = pt
    while k:
        if k & 1:
            result = add(result, addend)
        addend = add(addend, addend)
        k >>= 1
    return result


def parse(sec: bytes):
    """Parse a 33-byte compressed point. Raises ValueError if invalid."""
    if len(sec) != 33 or sec[0] not in (2, 3):
        raise ValueError("expected 33-byte compressed point")
    x = int.from_bytes(sec[1:], "big")
    if x >= P:
        raise ValueError("x out of range")
    y2 = (pow(x, 3, P) + 7) % P
    y = pow(y2, (P + 1) // 4, P)
    if y * y % P != y2:
        raise ValueError("x is not on the curve")
    if (y & 1) != (sec[0] & 1):
        y = P - y
    return (x, y)


def serialize(pt) -> bytes:
    if pt is INF:
        raise ValueError("cannot serialize point at infinity")
    x, y = pt
    return bytes([2 + (y & 1)]) + x.to_bytes(32, "big")
