"""Tiny Cashu mint client (NUT-01..09) using only the standard library.

Runs on the host (laptop or phone), never on the device.

Errors come in two kinds, and the difference matters for money:
  MintRejected    - the mint answered and refused (HTTP 4xx). For money-moving
                    calls the caller must still verify the outcome: "already
                    spent" can mean our own earlier request went through.
  MintUnavailable - no clear answer: network error, timeout, 5xx, garbage
                    reply. The request MAY have taken effect at the mint.
"""

import json
import socket
import urllib.error
import urllib.request


class MintError(Exception):
    pass


class MintRejected(MintError):
    """The mint refused the request; money-moving calls must still verify the outcome."""


class MintUnavailable(MintError):
    """Outcome unknown. The request may or may not have been processed."""


class MintClient:
    def __init__(self, url: str, timeout: int = 30):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.url + path, data=data, method=method,
            headers={"Content-Type": "application/json", "User-Agent": "nutsigner/0.1"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            err = MintRejected if 400 <= e.code < 500 else MintUnavailable
            raise err(f"{method} {path} -> HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise MintUnavailable(f"{method} {path} -> could not reach mint: {reason}") from None
        try:
            out = json.loads(raw)
        except ValueError:
            out = None
        if not isinstance(out, dict):
            raise MintUnavailable(f"{method} {path} -> mint sent a reply that is not a JSON object")
        return out

    @staticmethod
    def _field(resp, key, n=None):
        """resp[key] as a list (of length n). A 200 without it means outcome unknown."""
        val = resp.get(key)
        if not isinstance(val, list) or (n is not None and len(val) != n):
            raise MintUnavailable(f"mint reply has no valid {key!r}; the outcome is unknown")
        return val

    # NUT-06
    def info(self):
        return self._req("GET", "/v1/info")

    # NUT-02 / NUT-01
    def keysets(self):
        return self._field(self._req("GET", "/v1/keysets"), "keysets")

    def keys(self, keyset_id):
        ks = self._field(self._req("GET", f"/v1/keys/{keyset_id}"), "keysets")
        try:
            return {int(a): k for a, k in ks[0]["keys"].items()}
        except (IndexError, KeyError, TypeError, AttributeError, ValueError):
            raise MintUnavailable(f"mint sent malformed keys for keyset {keyset_id}") from None

    # NUT-04
    def mint_quote(self, amount, unit="sat"):
        return self._req("POST", "/v1/mint/quote/bolt11", {"amount": amount, "unit": unit})

    def mint_quote_state(self, quote_id):
        return self._req("GET", f"/v1/mint/quote/bolt11/{quote_id}")

    def mint(self, quote_id, outputs):
        resp = self._req("POST", "/v1/mint/bolt11", {"quote": quote_id, "outputs": outputs})
        return self._field(resp, "signatures", len(outputs))

    # NUT-03
    def swap(self, inputs, outputs):
        resp = self._req("POST", "/v1/swap", {"inputs": inputs, "outputs": outputs})
        return self._field(resp, "signatures", len(outputs))

    # NUT-05 (+ NUT-08 blank outputs for fee change)
    def melt_quote(self, invoice, unit="sat"):
        return self._req("POST", "/v1/melt/quote/bolt11", {"request": invoice, "unit": unit})

    def melt_quote_state(self, quote_id):
        return self._req("GET", f"/v1/melt/quote/bolt11/{quote_id}")

    def melt(self, quote_id, inputs, outputs):
        """Returns the melt quote as the mint reports it (state, change, preimage)."""
        return self._req("POST", "/v1/melt/bolt11", {"quote": quote_id, "inputs": inputs, "outputs": outputs})

    # NUT-07
    def check_state(self, ys):
        """ys: hex Y = hash_to_curve(secret). Returns states in the same order."""
        return self._field(self._req("POST", "/v1/checkstate", {"Ys": ys}), "states", len(ys))

    # NUT-09
    def restore(self, outputs):
        """Re-fetch blind signatures the mint already issued for these outputs.

        Returns {B_: signature} for every output the mint recognises.
        """
        resp = self._req("POST", "/v1/restore", {"outputs": outputs})
        sigs = resp.get("signatures", resp.get("promises", []))  # "promises" = older mints
        try:
            return {o["B_"]: s for o, s in zip(resp.get("outputs", []), sigs, strict=True)}
        except (KeyError, TypeError, ValueError):
            raise MintUnavailable("mint sent a malformed restore reply") from None


def quote_is_paid(q: dict) -> bool:
    """Newer mints report "state"; older ones a boolean "paid"."""
    return q.get("state") in ("PAID", "ISSUED") or q.get("paid") is True
