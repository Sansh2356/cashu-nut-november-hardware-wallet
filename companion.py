#!/usr/bin/env python3
"""NutSigner companion app: the laptop wallet with a browser UI, for the Pi signer.

  python3 companion.py --wallet alice.json            # opens http://127.0.0.1:8338
  python3 companion.py --wallet dev.json --mint http://127.0.0.1:3338 --no-browser

A thin JSON API over HostWallet, plus the built React UI (web/dist; build it
with `cd web && npm install && npm run build`). Listens on 127.0.0.1 only.
The wallet file is opened (and its lock taken) for one API call at a time, so
host_cli.py can use the same wallet while the app is open.
"""

import argparse
import json
import mimetypes
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from nutsigner import crypto, qr, wire
from nutsigner.host import HostWallet, WalletBusy, WalletError
from nutsigner.mint import MintClient, MintError, MintUnavailable

DEFAULT_MINT = "https://testnut.cashu.space"
DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "dist")
FRAME_LOOPS = 3  # request frames sent to the UI: the plain ones, then fountain-coded extras
IN_FLIGHT_HINT = ("The outcome is unknown, but nothing is lost: everything needed is saved "
                  "in the wallet file. Run recover.")


# ---------------------------------------------------------------- views (no secrets leave)

def pending_view(w):
    pend = w.state.get("pending")
    if not pend:
        return None
    req, outs = pend["request"], pend["outputs"]
    sent = [o for o in outs if o["role"] == "recipient"]
    if req["type"] == "melt":
        kind, amount = "melt", req["amount"]
    elif sent:
        kind, amount = "send", sum(o["amount"] for o in sent)
    else:
        kind, amount = "receive", sum(o["amount"] for o in outs)
    lock = crypto.parse_secret(sent[0]["secret"]) if sent else None
    return {"kind": kind, "amount": amount, "to": lock[1] if lock else None,
            "inputs": sum(p["amount"] for p in req["inputs"]),
            "fee_reserve": req.get("fee_reserve"), "invoice": req.get("invoice"),
            "submitted": bool(pend.get("submitted"))}


def state_view(w):
    pk = w.state.get("device_pubkey")
    pf = w.state.get("pending_fund")
    return {
        "mint": w.mint_url, "unit": w.unit, "device_pubkey": pk, "fingerprint": pk[2:10] if pk else None,
        "balance": w.balance(), "reserved": w.reserved_balance(), "proofs": len(w.state["proofs"]),
        "pending": pending_view(w),
        "pending_fund": pf and {"quote": pf["quote"], "invoice": pf.get("invoice"),
                                "amount": sum(o["amount"] for o in pf["outputs"])},
        "archived_funds": len(w.state.get("abandoned_funds", [])),
        "in_flight": w.in_flight(),
        "history": w.state.get("history", []),
        "tokens": w.state.get("undelivered_tokens", []),
    }


def frames_view(w):
    pend = w.state.get("pending")
    if not pend:
        raise WalletError("nothing pending")
    frames = qr.frames(wire.encode_request(pend["request"]))
    return {"pending": pending_view(w), "frames": [next(frames) for _ in range(frames.count * FRAME_LOOPS)]}


def recover_view(result):
    """recover()'s result without the proofs (they stay in the wallet file)."""
    out = {k: v for k, v in result.items() if k not in ("send_result", "melt_result")}
    for k in ("late_sends", "late_melts"):
        if k in out:
            out[k] = len(out[k])
    return out


# ---------------------------------------------------------------- actions

def submit(w, body):
    try:
        sig = wire.decode_signature(qr.read_single(body["qr"]))
    except ValueError as e:
        raise WalletError(f"that QR is not a NutSigner signature ({e})") from None
    out = w.submit(sig)
    result = dict(w.state["history"][-1])
    if len(out) == 2:  # melt: (preimage, change)
        result["preimage"] = out[0]
    return {"result": result, "state": state_view(w)}


def fund(w, body):
    pk = w.state.get("device_pubkey")
    if not pk:
        raise WalletError("pair the device first: scan its pubkey QR")
    return {"invoice": w.start_fund(int(body["amount"]), pk)["request"], "state": state_view(w)}


def abandon_fund(w, body):
    quote, outcome = w.abandon_fund()
    return {"quote": quote, "outcome": outcome, "state": state_view(w)}


def build(make):
    def action(w, body):
        make(w, body)
        return frames_view(w)
    return action


def then_state(do):
    def action(w, body):
        do(w, body)
        return {"state": state_view(w)}
    return action


def decode(body):
    """Text of the QR in a posted webcam frame (data: URL), or "": the browser's
    fallback for the Pi's dim LCD, using the CLI's OpenCV reader. No wallet needed."""
    import base64
    try:
        import cv2
        import numpy as np
        from nutsigner import qrview
    except ImportError:
        raise WalletError("OpenCV not installed (pip install -r requirements-host.txt)") from None
    img = cv2.imdecode(np.frombuffer(base64.b64decode(body["image"].split(",", 1)[-1]), np.uint8),
                       cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise WalletError("not an image")
    return {"text": qrview.decode(img)}


ACTIONS = {
    ("GET", "/api/state"): lambda w, b: state_view(w),
    ("GET", "/api/frames"): lambda w, b: frames_view(w),
    ("POST", "/api/pair"): then_state(lambda w, b: w.pair(b["pubkey"])),
    ("POST", "/api/fund"): fund,
    ("POST", "/api/send"): build(lambda w, b: w.build_send_request(int(b["amount"]), b.get("to") or None)),
    ("POST", "/api/receive"): build(lambda w, b: w.build_receive_request(b["token"])),
    ("POST", "/api/melt"): build(lambda w, b: w.build_melt_request(b["invoice"])),
    ("POST", "/api/submit"): submit,
    ("POST", "/api/cancel"): then_state(lambda w, b: w.cancel_pending()),
    ("POST", "/api/abandon-fund"): abandon_fund,
    ("POST", "/api/tokens/clear"): then_state(lambda w, b: w.clear_tokens()),
    ("POST", "/api/recover"): lambda w, b: {"result": recover_view(w.recover()), "state": state_view(w)},
    ("POST", "/api/decode"): decode,  # needs no wallet: see _handle
}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if ctype == "application/json":
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _foreign(self):
        """Refuse other web pages (DNS rebinding, cross-site POSTs): the API moves money."""
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = self.headers.get("Origin")
        return self.headers.get("Host") not in hosts or (origin is not None and origin.removeprefix("http://") not in hosts)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        if self._foreign():
            return self._send(403, {"error": "forbidden"})
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/"):
            return self._static(path) if method == "GET" else self._send(405, {"error": "method not allowed"})
        action = ACTIONS.get((method, path))
        if not action:
            return self._send(404, {"error": "not found"})
        body = {}
        if method == "POST":
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._send(415, {"error": "send JSON"})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) if 0 < n < 4 << 20 else b"{}")  # 4 MB: webcam frames
                assert isinstance(body, dict)
            except (ValueError, AssertionError):
                return self._send(400, {"error": "request body is not a JSON object"})
        srv, w = self.server, None
        try:
            if action is decode:
                code, out = 200, decode(body)
            else:
                w = HostWallet(srv.wallet_path, srv.mint, srv.mint_url)
                code, out = 200, action(w, body)
        except WalletBusy as e:
            code, out = 409, {"error": str(e)}
        except (MintError, WalletError, KeyError, TypeError, ValueError) as e:
            mint = isinstance(e, MintError)
            if isinstance(e, (KeyError, TypeError)) or type(e) is ValueError:
                e = f"bad request: {e}"
            code, out = (502 if mint else 400), {"error": str(e)}
            out["in_flight"] = bool(w and w.in_flight())
            if out["in_flight"]:
                out["hint"] = IN_FLIGHT_HINT
            elif isinstance(e, MintUnavailable):
                out["hint"] = "Check your connection and the mint URL, then try again."
        finally:
            if w is not None:
                w.close()
        self._send(code, out)

    def _static(self, path):
        root = os.path.realpath(self.server.dist)
        f = os.path.realpath(os.path.join(root, path.lstrip("/")))
        if not (f.startswith(root + os.sep) and os.path.isfile(f)):
            f = os.path.join(root, "index.html")  # the single-page app
        if not os.path.isfile(f):
            return self._send(503, b"UI not built: cd web && npm install && npm run build\n", "text/plain")
        with open(f, "rb") as fh:
            self._send(200, fh.read(), mimetypes.guess_type(f)[0] or "application/octet-stream")

    def log_message(self, *a):
        pass


def make_server(wallet_path, mint, mint_url, port=8338, dist=DIST):
    # ponytail: single-threaded, so one call at a time (a slow mint call stalls the UI);
    # also what keeps two calls from fighting over the wallet lock.
    srv = HTTPServer(("127.0.0.1", port), Handler)
    srv.wallet_path, srv.mint, srv.mint_url, srv.dist = wallet_path, mint, mint_url.rstrip("/"), dist
    return srv


def main():
    ap = argparse.ArgumentParser(description="NutSigner companion app (browser UI)")
    ap.add_argument("--wallet", default="host_wallet.json")
    ap.add_argument("--mint", default=DEFAULT_MINT)
    ap.add_argument("--port", type=int, default=8338)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    mint = MintClient(args.mint)
    try:  # finish anything a crash or a closed laptop left unfinished
        w = HostWallet(args.wallet, mint, args.mint)
        try:
            if w.recover():
                print("recover: finished an interrupted operation (see the app's history).")
        finally:
            w.close()
    except (MintError, WalletError) as e:
        print(f"recover on startup: {e}", file=sys.stderr)
    try:
        srv = make_server(args.wallet, mint, args.mint, args.port)
    except OSError as e:
        sys.exit(f"cannot listen on 127.0.0.1:{args.port}: {e}. Try --port.")
    url = f"http://127.0.0.1:{args.port}/"
    print(f"NutSigner companion: {url}  (wallet {args.wallet}, mint {args.mint}). Ctrl-C to stop.")
    if not os.path.isfile(os.path.join(DIST, "index.html")):
        print("The UI is not built yet: cd web && npm install && npm run build", file=sys.stderr)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
