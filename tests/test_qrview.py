"""Full QR loop through both CLIs, using recorded videos instead of cameras.

The request's animated QR and the signature QR are rendered, degraded
(downscaled, blurred, some frames dropped) and written to video files, as a
phone recording of the screen would be. The CLIs then read them back.
"""

import contextlib
import io
import json
import os
import random
import sys
import tempfile
import unittest
from unittest import mock

try:
    import cv2
    from nutsigner import qrview
except ImportError:  # OpenCV is a host-only dependency (requirements-host.txt)
    cv2 = None

import device_cli
import host_cli
from nutsigner import qr, wire
from nutsigner.device import DeviceSigner
from nutsigner.host import HostWallet
from tests.fake_mint import FakeMint
from tests.test_wire import MNEMONIC

SIZE = 480


def record(path, texts, drop=0.0, seed=1):
    """Write QR frames to a video like a shaky, lossy phone recording."""
    rnd = random.Random(seed)
    out = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 10, (SIZE, SIZE))
    for text in texts:
        if rnd.random() < drop:
            continue
        img = cv2.resize(qrview.render(text, SIZE), (SIZE, SIZE), interpolation=cv2.INTER_AREA)
        img = cv2.GaussianBlur(img, (3, 3), 0)
        out.write(cv2.cvtColor(img, cv2.COLOR_GRAY2BGR))
    out.release()


def run(module, argv):
    buf = io.StringIO()
    with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(buf):
        module.main()
    return buf.getvalue()


@unittest.skipIf(cv2 is None, "OpenCV not installed (pip install -r requirements-host.txt)")
class TestQRLoop(unittest.TestCase):
    def test_send_sign_submit_over_recorded_qr(self):
        tmp = tempfile.mkdtemp()
        mint = FakeMint()
        mint.keyset_id = "01" + os.urandom(32).hex()     # testnut-sized keyset id
        dev = DeviceSigner.from_mnemonic(MNEMONIC)
        wallet = os.path.join(tmp, "w.json")
        w = HostWallet(wallet, mint, host_cli.DEFAULT_MINT)
        w.fund(64, dev.pubkey_hex)
        req = w.build_send_request(21)
        w.close()

        # host screen -> device camera: 4 loops of the animation, 30% of frames lost
        frames = qr.frames(wire.encode_request(req))
        req_video = os.path.join(tmp, "request.avi")
        record(req_video, [next(frames) for _ in range(frames.count * 4)], drop=0.3)

        seed = os.path.join(tmp, "seed.json")
        with open(seed, "w") as f:
            json.dump({"mnemonic": MNEMONIC}, f)
        sig_file = os.path.join(tmp, "sig.json")
        with mock.patch.object(device_cli, "SEED_FILE", seed):
            out = run(device_cli, ["device_cli.py", "sign", "--scan", req_video, "--yes", "-o", sig_file])
        self.assertIn("Send 21 sat", out)

        # device screen -> host camera: the single signature QR
        with open(sig_file) as f:
            sig = json.load(f)
        sig_video = os.path.join(tmp, "signature.avi")
        record(sig_video, [qr.single(wire.encode_signature(sig))] * 5)

        with mock.patch.object(host_cli, "MintClient", lambda url: mint):
            out = run(host_cli, ["host_cli.py", "--wallet", wallet, "submit", "--scan", sig_video])
        self.assertIn("Swap accepted", out)
        self.assertIn("Balance: 43 sat", out)


def dim_lcd(text, size=200, lo=30, hi=70, bg=150, W=1280, H=720):
    """A 720p webcam frame of the Pi's dim LCD QR: small, low contrast, blurred, noisy."""
    import numpy as np
    img = cv2.resize(qrview.render(text, size), (size, size), interpolation=cv2.INTER_AREA)
    frame = np.full((H, W), bg, np.float32)
    y, x = (H - size) // 2, (W - size) // 2
    frame[y:y + size, x:x + size] = lo + img.astype(np.float32) / 255 * (hi - lo)
    frame = cv2.GaussianBlur(frame, (3, 3), 0) + np.random.default_rng(1).normal(0, 2, frame.shape)
    return cv2.cvtColor(np.clip(frame, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)


@unittest.skipIf(cv2 is None, "OpenCV not installed (pip install -r requirements-host.txt)")
class TestDimSignature(unittest.TestCase):
    def test_reads_dim_lcd_qr(self):
        text = qr.single(b"\x01" * 132)  # signature-sized
        frame = dim_lcd(text)
        self.assertEqual(cv2.QRCodeDetector().detectAndDecode(frame)[0], "")  # why decode() exists
        self.assertEqual(qrview.decode(frame), text)


if __name__ == "__main__":
    unittest.main()
