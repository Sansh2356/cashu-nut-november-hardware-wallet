"""Two NutSigner users: Alice sends ecash locked to Bob's device, Bob receives it and pays an invoice."""

import base64
import json
import os
import tempfile
import unittest

from nutsigner import crypto, qr, wire
from nutsigner.device import DeviceSigner, summary_lines
from nutsigner.host import HostWallet, WalletError
from nutsigner.mint import MintUnavailable
from tests.fake_mint import FakeMint, make_invoice
from tests.test_fixes import Flaky

ALICE = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
BOB = "legal winner thank year wave sausage worth useful legal winner thank yellow"
CAROL = "letter advice cage absurd amount doctor acoustic avoid letter advice cage above"
URL = "https://fake.mint"


def over_qr(req):
    """Host -> device as the animated QR carries it."""
    frames = qr.frames(wire.encode_request(req))
    a = qr.Assembler()
    for _ in range(frames.count * 3):
        if a.add(next(frames)):
            break
    return wire.decode_request(a.result())


def sign_over_qr(device, req):
    return wire.decode_signature(qr.read_single(qr.single(wire.encode_signature(device.sign(req)))))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mint = FakeMint(input_fee_ppk=100)  # like testnut
        self.alice_dev = DeviceSigner.from_mnemonic(ALICE)
        self.bob_dev = DeviceSigner.from_mnemonic(BOB)
        self.alice = self.wallet("alice.json")
        self.bob = self.wallet("bob.json")
        self.alice.fund(100, self.alice_dev.pubkey_hex)

    def wallet(self, name, mint=None, url=URL):
        w = HostWallet(os.path.join(self.tmp, name), mint or self.mint, url)
        self.addCleanup(w.close)
        return w

    def send(self, amount, to):
        req = self.alice.build_send_request(amount, to_pubkey=to)
        token, sent, _ = self.alice.submit(self.alice_dev.sign(req))
        self.alice.clear_tokens()
        return token


class TestDemo(Base):
    def test_mint_send_locked_receive_melt(self):
        # 2. Alice -> Bob, locked to Bob's device with SIG_ALL
        req = self.alice.build_send_request(21, to_pubkey=self.bob_dev.pubkey_hex)
        lines = summary_lines(self.alice_dev.review(over_qr(req)))
        self.assertIn(f" 21 to key {self.bob_dev.pubkey_hex[2:10]}..", lines)
        token, sent, _ = self.alice.submit(sign_over_qr(self.alice_dev, over_qr(req)))
        self.assertEqual(self.alice.balance(), 100 - 21 - 1)
        for p in sent:
            _, data, tags = crypto.parse_secret(p["secret"])
            self.assertEqual((data, tags), (self.bob_dev.pubkey_hex, [["sigflag", "SIG_ALL"]]))

        # 3. Bob receives: his device approves swapping them into his own locked coins
        req = self.bob.build_receive_request(token, self.bob_dev.pubkey_hex)
        on_device = over_qr(req)
        review = self.bob_dev.review(on_device)
        self.assertEqual((review["sending"], review["in_total"]), (0, 21))
        lines = summary_lines(review)
        print("\n  Bob's device:\n   " + "\n   ".join(lines))
        self.assertEqual(lines[:2], ["Receive 21 sat", f"Fee {review['fee']} sat"])
        _, sent, change = self.bob.submit(sign_over_qr(self.bob_dev, on_device))
        self.assertEqual(sent, [])
        self.assertEqual(self.bob.balance(), 21 - review["fee"])
        self.assertFalse(self.bob.state.get("undelivered_tokens"))
        token_secrets = {p["secret"] for p in json.loads(base64.urlsafe_b64decode(token[6:]))["token"][0]["proofs"]}
        for p in change:
            self.assertNotIn(p["secret"], token_secrets)  # new coins Alice never saw
            self.assertEqual(crypto.parse_secret(p["secret"])[1], self.bob_dev.pubkey_hex)

        # 4. Bob's device approves paying an invoice
        req = self.bob.build_melt_request(make_invoice(10))
        lines = summary_lines(self.bob_dev.review(over_qr(req)))
        self.assertEqual(lines[0], "Pay 10 sat (Lightning)")
        preimage, _ = self.bob.submit(sign_over_qr(self.bob_dev, over_qr(req)))
        self.assertEqual(self.mint.melt_quotes[req["quote"]]["state"], "PAID")
        self.assertLess(self.bob.balance(), 10)


class TestReceiveRefusals(Base):
    def assert_refused(self, token, words, pubkey=None):
        with self.assertRaises(WalletError) as cm:
            self.bob.build_receive_request(token, pubkey or self.bob_dev.pubkey_hex)
        self.assertIn(words, str(cm.exception))
        self.assertIsNone(self.bob.state["pending"])

    def test_locked_to_another_key(self):
        carol = DeviceSigner.from_mnemonic(CAROL)
        self.assert_refused(self.send(21, carol.pubkey_hex), "locked to a different key")

    def test_unlocked_token(self):
        self.assert_refused(self.send(21, None), "not locked")

    def test_sig_inputs_lock(self):
        # a plain P2PK (SIG_INPUTS) token made by some other wallet
        outs = self.alice._make_outputs([1, 4], self.mint.keyset_id,
                                        lambda: crypto.make_p2pk_secret(self.bob_dev.pubkey_hex, "SIG_INPUTS"),
                                        "recipient")
        proofs = self.mint_proofs(outs)
        self.assert_refused(self.token(proofs), "SIG_ALL")

    def mint_proofs(self, outs):
        q = self.mint.mint_quote(sum(o["amount"] for o in outs))
        return self.alice._unblind(outs, self.mint.mint(q["quote"], [{k: o[k] for k in ("amount", "id", "B_")}
                                                                    for o in outs]))

    def token(self, proofs, mint=URL):
        from nutsigner.host import encode_token_v3
        return encode_token_v3(mint, proofs)

    def test_other_mint(self):
        token = self.send(21, self.bob_dev.pubkey_hex)
        proofs = json.loads(base64.urlsafe_b64decode(token[6:]))["token"][0]["proofs"]
        self.assert_refused(self.token(proofs, "https://other.mint"), "other.mint")

    def test_double_receive(self):
        token = self.send(21, self.bob_dev.pubkey_hex)
        req = self.bob.build_receive_request(token, self.bob_dev.pubkey_hex)
        self.bob.submit(self.bob_dev.sign(req))
        self.assert_refused(token, "already been spent")
        self.assertEqual(self.bob.balance(), 20)

    def test_garbage(self):
        for bad in ("cashuAnotbase64!", "hello", "cashuB" + "x" * 20):
            self.assert_refused(bad, "token")

    def test_device_key_must_match_wallet(self):
        self.bob.fund(10, self.bob_dev.pubkey_hex)
        token = self.send(21, self.bob_dev.pubkey_hex)
        self.assert_refused(token, "different device key", pubkey=self.alice_dev.pubkey_hex)

    def test_fresh_wallet_needs_device_key(self):
        token = self.send(21, self.bob_dev.pubkey_hex)
        with self.assertRaises(WalletError) as cm:
            self.bob.build_receive_request(token)
        self.assertIn("--pubkey", str(cm.exception))


class TestReceiveRecovery(Base):
    def test_swap_reply_lost_then_recover(self):
        token = self.send(21, self.bob_dev.pubkey_hex)
        flaky = Flaky(self.mint)
        self.bob.close()
        bob = self.wallet("bob.json", flaky)
        req = bob.build_receive_request(token, self.bob_dev.pubkey_hex)
        flaky.fail["swap"] = "after"
        with self.assertRaises(MintUnavailable):
            bob.submit(self.bob_dev.sign(req))
        self.assertTrue(bob.in_flight())
        result = bob.recover()
        self.assertEqual(result["send"], "done")
        self.assertEqual(bob.balance(), 20)
        self.assertFalse(bob.state.get("undelivered_tokens"))


if __name__ == "__main__":
    unittest.main()
