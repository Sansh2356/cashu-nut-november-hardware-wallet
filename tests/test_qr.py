"""BC-UR transport: animated request frames and the single signature QR."""

import random
import unittest

from nutsigner import qr, wire
from tests.test_wire import testnut_request


class TestQR(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dev, cls.req = testnut_request()
        cls.data = wire.encode_request(cls.req)

    def test_in_order_frames(self):
        enc = qr.frames(self.data)
        a = qr.Assembler()
        while not a.add(enc.next_part()):
            pass
        self.assertEqual(wire.decode_request(a.result()), self.req)

    def test_shuffled_with_dropped_frames(self):
        rnd = random.Random(7)
        enc = qr.frames(self.data)
        n = enc.fountain_encoder.seq_len()
        sent = [enc.next_part() for _ in range(n * 3)]   # a few loops of the animation
        rnd.shuffle(sent)
        kept = [f for f in sent if rnd.random() > 0.4]    # the camera misses 40%
        a = qr.Assembler()
        done = any(a.add(f) for f in kept)
        self.assertTrue(done, f"not recovered from {len(kept)} of {len(sent)} frames (seq_len {n})")
        self.assertEqual(a.result(), self.data)

    def test_no_single_frame_is_enough_and_garbage_is_ignored(self):
        enc = qr.frames(self.data)
        a = qr.Assembler()
        for junk in ("", "hello", "UR:BYTES/1-9/LPADAX", "UR:CRYPTO-PSBT/xyz", "ur:bytes/zz"):
            self.assertFalse(a.add(junk))
        self.assertFalse(a.add(enc.next_part()))
        self.assertLess(a.progress, 1.0)
        with self.assertRaises(qr.QRError):
            a.result()

    def test_signature_is_one_frame(self):
        sig = wire.encode_signature(self.dev.sign(self.req))
        s = qr.single(sig)
        self.assertTrue(s.startswith("UR:BYTES/") and s == s.upper())
        self.assertEqual(wire.decode_signature(qr.read_single(s)), self.dev.sign(self.req))

    def test_wrong_ur_type_rejected(self):
        from nutsigner.ur2.ur import UR
        from nutsigner.ur2.ur_encoder import UREncoder
        s = UREncoder.encode(UR("crypto-psbt", b"\x41\x00"))
        with self.assertRaises(qr.QRError):
            qr.read_single(s)

    def test_frame_counts(self):
        for n in (80, 120, 150):
            print(f"\n  {len(self.data)}-byte request at {n} bytes/frame: "
                  f"{qr.frames(self.data, n).fountain_encoder.seq_len()} frames, "
                  f"{len(qr.frames(self.data, n).next_part())} chars each", end="")


if __name__ == "__main__":
    unittest.main()
