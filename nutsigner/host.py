"""Host-side wallet: talks to the mint, stores locked proofs, builds requests.

The host is assumed to be untrusted. It holds proofs that are all locked to the
device's key (SIG_ALL), so stealing this wallet file gives an attacker nothing
they can spend.

Crash safety: before any call that can move money (mint, swap, melt), the wallet
writes everything needed to finish it (the outputs and their blinding factors)
to disk and marks it in flight. If the call's outcome is unknown (timeout,
crash, bad reply), `recover()` asks the mint for the signatures it already
issued (NUT-09) or confirms nothing happened (NUT-07), and finishes the job.
For a melt it first asks the mint for the quote's state (NUT-05).
"""

import base64
import fcntl  # POSIX only (Linux, macOS, Pi); the host CLI doesn't target Windows
import json
import math
import os
import time

from . import crypto, curve
from .mint import MintError, MintRejected, MintUnavailable, quote_is_paid

# A cancelled send whose swap the mint once received keeps its inputs reserved
# until it lands or this long after cancel with the inputs still UNSPENT.
# ponytail: fixed delay; a mint completing a swap later than this would still
# be collected by recover, but its inputs may by then sit in a newer send.
RELEASE_AFTER_S = 3600

SEND_FAILED = ("this send can never complete: another transaction spent its inputs "
               "(for example a send you cancelled that the mint completed later). "
               "It was cleared; run recover to collect anything that completed late.")


MELT_FAILED = ("this payment can never complete: another transaction spent its inputs. "
               "It was cleared; the invoice was not paid by this wallet.")


class WalletError(Exception):
    pass


class WalletBusy(WalletError):
    """Another process (or wallet object) holds this wallet file."""


class RestoreUnsupported(WalletError):
    """The mint refused NUT-09 restore."""


def split_amount(amount: int):
    """Split into powers of two, smallest first (standard Cashu denominations)."""
    return [1 << i for i in range(amount.bit_length()) if amount >> i & 1]


def encode_token_v3(mint_url: str, proofs, unit="sat") -> str:
    """cashuA token (NUT-00 V3, JSON + base64url). Widely accepted by wallets."""
    token = {"token": [{"mint": mint_url, "proofs": proofs}], "unit": unit}
    return "cashuA" + base64.urlsafe_b64encode(json.dumps(token).encode()).decode()


def decode_token_v3(token: str):
    """(mint url, unit, proofs) of a cashuA token. Raises WalletError if malformed."""
    token = token.strip()
    if not token.startswith("cashuA"):
        raise WalletError("not a cashuA token (cashuB tokens are not supported yet)")
    try:
        b64 = token[6:]
        data = json.loads(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)))
        entries = data["token"]
        mints = {e["mint"].rstrip("/") for e in entries}
        proofs = [{k: p[k] for k in ("amount", "id", "secret", "C")} for e in entries for p in e["proofs"]]
        unit = data.get("unit", "sat")
        if len(mints) != 1 or not proofs or not all(
                type(p["amount"]) is int and p["amount"] > 0
                and all(isinstance(p[k], str) for k in ("id", "secret", "C")) for p in proofs):
            raise ValueError
        if len({p["secret"] for p in proofs}) != len(proofs):
            raise ValueError
    except Exception:
        raise WalletError("token is malformed") from None
    return mints.pop(), unit, proofs


def check_pubkey(pubkey_hex: str, what: str = "pubkey") -> str:
    """Return the key in lowercase if it is a valid compressed point, else raise."""
    try:
        key = pubkey_hex.strip().lower()
        curve.parse(bytes.fromhex(key))
        return key
    except Exception:
        raise WalletError(f"{what} is not a valid compressed secp256k1 public key "
                          "(66 hex characters starting with 02 or 03)") from None


def _blinded(outs):
    return [{k: o[k] for k in ("amount", "id", "B_")} for o in outs]


class HostWallet:
    def __init__(self, path: str, mint, mint_url: str, unit: str = "sat"):
        self.path = path
        self.mint = mint
        self.mint_url = mint_url.rstrip("/")
        self.unit = unit
        self._keysets = None
        self._keys = {}
        self.state = {"mint": self.mint_url, "unit": unit, "device_pubkey": None,
                      "proofs": [], "pending": None, "pending_fund": None}
        # One wallet object per file at a time, across processes: a second one
        # would act on stale state and overwrite the first one's results.
        # Held until close() or process exit, so state is always read under it.
        self._lock = open(path + ".lock", "a")
        try:
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WalletBusy(f"{path} is in use by another process; try again when it finishes") from None
            if os.path.exists(path):
                with open(path) as f:
                    self.state = json.load(f)
                self.state.setdefault("pending_fund", None)
                stored = (self.state.get("mint") or "").rstrip("/")
                if stored != self.mint_url or self.state.get("unit") != unit:
                    raise WalletError(
                        f"{path} belongs to {stored} ({self.state.get('unit')}), "
                        f"not {self.mint_url} ({unit}). Use a different --wallet file for each mint.")
        except BaseException:
            self.close()
            raise

    def close(self):
        """Release the file lock. The object must not be used afterwards."""
        self._lock.close()

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        try:  # make the rename itself durable
            fd = os.open(os.path.dirname(os.path.abspath(self.path)), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass  # some platforms can't fsync a directory

    # ------------------------------------------------------------ mint data (cached)

    def keysets(self):
        if self._keysets is None:
            sets = self.mint.keysets()
            if not all(isinstance(k, dict) and isinstance(k.get("id"), str) and "unit" in k for k in sets):
                raise MintUnavailable("mint sent a malformed keyset list")
            self._keysets = sets
        return self._keysets

    def _keys_for(self, keyset_id):
        if keyset_id not in self._keys:
            self._keys[keyset_id] = self.mint.keys(keyset_id)
        return self._keys[keyset_id]

    def _active_keyset(self):
        sets = [k for k in self.keysets() if k.get("active") and k.get("unit") == self.unit]
        if not sets:
            raise WalletError(f"mint has no active keyset for unit {self.unit}")
        return min(sets, key=lambda k: k.get("input_fee_ppk", 0))  # cheapest

    def _fee(self, proofs) -> int:
        ppk = {k["id"]: k.get("input_fee_ppk", 0) for k in self.keysets()}
        return math.ceil(sum(ppk.get(p["id"], 0) for p in proofs) / 1000)

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _make_outputs(amounts, keyset_id, secret_fn, role):
        outs = []
        for a in amounts:
            secret = secret_fn()
            r = crypto.random_scalar()
            outs.append({"amount": a, "id": keyset_id, "B_": crypto.blind(secret, r),
                         "secret": secret, "r": format(r, "064x"), "role": role})
        return outs

    def _unblind(self, outs, signatures):
        if len(signatures) != len(outs):
            raise MintError(f"mint returned {len(signatures)} signatures for {len(outs)} outputs")
        proofs = []
        for o, s in zip(outs, signatures, strict=True):
            if not isinstance(s, dict) or s.get("amount") != o["amount"] or s.get("id") != o["id"]:
                raise MintError("mint returned signatures that don't match the outputs")
            K = self._keys_for(s["id"]).get(s["amount"])
            try:
                C = crypto.unblind(s["C_"], int(o["r"], 16), K)
            except Exception:  # missing or invalid C_, or no key for that amount
                raise MintError("mint returned a malformed signature") from None
            proofs.append({"amount": o["amount"], "id": o["id"], "secret": o["secret"], "C": C})
        return proofs

    def _restore_all(self, outs):
        """NUT-09. Returns signatures in output order, [] if none, raises if partial."""
        try:
            got = self.mint.restore(_blinded(outs))
        except MintRejected as e:
            raise RestoreUnsupported(f"mint refused restore; it may not support NUT-09 ({e}). "
                              "Keep the wallet file; nothing has been deleted.") from None
        found = [got.get(o["B_"]) for o in outs]
        if all(found):
            return found
        if not any(found):
            return []
        raise WalletError("mint restored only some outputs; keep the wallet file and retry later")

    # ------------------------------------------------------------ actions

    def check_mint(self):
        """Refuse mints that don't advertise NUT-11; locks would be ignored."""
        info = self.mint.info()
        nuts = info.get("nuts", {})
        if not (nuts.get("11", {}) or {}).get("supported"):
            raise WalletError("mint does not advertise NUT-11 (P2PK). Locks would be unsafe.")
        return info

    def fund(self, amount: int, device_pubkey: str, wait_s: int = 120, on_invoice=None):
        """Mint new ecash directly locked to the device's key with SIG_ALL."""
        q = self.start_fund(amount, device_pubkey)
        if on_invoice:
            on_invoice(q["request"])
        deadline = time.time() + wait_s
        while not quote_is_paid(q):
            if time.time() > deadline:
                raise WalletError(f"invoice not paid yet (quote {q['quote']}); "
                                  "once it is paid, run recover")
            time.sleep(2)
            q = self.mint.mint_quote_state(q["quote"])
        return self._recover_fund()

    def start_fund(self, amount: int, device_pubkey: str) -> dict:
        """Get a mint quote and record it as pending_fund. Returns the quote (its
        "request" is the invoice to pay); recover() finishes it once paid."""
        device_pubkey = check_pubkey(device_pubkey, "device pubkey")
        known = self.state.get("device_pubkey")
        if known and known != device_pubkey:
            raise WalletError("this wallet is already locked to a different device key")
        if self.state.get("pending_fund"):
            raise WalletError("an earlier funding is unfinished; run recover first")
        if amount <= 0:
            raise WalletError("amount must be positive")
        self.check_mint()
        q = self.mint.mint_quote(amount, self.unit)
        quote_id = q.get("quote")
        if not isinstance(quote_id, str) or not isinstance(q.get("request"), str):
            raise MintUnavailable("mint sent a quote without an id or invoice")
        # record the quote and outputs BEFORE showing the invoice: once it is
        # paid, recover can always finish (or abandon-fund can hand it over).
        # The wallet is locked to the key only once a funding completes.
        self.state["pending_fund"] = {"quote": quote_id, "expiry": q.get("expiry"), "minted": False,
                                      "device_pubkey": device_pubkey, "invoice": q["request"],
                                      "outputs": self._fund_outputs(amount, device_pubkey)}
        self.save()
        return q

    def pair(self, device_pubkey: str) -> str:
        """Remember the device's key (scanned from its screen) for later funding."""
        device_pubkey = check_pubkey(device_pubkey, "device pubkey")
        known = self.state.get("device_pubkey")
        holds = any(self.state.get(k) for k in ("proofs", "pending", "pending_fund", "abandoned_funds",
                                                 "abandoned_sends", "unclaimed_change"))
        if known and known != device_pubkey and holds:
            raise WalletError("this wallet is already locked to a different device key")
        self.state["device_pubkey"] = device_pubkey
        self.save()
        return device_pubkey

    def _log(self, kind, amount, status="done", **extra):
        """Append to the wallet's history (saved by the caller's next save())."""
        self.state.setdefault("history", []).append(
            {"time": int(time.time()), "type": kind, "amount": amount, "status": status, **extra})

    def _fund_outputs(self, amount, pk):
        return self._make_outputs(split_amount(amount), self._active_keyset()["id"],
                                  lambda: crypto.make_p2pk_secret(pk, "SIG_ALL"), "change")

    def _recover_fund(self, archived=None):
        """Finish pending_fund (or an archived funding) if possible.

        Returns "done", "waiting", "mint_pending" or "expired" (never for archived).
        """
        pf = archived or self.state["pending_fund"]
        outs, sigs = self._restore_fund(pf)
        if not sigs:
            q = self.mint.mint_quote_state(pf["quote"])
            if q.get("state") == "ISSUED":
                raise WalletError("mint says the quote was issued but returned no signatures "
                                  "on restore; keep the wallet file and contact the mint")
            if not quote_is_paid(q):
                expiry = q.get("expiry", pf.get("expiry"))
                if (not archived and q.get("state") == "UNPAID"
                        and isinstance(expiry, (int, float)) and time.time() > expiry):
                    self.state["pending_fund"] = None  # can never be paid now
                    self._log("fund", sum(o["amount"] for o in pf["outputs"]), "expired")
                    self.save()
                    return "expired"
                # e.g. PENDING: our mint request may still be running at the mint
                return "mint_pending" if pf.get("minted", True) else "waiting"
            outs = pf["outputs"]
            pf["minted"] = True  # from here on, the outcome can be unknown
            self.save()
            try:
                sigs = self.mint.mint(pf["quote"], _blinded(outs))
            except MintRejected:
                # Retry once with fresh outputs on the current keyset (it may
                # have rotated). An earlier request may still be signing the old
                # ones, so they are kept and restored too, never discarded.
                self._keysets = None
                new = self._fund_outputs(sum(o["amount"] for o in outs), self._fund_pubkey(pf))
                pf.setdefault("retired_outputs", []).append(outs)
                pf["outputs"] = outs = new
                self.save()
                sigs = self.mint.mint(pf["quote"], _blinded(outs))
        self._finish_fund(self._unblind(outs, sigs), archived)
        return "done"

    def _restore_fund(self, pf):
        """NUT-09 over every output set sent for this quote: (outputs, sigs) of the signed one."""
        for outs in [pf["outputs"], *pf.get("retired_outputs", [])]:
            try:
                sigs = self._restore_all(outs)
            except RestoreUnsupported:
                break  # no NUT-09: the quote state still says what happened
            if sigs:
                return outs, sigs
        return None, []

    def abandon_fund(self):
        """Give up on an unfinished funding. Returns (quote id, outcome):

        "funded": the invoice was paid, so the funding is finished, never dropped;
        "forgotten": the invoice is unpaid, so the entry is deleted;
        "archived": a mint request of ours may still land, or the mint can't be
        reached; new funding is unblocked and recover keeps checking it.
        """
        pf = self.state.get("pending_fund")
        if not pf:
            raise WalletError("no unfinished funding")
        try:
            outcome = self._recover_fund()  # mints a PAID quote, restores an issued one
            if outcome in ("done", "expired"):
                return pf["quote"], "funded" if outcome == "done" else "forgotten"
            state = self.mint.mint_quote_state(pf["quote"]).get("state")
        except (MintError, WalletError):
            state = None
        if state == "UNPAID":
            self.state["pending_fund"] = None
            self.save()
            return pf["quote"], "forgotten"
        self.state.setdefault("abandoned_funds", []).append(pf)
        self.state["device_pubkey"] = self._fund_pubkey(pf)  # it may still land
        self.state["pending_fund"] = None
        self.save()
        return pf["quote"], "archived"

    def _fund_pubkey(self, pf):
        return pf.get("device_pubkey") or self.state["device_pubkey"]  # older files: state only

    def _finish_fund(self, new, archived=None):
        self.state["device_pubkey"] = self._fund_pubkey(archived or self.state["pending_fund"])
        self.state["proofs"].extend(new)
        self._log("fund", sum(p["amount"] for p in new))
        if archived:
            self.state["abandoned_funds"].remove(archived)
        else:
            self.state["pending_fund"] = None
        self.save()
        return new

    def _reserved(self):
        """Secrets of inputs a cancelled send may still spend (see RELEASE_AFTER_S)."""
        return {p["secret"] for old in self.state.get("abandoned_sends", []) if not old.get("released")
                for p in old["request"]["inputs"]}

    def _spendable(self):
        reserved = self._reserved()
        return [p for p in self.state["proofs"] if p["secret"] not in reserved]

    def balance(self) -> int:
        return sum(p["amount"] for p in self._spendable())

    def reserved_balance(self) -> int:
        return sum(p["amount"] for p in self.state["proofs"]) - self.balance()

    def _device_key_for_new_request(self, given=None):
        if self.state.get("pending"):
            raise WalletError("a send, payment or receive is already pending; submit or cancel it first")
        device_pk = self.state.get("device_pubkey")
        if given is not None:
            given = check_pubkey(given, "device pubkey")
            if device_pk and device_pk != given:
                raise WalletError("this wallet is already locked to a different device key")
            return given
        if not device_pk:
            raise WalletError("wallet has no device key yet; fund it first")
        return device_pk

    def _select(self, amount, tight=False):
        """Coin selection: largest first, re-checking the fee as inputs are added.

        tight (melts, where the device counts every input as at risk): take
        proofs that don't overshoot, close the gap with the smallest proof that
        does, then drop any proof (smallest first) the rest can do without.
        Returns (inputs, what is left after amount and input fee).
        """
        pool = sorted(self._spendable(), key=lambda p: -p["amount"])

        def short(c):  # how much c lacks, input fee included; < 0 is overshoot
            return amount + self._fee(c) - sum(p["amount"] for p in c)
        chosen = []
        if tight:
            for p in pool:
                if short(chosen + [p]) >= 0:
                    chosen.append(p)
        rest = [p for p in pool if p not in chosen]
        while short(chosen) > 0 and rest:
            fits = [p for p in rest if short(chosen + [p]) <= 0] if tight else []
            p = min(fits, key=lambda p: p["amount"]) if fits else rest[0]
            chosen.append(p)
            rest.remove(p)
        if tight:
            for p in sorted(chosen, key=lambda p: p["amount"]):
                if short([c for c in chosen if c is not p]) <= 0:
                    chosen.remove(p)
        if short(chosen) > 0:
            raise WalletError(f"insufficient balance: have {self.balance()}, "
                              f"need {amount + self._fee(chosen)}")
        return [{k: p[k] for k in ("amount", "id", "secret", "C")} for p in chosen], -short(chosen)

    def _new_pending(self, request, outs):
        # an honest host reveals every output so the device can verify all of them
        request["reveals"] = [{"index": i, "secret": o["secret"], "r": o["r"]} for i, o in enumerate(outs)]
        self.state["pending"] = {"outputs": outs, "request": request, "submitted": False}
        self.save()
        return request

    def build_send_request(self, amount: int, to_pubkey: str = None) -> dict:
        """Choose inputs, build outputs and return the request for the device."""
        device_pk = self._device_key_for_new_request()
        if amount <= 0:
            raise WalletError("amount must be positive")
        if to_pubkey is not None:  # "" must be refused, not treated as "no recipient"
            to_pubkey = check_pubkey(to_pubkey, "recipient pubkey (--to)")
        inputs, change = self._select(amount)

        ks = self._active_keyset()
        if to_pubkey is not None:
            # SIG_ALL, like our own coins: the recipient is a NutSigner device
            rcp_secret = lambda: crypto.make_p2pk_secret(to_pubkey, "SIG_ALL")
        else:
            rcp_secret = lambda: os.urandom(32).hex()
        outs = self._make_outputs(split_amount(amount), ks["id"], rcp_secret, "recipient")
        outs += self._make_outputs(split_amount(change), ks["id"],
                                   lambda: crypto.make_p2pk_secret(device_pk, "SIG_ALL"), "change")
        return self._new_pending({"v": 1, "type": "swap", "mint": self.mint_url, "unit": self.unit,
                                  "inputs": inputs, "outputs": _blinded(outs)}, outs)

    def build_receive_request(self, token: str, device_pubkey: str = None) -> dict:
        """Swap a token locked to this wallet's device (send --to) into fresh coins locked to it.

        Until the swap, the sender still knows the proofs; the device signs a
        swap whose outputs are all change. device_pubkey sets the key of a new wallet.
        """
        if device_pubkey is None and not self.state.get("device_pubkey"):
            raise WalletError("this wallet has no device key yet; give it with --pubkey")
        device_pk = self._device_key_for_new_request(device_pubkey)
        mint, unit, proofs = decode_token_v3(token)
        if mint != self.mint_url or unit != self.unit:
            raise WalletError(f"token is for {mint} ({unit}); this wallet uses {self.mint_url} ({self.unit})")
        for p in proofs:
            parsed = crypto.parse_secret(p["secret"])
            if not parsed or parsed[0] != "P2PK":
                raise WalletError("token is not locked to a key; only tokens sent to your "
                                  "device key (send --to) can be received")
            _, data, tags = parsed
            try:
                ours = crypto.xonly(data) == crypto.xonly(device_pk)
            except Exception:
                ours = False
            if not ours:
                raise WalletError("token is locked to a different key, not this wallet's device")
            if tags != [["sigflag", "SIG_ALL"]]:
                raise WalletError("token's lock is not a plain SIG_ALL lock; the device can't sign it")
        if not {p["id"] for p in proofs} <= {k["id"] for k in self.keysets() if k.get("unit") == self.unit}:
            raise WalletError("token uses a keyset this mint doesn't have")
        states = self._input_states({"request": {"inputs": proofs}})
        if any(s != "UNSPENT" for s in states):
            raise WalletError("token has already been spent (received before?)")
        keep = sum(p["amount"] for p in proofs) - self._fee(proofs)
        if keep <= 0:
            raise WalletError("token is worth no more than the mint's fee to receive it")
        outs = self._make_outputs(split_amount(keep), self._active_keyset()["id"],
                                  lambda: crypto.make_p2pk_secret(device_pk, "SIG_ALL"), "change")
        self.state["device_pubkey"] = device_pk  # submit checks the signature against it
        return self._new_pending({"v": 1, "type": "swap", "mint": self.mint_url, "unit": self.unit,
                                  "inputs": proofs, "outputs": _blinded(outs)}, outs)

    def build_melt_request(self, invoice: str) -> dict:
        """Get a melt quote for a bolt11 invoice and build the request for the device.

        Change for the unused fee reserve comes back on NUT-08 blank outputs
        (amount 0; the mint fills in amounts), locked to the device like swap change.
        """
        device_pk = self._device_key_for_new_request()
        if self.unit != "sat":
            raise WalletError("paying invoices is only supported in sat")
        q = self.mint.melt_quote(invoice.strip(), self.unit)
        amount, fee_reserve = q.get("amount"), q.get("fee_reserve")
        if not (isinstance(q.get("quote"), str) and isinstance(amount, int) and amount > 0
                and isinstance(fee_reserve, int) and fee_reserve >= 0):
            raise MintUnavailable("mint sent a malformed melt quote")
        inputs, _ = self._select(amount + fee_reserve, tight=True)
        # enough blank outputs for all of it to come back (the device checks)
        overpaid = sum(p["amount"] for p in inputs) - amount
        outs = self._make_outputs([0] * overpaid.bit_length(), self._active_keyset()["id"],
                                  lambda: crypto.make_p2pk_secret(device_pk, "SIG_ALL"), "change")
        return self._new_pending({"v": 1, "type": "melt", "mint": self.mint_url, "unit": self.unit,
                                  "invoice": invoice.strip(), "quote": q["quote"],
                                  "amount": amount, "fee_reserve": fee_reserve,
                                  "inputs": inputs, "outputs": _blinded(outs)}, outs)

    def cancel_pending(self):
        pend = self.state.get("pending")
        if pend and pend.get("submitted"):
            raise WalletError("this send was already submitted and its outcome is unknown; "
                              "run recover instead of cancel")
        if pend and pend.get("ever_submitted"):
            # The mint once saw this swap or melt and might still complete it
            # (slow mint). Keep its outputs so recover() can collect the result.
            pend["cancelled_at"] = time.time()
            self.state.setdefault("abandoned_sends", []).append(pend)
        self.state["pending"] = None
        self.save()

    def _witnessed_inputs(self, signature):
        """Check the device's signature against the pending request; inputs with witness."""
        if not (isinstance(signature, dict) and isinstance(signature.get("signature"), str)):
            raise WalletError("signature file is not a device signature")
        req = self.state["pending"]["request"]
        msg = crypto.sig_all_message(req["inputs"], req["outputs"], req.get("quote"))
        if not crypto.schnorr_verify(self.state["device_pubkey"], msg, signature["signature"]):
            raise WalletError("device signature does not match the pending request")
        inputs = [dict(p) for p in req["inputs"]]
        inputs[0]["witness"] = json.dumps({"signatures": [signature["signature"]]}, separators=(",", ":"))
        return inputs

    def submit(self, signature: dict):
        """Attach the device's SIG_ALL witness, swap at the mint, store change.

        Returns (token, sent_proofs, change_proofs), or for a melt
        (payment preimage, change_proofs).
        """
        pend = self.state.get("pending")
        if not pend:
            raise WalletError("nothing pending")
        if pend["request"]["type"] == "melt":
            return self._submit_melt(signature)
        if pend.get("submitted"):
            # an earlier attempt was interrupted: find out what happened first
            result = self._recover_send()
            if result.get("send") == "done":
                return result["send_result"]
            if result.get("send") == "failed":
                raise WalletError(SEND_FAILED)
            pend = self.state["pending"]

        req = pend["request"]
        inputs = self._witnessed_inputs(signature)
        # record the in-flight swap BEFORE calling the mint
        pend["submitted"] = pend["ever_submitted"] = True
        self.save()
        try:
            sigs = self.mint.swap(inputs, req["outputs"])
        except MintRejected:
            # A 4xx doesn't prove the inputs are untouched ("already spent" may
            # mean this very swap landed). Restore our outputs, or clear
            # `submitted` only if the inputs are confirmed UNSPENT, or drop the
            # send if another transaction spent them, or raise still in flight.
            result = self._recover_send()
            if result.get("send") == "done":
                return result["send_result"]
            if result.get("send") == "failed":
                raise WalletError(SEND_FAILED) from None
            raise
        return self._finish_send(self._unblind(pend["outputs"], sigs))

    def _submit_melt(self, signature):
        pend = self.state["pending"]
        if pend.get("submitted"):  # an earlier attempt was interrupted
            done = self._melt_outcome(self._recover_melt())
            if done:
                return done
        req = pend["request"]
        inputs = self._witnessed_inputs(signature)
        pend["submitted"] = pend["ever_submitted"] = True  # BEFORE calling the mint
        self.save()
        try:
            q = self.mint.melt(req["quote"], inputs, req["outputs"])
        except MintRejected:
            # as with swap, a 4xx alone doesn't prove the inputs are untouched
            done = self._melt_outcome(self._recover_melt())
            if done:
                return done
            raise
        if q.get("state") == "PAID":
            return self._finish_melt(q)
        done = self._melt_outcome(self._recover_melt())
        if done:
            return done
        raise WalletError("the mint could not pay the invoice; your funds are untouched. "
                          "Submit again or cancel.")

    def _melt_outcome(self, result):
        """(preimage, change) if the melt is done, None if not sent; raises otherwise."""
        if result["melt"] == "pending":
            raise WalletError("the mint is still paying the invoice; run recover later")
        if result["melt"] == "failed":
            raise WalletError(MELT_FAILED)
        return result.get("melt_result")

    def _recover_melt(self) -> dict:
        req = self.state["pending"]["request"]
        q = self.mint.melt_quote_state(req["quote"])
        if q.get("state") == "PAID":
            return {"melt": "done", "melt_result": self._finish_melt(q)}
        if q.get("state") == "PENDING":
            return {"melt": "pending"}
        states = self._input_states(self.state["pending"]) if q.get("state") == "UNPAID" else []
        if states and all(s == "UNSPENT" for s in states):
            self.state["pending"]["submitted"] = False  # nothing was paid; inputs are ours
            self.save()
            return {"melt": "not_sent"}
        if states and set(states) <= {"SPENT", "UNSPENT"}:
            # Unpaid, yet inputs spent: another transaction spent them, so this
            # melt can never complete. Ask again in case ours landed just now.
            q = self.mint.melt_quote_state(req["quote"])
            if q.get("state") == "PAID":
                return {"melt": "done", "melt_result": self._finish_melt(q)}
            gone = {p["secret"] for p, st in zip(req["inputs"], states, strict=True) if st == "SPENT"}
            self.state["proofs"] = [p for p in self.state["proofs"] if p["secret"] not in gone]
            self.state["pending"] = None
            self._log("melt", req["amount"], "failed")
            self.save()
            return {"melt": "failed"}
        raise WalletError(f"melt quote is {q.get('state')!r} at the mint; keep the wallet file "
                          "and retry recover later")

    def _finish_melt(self, q, abandoned=None):
        """Keep the change the mint signed for the unused fee reserve. Returns (preimage, change)."""
        pend = abandoned or self.state["pending"]
        sigs, outs = q.get("change"), pend["outputs"]
        if sigs:
            if not isinstance(sigs, list) or len(sigs) > len(outs):
                raise MintError("mint returned malformed melt change")
            change = self._unblind_change(outs[:len(sigs)], sigs)  # NUT-08: the first outputs
        else:
            # some mints leave change out of the quote state; NUT-09 still has it
            change = self._restore_change(outs) if outs else []
            if change is None:  # restore refused: keep the blinding data, retry in recover
                self.state.setdefault("unclaimed_change", []).append(outs)
                change = []
        spent = {p["secret"] for p in pend["request"]["inputs"]}
        self.state["proofs"] = [p for p in self.state["proofs"] if p["secret"] not in spent] + change
        req = pend["request"]
        self._log("melt", req["amount"], fee=sum(p["amount"] for p in req["inputs"]) - req["amount"]
                  - sum(p["amount"] for p in change))
        if abandoned:
            self.state["abandoned_sends"].remove(abandoned)
        else:
            self.state["pending"] = None
        self.save()
        return q.get("payment_preimage"), change

    def _unblind_change(self, outs, sigs):
        """NUT-08 change: the mint picked each blank output's amount."""
        return self._unblind([dict(o, amount=s.get("amount") if isinstance(s, dict) else None)
                              for o, s in zip(outs, sigs)], sigs)

    def _restore_change(self, outs):
        """Change proofs the mint signed on these blank outputs (NUT-09); None if refused."""
        try:
            got = self.mint.restore(_blinded(outs))
        except MintRejected:
            return None
        found = [o for o in outs if o["B_"] in got]
        return self._unblind_change(found, [got[o["B_"]] for o in found])

    def _finish_send(self, new, abandoned=None):
        pend = abandoned or self.state["pending"]
        spent = {p["secret"] for p in pend["request"]["inputs"]}
        self.state["proofs"] = [p for p in self.state["proofs"] if p["secret"] not in spent]
        roles = [o["role"] for o in pend["outputs"]]
        change = [p for p, r in zip(new, roles, strict=True) if r == "change"]
        sent = [p for p, r in zip(new, roles, strict=True) if r == "recipient"]
        self.state["proofs"].extend(change)
        token = encode_token_v3(self.mint_url, sent, self.unit)
        fee = sum(p["amount"] for p in pend["request"]["inputs"]) - sum(p["amount"] for p in new)
        if sent:
            to = {crypto.parse_secret(p["secret"])[1] for p in sent if crypto.parse_secret(p["secret"])}
            self._log("send", sum(p["amount"] for p in sent), fee=fee, to=to.pop() if to else None)
        else:
            self._log("receive", sum(p["amount"] for p in change), fee=fee)
        # the recipient's proofs exist nowhere else: keep the token until shown
        if sent:
            self.state.setdefault("undelivered_tokens", []).append(token)
        if abandoned:
            self.state["abandoned_sends"].remove(abandoned)
        else:
            self.state["pending"] = None
        self.save()
        return token, sent, change

    def clear_tokens(self, shown):
        """Forget the undelivered tokens in shown (those the user has seen); keep the rest."""
        if not isinstance(shown, list):
            raise WalletError("list the tokens that were shown")
        self.state["undelivered_tokens"] = [t for t in self.state.get("undelivered_tokens", [])
                                            if t not in shown]
        self.save()

    def in_flight(self) -> bool:
        pend = self.state.get("pending")
        pf = self.state.get("pending_fund")
        # a funding whose invoice is still unpaid never reached mint(): not in flight
        return bool((pf and pf.get("minted", True)) or (pend and pend.get("submitted")))

    def recover(self) -> dict:
        """Finish or roll back any operation whose outcome was unknown.

        Returns a dict describing what happened, e.g.
        {"fund": "done", "send": "not_sent"}.
        """
        # Every step runs and saves what it finished (tokens included); the
        # first error is raised at the end.
        result, errors = {}, []
        if self.state.get("pending_fund"):
            try:
                result["fund"] = self._recover_fund()
            except (MintError, WalletError) as e:
                errors.append(e)

        # archived fundings: same logic as the pending one; never deleted here
        for old in list(self.state.get("abandoned_funds", [])):
            amount = sum(o["amount"] for o in old["outputs"])
            try:
                if self._recover_fund(old) == "done":
                    result["late_funds"] = result.get("late_funds", 0) + amount
            except (MintRejected, WalletError):  # refused or issued elsewhere: keep, retry later
                result.setdefault("archived_stuck", []).append(old["quote"])
            except MintError as e:
                errors.append(e)

        # cancelled sends and melts: collect late completions, release lapsed reservations
        for old in list(self.state.get("abandoned_sends", [])):
            try:
                if old["request"]["type"] == "melt":
                    try:
                        q = self.mint.melt_quote_state(old["request"]["quote"])
                    except MintRejected:  # quote forgotten: not paid; input states decide
                        q = {}
                    if q.get("state") == "PAID":
                        result.setdefault("late_melts", []).append(self._finish_melt(q, old))
                        continue
                    if q.get("state") not in ("UNPAID", None):
                        continue  # e.g. PENDING: may still be paid, keep the inputs reserved
                    sigs = []
                else:
                    try:
                        sigs = self._restore_all(old["outputs"])
                    except RestoreUnsupported:
                        sigs = []
                if sigs:
                    result.setdefault("late_sends", []).append(
                        self._finish_send(self._unblind(old["outputs"], sigs), old))
                elif (not old.get("released") and time.time() - old["cancelled_at"] > RELEASE_AFTER_S
                      and all(s == "UNSPENT" for s in self._input_states(old))):
                    old["released"] = True  # inputs spendable again; outputs still watched
                    self.save()
                    result["released"] = (result.get("released", 0)
                                          + sum(p["amount"] for p in old["request"]["inputs"]))
            except (MintError, WalletError) as e:
                errors.append(e)

        # change of paid melts the mint wouldn't restore at the time
        for outs in list(self.state.get("unclaimed_change", [])):
            try:
                change = self._restore_change(outs)
                if change is not None:  # [] means the whole fee reserve was used
                    self.state["proofs"].extend(change)
                    self.state["unclaimed_change"].remove(outs)
                    self.save()
                    if change:
                        result["late_change"] = result.get("late_change", 0) + sum(p["amount"] for p in change)
            except (MintError, WalletError) as e:
                errors.append(e)

        try:
            result.update(self._recover_send())
        except (MintError, WalletError) as e:
            errors.append(e)
        if errors:
            raise errors[0]
        return result

    def _input_states(self, entry):
        ys = [curve.serialize(crypto.hash_to_curve(p["secret"].encode())).hex()
              for p in entry["request"]["inputs"]]
        states = [s.get("state") if isinstance(s, dict) else None for s in self.mint.check_state(ys)]
        if len(states) != len(ys) or not all(isinstance(s, str) for s in states):
            raise MintUnavailable("mint sent malformed proof states")
        return states

    def _recover_send(self) -> dict:
        pend = self.state.get("pending")
        if not (pend and pend.get("submitted")):
            return {}
        if pend["request"]["type"] == "melt":
            return self._recover_melt()
        try:
            sigs = self._restore_all(pend["outputs"])
        except RestoreUnsupported:
            sigs = None  # no NUT-09: NUT-07 alone can still prove nothing happened
        if sigs:
            return {"send": "done", "send_result": self._finish_send(self._unblind(pend["outputs"], sigs))}
        states = self._input_states(pend)
        if all(s == "UNSPENT" for s in states):
            pend["submitted"] = False  # the swap never happened
            self.save()
            return {"send": "not_sent"}
        if sigs == [] and set(states) <= {"SPENT", "UNSPENT"}:
            # A swap marks its inputs SPENT and signs its outputs at once, so
            # SPENT inputs with no signatures for us mean another transaction
            # spent them (e.g. a cancelled send that completed later) and this
            # send can never complete. Restore again in case ours landed just now.
            sigs = self._restore_all(pend["outputs"])
            if sigs:
                return {"send": "done", "send_result": self._finish_send(self._unblind(pend["outputs"], sigs))}
            gone = {p["secret"] for p, st in zip(pend["request"]["inputs"], states, strict=True) if st == "SPENT"}
            self.state["proofs"] = [p for p in self.state["proofs"] if p["secret"] not in gone]
            self.state["pending"] = None
            sent = sum(o["amount"] for o in pend["outputs"] if o["role"] == "recipient")
            self._log("send" if sent else "receive", sent or sum(o["amount"] for o in pend["outputs"]), "failed")
            self.save()
            return {"send": "failed"}
        raise WalletError(
            f"inputs are {sorted(set(states))} at the mint but it returned no signatures "
            "for our outputs; keep the wallet file and retry recover later")
