"""NutSigner device logic: review a signing request, then sign it.

This module is what runs on the Pi. It never talks to a mint and never sees
the network. It receives a request from the (untrusted) host, checks it, shows
the user what will happen, and returns one Schnorr signature over the whole
swap (NUT-11 SIG_ALL).

Request format (v1), sent by the host:

{
  "v": 1,
  "type": "swap",
  "mint": "https://testnut.cashu.space",
  "unit": "sat",
  "inputs":  [{"amount": 8, "id": "00..", "secret": "[\"P2PK\",...]", "C": "02.."}],
  "outputs": [{"amount": 4, "id": "00..", "B_": "03.."}],
  "reveals": [{"index": 1, "secret": "...", "r": "hex"}]   # optional, per output
}

A "reveal" lets the device prove what an output really is: it recomputes
B_ = hash_to_curve(secret) + r*G and checks it matches. Outputs without a
reveal are shown as UNVERIFIED, because the device cannot tell who owns them.
"""

from embit import bip32, bip39, ec

from . import crypto, curve

# Project-specific derivation path for the P2PK signing key. 129372 is the
# Cashu coin type used by NUT-13; the 11' branch (for NUT-11) keeps this key
# apart from NUT-13's deterministic secrets.
DERIVATION_PATH = "m/129372'/11'/0'/0/0"

SUPPORTED_TAGS = {"sigflag"}  # v1: plain SIG_ALL locks only


class Rejected(Exception):
    """The request failed a check. The device must not sign it."""


class DeviceSigner:
    def __init__(self, privkey: ec.PrivateKey, max_send: int = None):
        self.privkey = privkey
        self.pubkey_hex = privkey.get_public_key().sec().hex()
        self.max_send = max_send

    @classmethod
    def from_mnemonic(cls, mnemonic: str, passphrase: str = "", **kw):
        if not bip39.mnemonic_is_valid(mnemonic):
            raise ValueError("invalid BIP-39 mnemonic")
        return cls.from_seed_bytes(bip39.mnemonic_to_seed(mnemonic, passphrase), **kw)

    @classmethod
    def from_seed_bytes(cls, seed: bytes, **kw):
        """From a BIP-39 seed (e.g. SeedSigner's Seed.seed_bytes, passphrase included)."""
        return cls(bip32.HDKey.from_seed(seed).derive(DERIVATION_PATH).key, **kw)

    # ------------------------------------------------------------ review

    def review(self, req: dict) -> dict:
        """Check a request and describe it. Raises Rejected on any problem.

        Any unexpected error while parsing (wrong JSON types, missing fields)
        also becomes Rejected, so the device shows a rejection, not a crash.
        """
        try:
            return self._review(req)
        except Rejected:
            raise
        except Exception as e:
            raise Rejected(f"malformed request ({type(e).__name__})") from None

    def _review(self, req: dict) -> dict:
        if not isinstance(req, dict):
            raise Rejected("request is not a JSON object")
        if req.get("v") != 1:
            raise Rejected("unsupported request version")
        if req.get("type") != "swap":
            raise Rejected("only swap requests are supported in v1")

        inputs = req.get("inputs") or []
        outputs = req.get("outputs") or []
        if not inputs or not outputs:
            raise Rejected("request needs at least one input and one output")

        # --- inputs: all must be ours, all must carry identical SIG_ALL conditions
        first_tags = None
        in_total = 0
        for i, p in enumerate(inputs):
            parsed = crypto.parse_secret(p.get("secret", ""))
            if parsed is None or parsed[0] != "P2PK":
                raise Rejected(f"input {i} is not P2PK-locked")
            _, data, tags = parsed
            if not _valid_tags(tags):
                raise Rejected(f"input {i} has malformed tags")
            if not isinstance(data, str) or crypto.xonly(data) != crypto.xonly(self.pubkey_hex):
                raise Rejected(f"input {i} is locked to a different key")
            if {t[0] for t in tags} - SUPPORTED_TAGS:
                raise Rejected(f"input {i} has spending conditions this device does not support")
            if crypto.get_tag(tags, "sigflag") != ["SIG_ALL"]:
                raise Rejected(f"input {i} is not SIG_ALL")
            if first_tags is None:
                first_tags = tags
            elif tags != first_tags:
                raise Rejected("inputs have different spending conditions")
            in_total += _positive_amount(p, f"input {i}")
            _check_point(p.get("C"), f"input {i} C")

        # --- outputs: well-formed
        out_total = 0
        for j, o in enumerate(outputs):
            out_total += _positive_amount(o, f"output {j}")
            _check_point(o.get("B_"), f"output {j} B_")

        fee = in_total - out_total
        if fee < 0:
            raise Rejected("outputs are worth more than inputs")

        # --- classify every output, using reveals where the host gave them
        kinds = ["unverified"] * len(outputs)
        detail = [None] * len(outputs)
        seen = set()
        for rv in req.get("reveals") or []:
            if not isinstance(rv, dict):
                raise Rejected("reveal is not a JSON object")
            j = rv.get("index")
            if not isinstance(j, int) or not 0 <= j < len(outputs) or j in seen:
                raise Rejected("bad reveal index")
            seen.add(j)
            try:
                r = int(rv["r"], 16)
                if not 0 < r < curve.N:
                    raise ValueError
                expected = crypto.blind(rv["secret"], r)
            except Exception:
                raise Rejected(f"reveal for output {j} is malformed")
            if expected != outputs[j]["B_"].lower():
                raise Rejected(f"reveal for output {j} does not match its B_ (host is lying)")
            parsed = crypto.parse_secret(rv["secret"])
            if parsed and parsed[0] == "P2PK":
                _, data, tags = parsed
                if not isinstance(data, str) or not _valid_tags(tags):
                    raise Rejected(f"reveal for output {j} has a malformed lock")
                if crypto.xonly(data) == crypto.xonly(self.pubkey_hex) \
                        and crypto.get_tag(tags, "sigflag") == ["SIG_ALL"] \
                        and not ({t[0] for t in tags} - SUPPORTED_TAGS):
                    kinds[j] = "change"
                else:
                    kinds[j] = "locked"
                    detail[j] = data
            elif parsed:
                kinds[j] = "other"  # e.g. HTLC; treated as leaving
            else:
                kinds[j] = "bearer"

        change = sum(o["amount"] for o, k in zip(outputs, kinds) if k == "change")
        leaving = out_total - change
        if self.max_send is not None and leaving > self.max_send:
            raise Rejected(f"sending {leaving} exceeds this device's limit of {self.max_send}")

        recipients = {}
        for o, k, d in zip(outputs, kinds, detail):
            if k == "change":
                continue
            key = (k, d)
            recipients[key] = recipients.get(key, 0) + o["amount"]

        return {
            "mint": req.get("mint", "?"),
            "unit": req.get("unit", "sat"),
            "in_total": in_total,
            "sending": leaving,
            "change": change,
            "fee": fee,
            "recipients": [
                {"kind": k, "pubkey": d, "amount": a} for (k, d), a in recipients.items()
            ],
            "unverified": sum(1 for k in kinds if k == "unverified"),
        }

    # ------------------------------------------------------------ sign

    def sign(self, req: dict) -> dict:
        """Review again (never trust an earlier review) and sign SIG_ALL."""
        self.review(req)
        msg = crypto.sig_all_message(req["inputs"], req["outputs"])
        sig = crypto.schnorr_sign(self.privkey, msg)
        if not crypto.schnorr_verify(self.pubkey_hex, msg, sig):
            raise Rejected("self-check of signature failed")
        return {
            "v": 1,
            "pubkey": self.pubkey_hex,
            "msg_hash": crypto.message_hash(msg).hex(),
            "signature": sig,
        }


def summary_lines(review: dict) -> list:
    """Short lines that fit a small screen (about 20 characters each)."""
    u = review["unit"]
    mint = review["mint"].replace("https://", "").replace("http://", "")
    lines = [f"Send {review['sending']} {u}"]
    for rcp in review["recipients"]:
        if rcp["kind"] == "locked":
            lines.append(f" {rcp['amount']} to key {rcp['pubkey'][2:10]}..")
        elif rcp["kind"] == "bearer":
            lines.append(f" {rcp['amount']} as bearer token")
        elif rcp["kind"] == "other":
            lines.append(f" {rcp['amount']} other condition")
        else:
            lines.append(f" {rcp['amount']} UNVERIFIED")
    lines.append(f"Change {review['change']} {u}")
    lines.append(f"Fee {review['fee']} {u}")
    lines.append(f"Mint {mint[:16]}")
    if review["unverified"]:
        lines.append("! Unverified outputs")
    return lines


def _valid_tags(tags) -> bool:
    """NUT-10 tags: a list of non-empty lists of strings."""
    return isinstance(tags, list) and all(
        isinstance(t, list) and t and all(isinstance(x, str) for x in t) for t in tags)


def _positive_amount(obj, what) -> int:
    a = obj.get("amount")
    if not isinstance(a, int) or isinstance(a, bool) or a <= 0:
        raise Rejected(f"{what} has an invalid amount")
    return a


def _check_point(hexstr, what):
    try:
        curve.parse(bytes.fromhex(hexstr))
    except Exception:
        raise Rejected(f"{what} is not a valid curve point")
