#!/usr/bin/env python3
"""Serve FakeMint over HTTP for offline development.

  python3 -m tests.fake_mint_server            # http://127.0.0.1:3338
  python3 host_cli.py --mint http://127.0.0.1:3338 info

State lives in memory and is lost when the server stops.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nutsigner.mint import MintError  # FakeMint raises MintRejected, a subclass
from tests.fake_mint import FakeMint

MINT = FakeMint()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self, method):
        try:
            body = None
            if method == "POST":
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            p = self.path
            if method == "GET" and p == "/v1/info":
                return self._send(200, MINT.info())
            if method == "GET" and p == "/v1/keysets":
                return self._send(200, {"keysets": MINT.keysets()})
            if method == "GET" and p.startswith("/v1/keys/"):
                kid = p.rsplit("/", 1)[1]
                keys = {str(a): k for a, k in MINT.keys(kid).items()}
                return self._send(200, {"keysets": [{"id": kid, "unit": "sat", "keys": keys}]})
            if method == "POST" and p == "/v1/mint/quote/bolt11":
                return self._send(200, MINT.mint_quote(body["amount"], body.get("unit", "sat")))
            if method == "GET" and p.startswith("/v1/mint/quote/bolt11/"):
                return self._send(200, MINT.mint_quote_state(p.rsplit("/", 1)[1]))
            if method == "POST" and p == "/v1/mint/bolt11":
                return self._send(200, {"signatures": MINT.mint(body["quote"], body["outputs"])})
            if method == "POST" and p == "/v1/checkstate":
                return self._send(200, {"states": MINT.check_state(body["Ys"])})
            if method == "POST" and p == "/v1/restore":
                got = MINT.restore(body["outputs"])
                outs = [o for o in body["outputs"] if o["B_"] in got]
                return self._send(200, {"outputs": outs, "signatures": [got[o["B_"]] for o in outs]})
            if method == "POST" and p == "/v1/swap":
                return self._send(200, {"signatures": MINT.swap(body["inputs"], body["outputs"])})
            self._send(404, {"detail": "not found"})
        except MintError as e:
            self._send(400, {"detail": str(e), "code": 0})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3338
    MINT = FakeMint(input_fee_ppk=int(sys.argv[2]) if len(sys.argv) > 2 else 0)
    print(f"Fake mint on http://127.0.0.1:{port} (fee_ppk={MINT.fee_ppk})")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
