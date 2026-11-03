"""Compact binary format (v2) for requests and signatures, sized for QR codes.

Request v2:
  "NS" | version 2 | type (1 = swap)
  mint url, unit                         (length + bytes each)
  keyset table   count, then each id     (length + bytes)   <- listed once
  inputs         count, then each: amount, keyset index, secret (length + bytes), C (33 bytes)
  outputs        count, then each: amount, keyset index, B_ (33 bytes)
  reveals        count, then each: output index, secret (length + bytes), r (32 bytes)

Amounts, counts, lengths and indexes are unsigned LEB128 varints.

A secret is one kind byte, then:
  0  raw UTF-8 (length + bytes)
  1  32 bytes, rendered as 64 lowercase hex chars        (random bearer secret)
  2  P2PK: nonce (32) | pubkey (33) | tags JSON (length + bytes), rendered as
     ["P2PK",{"nonce":..,"data":..,"tags":..}] with no spaces
The mint hashes the exact secret string, so the encoder only picks kind 1 or 2
when rendering it back gives the identical string; otherwise it uses kind 0.
`decode_request()` returns the same v1 dict that `device.review()` accepts.

Signature v2: "NS" | version 2 | pubkey (33) | message hash (32) | signature (64)
= 132 bytes.

Runs on the device, so the decoders treat every byte as hostile: anything
malformed raises WireError (a ValueError), never anything else.
"""

import json

MAGIC = b"NS"
VERSION = 2
TYPE_SWAP = 1
SIGNATURE_LEN = 3 + 33 + 32 + 64
MAX_VARINT_BYTES = 10  # 64-bit values; refuse huge ints from hostile input


class WireError(ValueError):
    pass


# ------------------------------------------------------------------ encoding

def _uv(n: int) -> bytes:
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise WireError(f"cannot encode {n!r} as an unsigned varint")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _blob(b: bytes) -> bytes:
    return _uv(len(b)) + b


def _fixed(hexstr: str, n: int, what: str) -> bytes:
    try:
        b = bytes.fromhex(hexstr)
    except (TypeError, ValueError):
        raise WireError(f"{what} is not hex") from None
    if len(b) != n:
        raise WireError(f"{what} must be {n} bytes")
    return b


def _p2pk(nonce: bytes, pubkey: bytes, tags_json: str) -> str:
    return f'["P2PK",{{"nonce":"{nonce.hex()}","data":"{pubkey.hex()}","tags":{tags_json}}}]'


def _secret(s: str) -> bytes:
    try:
        if len(s) == 64 and bytes.fromhex(s).hex() == s:
            return b"\x01" + bytes.fromhex(s)
        kind, body = json.loads(s)
        nonce, pubkey = bytes.fromhex(body["nonce"]), bytes.fromhex(body["data"])
        tags = json.dumps(body["tags"], separators=(",", ":"))
        if kind == "P2PK" and len(nonce) == 32 and len(pubkey) == 33 and _p2pk(nonce, pubkey, tags) == s:
            return b"\x02" + nonce + pubkey + _blob(tags.encode())
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    return b"\x00" + _blob(s.encode())


def encode_request(req: dict) -> bytes:
    """v1 request dict -> v2 bytes."""
    if req.get("v") != 1 or req.get("type") != "swap":
        raise WireError("only v1 swap requests can be encoded")
    ids = []
    for item in req["inputs"] + req["outputs"]:
        if item["id"] not in ids:
            ids.append(item["id"])
    index = {kid: i for i, kid in enumerate(ids)}

    out = bytearray(MAGIC + bytes([VERSION, TYPE_SWAP]))
    out += _blob(req["mint"].encode()) + _blob(req["unit"].encode())
    out += _uv(len(ids))
    for kid in ids:
        try:
            out += _blob(bytes.fromhex(kid))
        except (TypeError, ValueError):
            raise WireError(f"keyset id {kid!r} is not hex") from None
    out += _uv(len(req["inputs"]))
    for p in req["inputs"]:
        out += _uv(p["amount"]) + _uv(index[p["id"]]) + _secret(p["secret"])
        out += _fixed(p["C"], 33, "input C")
    out += _uv(len(req["outputs"]))
    for o in req["outputs"]:
        out += _uv(o["amount"]) + _uv(index[o["id"]]) + _fixed(o["B_"], 33, "output B_")
    reveals = req.get("reveals") or []
    out += _uv(len(reveals))
    for rv in reveals:
        out += _uv(rv["index"]) + _secret(rv["secret"]) + _fixed(rv["r"].rjust(64, "0"), 32, "reveal r")
    return bytes(out)


def encode_signature(sig: dict) -> bytes:
    """Device signature dict -> 132 bytes."""
    return (MAGIC + bytes([VERSION]) + _fixed(sig["pubkey"], 33, "pubkey")
            + _fixed(sig["msg_hash"], 32, "msg_hash") + _fixed(sig["signature"], 64, "signature"))


# ------------------------------------------------------------------ decoding

class _Reader:
    def __init__(self, data: bytes):
        self.data = bytes(data)
        self.pos = 0

    def take(self, n: int) -> bytes:
        if n > len(self.data) - self.pos:
            raise WireError("truncated")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def uv(self) -> int:
        n = shift = 0
        for i in range(MAX_VARINT_BYTES):
            b = self.take(1)[0]
            n |= (b & 0x7F) << shift
            if not b & 0x80:
                if b == 0 and i > 0:
                    raise WireError("non-canonical varint")
                return n
            shift += 7
        raise WireError("varint too long")

    def blob(self) -> bytes:
        return self.take(self.uv())

    def text(self, what: str) -> str:
        try:
            return self.blob().decode("utf-8")
        except UnicodeDecodeError:
            raise WireError(f"{what} is not UTF-8") from None

    def secret(self) -> str:
        kind = self.take(1)[0]
        if kind == 0:
            return self.text("secret")
        if kind == 1:
            return self.take(32).hex()
        if kind == 2:
            nonce, pubkey = self.take(32), self.take(33)
            return _p2pk(nonce, pubkey, self.text("secret tags"))
        raise WireError("unknown secret kind")

    def index(self, size: int, what: str) -> int:
        i = self.uv()
        if i >= size:
            raise WireError(f"{what} out of range")
        return i


def decode_request(data: bytes) -> dict:
    """v2 bytes -> v1 request dict. Raises WireError on any malformed input."""
    r = _Reader(data)
    if r.take(2) != MAGIC or r.take(1)[0] != VERSION:
        raise WireError("not a NutSigner v2 message")
    if r.take(1)[0] != TYPE_SWAP:
        raise WireError("unsupported request type")
    req = {"v": 1, "type": "swap", "mint": r.text("mint"), "unit": r.text("unit")}
    ids = [r.blob().hex() for _ in range(r.uv())]
    req["inputs"] = [{"amount": r.uv(), "id": ids[r.index(len(ids), "keyset index")],
                      "secret": r.secret(), "C": r.take(33).hex()}
                     for _ in range(r.uv())]
    req["outputs"] = [{"amount": r.uv(), "id": ids[r.index(len(ids), "keyset index")],
                       "B_": r.take(33).hex()}
                      for _ in range(r.uv())]
    req["reveals"] = [{"index": r.uv(), "secret": r.secret(), "r": r.take(32).hex()}
                      for _ in range(r.uv())]
    if r.pos != len(r.data):
        raise WireError("trailing bytes")
    return req


def decode_signature(data: bytes) -> dict:
    data = bytes(data)
    if len(data) != SIGNATURE_LEN:
        raise WireError(f"signature must be {SIGNATURE_LEN} bytes")
    if data[:2] != MAGIC or data[2] != VERSION:
        raise WireError("not a NutSigner v2 signature")
    return {"v": 1, "pubkey": data[3:36].hex(), "msg_hash": data[36:68].hex(),
            "signature": data[68:].hex()}
