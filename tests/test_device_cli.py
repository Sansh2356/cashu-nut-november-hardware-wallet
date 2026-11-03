"""Device CLI: seed kept in memory only with --ask-seed."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import device_cli
from tests.test_wire import MNEMONIC, testnut_request


class TestAskSeed(unittest.TestCase):
    def test_sign_without_seed_file_writes_nothing_else(self):
        dev, req = testnut_request()
        tmp = tempfile.mkdtemp()
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            with open("request.json", "w") as f:
                json.dump(req, f)
            err = io.StringIO()
            typed = "  " + MNEMONIC.upper().replace(" ", "   ") + " \n"
            with mock.patch("getpass.getpass", return_value=typed), \
                    mock.patch.object(sys, "argv", ["device_cli.py", "--ask-seed", "sign", "request.json", "--yes"]), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                device_cli.main()
            self.assertEqual(sorted(os.listdir(tmp)), ["request.json", "signature.json"])
            with open("signature.json") as f:
                self.assertEqual(json.load(f)["pubkey"], dev.pubkey_hex)
            self.assertIn(dev.pubkey_hex, err.getvalue())
        finally:
            os.chdir(cwd)

    def test_bad_words_refused(self):
        with mock.patch("getpass.getpass", return_value="not a real mnemonic"), \
                mock.patch.object(sys, "argv", ["device_cli.py", "--ask-seed", "pubkey"]):
            with self.assertRaises(SystemExit) as cm:
                device_cli.main()
        self.assertIn("Invalid mnemonic", str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()
