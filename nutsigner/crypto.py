"""Cashu crypto helpers: NUT-00 blinding, NUT-10/11 P2PK secrets, SIG_ALL signing.

Blinding and Schnorr (BIP-340) both use embit's libsecp256k1 bindings, which
ship a build for the Pi Zero's armv6l CPU. If the compiled library can't load,
embit falls back to pure Python, but that fallback lacks the point operations
blinding needs, so blinding then falls back to curve.py (FAST is False).
"""

import hashlib
import json
import os

from embit import ec
from embit.util import secp256k1 as secp

from . import curve

DOMAIN_SEPARATOR = b"Secp256k1_HashToCurve_Cashu_"

# True when embit loaded compiled libsecp256k1 with the point operations we use.
FAST = all(hasattr(secp, f) for f in ("ec_pubkey_parse", "ec_pubkey_serialize", "ec_pubkey_create",
                                      "ec_pubkey_combine", "ec_pubkey_negate", "ec_pubkey_tweak_mul"))


# ---------------------------------------------------------------- NUT-00

def _hash_to_curve_sec(message: bytes) -> bytes:
    """NUT-00 hash_to_curve as a 33-byte compressed point."""
    msg_hash = hashlib.sha256(DOMAIN_SEPARATOR + message).digest()
    for counter in range(2**16):
        sec = b"\x02" + hashlib.sha256(msg_hash + counter.to_bytes(4, "little")).digest()
        try:
            secp.ec_pubkey_parse(sec) if FAST else curve.parse(sec)
            return sec
        except ValueError:
            continue
    raise ValueError("no valid point found")


def hash_to_curve(message: bytes):
    """NUT-00 hash_to_curve. Returns a curve point (x, y)."""
    return curve.parse(_hash_to_curve_sec(message))


def random_scalar() -> int:
    while True:
        k = int.from_bytes(os.urandom(32), "big")
        if 0 < k < curve.N:
            return k


def blind_bytes(x: bytes, r: int) -> str:
    """B_ = hash_to_curve(x) + r*G, as a hex compressed point."""
    Y = _hash_to_curve_sec(x)
    if not FAST:
        return curve.serialize(curve.add(curve.parse(Y), curve.mul(r))).hex()
    rG = secp.ec_pubkey_create(r.to_bytes(32, "big"))
    return secp.ec_pubkey_serialize(secp.ec_pubkey_combine(secp.ec_pubkey_parse(Y), rG)).hex()


def blind(secret: str, r: int) -> str:
    return blind_bytes(secret.encode(), r)


def unblind(C_hex: str, r: int, K_hex: str) -> str:
    """C = C_ - r*K, where K is the mint's public key for this amount."""
    if not FAST:
        C_ = curve.parse(bytes.fromhex(C_hex))
        K = curve.parse(bytes.fromhex(K_hex))
        return curve.serialize(curve.add(C_, curve.neg(curve.mul(r, K)))).hex()
    rK = secp.ec_pubkey_parse(bytes.fromhex(K_hex))  # fresh buffer: tweak_mul edits it in place
    secp.ec_pubkey_tweak_mul(rK, r.to_bytes(32, "big"))
    C_ = secp.ec_pubkey_parse(bytes.fromhex(C_hex))
    return secp.ec_pubkey_serialize(secp.ec_pubkey_combine(C_, secp.ec_pubkey_negate(rK))).hex()


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
