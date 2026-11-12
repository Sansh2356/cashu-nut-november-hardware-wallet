"""In-process Cashu mint for offline tests.

Implements the parts of NUT-00/02/03/04/05/08/11 that NutSigner relies on, with real
BDHKE signatures and real SIG_ALL / SIG_INPUTS enforcement. Same method names
as nutsigner.mint.MintClient so the host code can't tell the difference.
"""

import hashlib
import json
import math
import os
import uuid

from nutsigner import bolt11, crypto, curve
from nutsigner.host import split_amount
from nutsigner.mint import MintRejected as MintError  # a refusal is a 4xx


class FakeMint:
    def __init__(self, input_fee_ppk=0, max_order=20):
        self.retired = {}  # keyset id -> (privs, pubs): still spendable, no new signatures
        self.rotate_keyset(max_order)
        self.fee_ppk = input_fee_ppk
        self.quotes = {}
        self.spent = set()
        self.issued = {}  # B_ -> blind signature, for NUT-09 restore
        self.melt_quotes = {}
        self.melt_fee_reserve = 4  # per melt quote
        self.ln_fee = 1            # what paying an invoice really costs
        self.melt_pending = False  # True: melt leaves the payment PENDING until settle_melt()

    def rotate_keyset(self, max_order=20):
        if hasattr(self, "keyset_id"):
            self.retired[self.keyset_id] = (self.privs, self.pubs)
        self.privs = {1 << i: crypto.random_scalar() for i in range(max_order)}
        self.pubs = {a: curve.serialize(curve.mul(k)).hex() for a, k in self.privs.items()}
        joined = b"".join(bytes.fromhex(self.pubs[a]) for a in sorted(self.pubs))
        self.keyset_id = "00" + hashlib.sha256(joined).hexdigest()[:14]

    # --- public API (mirrors MintClient)
    def info(self):
        return {"name": "fake", "nuts": {"7": {"supported": True}, "9": {"supported": True},
                                          "11": {"supported": True}}}

    def keysets(self):
        return [{"id": k, "unit": "sat", "active": k == self.keyset_id, "input_fee_ppk": self.fee_ppk}
                for k in [self.keyset_id, *self.retired]]

    def keys(self, keyset_id):
        if keyset_id in self.retired:
            return dict(self.retired[keyset_id][1])
        self._check_keyset(keyset_id)
        return dict(self.pubs)

    def mint_quote(self, amount, unit="sat"):
        q = {"quote": uuid.uuid4().hex, "request": "lnbcFAKE", "amount": amount,
             "state": "PAID", "unit": unit}  # testnut-style: auto-paid
        self.quotes[q["quote"]] = q
        return dict(q)

    def mint_quote_state(self, quote_id):
        return dict(self.quotes[quote_id])

    def mint(self, quote_id, outputs):
        q = self.quotes.get(quote_id)
        if not q or q["state"] != "PAID":
            raise MintError("quote not payable")
        if sum(o["amount"] for o in outputs) != q["amount"]:
            raise MintError("outputs do not match quote amount")
        sigs = [self._sign(o) for o in outputs]
        q["state"] = "ISSUED"
        return sigs

    def swap(self, inputs, outputs):
        for p in inputs:
            self._verify_proof(p)
        ys = [p["secret"] for p in inputs]
        if len(set(ys)) != len(ys) or any(y in self.spent for y in ys):
            raise MintError("proofs already spent")
        if len({o["B_"] for o in outputs}) != len(outputs):
            raise MintError("duplicate outputs")
        fee = math.ceil(len(inputs) * self.fee_ppk / 1000)
        if sum(p["amount"] for p in inputs) - fee != sum(o["amount"] for o in outputs):
            raise MintError("inputs - fee != outputs")
        self._verify_spending_conditions(inputs, outputs)
        self.spent.update(ys)
        return [self._sign(o) for o in outputs]

    def melt_quote(self, invoice, unit="sat"):
        amount = bolt11.decode(invoice)[0]
        if amount is None:
            raise MintError("amountless invoices are not supported")
        q = {"quote": uuid.uuid4().hex, "request": invoice, "amount": amount, "unit": unit,
             "fee_reserve": self.melt_fee_reserve, "state": "UNPAID", "expiry": 2**31,
             "payment_preimage": None, "change": None}
        self.melt_quotes[q["quote"]] = q
        return self.melt_quote_state(q["quote"])

    def melt_quote_state(self, quote_id):
        if quote_id not in self.melt_quotes:
            raise MintError("quote not found")  # a 4xx, like a 404
        return {k: v for k, v in self.melt_quotes[quote_id].items() if not k.startswith("_")}

    def melt(self, quote_id, inputs, outputs):
        q = self.melt_quotes.get(quote_id)
        if not q or q["state"] != "UNPAID":
            raise MintError("quote not payable")
        for p in inputs:
            self._verify_proof(p)
        ys = [p["secret"] for p in inputs]
        if len(set(ys)) != len(ys) or any(y in self.spent for y in ys):
            raise MintError("proofs already spent")
        if len({o["B_"] for o in outputs}) != len(outputs):
            raise MintError("duplicate outputs")
        overpaid = (sum(p["amount"] for p in inputs) - math.ceil(len(inputs) * self.fee_ppk / 1000)
                    - q["amount"])
        if overpaid < q["fee_reserve"]:
            raise MintError("inputs do not cover amount + fee reserve + input fee")
        self._verify_spending_conditions(inputs, outputs, quote_id)
        self.spent.update(ys)
        q.update(state="PENDING", _ys=ys, _outputs=outputs, _overpaid=overpaid)
        if not self.melt_pending:
            self.settle_melt(quote_id)
        return self.melt_quote_state(quote_id)

    def settle_melt(self, quote_id, ok=True):
        """The Lightning payment of a PENDING melt succeeds (or fails: inputs released)."""
        q = self.melt_quotes[quote_id]
        if not ok:
            self.spent.difference_update(q["_ys"])
            q["state"] = "UNPAID"
            return
        amounts = split_amount(q["_overpaid"] - self.ln_fee)  # NUT-08: the first outputs get it
        q["change"] = [self._sign(dict(o, amount=a)) for o, a in zip(q["_outputs"], amounts)]
        q.update(state="PAID", payment_preimage=os.urandom(32).hex())

    def check_state(self, ys):
        spent_ys = {curve.serialize(crypto.hash_to_curve(x.encode())).hex() for x in self.spent}
        return [{"Y": y, "state": "SPENT" if y in spent_ys else "UNSPENT", "witness": None} for y in ys]

    def restore(self, outputs):
        return {o["B_"]: self.issued[o["B_"]] for o in outputs if o["B_"] in self.issued}

    # --- internals
    def _check_keyset(self, kid):
        if kid in self.retired:
            raise MintError("keyset inactive")
        if kid != self.keyset_id:
            raise MintError("unknown keyset")

    def _sign(self, o):
        self._check_keyset(o["id"])
        k = self.privs.get(o["amount"])
        if k is None:
            raise MintError("bad amount")
        if o["B_"] in self.issued:
            raise MintError("outputs have already been signed before")
        B_ = curve.parse(bytes.fromhex(o["B_"]))
        sig = {"id": o["id"], "amount": o["amount"], "C_": curve.serialize(curve.mul(k, B_)).hex()}
        self.issued[o["B_"]] = sig
        return sig

    def _verify_proof(self, p):
        if p["id"] in self.retired:
            k = self.retired[p["id"]][0][p["amount"]]
        else:
            self._check_keyset(p["id"])
            k = self.privs[p["amount"]]
        Y = crypto.hash_to_curve(p["secret"].encode())
        if curve.serialize(curve.mul(k, Y)).hex() != p["C"]:
            raise MintError("invalid proof")

    def _verify_spending_conditions(self, inputs, outputs, quote_id=None):
        parsed = [crypto.parse_secret(p["secret"]) for p in inputs]
        sig_all = [x for x in parsed if x and x[0] == "P2PK" and crypto.get_tag(x[2], "sigflag") == ["SIG_ALL"]]
        if sig_all:
            first = parsed[0]
            for x in parsed:
                if not x or x[0] != first[0] or x[1] != first[1] or x[2] != first[2]:
                    raise MintError("SIG_ALL inputs must share kind, data and tags")
            msg = crypto.sig_all_message(inputs, outputs, quote_id)
            if not _has_valid_sig(first[1], msg, inputs[0].get("witness")):
                raise MintError("SIG_ALL witness invalid")
            return
        for p, x in zip(inputs, parsed):
            if x and x[0] == "P2PK":
                if not _has_valid_sig(x[1], p["secret"], p.get("witness")):
                    raise MintError("P2PK witness invalid")


def make_invoice(amount_sat=None, hrp="lnbc") -> str:
    """A real, signed bolt11 invoice from a throwaway node key (real mints accept it)."""
    import time
    from embit.bech32 import CHARSET, Encoding, bech32_create_checksum, convertbits
    from embit.util import secp256k1 as secp
    if amount_sat is not None:
        hrp += f"{amount_sat * 10}n"  # 1 sat = 10 nanobitcoin
    t = int(time.time())
    data = [t >> 5 * i & 31 for i in range(6, -1, -1)]                    # timestamp, 35 bits
    data += [1, 1, 20] + convertbits(os.urandom(32), 8, 5)               # p: payment hash
    data += [16, 1, 20] + convertbits(os.urandom(32), 8, 5)              # s: payment secret
    data += [13, 0, 0]                                                   # d: empty description
    data += [5, 0, 3, 16, 8, 0] + [24, 0, 2, 4, 16]                      # 9: features, c: cltv 18
    msg = hashlib.sha256(hrp.encode() + bytes(convertbits(data, 5, 8))).digest()
    sig, rec = secp.ecdsa_recoverable_signature_serialize_compact(
        secp.ecdsa_sign_recoverable(msg, os.urandom(32)))
    data += convertbits(sig + bytes([rec]), 8, 5)
    data += bech32_create_checksum(Encoding.BECH32, hrp, data)
    return hrp + "1" + "".join(CHARSET[d] for d in data)


def _has_valid_sig(pubkey, msg, witness):
    if not witness:
        return False
    try:
        sigs = json.loads(witness)["signatures"]
    except Exception:
        return False
    return any(crypto.schnorr_verify(pubkey, msg, s) for s in sigs)
