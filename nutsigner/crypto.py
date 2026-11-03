"""Cashu crypto helpers: NUT-00 blinding, NUT-10/11 P2PK secrets, SIG_ALL signing.

Point math lives in curve.py (pure Python). Schnorr (BIP-340) signing and
verification use embit, which wraps libsecp256k1 and ships a build for the
Pi Zero's armv6l CPU.
"""

import hashlib
import json
import os

from embit import ec

from . import curve

DOMAIN_SEPARATOR = b"Secp256k1_HashToCurve_Cashu_"


# ---------------------------------------------------------------- NUT-00

def hash_to_curve(message: bytes):
    """NUT-00 hash_to_curve. Returns a curve point (x, y)."""
    msg_hash = hashlib.sha256(DOMAIN_SEPARATOR + message).digest()
    for counter in range(2**16):
        h = hashlib.sha256(msg_hash + counter.to_bytes(4, "little")).digest()
        try:
            return curve.parse(b"\x02" + h)
        except ValueError:
            continue
    raise ValueError("no valid point found")


def random_scalar() -> int:
    while True:
        k = int.from_bytes(os.urandom(32), "big")
        if 0 < k < curve.N:
            return k


def blind(secret: str, r: int) -> str:
    """B_ = hash_to_curve(secret) + r*G, as a hex compressed point."""
    Y = hash_to_curve(secret.encode())
    return curve.serialize(curve.add(Y, curve.mul(r))).hex()


def blind_bytes(x: bytes, r: int) -> str:
    """Same as blind() but for raw byte secrets (used by the spec test vectors)."""
    Y = hash_to_curve(x)
    return curve.serialize(curve.add(Y, curve.mul(r))).hex()


def unblind(C_hex: str, r: int, K_hex: str) -> str:
    """C = C_ - r*K, where K is the mint's public key for this amount."""
    C_ = curve.parse(bytes.fromhex(C_hex))
    K = curve.parse(bytes.fromhex(K_hex))
    return curve.serialize(curve.add(C_, curve.neg(curve.mul(r, K)))).hex()


# ---------------------------------------------------------------- NUT-10/11

def make_p2pk_secret(pubkey_hex: str, sigflag: str = "SIG_ALL", extra_tags=None) -> str:
    """Build a NUT-10 P2PK secret string locked to pubkey_hex."""
    tags = [["sigflag", sigflag]]
    if extra_tags:
        tags.extend(extra_tags)
    body = {"nonce": os.urandom(32).hex(), "data": pubkey_hex, "tags": tags}
    return json.dumps(["P2PK", body], separators=(",", ":"))


def parse_secret(secret: str):
    """Return (kind, data, tags) for a NUT-10 secret, or None for a plain secret."""
    try:
        obj = json.loads(secret)
    except (ValueError, TypeError):
        return None
    if not (isinstance(obj, list) and len(obj) == 2 and isinstance(obj[1], dict)):
        return None
    body = obj[1]
    return obj[0], body.get("data"), body.get("tags") or []


def get_tag(tags, key):
    for t in tags:
        if t and t[0] == key:
            return t[1:]
    return None


def xonly(pubkey_hex: str) -> str:
    """Lowercase x-coordinate, used to compare keys per NUT-11 canonicalisation."""
    return pubkey_hex.lower()[2:]


def sig_all_message(inputs, outputs, quote_id: str = None) -> str:
    """NUT-11 SIG_ALL message for a swap (or a melt when quote_id is given).

    msg = secret_0 || C_0 || ... || amount_0 || B_0 || ... [|| quote_id]
    """
    parts = []
    for p in inputs:
        parts.append(p["secret"])
        parts.append(p["C"])
    for o in outputs:
        parts.append(str(int(o["amount"])))
        parts.append(o["B_"])
    if quote_id is not None:
        parts.append(quote_id)
    return "".join(parts)


def message_hash(msg: str) -> bytes:
    return hashlib.sha256(msg.encode("utf-8")).digest()


def schnorr_sign(privkey: ec.PrivateKey, msg: str) -> str:
    return privkey.schnorr_sign(message_hash(msg)).serialize().hex()


def schnorr_verify(pubkey_hex: str, msg: str, sig_hex: str) -> bool:
    try:
        pub = ec.PublicKey.parse(bytes.fromhex(pubkey_hex))
        sig = ec.SchnorrSig.parse(bytes.fromhex(sig_hex))
        return pub.schnorr_verify(sig, message_hash(msg))
    except Exception:
        return False
