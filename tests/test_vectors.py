"""Official Cashu test vectors (cashubtc/nuts tests/00-tests.md and tests/11-test.md)."""

import hashlib
import json
import unittest

from nutsigner import crypto, curve


class TestNut00(unittest.TestCase):
    def test_hash_to_curve(self):
        vectors = [
            ("00" * 32, "024cce997d3b518f739663b757deaec95bcd9473c30a14ac2fd04023a739d1a725"),
            ("00" * 31 + "01", "022e7158e11c9506f1aa4248bf531298daa7febd6194f003edcd9b93ade6253acf"),
            ("00" * 31 + "02", "026cdbe15362df59cd1dd3c9c11de8aedac2106eca69236ecd9fbe117af897be4f"),
        ]
        for msg, point in vectors:
            Y = crypto.hash_to_curve(bytes.fromhex(msg))
            self.assertEqual(curve.serialize(Y).hex(), point)

    def test_blinded_messages(self):
        vectors = [
            ("d341ee4871f1f889041e63cf0d3823c713eea6aff01e80f1719f08f9e5be98f6",
             "99fce58439fc37412ab3468b73db0569322588f62fb3a49182d67e23d877824a",
             "033b1a9737a40cc3fd9b6af4b723632b76a67a36782596304612a6c2bfb5197e6d"),
            ("f1aaf16c2239746f369572c0784d9dd3d032d952c2d992175873fb58fae31a60",
             "f78476ea7cc9ade20f9e05e58a804cf19533f03ea805ece5fee88c8e2874ba50",
             "029bdf2d716ee366eddf599ba252786c1033f47e230248a4612a5670ab931f1763"),
        ]
        for x, r, B_ in vectors:
            self.assertEqual(crypto.blind_bytes(bytes.fromhex(x), int(r, 16)), B_)

    def test_blind_sign_unblind_roundtrip(self):
        k = 0x7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F7F
        B_ = curve.parse(bytes.fromhex("02a9acc1e48c25eeeb9289b5031cc57da9fe72f3fe2861d264bdc074209b107ba2"))
        C_ = curve.serialize(curve.mul(k, B_)).hex()
        self.assertEqual(C_, "0398bc70ce8184d27ba89834d19f5199c84443c31131e48d3c1214db24247d005d")


SWAP_INPUT = {
    "amount": 2,
    "id": "00bfa73302d12ffd",
    "secret": "[\"P2PK\",{\"nonce\":\"c7f280eb55c1e8564e03db06973e94bc9b666d9e1ca42ad278408fe625950303\",\"data\":\"030d8acedfe072c9fa449a1efe0817157403fbec460d8e79f957966056e5dd76c1\",\"tags\":[[\"sigflag\",\"SIG_ALL\"]]}]",
    "C": "02c97ee3d1db41cf0a3ddb601724be8711a032950811bf326f8219c50c4808d3cd",
    "witness": "{\"signatures\":[\"ce017ca25b1b97df2f72e4b49f69ac26a240ce14b3690a8fe619d41ccc42d3c1282e073f85acd36dc50011638906f35b56615f24e4d03e8effe8257f6a808538\"]}",
}
SWAP_OUTPUT = {"amount": 2, "id": "00bfa73302d12ffd",
               "B_": "038ec853d65ae1b79b5cdbc2774150b2cb288d6d26e12958a16fb33c32d9a86c39"}


class TestNut11(unittest.TestCase):
    def test_sig_inputs_vector(self):
        secret = "[\"P2PK\",{\"nonce\":\"859d4935c4907062a6297cf4e663e2835d90d97ecdd510745d32f6816323a41f\",\"data\":\"0249098aa8b9d2fbec49ff8598feb17b592b986e62319a4fa488a3dc36387157a7\",\"tags\":[[\"sigflag\",\"SIG_INPUTS\"]]}]"
        pub = "0249098aa8b9d2fbec49ff8598feb17b592b986e62319a4fa488a3dc36387157a7"
        good = "60f3c9b766770b46caac1d27e1ae6b77c8866ebaeba0b9489fe6a15a837eaa6fcd6eaa825499c72ac342983983fd3ba3a8a41f56677cc99ffd73da68b59e1383"
        bad = "83564aca48c668f50d022a426ce0ed19d3a9bdcffeeaee0dc1e7ea7e98e9eff1840fcc821724f623468c94f72a8b0a7280fa9ef5a54a1b130ef3055217f467b3"
        self.assertTrue(crypto.schnorr_verify(pub, secret, good))
        self.assertFalse(crypto.schnorr_verify(pub, secret, bad))

    def test_sig_all_swap_message(self):
        expected = '["P2PK",{"nonce":"c7f280eb55c1e8564e03db06973e94bc9b666d9e1ca42ad278408fe625950303","data":"030d8acedfe072c9fa449a1efe0817157403fbec460d8e79f957966056e5dd76c1","tags":[["sigflag","SIG_ALL"]]}]02c97ee3d1db41cf0a3ddb601724be8711a032950811bf326f8219c50c4808d3cd2038ec853d65ae1b79b5cdbc2774150b2cb288d6d26e12958a16fb33c32d9a86c39'
        msg = crypto.sig_all_message([SWAP_INPUT], [SWAP_OUTPUT])
        self.assertEqual(msg, expected)
        self.assertEqual(hashlib.sha256(msg.encode()).hexdigest(),
                         "de7f9e3ca0fcc5ed3258fcf83dbf1be7fa78a5ed6da7bf2aa60d61e9dc6eb09a")

    def test_sig_all_swap_signature_verifies(self):
        msg = crypto.sig_all_message([SWAP_INPUT], [SWAP_OUTPUT])
        sig = json.loads(SWAP_INPUT["witness"])["signatures"][0]
        pub = crypto.parse_secret(SWAP_INPUT["secret"])[1]
        self.assertTrue(crypto.schnorr_verify(pub, msg, sig))
        # Changing an output amount must break the signature.
        tampered = dict(SWAP_OUTPUT, amount=1)
        self.assertFalse(crypto.schnorr_verify(pub, crypto.sig_all_message([SWAP_INPUT], [tampered]), sig))

    def test_sig_all_melt_message(self):
        inp = {
            "amount": 2, "id": "00bfa73302d12ffd",
            "secret": "[\"P2PK\",{\"nonce\":\"bbf9edf441d17097e39f5095a3313ba24d3055ab8a32f758ff41c10d45c4f3de\",\"data\":\"029116d32e7da635c8feeb9f1f4559eb3d9b42d400f9d22a64834d89cde0eb6835\",\"tags\":[[\"sigflag\",\"SIG_ALL\"]]}]",
            "C": "02a9d461ff36448469dccf828fa143833ae71c689886ac51b62c8d61ddaa10028b",
        }
        out = {"amount": 0, "id": "00bfa73302d12ffd",
               "B_": "038ec853d65ae1b79b5cdbc2774150b2cb288d6d26e12958a16fb33c32d9a86c39"}
        quote = "cF8911fzT88aEi1d-6boZZkq5lYxbUSVs-HbJxK0"
        msg = crypto.sig_all_message([inp], [out], quote_id=quote)
        self.assertEqual(hashlib.sha256(msg.encode()).hexdigest(),
                         "9efa1067cc7dc870f4074f695115829c3cd817a6866c3b84e9814adf3c3cf262")
        sig = "478224fbe715e34f78cb33451db6fcf8ab948afb8bd04ff1a952c92e562ac0f7c1cb5e61809410635be0aa94d0448f7f7959bd5762cc3802b0a00ff58b2da747"
        self.assertTrue(crypto.schnorr_verify(
            "029116d32e7da635c8feeb9f1f4559eb3d9b42d400f9d22a64834d89cde0eb6835", msg, sig))

    def test_curve_matches_libsecp256k1(self):
        """Cross-check the pure-Python curve against embit's libsecp256k1."""
        from embit import ec
        for k in (1, 2, 12345, curve.N - 1):
            ours = curve.serialize(curve.mul(k)).hex()
            theirs = ec.PrivateKey(k.to_bytes(32, "big")).get_public_key().sec().hex()
            self.assertEqual(ours, theirs)


class TestBlindingBackends(unittest.TestCase):
    """Compiled libsecp256k1 and the curve.py fallback must agree exactly."""

    def test_fast_path_loaded(self):
        self.assertTrue(crypto.FAST, "embit's compiled libsecp256k1 did not load")

    def test_fallback_matches_fast(self):
        import os
        from unittest import mock
        cases = []
        for _ in range(50):
            x, r, k = os.urandom(32), crypto.random_scalar(), crypto.random_scalar()
            K = curve.serialize(curve.mul(k)).hex()
            B_ = crypto.blind_bytes(x, r)
            cases.append((x, r, K, B_, crypto.unblind(B_, r, K)))
        with mock.patch.object(crypto, "FAST", False):
            TestNut00("test_blinded_messages").test_blinded_messages()  # spec vectors, fallback path
            for x, r, K, B_, C in cases:
                self.assertEqual(crypto.blind_bytes(x, r), B_)
                self.assertEqual(crypto.unblind(B_, r, K), C)


if __name__ == "__main__":
    unittest.main()
