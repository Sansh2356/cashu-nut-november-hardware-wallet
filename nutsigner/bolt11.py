"""Minimal BOLT #11 reader: amount and payment hash, nothing else.

Runs on the device, so it checks the bech32 checksum and treats the input as
hostile (anything wrong raises ValueError). It does not verify the node
signature: the device cannot know which node the user meant to pay anyway.
"""

import re

from embit.bech32 import CHARSET, Encoding, bech32_hrp_expand, bech32_polymod, convertbits

# hrp = "ln" + currency + optional amount (digits + optional multiplier)
_HRP = re.compile(r"ln([a-z]+?)(?:(\d+)([munp]?))?")
_MSAT_PER = {"": 10**11, "m": 10**8, "u": 10**5, "n": 10**2}  # msat per unit; "p" is 1/10 msat


def decode(invoice: str):
    """Return (amount in sat or None if amountless, payment hash hex)."""
    if not isinstance(invoice, str):
        raise ValueError("invoice is not a string")
    s = invoice.strip()
    if s.lower().startswith("lightning:"):
        s = s[10:]
    if s != s.lower() and s != s.upper():
        raise ValueError("mixed-case invoice")
    s = s.lower()
    pos = s.rfind("1")
    hrp, body = s[:pos], s[pos + 1:]
    m = _HRP.fullmatch(hrp)
    if pos < 1 or not m or len(body) < 7 + 104 + 6 or any(c not in CHARSET for c in body):
        raise ValueError("not a bolt11 invoice")
    data = [CHARSET.index(c) for c in body]
    if bech32_polymod(bech32_hrp_expand(hrp) + data) != Encoding.BECH32:
        raise ValueError("bad invoice checksum")

    amount = None
    if m.group(2) is not None:
        n, mult = int(m.group(2)), m.group(3)
        if m.group(2).startswith("0"):
            raise ValueError("invoice amount has a leading zero")
        if mult == "p":
            if n % 10:
                raise ValueError("invoice amount is not a whole msat")
            msat = n // 10
        else:
            msat = n * _MSAT_PER[mult]
        if msat % 1000:
            raise ValueError("invoice amount is not a whole number of sats")
        amount = msat // 1000

    # tagged fields sit between the 7-word timestamp and the 104-word signature
    fields, i, payment_hash = data[7:-6 - 104], 0, None
    while i + 3 <= len(fields):
        tag, length = fields[i], fields[i + 1] * 32 + fields[i + 2]
        value = fields[i + 3:i + 3 + length]
        if len(value) != length:
            raise ValueError("truncated invoice field")
        if tag == 1 and length == 52 and payment_hash is None:  # 'p'
            payment_hash = bytes(convertbits(value, 5, 8, False) or b"").hex()
        i += 3 + length
    if i != len(fields) or not payment_hash or len(payment_hash) != 64:
        raise ValueError("invoice has no payment hash")
    return amount, payment_hash
