"""Request/signature v2 wire format."""

import os
import tempfile
import unittest

from nutsigner import wire
from nutsigner.device import DeviceSigner
from nutsigner.host import HostWallet
from tests.fake_mint import FakeMint

MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


def testnut_request():
    """A 1-input, 7-output request with a 33-byte (v2) keyset id, like testnut's."""
    mint = FakeMint()
    mint.keyset_id = "01" + os.urandom(32).hex()
    dev = DeviceSigner.from_mnemonic(MNEMONIC)
    w = HostWallet(os.path.join(tempfile.mkdtemp(), "w.json"), mint, "https://testnut.cashu.space")
    w.fund(64, dev.pubkey_hex)                # one 64-sat proof
    req = w.build_send_request(21)             # 21 = 1+4+16, change 43 = 1+2+8+32
    w.close()
    return dev, req


class TestWire(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dev, cls.req = testnut_request()

    def test_shape(self):
        self.assertEqual((len(self.req["inputs"]), len(self.req["outputs"])), (1, 7))

    def test_request_roundtrip(self):
        data = wire.encode_request(self.req)
        self.assertEqual(wire.decode_request(data), self.req)
        # the decoded request is signable by the unchanged device logic
        self.assertEqual(self.dev.review(wire.decode_request(data))["sending"], 21)

    def test_every_secret_shape_roundtrips_exactly(self):
        from nutsigner import crypto
        pk = self.dev.pubkey_hex
        secrets = [
            crypto.make_p2pk_secret(pk, "SIG_ALL"),                      # compact P2PK
            crypto.make_p2pk_secret(pk, "SIG_INPUTS", [["locktime", "1"]]),
            os.urandom(32).hex(),                                        # compact bearer
            os.urandom(32).hex().upper(),                                # raw: not lowercase
            '["P2PK", {"nonce": "%s", "data": "%s", "tags": []}]' % ("00" * 32, pk),  # raw: spaces
            '["P2PK",{"data":"%s","nonce":"%s","tags":[]}]' % (pk, "00" * 32),        # raw: key order
            '["HTLC",{"nonce":"%s","data":"%s","tags":[]}]' % ("00" * 32, pk),         # raw: kind
            "héllo ✓", "", "[1,2]", "null",
        ]
        req = dict(self.req, reveals=[dict(self.req["reveals"][0], secret=s) for s in secrets])
        req["inputs"] = [dict(self.req["inputs"][0], secret=s) for s in secrets]
        self.assertEqual(wire.decode_request(wire.encode_request(req)), req)

    def test_signature_roundtrip(self):
        sig = self.dev.sign(self.req)
        data = wire.encode_signature(sig)
        self.assertEqual(len(data), wire.SIGNATURE_LEN)
        self.assertEqual(wire.decode_signature(data), sig)

    def test_truncated_and_trailing_rejected(self):
        data = wire.encode_request(self.req)
        for n in range(len(data)):
            with self.assertRaises(wire.WireError, msg=f"truncated to {n}"):
                wire.decode_request(data[:n])
        with self.assertRaises(wire.WireError):
            wire.decode_request(data + b"\x00")
        sig = wire.encode_signature(self.dev.sign(self.req))
        for bad in (sig[:-1], sig + b"\x00", b"XX" + sig[2:]):
            with self.assertRaises(wire.WireError):
                wire.decode_signature(bad)

    def test_hostile_bytes_only_raise_wireerror(self):
        data = bytearray(wire.encode_request(self.req))
        rnd = __import__("random").Random(1)
        for _ in range(2000):
            bad = bytearray(data)
            for _ in range(rnd.randint(1, 4)):
                bad[rnd.randrange(len(bad))] = rnd.randrange(256)
            try:
                wire.decode_request(bytes(bad))
            except wire.WireError:
                pass
        for bad in (b"NS\x02\x01" + b"\xff" * 11,          # varint too long
                    b"NS\x02\x01\x80\x00"):               # non-canonical varint
            with self.assertRaises(wire.WireError):
                wire.decode_request(bad)

    def test_melt_roundtrip(self):
        from tests.fake_mint import make_invoice
        mint = FakeMint()
        w = HostWallet(os.path.join(tempfile.mkdtemp(), "w.json"), mint, "https://testnut.cashu.space")
        w.fund(64, self.dev.pubkey_hex)
        req = w.build_melt_request(make_invoice(21))
        w.close()
        data = wire.encode_request(req)
        self.assertEqual(data[3], wire.TYPE_MELT)
        self.assertEqual(wire.decode_request(data), req)
        self.assertEqual(self.dev.review(wire.decode_request(data))["sending"], 21)
        for n in range(len(data)):
            with self.assertRaises(wire.WireError):
                wire.decode_request(data[:n])
        # an empty-outputs melt (exact amount, no change) also round-trips
        exact = dict(req, outputs=[], reveals=[])
        self.assertEqual(wire.decode_request(wire.encode_request(exact)), exact)

    def test_swap_encoding_unchanged(self):
        data = wire.encode_request(self.req)
        self.assertEqual(data[:4], b"NS\x02\x01")
        self.assertNotIn("invoice", wire.decode_request(data))

    def test_sizes(self):
        import json
        v1 = len(json.dumps(self.req, separators=(",", ":")).encode())
        v2 = len(wire.encode_request(self.req))
        print(f"\n  1-input, 7-output testnut request: v1 JSON {v1} bytes, v2 binary {v2} bytes "
              f"({100 * v2 // v1}%)")
        self.assertLess(v2, v1 // 2)


if __name__ == "__main__":
    unittest.main()
