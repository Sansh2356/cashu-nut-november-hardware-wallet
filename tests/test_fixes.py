"""Regression tests for the review findings (crash safety, validation, errors)."""

import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from nutsigner import crypto
from nutsigner.device import DeviceSigner, Rejected
from nutsigner.host import HostWallet, WalletError
from nutsigner.mint import MintClient, MintError, MintRejected, MintUnavailable
from tests.fake_mint import FakeMint, make_invoice

MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
URL = "https://fake.mint"


class Flaky:
    """Wraps a mint and fails one chosen call in a chosen way.

    "before": the request never reaches the mint.
    "after":  the mint processes it, then the reply is lost.
    "short":  the mint processes it and replies with one signature missing.
    "reject": the mint refuses it (4xx) without processing it.
    """

    def __init__(self, mint):
        self.m = mint
        self.fail = {}
        self.calls = {}

    def __getattr__(self, name):
        orig = getattr(self.m, name)

        def call(*a, **kw):
            self.calls[name] = self.calls.get(name, 0) + 1
            mode = self.fail.pop(name, None)
            if mode == "before":
                raise MintUnavailable(f"{name}: connection reset (before)")
            if mode == "reject":
                raise MintRejected(f"{name}: refused (reject)")
            out = orig(*a, **kw)
            if mode == "after":
                raise MintUnavailable(f"{name}: read timed out (after)")
            if mode == "short":
                return out[:-1]
            return out
        return call


def spend_freely(mint, proofs):
    """Prove recipient proofs are real by swapping them with no witness."""
    outs = [{"amount": p["amount"], "id": p["id"],
             "B_": crypto.blind(os.urandom(32).hex(), crypto.random_scalar())} for p in proofs]
    mint.swap(proofs, outs)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "w.json")
        self.real = FakeMint()
        self.mint = Flaky(self.real)
        self.device = DeviceSigner.from_mnemonic(MNEMONIC)
        self.w = HostWallet(self.path, self.mint, URL)
        self.addCleanup(lambda: self.w.close())

    def reopen(self):
        """A fresh process: new wallet object, empty caches, state from disk.

        The previous object is closed first: only one may hold the file lock.
        """
        self.w.close()
        self.w = HostWallet(self.path, self.mint, URL)
        return self.w

    def swap_behind_wallets_back(self, req, sig):
        inputs = [dict(p) for p in req["inputs"]]
        inputs[0]["witness"] = json.dumps({"signatures": [sig["signature"]]}, separators=(",", ":"))
        self.real.swap(inputs, req["outputs"])


# ------------------------------------------------------------------ 1. crash safety

class TestSubmitRecovery(Base):
    def setUp(self):
        super().setUp()
        self.w.fund(100, self.device.pubkey_hex)
        self.req = self.w.build_send_request(21)
        self.sig = self.device.sign(self.req)

    def assert_finished(self, w, token_result):
        token, sent, change = token_result
        self.assertEqual(sum(p["amount"] for p in sent), 21)
        self.assertEqual(w.balance(), 79)
        self.assertFalse(w.in_flight())
        self.assertIsNone(w.state["pending"])
        spend_freely(self.real, sent)

    def test_swap_reply_lost(self):
        self.mint.fail["swap"] = "after"
        with self.assertRaises(MintUnavailable):
            self.w.submit(self.sig)
        w = self.reopen()
        self.assertTrue(w.in_flight())
        res = w.recover()
        self.assertEqual(res["send"], "done")
        self.assert_finished(w, res["send_result"])

    def test_swap_never_reached_mint(self):
        self.mint.fail["swap"] = "before"
        with self.assertRaises(MintUnavailable):
            self.w.submit(self.sig)
        w = self.reopen()
        self.assertEqual(w.recover(), {"send": "not_sent"})
        self.assertFalse(w.in_flight())
        self.assert_finished(w, w.submit(self.sig))  # same signature still valid

    def test_unblind_fails_after_swap(self):
        w = self.reopen()  # empty keys cache, so _unblind must fetch keys
        self.mint.fail["keys"] = "before"
        with self.assertRaises(MintUnavailable):
            w.submit(self.sig)
        w = self.reopen()
        res = w.recover()
        self.assert_finished(w, res["send_result"])

    def test_short_reply_keeps_funds(self):
        self.mint.fail["swap"] = "short"
        with self.assertRaises(MintError):
            self.w.submit(self.sig)
        w = self.reopen()
        self.assertTrue(w.in_flight())
        self.assert_finished(w, w.recover()["send_result"])

    def test_resubmit_after_lost_reply_recovers_automatically(self):
        self.mint.fail["swap"] = "after"
        with self.assertRaises(MintUnavailable):
            self.w.submit(self.sig)
        w = self.reopen()
        self.assert_finished(w, w.submit(self.sig))

    def test_rejected_swap_is_not_in_flight(self):
        bad = dict(self.sig, signature=crypto.schnorr_sign(self.device.privkey, "something else"))
        with self.assertRaises(WalletError):  # caught locally before the mint
            self.w.submit(bad)
        self.mint.fail["swap"] = "reject"  # refused, inputs confirmed UNSPENT
        with self.assertRaises(MintRejected):
            self.w.submit(self.sig)
        self.assertFalse(self.w.in_flight())
        self.w.cancel_pending()

    def test_rejected_with_inputs_spent_elsewhere_is_dropped(self):
        # a 4xx does not mean "untouched": here the inputs were spent by another
        # transaction and none of our outputs were signed, so it can never land
        self.real.spent.add(self.req["inputs"][0]["secret"])
        with self.assertRaises(WalletError):
            self.w.submit(self.sig)
        w = self.reopen()
        self.assertFalse(w.in_flight())
        self.assertIsNone(w.state["pending"])
        self.assertEqual(w.balance(), 100 - 64)  # the spent input is gone

    def test_already_spent_by_this_swap_is_finished(self):
        # the swap landed (e.g. via another process); the mint now says "already spent"
        self.swap_behind_wallets_back(self.req, self.sig)
        self.assert_finished(self.w, self.w.submit(self.sig))

    def test_second_process_cannot_open_wallet(self):
        with self.assertRaises(WalletError):
            HostWallet(self.path, self.mint, URL)
        self.assert_finished(self.w, self.w.submit(self.sig))
        self.assertEqual(self.reopen().balance(), 79)  # opens once the first one closes

    def test_swap_lands_after_cancel(self):
        # slow mint: recover says not_sent, user cancels, then the swap completes
        self.mint.fail["swap"] = "before"
        with self.assertRaises(MintUnavailable):
            self.w.submit(self.sig)
        self.assertEqual(self.w.recover(), {"send": "not_sent"})
        self.w.cancel_pending()
        self.swap_behind_wallets_back(self.req, self.sig)
        late = self.reopen().recover()["late_sends"]
        self.assertEqual(len(late), 1)
        self.assertEqual(self.w.state["abandoned_sends"], [])
        self.assert_finished(self.w, late[0])

    def cancel_after_not_sent(self):
        self.mint.fail["swap"] = "before"
        with self.assertRaises(MintUnavailable):
            self.w.submit(self.sig)
        self.assertEqual(self.w.recover(), {"send": "not_sent"})
        self.w.cancel_pending()

    def test_cancelled_send_inputs_reserved(self):
        self.cancel_after_not_sent()
        old = {p["secret"] for p in self.req["inputs"]}
        self.assertEqual(self.w.balance(), 100 - 64)  # the 64 input is reserved
        self.assertEqual(self.w.reserved_balance(), 64)
        req2 = self.w.build_send_request(10)
        self.assertFalse(old & {p["secret"] for p in req2["inputs"]})
        self.swap_behind_wallets_back(self.req, self.sig)  # old send lands late
        self.w.submit(self.device.sign(req2))  # unaffected
        self.w.recover()
        self.assertEqual((self.w.balance(), self.w.reserved_balance()), (100 - 21 - 10, 0))

    def test_released_inputs_reused_then_old_send_lands(self):
        # an hour later the reservation lapses; the old swap then lands anyway
        self.cancel_after_not_sent()
        self.w.state["abandoned_sends"][0]["cancelled_at"] -= 2 * 3600
        self.assertEqual(self.w.recover(), {"released": 64})
        self.assertEqual(self.w.balance(), 100)
        req2 = self.w.build_send_request(10)  # reuses the 64 input
        self.swap_behind_wallets_back(self.req, self.sig)
        with self.assertRaises(WalletError):  # can never complete: dropped, not stuck
            self.w.submit(self.device.sign(req2))
        self.assertFalse(self.w.in_flight())
        self.assertEqual(len(self.w.recover()["late_sends"]), 1)
        self.assertEqual(self.w.balance(), 79)
        self.w.build_send_request(5)  # wallet usable again

    def test_late_token_survives_failing_recover(self):
        self.cancel_after_not_sent()
        req2 = self.w.build_send_request(10)
        self.mint.fail["swap"] = "before"
        with self.assertRaises(MintUnavailable):
            self.w.submit(self.device.sign(req2))
        self.swap_behind_wallets_back(self.req, self.sig)
        self.mint.fail["check_state"] = "before"  # the pending send's step fails
        with self.assertRaises(MintUnavailable):
            self.w.recover()
        tokens = self.reopen().state["undelivered_tokens"]
        self.assertEqual(len(tokens), 1)  # the late send's token is on disk
        self.w.clear_tokens()
        self.assertEqual(self.reopen().state["undelivered_tokens"], [])

    def test_malformed_check_state_is_unknown_outcome(self):
        self.mint.fail["swap"] = "before"
        with self.assertRaises(MintUnavailable):
            self.w.submit(self.sig)
        self.real.check_state = lambda ys: [{"state": ["SPENT"]} for _ in ys]
        with self.assertRaises(MintUnavailable):
            self.w.recover()
        self.assertTrue(self.w.in_flight())

    def test_malformed_signature_refused(self):
        for bad in ({}, [], {"signature": 5}, None):
            with self.subTest(bad=bad), self.assertRaises(WalletError):
                self.w.submit(bad)
        self.assertFalse(self.w.in_flight())

    def test_failed_fund_step_does_not_block_cancelled_send(self):
        self.cancel_after_not_sent()
        self.swap_behind_wallets_back(self.req, self.sig)  # old send lands late
        self.mint.fail["mint"] = "before"
        with self.assertRaises(MintUnavailable):
            self.w.fund(5, self.device.pubkey_hex)
        self.mint.fail["restore"] = "before"  # the fund step fails...
        with self.assertRaises(MintUnavailable):
            self.w.recover()
        self.assertEqual(self.w.balance(), 79)  # ...the late send was still collected
        self.assertEqual(len(self.w.state["undelivered_tokens"]), 1)
        self.assertEqual(self.w.recover(), {"fund": "done"})
        self.assertEqual(self.w.balance(), 84)

    def test_no_restore_falls_back_to_check_state(self):
        self.mint.fail["swap"] = "reject"
        self.mint.fail["restore"] = "reject"  # mint without NUT-09
        with self.assertRaises(MintRejected):
            self.w.submit(self.sig)
        self.assertFalse(self.w.in_flight())
        self.real.spent.add(self.req["inputs"][0]["secret"])
        self.mint.fail["restore"] = "reject"
        with self.assertRaises(WalletError):  # spent, can't restore: stays in flight
            self.w.submit(self.sig)
        self.assertTrue(self.w.in_flight())

    def test_cancel_refused_while_in_flight(self):
        self.mint.fail["swap"] = "after"
        with self.assertRaises(MintUnavailable):
            self.w.submit(self.sig)
        with self.assertRaises(WalletError):
            self.reopen().cancel_pending()


class TestMeltRecovery(Base):
    def setUp(self):
        super().setUp()
        self.w.fund(100, self.device.pubkey_hex)
        self.req = self.w.build_melt_request(make_invoice(30))
        self.sig = self.device.sign(self.req)
        self.paid = 100 - 30 - self.real.ln_fee

    def assert_paid(self, w, result):
        preimage, change = result
        self.assertEqual(preimage, self.real.melt_quotes[self.req["quote"]]["payment_preimage"])
        self.assertEqual(w.balance(), self.paid)
        self.assertFalse(w.in_flight())
        self.assertIsNone(w.state["pending"])

    def test_melt_reply_lost(self):
        self.mint.fail["melt"] = "after"
        with self.assertRaises(MintUnavailable):
            self.w.submit(self.sig)
        w = self.reopen()
        self.assertTrue(w.in_flight())
        res = w.recover()
        self.assertEqual(res["melt"], "done")
        self.assert_paid(w, res["melt_result"])

    def test_melt_reply_lost_then_submit_again(self):
        self.mint.fail["melt"] = "after"
        with self.assertRaises(MintUnavailable):
            self.w.submit(self.sig)
        w = self.reopen()
        self.assert_paid(w, w.submit(self.sig))

    def test_change_from_restore_when_quote_state_lacks_it(self):
        self.mint.fail["melt"] = "after"
        with self.assertRaises(MintUnavailable):
            self.w.submit(self.sig)
        orig = self.real.melt_quote_state
        self.real.melt_quote_state = lambda q: dict(orig(q), change=None)
        self.assert_paid(self.w, self.w.recover()["melt_result"])

    def test_melt_never_reached_mint(self):
        self.mint.fail["melt"] = "before"
        with self.assertRaises(MintUnavailable):
            self.w.submit(self.sig)
        w = self.reopen()
        self.assertEqual(w.recover(), {"melt": "not_sent"})
        self.assertFalse(w.in_flight())
        self.assert_paid(w, w.submit(self.sig))  # same signature still valid

    def test_lightning_payment_pending(self):
        self.real.melt_pending = True  # mint takes the inputs, payment in flight
        with self.assertRaises(WalletError):
            self.w.submit(self.sig)
        w = self.reopen()
        self.assertEqual(w.recover(), {"melt": "pending"})
        self.assertTrue(w.in_flight())
        with self.assertRaises(WalletError):
            w.cancel_pending()
        self.real.settle_melt(self.req["quote"])  # payment succeeds later
        res = w.recover()
        self.assertEqual(res["melt"], "done")
        self.assert_paid(w, res["melt_result"])

    def test_lightning_payment_fails(self):
        self.real.melt_pending = True
        with self.assertRaises(WalletError):
            self.w.submit(self.sig)
        self.real.settle_melt(self.req["quote"], ok=False)  # mint gives the inputs back
        self.assertEqual(self.w.recover(), {"melt": "not_sent"})
        self.w.cancel_pending()
        self.assertEqual(self.w.balance() + self.w.reserved_balance(), 100)

    def test_rejected_melt_is_not_in_flight(self):
        self.mint.fail["melt"] = "reject"
        with self.assertRaises(MintRejected):
            self.w.submit(self.sig)
        self.assertFalse(self.w.in_flight())
        self.w.cancel_pending()
        self.assertEqual(self.w.balance() + self.w.reserved_balance(), 100)

    def in_total(self):
        return sum(p["amount"] for p in self.req["inputs"])

    def cancel_after_not_sent(self):
        self.mint.fail["melt"] = "before"
        with self.assertRaises(MintUnavailable):
            self.w.submit(self.sig)
        self.assertEqual(self.w.recover(), {"melt": "not_sent"})
        self.w.cancel_pending()

    def test_cancelled_melt_lands_later(self):
        # the cancel races a melt request still in transit; recover collects it
        self.cancel_after_not_sent()
        self.assertEqual(self.w.reserved_balance(), self.in_total())  # inputs kept out of new sends
        inputs = [dict(p) for p in self.req["inputs"]]
        inputs[0]["witness"] = json.dumps({"signatures": [self.sig["signature"]]})
        self.real.melt(self.req["quote"], inputs, self.req["outputs"])
        res = self.reopen().recover()
        self.assertEqual(len(res["late_melts"]), 1)
        self.assertEqual((self.w.balance(), self.w.reserved_balance()), (self.paid, 0))
        self.assertEqual(self.w.state["abandoned_sends"], [])

    def test_cancelled_melt_released_after_an_hour(self):
        self.cancel_after_not_sent()
        self.w.state["abandoned_sends"][0]["cancelled_at"] -= 2 * 3600
        self.assertEqual(self.w.recover(), {"released": self.in_total()})
        self.assertEqual(self.w.balance(), 100)

    def test_unpaid_quote_with_spent_inputs_fails(self):
        # another transaction spent the inputs: this melt can never complete
        self.mint.fail["melt"] = "before"
        with self.assertRaises(MintUnavailable):
            self.w.submit(self.sig)
        self.real.spent.add(self.req["inputs"][0]["secret"])
        self.assertEqual(self.reopen().recover(), {"melt": "failed"})
        self.assertIsNone(self.w.state["pending"])
        self.assertFalse(self.w.in_flight())
        self.assertEqual(self.w.balance(), 100 - self.req["inputs"][0]["amount"])
        self.w.build_melt_request(make_invoice(5))  # wallet usable again

    def test_change_kept_when_paid_but_unrestorable(self):
        # PAID, no change in the quote state and restore refused: keep the
        # outputs' blinding data and collect the change on a later recover
        self.mint.fail["melt"] = "after"
        with self.assertRaises(MintUnavailable):
            self.w.submit(self.sig)
        orig = self.real.melt_quote_state
        self.real.melt_quote_state = lambda q: dict(orig(q), change=None)
        self.mint.fail["restore"] = "reject"
        self.assertEqual(self.w.recover()["melt"], "done")
        self.assertIsNone(self.w.state["pending"])
        self.assertEqual(self.w.balance(), 100 - self.in_total())
        self.assertEqual(self.reopen().recover(), {"late_change": self.paid - (100 - self.in_total())})
        self.assertEqual(self.w.balance(), self.paid)
        self.assertEqual(self.w.state["unclaimed_change"], [])

    def test_unclaimed_change_dropped_when_restore_finds_none(self):
        # the whole fee reserve was used: restore works but finds nothing; stop retrying
        self.mint.fail["melt"] = "after"
        with self.assertRaises(MintUnavailable):
            self.w.submit(self.sig)
        orig = self.real.melt_quote_state
        self.real.melt_quote_state = lambda q: dict(orig(q), change=None)
        self.mint.fail["restore"] = "reject"
        self.w.recover()
        self.real.restore = lambda outputs: {}
        self.assertEqual(self.w.recover(), {})
        self.assertEqual(self.w.state["unclaimed_change"], [])

    def test_failed_path_rechecks_quote(self):
        # quote UNPAID on the first lookup, then our melt lands: PAID on the second
        self.mint.fail["melt"] = "before"
        with self.assertRaises(MintUnavailable):
            self.w.submit(self.sig)
        orig, calls = self.real.melt_quote_state, []

        def state(q):
            calls.append(q)
            if len(calls) == 1:
                before = orig(q)
                inputs = [dict(p) for p in self.req["inputs"]]
                inputs[0]["witness"] = json.dumps({"signatures": [self.sig["signature"]]})
                self.real.melt(q, inputs, self.req["outputs"])  # lands right after we looked
                return before
            return orig(q)
        self.real.melt_quote_state = state
        res = self.w.recover()
        self.assertEqual(res["melt"], "done")
        self.assert_paid(self.w, res["melt_result"])

    def test_forgotten_quote_of_cancelled_melt_released(self):
        self.cancel_after_not_sent()
        del self.real.melt_quotes[self.req["quote"]]  # the mint answers 404
        self.w.state["abandoned_sends"][0]["cancelled_at"] -= 2 * 3600
        self.assertEqual(self.w.recover(), {"released": self.in_total()})
        self.assertEqual(self.w.balance(), 100)

    def test_cancelled_melt_not_released_while_quote_pending(self):
        self.cancel_after_not_sent()
        self.real.melt_quotes[self.req["quote"]]["state"] = "PENDING"
        self.w.state["abandoned_sends"][0]["cancelled_at"] -= 2 * 3600
        self.assertEqual(self.w.recover(), {})
        self.assertEqual(self.w.reserved_balance(), self.in_total())


class TestFundRecovery(Base):
    def test_mint_reply_lost(self):
        self.mint.fail["mint"] = "after"
        with self.assertRaises(MintUnavailable):
            self.w.fund(100, self.device.pubkey_hex)
        w = self.reopen()
        self.assertTrue(w.in_flight())
        self.assertEqual(w.recover(), {"fund": "done"})
        self.assertEqual(w.balance(), 100)

    def test_mint_never_reached(self):
        self.mint.fail["mint"] = "before"
        with self.assertRaises(MintUnavailable):
            self.w.fund(100, self.device.pubkey_hex)
        w = self.reopen()
        self.assertEqual(w.recover(), {"fund": "done"})  # re-mints with the saved outputs
        self.assertEqual(w.balance(), 100)

    def test_malformed_quote_or_keysets(self):
        class NoRequest(FakeMint):
            def mint_quote(self, amount, unit="sat"):
                return {"state": "PAID"}

        class NoId(FakeMint):
            def keysets(self):
                return [{"unit": "sat", "active": True}]

        for i, mint in enumerate((NoRequest(), NoId())):
            w = HostWallet(os.path.join(self.tmp, f"m{i}.json"), mint, URL)
            self.addCleanup(w.close)
            with self.assertRaises(MintUnavailable):
                w.fund(8, self.device.pubkey_hex)
            self.assertFalse(w.in_flight())

    def test_fund_recovers_without_nut09(self):
        self.mint.fail["mint"] = "before"
        with self.assertRaises(MintUnavailable):
            self.w.fund(100, self.device.pubkey_hex)
        self.mint.fail["restore"] = "reject"  # quote is PAID, not ISSUED: re-mint
        self.assertEqual(self.w.recover(), {"fund": "done"})
        self.assertEqual(self.w.balance(), 100)

    def test_fund_refused_keeps_paid_quote(self):
        self.mint.fail["mint"] = "reject"  # one refusal: retried with fresh outputs
        self.w.fund(100, self.device.pubkey_hex)
        self.assertEqual(self.w.balance(), 100)

        class Refuse(FakeMint):  # refuses every output set, e.g. minting paused
            def mint(self, quote_id, outputs):
                raise MintRejected("minting temporarily disabled")
        self.real.__class__ = Refuse
        with self.assertRaises(MintRejected):
            self.w.fund(50, self.device.pubkey_hex)
        self.assertTrue(self.w.in_flight())  # the PAID quote is not forgotten
        with self.assertRaises(MintRejected):
            self.w.recover()
        pf = self.w.state["pending_fund"]
        self.assertEqual(self.w.abandon_fund(), (pf["quote"], True))  # may still land: archived
        self.assertFalse(self.w.in_flight())
        self.assertEqual(self.w.recover(), {"archived_stuck": [pf["quote"]]})  # reported, kept
        self.real.__class__ = FakeMint  # the mint accepts again
        self.w.fund(5, self.device.pubkey_hex)  # new funding unblocked
        self.assertEqual(self.w.recover(), {"late_funds": 50})  # recover mints the archived quote
        self.assertEqual((self.w.balance(), self.w.state["abandoned_funds"]), (155, []))

    def test_keyset_rotates_during_fund(self):
        self.w.fund(4, self.device.pubkey_hex)  # fills the keyset cache
        self.w.fund(8, self.device.pubkey_hex, on_invoice=lambda _: self.real.rotate_keyset())
        self.assertEqual(self.w.balance(), 12)
        self.assertTrue(all(p["id"] == self.real.keyset_id for p in self.w.state["proofs"][-1:]))

    def test_cli_recover_mentions_archived_funds(self):
        import contextlib
        import io
        import sys
        from unittest import mock
        import host_cli
        self.mint.fail["mint"] = "before"
        with self.assertRaises(MintUnavailable):
            self.w.fund(8, self.device.pubkey_hex)
        orig = self.real.mint_quote_state
        self.real.mint_quote_state = lambda q: dict(orig(q), state="PENDING")
        self.w.abandon_fund()
        self.w.close()
        out = io.StringIO()
        with mock.patch.object(host_cli, "MintClient", lambda url: self.real), \
                mock.patch.object(sys, "argv", ["host_cli.py", "--mint", URL, "--wallet", self.path, "recover"]), \
                contextlib.redirect_stdout(out):
            host_cli.main()
        self.assertIn("1 archived funding(s) still waiting", out.getvalue())
        self.assertNotIn("Nothing to recover", out.getvalue())
        self.w = HostWallet(self.path, self.mint, URL)  # for cleanup

    def test_archived_pending_quote_becomes_paid(self):
        self.mint.fail["mint"] = "before"
        with self.assertRaises(MintUnavailable):
            self.w.fund(8, self.device.pubkey_hex)
        orig = self.real.mint_quote_state
        self.real.mint_quote_state = lambda q: dict(orig(q), state="PENDING")
        self.assertTrue(self.w.abandon_fund()[1])
        self.assertEqual(self.w.recover(), {})  # still PENDING: kept, not minted
        self.real.mint_quote_state = orig  # now PAID with no request in flight
        self.assertEqual(self.reopen().recover(), {"late_funds": 8})
        self.assertEqual((self.w.balance(), self.w.state["abandoned_funds"]), (8, []))

    def test_archived_fund_after_keyset_rotation(self):
        self.mint.fail["mint"] = "before"
        with self.assertRaises(MintUnavailable):
            self.w.fund(8, self.device.pubkey_hex)
        orig = self.real.mint_quote_state
        self.real.mint_quote_state = lambda q: dict(orig(q), state="PENDING")
        self.assertTrue(self.w.abandon_fund()[1])
        self.real.mint_quote_state = orig  # PAID, nothing in flight
        self.real.rotate_keyset()  # archived outputs are now on an inactive keyset
        w = self.reopen()
        self.assertEqual(w.recover(), {"late_funds": 8})  # refused, retried with fresh outputs
        self.assertEqual(w.balance(), 8)
        self.assertTrue(all(p["id"] == self.real.keyset_id for p in w.state["proofs"]))

    def land(self, pf):
        """Model a slow mint request of ours that completes at the mint now."""
        FakeMint.mint(self.real, pf["quote"],
                      [{k: o[k] for k in ("amount", "id", "B_")} for o in pf["outputs"]])

    def test_pending_quote_after_timed_out_mint(self):
        # the mint() call timed out; the mint is still processing (quote PENDING)
        self.mint.fail["mint"] = "before"
        with self.assertRaises(MintUnavailable):
            self.w.fund(8, self.device.pubkey_hex)
        orig = self.real.mint_quote_state
        self.real.mint_quote_state = lambda q: dict(orig(q), state="PENDING")
        self.assertEqual(self.w.recover(), {"fund": "mint_pending"})
        self.assertTrue(self.w.in_flight())
        pf = self.w.state["pending_fund"]
        self.assertTrue(self.w.abandon_fund()[1])  # archived, not deleted
        self.assertIsNone(self.w.state["pending_fund"])
        self.assertEqual(self.w.recover(), {})  # nothing yet; entry kept
        self.land(pf)  # the slow request lands
        self.assertEqual(self.reopen().recover(), {"late_funds": 8})
        self.assertEqual(self.w.balance(), 8)

    def test_abandon_paid_quote_never_minted(self):
        orig = self.real.mint_quote

        def unpaid(amount, unit="sat"):
            q = orig(amount, unit)
            self.real.quotes[q["quote"]]["state"] = q["state"] = "UNPAID"
            return q
        self.real.mint_quote = unpaid
        with self.assertRaises(WalletError):
            self.w.fund(8, self.device.pubkey_hex, wait_s=0)
        quote = self.w.state["pending_fund"]["quote"]
        self.real.quotes[quote]["state"] = "PAID"  # paid, but we never called mint()
        self.assertEqual(self.w.abandon_fund(), (quote, False))  # deleted: claim elsewhere

    def test_slow_first_mint_lands_after_retry(self):
        # mint(A) times out but is still running; the re-mint is refused
        # ("quote pending"), A is replaced by B, then the first request signs A
        real, late = self.real, []

        def mint(quote_id, outputs):
            if not late:
                late.append(outputs)
                raise MintUnavailable("timeout")
            if late[0] is not None:
                late.append(late[0])
                late[0] = None
                raise MintRejected("quote pending")
            if len(late) == 2:
                FakeMint.mint(real, quote_id, late.pop())  # A lands now
            return FakeMint.mint(real, quote_id, outputs)
        real.mint = mint
        with self.assertRaises(MintUnavailable):
            self.w.fund(8, self.device.pubkey_hex)
        with self.assertRaises(MintRejected):  # B refused: quote now ISSUED to A
            self.w.recover()
        with self.assertRaises(WalletError):  # A is restorable: no abandoning
            self.w.abandon_fund()
        self.assertEqual(self.reopen().recover(), {"fund": "done"})
        self.assertEqual(self.w.balance(), 8)

    def test_invoice_paid_late(self):
        orig = self.real.mint_quote

        def unpaid(amount, unit="sat"):
            q = orig(amount, unit)
            self.real.quotes[q["quote"]]["state"] = q["state"] = "UNPAID"
            return q
        self.real.mint_quote = unpaid
        with self.assertRaises(WalletError):
            self.w.fund(100, self.device.pubkey_hex, wait_s=0)
        quote = self.reopen().state["pending_fund"]["quote"]  # saved before the invoice
        self.assertFalse(self.w.in_flight())  # unpaid: never reached mint()
        self.assertEqual(self.w.recover(), {"fund": "waiting"})
        self.real.quotes[quote]["state"] = "PAID"
        self.assertEqual(self.w.recover(), {"fund": "done"})
        self.assertEqual(self.w.balance(), 100)
        # an UNPAID quote is only dropped once it has expired
        with self.assertRaises(WalletError):
            self.w.fund(5, self.device.pubkey_hex, wait_s=0)
        self.real.quotes[self.w.state["pending_fund"]["quote"]]["expiry"] = 1
        self.assertEqual(self.w.recover(), {"fund": "expired"})
        self.assertFalse(self.w.in_flight())

    def test_abandon_archives_when_issued_without_nut09(self):
        self.mint.fail["mint"] = "after"  # issued, reply lost
        with self.assertRaises(MintUnavailable):
            self.w.fund(8, self.device.pubkey_hex)
        self.mint.fail["restore"] = "reject"
        self.mint.fail["mint_quote_state"] = "before"  # can't check: archive, keep secrets
        self.assertTrue(self.w.abandon_fund()[1])
        self.assertEqual(len(self.w.state["abandoned_funds"]), 1)
        self.assertEqual(self.w.recover(), {"late_funds": 8})  # restore works again
        self.assertEqual(self.w.balance(), 8)

    def test_abandoned_fund_does_not_lock_key(self):
        self.real.mint_quote = lambda a, unit="sat": {"quote": "q", "request": "lnbc", "state": "UNPAID"}
        self.real.quotes["q"] = {"state": "UNPAID"}
        with self.assertRaises(WalletError):
            self.w.fund(8, self.device.pubkey_hex, wait_s=0)
        self.w.abandon_fund()
        self.assertIsNone(self.w.state["device_pubkey"])

    def test_fund_blocked_until_recovered(self):
        self.mint.fail["mint"] = "after"
        with self.assertRaises(MintUnavailable):
            self.w.fund(100, self.device.pubkey_hex)
        with self.assertRaises(WalletError):
            self.reopen().fund(5, self.device.pubkey_hex)


# ------------------------------------------------------------------ 2. one wallet, one mint

class TestWalletMint(Base):
    def test_other_mint_refused(self):
        self.w.fund(10, self.device.pubkey_hex)
        self.w.close()
        with self.assertRaises(WalletError) as e:
            HostWallet(self.path, self.mint, "https://testnut.cashu.space")
        self.assertIn("belongs to", str(e.exception))
        HostWallet(self.path, self.mint, URL + "/").close()  # trailing slash is the same mint

    def test_other_device_key_refused(self):
        self.w.fund(10, self.device.pubkey_hex)
        other = DeviceSigner.from_mnemonic(
            "legal winner thank year wave sausage worth useful legal winner thank yellow")
        with self.assertRaises(WalletError):
            self.w.fund(10, other.pubkey_hex)


# ------------------------------------------------------------------ 3. recipient key

class TestRecipientKey(Base):
    def test_bad_keys_refused_and_nothing_pending(self):
        self.w.fund(50, self.device.pubkey_hex)
        good = self.device.pubkey_hex
        for bad in ("02" + "00" * 32,             # x = 0 is not on the curve
                    good[:-1],                    # one character short
                    good[:-1] + "g",              # not hex
                    "04" + good[2:],              # wrong prefix
                    ""):
            with self.subTest(bad=bad):
                with self.assertRaises(WalletError):
                    self.w.build_send_request(5, to_pubkey=bad)
                self.assertIsNone(self.w.state["pending"])
        self.w.build_send_request(5, to_pubkey=good.upper())  # upper case is fine
        self.assertEqual(self.w.state["pending"]["outputs"][0]["role"], "recipient")


# ------------------------------------------------------------------ 5. malformed requests

class TestMalformedRequests(Base):
    def test_never_crashes(self):
        self.w.fund(100, self.device.pubkey_hex)
        req = self.w.build_send_request(21)
        mine = self.device.pubkey_hex

        def mutate(fn):
            import copy
            r = copy.deepcopy(req)
            fn(r)
            return r

        def p2pk(data, tags=None):
            import json
            return json.dumps(["P2PK", {"nonce": "00", "data": data,
                                        "tags": tags if tags is not None else [["sigflag", "SIG_ALL"]]}])

        cases = {
            "not an object": [],
            "inputs is a string": mutate(lambda r: r.update(inputs="abc")),
            "input is null": mutate(lambda r: r["inputs"].__setitem__(0, None)),
            "amount is bool": mutate(lambda r: r["outputs"][0].update(amount=True)),
            "B_ is int": mutate(lambda r: r["outputs"][0].update(B_=5)),
            "input tags empty list": mutate(lambda r: r["inputs"][0].update(secret=p2pk(mine, [[]]))),
            "input tags a dict": mutate(lambda r: r["inputs"][0].update(secret=p2pk(mine, {"a": 1}))),
            "input data a list": mutate(lambda r: r["inputs"][0].update(secret=p2pk([mine]))),
            "reveal not a dict": mutate(lambda r: r["reveals"].__setitem__(0, "x")),
            "reveal r not hex": mutate(lambda r: r["reveals"][0].update(r="zz")),
            "reveals a string": mutate(lambda r: r.update(reveals="abc")),
        }
        # a reveal whose P2PK data is a list: B_ must match, so build a real output
        r_ = crypto.random_scalar()
        secret = p2pk(["not", "a", "key"])
        lst = mutate(lambda r: None)
        lst["outputs"][0]["B_"] = crypto.blind(secret, r_)
        lst["reveals"][0] = {"index": 0, "secret": secret, "r": format(r_, "064x")}
        cases["revealed lock data is a list"] = lst

        for name, bad in cases.items():
            with self.subTest(name):
                with self.assertRaises(Rejected):
                    self.device.review(bad)
                with self.assertRaises(Rejected):
                    self.device.sign(bad)


# ------------------------------------------------------------------ 6. keysets fetched once

class TestKeysetCaching(Base):
    def test_one_keysets_call_per_send(self):
        for _ in range(8):  # many small proofs so selection loops
            self.reopen().fund(3, self.device.pubkey_hex)
        w = self.reopen()
        before = self.mint.calls.get("keysets", 0)
        w.build_send_request(20)
        self.assertEqual(self.mint.calls["keysets"] - before, 1)


# ------------------------------------------------------------------ 6b. CLI errors

class TestCliErrors(unittest.TestCase):
    def run_cli(self, *argv):
        import sys
        from unittest import mock
        import host_cli
        with mock.patch.object(host_cli, "MintClient", lambda url: self.mint), \
                mock.patch.object(sys, "argv", ["host_cli.py", *argv]), \
                self.assertRaises(SystemExit) as e:
            host_cli.main()
        return str(e.exception.code)

    def test_cli_melt_then_submit(self):
        import contextlib
        import io
        import sys
        from unittest import mock
        import host_cli
        mint, tmp = FakeMint(), tempfile.mkdtemp()
        wp, rp, sp = (os.path.join(tmp, n) for n in ("w.json", "r.json", "s.json"))
        dev = DeviceSigner.from_mnemonic(MNEMONIC)
        w = HostWallet(wp, mint, host_cli.DEFAULT_MINT)
        w.fund(50, dev.pubkey_hex)
        w.close()
        out = io.StringIO()

        def cli(*argv):
            with mock.patch.object(host_cli, "MintClient", lambda url: mint), \
                    mock.patch.object(sys, "argv", ["host_cli.py", "--wallet", wp, *argv]), \
                    contextlib.redirect_stdout(out):
                host_cli.main()
        cli("melt", make_invoice(20), "-o", rp)
        with open(rp) as f, open(sp, "w") as g:
            json.dump(dev.sign(json.load(f)), g)
        cli("submit", sp)
        self.assertIn("Invoice paid", out.getvalue())
        self.assertIn(f"Balance: {50 - 20 - mint.ln_fee} sat", out.getvalue())

    def test_os_errors_are_clean(self):
        self.mint = FakeMint()
        tmp = tempfile.mkdtemp()
        wp = os.path.join(tmp, "w.json")
        self.assertIn("Error:", self.run_cli("--wallet", "/nonexistent/dir/w.json", "balance"))
        with open(wp, "w") as f:
            f.write("{corrupt")
        self.assertIn("Error:", self.run_cli("--wallet", wp, "balance"))
        os.remove(wp)
        import host_cli
        w = HostWallet(wp, self.mint, host_cli.DEFAULT_MINT)
        w.fund(10, DeviceSigner.from_mnemonic(MNEMONIC).pubkey_hex)
        w.close()
        msg = self.run_cli("--wallet", wp, "send", "3", "-o", "/nonexistent/dir/r.json")
        self.assertIn("run cancel", msg)
        self.assertNotIn("outcome is unknown", msg)


# ------------------------------------------------------------------ 7. network errors over real HTTP

class TestHttpErrors(unittest.TestCase):
    def test_unreachable_mint(self):
        with self.assertRaises(MintUnavailable):
            MintClient("http://127.0.0.1:9", timeout=2).info()

    def test_http_server_roundtrip_and_restore(self):
        from tests import fake_mint_server as srv
        srv.MINT = FakeMint()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            client = MintClient(url)
            with self.assertRaises(MintRejected):  # a 400 from the mint
                client.swap([], [{"amount": 1, "id": "00", "B_": "02"}])
            tmp = tempfile.mkdtemp()
            dev = DeviceSigner.from_mnemonic(MNEMONIC)
            w = HostWallet(os.path.join(tmp, "w.json"), client, url)
            w.fund(40, dev.pubkey_hex)
            req = w.build_send_request(7)
            # simulate a lost reply: do the swap behind the wallet's back, then recover
            w.state["pending"]["submitted"] = True
            w.save()
            inputs = [dict(p) for p in req["inputs"]]
            inputs[0]["witness"] = json.dumps({"signatures": [dev.sign(req)["signature"]]})
            client.swap(inputs, req["outputs"])
            w.close()
            w = HostWallet(os.path.join(tmp, "w.json"), client, url)
            self.assertEqual(w.recover()["send"], "done")
            w.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_melt_over_http(self):
        from tests import fake_mint_server as srv
        srv.MINT = FakeMint()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            dev = DeviceSigner.from_mnemonic(MNEMONIC)
            client = MintClient(url)
            w = HostWallet(os.path.join(tempfile.mkdtemp(), "w.json"), client, url)
            self.addCleanup(w.close)
            w.fund(40, dev.pubkey_hex)
            req = w.build_melt_request(make_invoice(7))
            preimage, change = w.submit(dev.sign(req))
            self.assertEqual(client.melt_quote_state(req["quote"])["state"], "PAID")
            self.assertEqual(w.balance(), 40 - 7 - srv.MINT.ln_fee)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_empty_200_swap_reply_is_unknown_outcome(self):
        from tests import fake_mint_server as srv

        class EmptySwap(srv.Handler):  # processes the swap, then replies {}
            def do_POST(self):
                if self.path != "/v1/swap":
                    return super().do_POST()
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                srv.MINT.swap(body["inputs"], body["outputs"])
                self._send(200, {})

        srv.MINT = FakeMint()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), EmptySwap)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            dev = DeviceSigner.from_mnemonic(MNEMONIC)
            w = HostWallet(os.path.join(tempfile.mkdtemp(), "w.json"), MintClient(url), url)
            w.fund(40, dev.pubkey_hex)
            req = w.build_send_request(7)
            with self.assertRaises(MintUnavailable):
                w.submit(dev.sign(req))
            self.assertTrue(w.in_flight())
            self.assertEqual(w.recover()["send"], "done")
            self.assertEqual(w.balance(), 33)
            w.close()
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
