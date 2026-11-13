"""host_cli: pubkey --scan, and no wallet lock held while a QR window is open.

A stand-in for nutsigner.qrview replaces the camera and the window, so these
run without OpenCV.
"""

import os
import tempfile
import types
import unittest
from unittest import mock

import host_cli
from nutsigner.device import DeviceSigner
from nutsigner.host import HostWallet
from tests.fake_mint import FakeMint
from tests.test_qrview import run
from tests.test_wire import MNEMONIC

PK = DeviceSigner.from_mnemonic(MNEMONIC).pubkey_hex


def fake_qrview(seen=(), show=None):
    """Camera that shows each string in `seen` in turn; window that calls `show`."""
    def scan(done, source=0, preview=None, caption=None):
        return any(done(text) for text in seen)
    return types.SimpleNamespace(scan=scan, show=lambda frames, **kw: show and show())


class TestHostCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.wallet = os.path.join(self.tmp, "w.json")
        self.mint = FakeMint()

    def cli(self, *argv, qrview):
        with mock.patch.dict("sys.modules", {"nutsigner.qrview": qrview}), \
             mock.patch("nutsigner.qrview", qrview, create=True), \
             mock.patch.object(host_cli, "MintClient", lambda url: self.mint):
            return run(host_cli, ["host_cli.py", "--wallet", self.wallet, *argv])

    def test_pubkey_scan(self):
        # the Pi shows the key in uppercase; other QR codes in view are skipped
        out = self.cli("pubkey", "--scan", qrview=fake_qrview(["UR:BYTES/XYZ", "hello", PK.upper()]))
        self.assertEqual(out, PK + "\n")
        self.assertFalse(os.path.exists(self.wallet))  # no wallet needed

    def test_pubkey_scan_nothing(self):
        with self.assertRaises(SystemExit) as cm:
            self.cli("pubkey", "--scan", qrview=fake_qrview(["nope"]))
        self.assertIn("no device pubkey QR", str(cm.exception.code))

    def test_qr_window_does_not_hold_the_wallet(self):
        w = HostWallet(self.wallet, self.mint, host_cli.DEFAULT_MINT)
        w.fund(64, PK)
        w.close()
        opened = []

        def window_open():  # e.g. submit --scan in another terminal
            HostWallet(self.wallet, self.mint, host_cli.DEFAULT_MINT).close()
            opened.append(True)
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        out = self.cli("send", "5", "--qr", qrview=fake_qrview(show=window_open))
        self.assertEqual(opened, [True])
        self.assertIn("animated QR", out)

    def test_scan_does_not_hold_the_wallet(self):
        from nutsigner import qr, wire
        w = HostWallet(self.wallet, self.mint, host_cli.DEFAULT_MINT)
        w.fund(64, PK)
        sig = DeviceSigner.from_mnemonic(MNEMONIC).sign(w.build_send_request(5))
        w.close()
        opened = []

        def scan(done, source=0, preview=None, caption=None):  # the camera is up for a while
            HostWallet(self.wallet, self.mint, host_cli.DEFAULT_MINT).close()  # e.g. balance meanwhile
            opened.append(True)
            return done(qr.single(wire.encode_signature(sig)))
        out = self.cli("submit", "--scan", qrview=types.SimpleNamespace(scan=scan))
        self.assertEqual(opened, [True])
        self.assertIn("cashuA", out)  # submitted: the token for the recipient


if __name__ == "__main__":
    unittest.main()
