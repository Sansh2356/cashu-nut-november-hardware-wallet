# Demo: two NutSigner users on testnut

Alice mints 100 sat locked to her device, sends 21 sat locked to Bob's device,
Bob receives them with his own wallet and device, then pays a Lightning
invoice. One Pi plays both devices: the seed is swapped between Alice and Bob.
testnut's ecash and Lightning are fake, so the seeds below are throwaway.

```bash
ALICE="abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
BOB="legal winner thank year wave sausage worth useful legal winner thank yellow"
```

Laptop: this repo, `pip install -r requirements-host.txt` (OpenCV for the QR
window and webcam). Pi: the SeedSigner fork (`PYTHONPATH=src .venv/bin/python src/main.py`).
Every `--qr` opens a window with the animated request QR. Scan it with the Pi
(Home > Scan), pick the seed, and review. On approval the Pi shows the signature
QR, which `submit --scan` reads with the webcam.

## 0. Alice's seed on the Pi

Pi: Seeds > Enter 12-word seed > Alice's words. Then Tools > Cashu > Show pubkey.

```bash
pk() { python3 -c "import sys; from nutsigner.device import DeviceSigner as D; print(D.from_mnemonic(sys.argv[1]).pubkey_hex)" "$1"; }
APK=$(pk "$ALICE"); BPK=$(pk "$BOB")
echo $APK    # same key as the Pi's QR (shown in uppercase)
```

## 1. Alice mints 100 sat, locked to her device

```bash
python3 host_cli.py --wallet alice.json fund 100 --pubkey $APK
```

## 2. Alice sends 21 sat locked to Bob's device

```bash
python3 host_cli.py --wallet alice.json send 21 --to $BPK --qr
```

Pi shows `Send 21 sat / 21 to key <Bob's key>..`, then `Change 42 sat / Fee 1 sat`. Approve.

```bash
python3 host_cli.py --wallet alice.json submit --scan | tee alice-out.txt
TOKEN=$(grep cashuA alice-out.txt)
```

## 3. Swap seeds; Bob receives

Pi: Seeds > Alice's fingerprint > Discard seed. Then Seeds > Load a seed > Enter 12-word seed > Bob's words.

```bash
python3 host_cli.py --wallet bob.json receive $TOKEN --pubkey $BPK --qr
```

Pi shows `Receive 21 sat / Fee 1 sat / Mint testnut.cashu.sp`. Approve.

```bash
python3 host_cli.py --wallet bob.json submit --scan      # Received 20 sat, locked to your device
python3 host_cli.py --wallet bob.json receive $TOKEN     # refused: already spent (optional shot)
```

## 4. Bob pays a Lightning invoice

```bash
INVOICE=$(python3 -c "from tests.fake_mint import make_invoice; print(make_invoice(5))")
python3 host_cli.py --wallet bob.json melt $INVOICE      # writes request.json, no QR yet
```

**Tamper shot.** Show the device a request whose amount the host changed:

```bash
python3 -c "import json; from nutsigner import qr, qrview, wire; r = json.load(open('request.json')); r['amount'] -= 1; qrview.show(qr.frames(wire.encode_request(r)), caption='TAMPERED')"
```

Pi: `Request rejected: invoice is for 5 sat but the quote says 4`. Press OK.

**Honest request.** Show the real one:

```bash
python3 -c "import json; from nutsigner import qr, qrview, wire; qrview.show(qr.frames(wire.encode_request(json.load(open('request.json')))))"
```

Pi page 1: `Pay 5 sat (Lightning) / Max fee 1 sat / Mint testnut.cashu.sp`.
Page 2 (warning): `Up to 16 sat at risk / Payee not verified`. Approve.
(For a reject shot, press Reject here instead, then show the QR again.)

```bash
python3 host_cli.py --wallet bob.json submit --scan      # Invoice paid. Preimage: ... Change kept: ...
python3 host_cli.py --wallet bob.json balance
```

The fees are testnut's on 2026-11-12 (100 ppk per input, 1 sat Lightning fee
reserve for 5 sat); the screens show the live numbers. If anything is
interrupted, `recover` on the same wallet file finishes it.
