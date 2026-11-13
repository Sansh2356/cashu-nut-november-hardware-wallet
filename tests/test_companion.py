"""Companion app API, end to end over HTTP, with a DeviceSigner standing in for the Pi.

Every call goes through the real server exactly as the browser makes it:
JSON in, JSON out, QR frame strings out, the scanned signature QR text in.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import companion
from nutsigner import qr, wire
from nutsigner.device import DeviceSigner
from nutsigner.host import HostWallet
from nutsigner.mint import MintClient
from tests.fake_mint import FakeMint, make_invoice
from tests.test_fixes import Flaky
from tests.test_receive import ALICE, BOB, URL


class App:
    """One companion server on a free port, and the calls the UI makes."""

    def __init__(self, test, wallet, mint, url=URL):
        self.wallet = wallet
        self.server = companion.make_server(wallet, mint, url, port=0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def get(self, path):
        status, out = self.call("GET", path)
        assert status == 200, out
        return out

    def post(self, path, body=None, expect=200):
        status, out = self.call("POST", path, body or {})
        assert status == expect, (status, out)
        return out

    def sign(self, device, view):
        """The Pi: scan the animated request QR, approve, show the signature QR."""
        a = qr.Assembler()
        for frame in view["frames"]:
            if a.add(frame):
                break
        sig = device.sign(wire.decode_request(a.result()))
        return qr.single(wire.encode_signature(sig))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mint = FakeMint(input_fee_ppk=100)  # like testnut
        self.alice_dev = DeviceSigner.from_mnemonic(ALICE)
        self.bob_dev = DeviceSigner.from_mnemonic(BOB)
        self.alice = App(self, os.path.join(self.tmp, "alice.json"), self.mint)

    def funded(self, app, dev, amount=100):
        # the Pi's QR shows the key in uppercase
        app.post("/api/pair", {"pubkey": dev.pubkey_hex.upper()})
        fund = app.post("/api/fund", {"amount": amount})
        self.assertTrue(fund["invoice"].startswith("lnbc"))
        self.assertEqual(app.get("/api/state")["pending_fund"]["invoice"], fund["invoice"])
        self.assertEqual(app.post("/api/recover")["result"]["fund"], "done")  # the UI's poll
        return app.get("/api/state")


class TestFlows(Base):
    def test_pair_fund_send_receive_melt(self):
        state = self.funded(self.alice, self.alice_dev)
        self.assertEqual((state["balance"], state["reserved"], state["proofs"]), (100, 0, 3))
        self.assertEqual(state["device_pubkey"], self.alice_dev.pubkey_hex)
        self.assertEqual(state["fingerprint"], self.alice_dev.pubkey_hex[2:10])
        self.assertEqual(state["mint"], URL)
        self.assertIsNone(state["pending_fund"])

        # Alice sends 21 locked to Bob's device
        view = self.alice.post("/api/send", {"amount": 21, "to": self.bob_dev.pubkey_hex.upper()})
        self.assertEqual(view["pending"]["kind"], "send")
        self.assertEqual(view["pending"]["amount"], 21)
        self.assertEqual(self.alice.get("/api/state")["pending"]["kind"], "send")
        self.assertEqual(self.alice.get("/api/frames")["frames"], view["frames"])  # reload-safe
        out = self.alice.post("/api/submit", {"qr": self.alice.sign(self.alice_dev, view)})
        self.assertEqual(out["result"]["type"], "send")
        token = self.alice.get("/api/state")["tokens"][0]
        self.assertTrue(token.startswith("cashuA"))
        self.alice.post("/api/tokens/clear")
        state = self.alice.get("/api/state")
        self.assertEqual((state["tokens"], state["balance"], state["pending"]), ([], 78, None))

        # Bob: a fresh wallet, paired by scanning his device, receives it
        bob = App(self, os.path.join(self.tmp, "bob.json"), self.mint)
        bob.post("/api/pair", {"pubkey": self.bob_dev.pubkey_hex.upper()})
        view = bob.post("/api/receive", {"token": token})
        self.assertEqual((view["pending"]["kind"], view["pending"]["amount"]), ("receive", 20))
        out = bob.post("/api/submit", {"qr": bob.sign(self.bob_dev, view)})
        self.assertEqual((out["result"]["type"], out["result"]["amount"]), ("receive", 20))
        self.assertEqual(bob.get("/api/state")["balance"], 20)

        # Bob pays an invoice
        view = bob.post("/api/melt", {"invoice": make_invoice(10)})
        self.assertEqual((view["pending"]["kind"], view["pending"]["amount"]), ("melt", 10))
        self.assertEqual(view["pending"]["fee_reserve"], 4)
        out = bob.post("/api/submit", {"qr": bob.sign(self.bob_dev, view)})
        self.assertEqual(out["result"]["type"], "melt")
        self.assertTrue(out["result"]["preimage"])
        state = bob.get("/api/state")
        self.assertLess(state["balance"], 10)

        history = [(h["type"], h["amount"], h["status"]) for h in self.alice.get("/api/state")["history"]]
        self.assertEqual(history, [("fund", 100, "done"), ("send", 21, "done")])
        history = [(h["type"], h["amount"], h["status"]) for h in state["history"]]
        self.assertEqual(history, [("receive", 20, "done"), ("melt", 10, "done")])

    def test_send_without_recipient(self):
        self.funded(self.alice, self.alice_dev)
        view = self.alice.post("/api/send", {"amount": 5})
        self.assertIsNone(view["pending"]["to"])
        self.alice.post("/api/submit", {"qr": self.alice.sign(self.alice_dev, view)})
        self.assertEqual(len(self.alice.get("/api/state")["tokens"]), 1)

    def test_cancel(self):
        self.funded(self.alice, self.alice_dev)
        self.alice.post("/api/send", {"amount": 5})
        self.alice.post("/api/cancel")
        state = self.alice.get("/api/state")
        self.assertEqual((state["pending"], state["balance"]), (None, 100))

    def test_wallet_not_locked_between_calls(self):
        self.funded(self.alice, self.alice_dev)
        HostWallet(self.alice.wallet, self.mint, URL).close()  # the CLI can open it


class TestErrors(Base):
    def test_bad_token(self):
        self.funded(self.alice, self.alice_dev)
        out = self.alice.post("/api/receive", {"token": "cashuAgarbage"}, expect=400)
        self.assertIn("malformed", out["error"])
        self.assertFalse(out["in_flight"])

    def test_tampered_signature(self):
        self.funded(self.alice, self.alice_dev)
        view = self.alice.post("/api/send", {"amount": 5})
        honest = self.alice.sign(self.alice_dev, view)
        sig = wire.decode_signature(qr.read_single(honest))
        sig["signature"] = sig["signature"][:-2] + ("00" if sig["signature"][-2:] != "00" else "11")
        out = self.alice.post("/api/submit", {"qr": qr.single(wire.encode_signature(sig))}, expect=400)
        self.assertIn("does not match", out["error"])
        self.assertEqual(self.alice.get("/api/state")["pending"]["kind"], "send")  # still pending
        out = self.alice.post("/api/submit", {"qr": "UR:BYTES/NOTASIGNATURE"}, expect=400)
        self.assertIn("signature", out["error"])
        self.alice.post("/api/submit", {"qr": honest})

    def test_mint_down(self):
        down = App(self, os.path.join(self.tmp, "down.json"), MintClient("http://127.0.0.1:1"),
                   "http://127.0.0.1:1")
        down.post("/api/pair", {"pubkey": self.alice_dev.pubkey_hex})
        out = down.post("/api/fund", {"amount": 10}, expect=502)
        self.assertIn("could not reach mint", out["error"])
        self.assertFalse(out["in_flight"])

    def test_lost_reply_then_recover(self):
        flaky = Flaky(self.mint)
        app = App(self, os.path.join(self.tmp, "f.json"), flaky)
        self.funded(app, self.alice_dev)
        view = app.post("/api/send", {"amount": 5})
        flaky.fail["swap"] = "after"
        out = app.post("/api/submit", {"qr": app.sign(self.alice_dev, view)}, expect=502)
        self.assertTrue(out["in_flight"])
        self.assertIn("nothing is lost", out["hint"])
        self.assertTrue(app.get("/api/state")["in_flight"])
        result = app.post("/api/recover")["result"]
        self.assertEqual(result["send"], "done")
        self.assertEqual(len(app.get("/api/state")["tokens"]), 1)

    def test_busy_wallet(self):
        w = HostWallet(self.alice.wallet, self.mint, URL)  # the CLI is mid-command
        self.addCleanup(w.close)
        out = self.alice.post("/api/recover", expect=409)
        self.assertIn("in use", out["error"])

    def test_refuses_other_sites(self):
        status, _ = self.alice.call("GET", "/api/state", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.alice.call("POST", "/api/cancel", {}, headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.alice.call("POST", "/api/cancel", {}, headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)


class TestAbandonFund(Base):
    def test_paid_funding_is_finished_not_lost(self):
        self.alice.post("/api/pair", {"pubkey": self.alice_dev.pubkey_hex})
        self.alice.post("/api/fund", {"amount": 100})  # FakeMint marks the quote PAID at once
        quote = self.alice.get("/api/state")["pending_fund"]["quote"]
        out = self.alice.post("/api/abandon-fund")  # "Give up" before the UI's poll
        self.assertEqual((out["quote"], out["outcome"]), (quote, "funded"))
        self.assertEqual((out["state"]["balance"], out["state"]["pending_fund"]), (100, None))

    def test_mint_unreachable_archives(self):
        flaky = Flaky(self.mint)
        app = App(self, os.path.join(self.tmp, "f.json"), flaky)
        app.post("/api/pair", {"pubkey": self.alice_dev.pubkey_hex})
        app.post("/api/fund", {"amount": 100})
        quote = app.get("/api/state")["pending_fund"]["quote"]
        flaky.fail["restore"] = "before"
        out = app.post("/api/abandon-fund")
        self.assertEqual((out["quote"], out["outcome"]), (quote, "archived"))
        self.assertEqual(out["state"]["archived_funds"], 1)
        self.assertEqual(app.post("/api/recover")["result"], {"late_funds": 100})

    def test_unpaid_invoice_is_forgotten(self):
        orig = self.mint.mint_quote

        def unpaid(amount, unit="sat"):
            q = orig(amount, unit)
            self.mint.quotes[q["quote"]]["state"] = q["state"] = "UNPAID"
            return q
        self.mint.mint_quote = unpaid
        self.alice.post("/api/pair", {"pubkey": self.alice_dev.pubkey_hex})
        self.alice.post("/api/fund", {"amount": 100})
        out = self.alice.post("/api/abandon-fund")
        self.assertEqual(out["outcome"], "forgotten")
        self.assertEqual((out["state"]["pending_fund"], out["state"]["archived_funds"]), (None, 0))


class TestDecode(Base):
    """The browser's fallback: post a webcam frame, the OpenCV reader decodes it."""

    def setUp(self):
        super().setUp()
        try:
            import cv2  # noqa: F401
        except ImportError:
            self.skipTest("OpenCV not installed (pip install -r requirements-host.txt)")

    def jpeg(self, img):
        import base64

        import cv2
        return "data:image/jpeg;base64," + base64.b64encode(cv2.imencode(".jpg", img)[1]).decode()

    def test_decodes_a_dim_frame(self):
        import numpy as np

        from nutsigner import qrview
        text = qr.single(b"\x01" * 132)
        img = qrview.render(text, 300)
        frame = np.full((720, 1280), 90, np.uint8)  # dim LCD, small in a 720p frame
        frame[200:200 + img.shape[0], 400:400 + img.shape[1]] = 90 + (img // 255) * 40
        self.assertEqual(self.alice.post("/api/decode", {"image": self.jpeg(frame)})["text"], text)

    def test_no_qr_and_bad_image(self):
        import numpy as np
        self.assertEqual(self.alice.post("/api/decode", {"image": self.jpeg(np.zeros((90, 160), np.uint8))}),
                         {"text": ""})
        self.alice.post("/api/decode", {"image": "data:image/jpeg;base64,AAAA"}, expect=400)

    def test_works_while_the_cli_holds_the_wallet(self):
        import numpy as np
        w = HostWallet(self.alice.wallet, self.mint, URL)
        self.addCleanup(w.close)
        self.alice.post("/api/decode", {"image": self.jpeg(np.zeros((90, 160), np.uint8))})


if __name__ == "__main__":
    unittest.main()
