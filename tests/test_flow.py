"""End-to-end flow and attack tests against the in-process mint."""

import copy
import json
import os
import tempfile
import unittest

from nutsigner import crypto
from nutsigner.device import DeviceSigner, Rejected, summary_lines
from nutsigner.host import HostWallet
from nutsigner.mint import MintError
from tests.fake_mint import FakeMint

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


if __name__ == "__main__":
    unittest.main()
