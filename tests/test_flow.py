"""End-to-end flow and attack tests against the in-process mint."""

import copy
import json
import os
import tempfile
import unittest

from nutsigner import crypto, qr, wire
from nutsigner.device import DeviceSigner, Rejected, summary_lines
from nutsigner.host import HostWallet, WalletError
from nutsigner.mint import MintError
from tests.fake_mint import FakeMint, make_invoice

MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


class FlowBase(unittest.TestCase):
    fee_ppk = 0

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mint = FakeMint(input_fee_ppk=self.fee_ppk)
        self.device = DeviceSigner.from_mnemonic(MNEMONIC)
        self.wallet = HostWallet(os.path.join(self.tmp, "w.json"), self.mint, "https://fake.mint")
        self.addCleanup(self.wallet.close)
        self.wallet.fund(100, self.device.pubkey_hex)


class TestHappyPath(FlowBase):
    def test_fund_send_submit(self):
        self.assertEqual(self.wallet.balance(), 100)
        req = self.wallet.build_send_request(21)
        review = self.device.review(req)
        self.assertEqual(review["sending"], 21)
        self.assertEqual(review["change"], review["in_total"] - 21 - review["fee"])
        self.assertEqual(review["unverified"], 0)
        sig = self.device.sign(req)
        token, sent, change = self.wallet.submit(sig)
        self.assertTrue(token.startswith("cashuA"))
        self.assertEqual(sum(p["amount"] for p in sent), 21)
        self.assertEqual(self.wallet.balance(), 79)
        # change is locked back to the device
        for p in change:
            self.assertEqual(crypto.parse_secret(p["secret"])[1], self.device.pubkey_hex)
        # recipient proofs are valid, plain bearer ecash: spend them without any witness
        out = [{"amount": p["amount"], "id": p["id"], "B_": crypto.blind(os.urandom(32).hex(), crypto.random_scalar())}
               for p in sent]
        self.mint.swap(sent, out)

    def test_send_to_pubkey_is_shown(self):
        other = DeviceSigner.from_mnemonic("legal winner thank year wave sausage worth useful legal winner thank yellow")
        req = self.wallet.build_send_request(10, to_pubkey=other.pubkey_hex)
        review = self.device.review(req)
        self.assertEqual(review["recipients"][0]["kind"], "locked")
        self.assertEqual(review["recipients"][0]["pubkey"], other.pubkey_hex)
        print("\n  device screen:\n   " + "\n   ".join(summary_lines(review)))

    def test_locked_proofs_unspendable_without_device(self):
        """A thief with the wallet file but no device signature gets nothing."""
        proofs = self.wallet.state["proofs"]
        out = [{"amount": p["amount"], "id": p["id"], "B_": crypto.blind(os.urandom(32).hex(), crypto.random_scalar())}
               for p in proofs]
        with self.assertRaises(MintError):
            self.mint.swap(proofs, out)


class TestAttacks(FlowBase):
    def test_host_swaps_outputs_after_signing(self):
        req = self.wallet.build_send_request(21)
        sig = self.device.sign(req)
        # malicious host redirects the change output to itself
        evil = copy.deepcopy(req)
        idx = next(i for i, o in enumerate(self.wallet.state["pending"]["outputs"]) if o["role"] == "change")
        evil["outputs"][idx]["B_"] = crypto.blind(os.urandom(32).hex(), crypto.random_scalar())
        inputs = [dict(p) for p in evil["inputs"]]
        inputs[0]["witness"] = json.dumps({"signatures": [sig["signature"]]})
        with self.assertRaises(MintError):
            self.mint.swap(inputs, evil["outputs"])

    def test_host_lies_in_reveal(self):
        req = self.wallet.build_send_request(21)
        idx = next(i for i, o in enumerate(self.wallet.state["pending"]["outputs"]) if o["role"] == "recipient")
        # claim a recipient output is change by revealing a different secret for it
        req["reveals"][idx]["secret"] = crypto.make_p2pk_secret(self.device.pubkey_hex)
        with self.assertRaises(Rejected):
            self.device.review(req)

    def test_hidden_outputs_are_flagged(self):
        req = self.wallet.build_send_request(21)
        req["reveals"] = []
        review = self.device.review(req)
        self.assertEqual(review["change"], 0)
        self.assertEqual(review["unverified"], len(req["outputs"]))
        self.assertIn("! Unverified outputs", summary_lines(review))

    def test_foreign_inputs_rejected(self):
        req = self.wallet.build_send_request(21)
        other = DeviceSigner.from_mnemonic("legal winner thank year wave sausage worth useful legal winner thank yellow")
        with self.assertRaises(Rejected):
            other.review(req)

    def test_sig_inputs_style_signature_rejected_by_mint(self):
        """Signing each secret alone (SIG_INPUTS) must not unlock SIG_ALL proofs."""
        req = self.wallet.build_send_request(21)
        inputs = [dict(p) for p in req["inputs"]]
        for p in inputs:
            s = crypto.schnorr_sign(self.device.privkey, p["secret"])
            p["witness"] = json.dumps({"signatures": [s]})
        with self.assertRaises(MintError):
            self.mint.swap(inputs, req["outputs"])

    def test_spending_limit(self):
        dev = DeviceSigner(self.device.privkey, max_send=10)
        req = self.wallet.build_send_request(21)
        with self.assertRaises(Rejected):
            dev.review(req)


class TestWithFees(FlowBase):
    fee_ppk = 600  # 0.6 sat per input, like testnut's fee mint

    def test_fees_accounted(self):
        req = self.wallet.build_send_request(21)
        review = self.device.review(req)
        self.assertGreater(review["fee"], 0)
        token, sent, change = self.wallet.submit(self.device.sign(req))
        self.assertEqual(self.wallet.balance(), 100 - 21 - review["fee"])

    def test_melt_with_input_fees(self):
        req = self.wallet.build_melt_request(make_invoice(30))
        in_fee = self.wallet._fee(req["inputs"])
        self.assertGreater(in_fee, 0)
        self.assertGreaterEqual(sum(p["amount"] for p in req["inputs"]), 30 + req["fee_reserve"] + in_fee)
        self.wallet.submit(self.device.sign(req))
        self.assertEqual(self.wallet.balance(), 100 - 30 - self.mint.ln_fee - in_fee)


class TestMelt(FlowBase):
    """Pay a Lightning invoice from locked ecash (NUT-05 + NUT-08 + SIG_ALL)."""

    def setUp(self):
        super().setUp()
        self.invoice = make_invoice(30)
        self.req = self.wallet.build_melt_request(self.invoice)

    def change_index(self):
        return next(i for i, o in enumerate(self.wallet.state["pending"]["outputs"]) if o["role"] == "change")

    def test_end_to_end_over_qr(self):
        req = self.req
        self.assertEqual((req["type"], req["invoice"], req["amount"]), ("melt", self.invoice, 30))
        self.assertEqual(req["fee_reserve"], self.mint.melt_fee_reserve)
        self.assertTrue(all(o["amount"] == 0 for o in req["outputs"]))  # NUT-08 blank outputs
        # host -> device: animated QR of the v2 bytes
        frames = qr.frames(wire.encode_request(req))
        a = qr.Assembler()
        for _ in range(frames.count * 3):
            if a.add(next(frames)):
                break
        on_device = wire.decode_request(a.result())
        self.assertEqual(on_device, req)
        review = self.device.review(on_device)
        self.assertEqual((review["sending"], review["fee_reserve"]), (30, self.mint.melt_fee_reserve))
        lines = summary_lines(review)
        print("\n  device screen:\n   " + "\n   ".join(lines))
        self.assertIn("Pay 30 sat (Lightning)", lines)
        # device -> host: single signature QR
        sig = wire.decode_signature(qr.read_single(qr.single(wire.encode_signature(self.device.sign(on_device)))))
        preimage, change = self.wallet.submit(sig)
        self.assertEqual(self.mint.melt_quotes[req["quote"]]["state"], "PAID")
        self.assertEqual(preimage, self.mint.melt_quotes[req["quote"]]["payment_preimage"])
        in_total = sum(p["amount"] for p in req["inputs"])
        self.assertEqual(sum(p["amount"] for p in change), in_total - 30 - self.mint.ln_fee)
        self.assertEqual(self.wallet.balance(), 100 - 30 - self.mint.ln_fee)
        self.assertIsNone(self.wallet.state["pending"])
        for p in change:  # change is locked to the device again...
            self.assertEqual(crypto.parse_secret(p["secret"])[1], self.device.pubkey_hex)
        # ...and spendable with the device: send everything that is left
        bal = self.wallet.balance()
        r2 = self.wallet.build_send_request(bal)
        self.wallet.submit(self.device.sign(r2))
        self.assertEqual(self.wallet.balance(), 0)

    def test_invoice_quote_amount_mismatch(self):
        for bad in (dict(self.req, amount=29), dict(self.req, invoice=make_invoice(31))):
            with self.assertRaises(Rejected):
                self.device.review(bad)

    def test_amountless_invoice_rejected(self):
        with self.assertRaises(Rejected):
            self.device.review(dict(self.req, invoice=make_invoice(None)))

    def test_garbage_invoice_rejected(self):
        inv = self.invoice
        for bad in (inv[:-1] + ("q" if inv[-1] != "q" else "p"), "lnbc1", "", "bc1qxyz", 5):
            with self.subTest(bad=bad), self.assertRaises(Rejected):
                self.device.review(dict(self.req, invoice=bad))

    def test_non_change_output_rejected(self):
        idx = self.change_index()
        secret, r = os.urandom(32).hex(), crypto.random_scalar()
        req = copy.deepcopy(self.req)
        req["outputs"][idx]["B_"] = crypto.blind(secret, r)  # a bearer output, honestly revealed
        req["reveals"][idx].update(secret=secret, r=format(r, "064x"))
        with self.assertRaises(Rejected):
            self.device.review(req)
        hidden = copy.deepcopy(self.req)
        hidden["reveals"] = hidden["reveals"][1:]  # one output without a reveal
        with self.assertRaises(Rejected):
            self.device.review(hidden)

    def test_tampered_change_reveal(self):
        req = copy.deepcopy(self.req)
        req["reveals"][self.change_index()]["secret"] = crypto.make_p2pk_secret(self.device.pubkey_hex)
        with self.assertRaises(Rejected):
            self.device.review(req)

    def test_inputs_must_cover_amount_and_fee_reserve(self):
        in_total = sum(p["amount"] for p in self.req["inputs"])
        with self.assertRaises(Rejected):
            self.device.review(dict(self.req, fee_reserve=in_total - 30 + 1))

    def test_spending_limit_counts_fee_reserve(self):
        limit = 30 + self.req["fee_reserve"]
        DeviceSigner(self.device.privkey, max_send=limit).review(self.req)
        with self.assertRaises(Rejected):
            DeviceSigner(self.device.privkey, max_send=limit - 1).review(self.req)

    def test_signature_reused_for_other_quote_rejected_by_mint(self):
        sig = self.device.sign(self.req)
        other = self.mint.melt_quote(self.invoice, "sat")["quote"]
        inputs = [dict(p) for p in self.req["inputs"]]
        inputs[0]["witness"] = json.dumps({"signatures": [sig["signature"]]})
        with self.assertRaises(MintError):
            self.mint.melt(other, inputs, self.req["outputs"])
        self.mint.melt(self.req["quote"], inputs, self.req["outputs"])  # the signed quote works

    def test_swap_signature_does_not_unlock_melt(self):
        swap_sig = crypto.schnorr_sign(self.device.privkey,
                                       crypto.sig_all_message(self.req["inputs"], self.req["outputs"]))
        with self.assertRaises(WalletError):
            self.wallet.submit({"signature": swap_sig})


if __name__ == "__main__":
    unittest.main()
