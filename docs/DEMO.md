# Demo: two NutSigner users on testnut, from the companion app

Alice mints 100 sat locked to her device, sends 21 sat locked to Bob's device,
Bob receives them with his own wallet and pays a Lightning invoice. A tampered
request is rejected on the Pi, and a plain token is redeemed in cashu.me.

One Pi plays both devices: the seed is switched between Alice and Bob. Each
seed is made on the Pi and backed up on paper as a SeedQR. **No seed words
ever touch the laptop:** the app only learns each device's public key, by
scanning it from the Pi's screen. testnut's ecash and Lightning are fake.

## Setup

Laptop (this repo):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-host.txt        # OpenCV: the app's fallback reader for the Pi's dim LCD
(cd web && npm install && npm run build)    # the UI, once
```

Two app windows, one per wallet file (each opens a browser tab):

```bash
python3 companion.py --wallet alice.json               # http://127.0.0.1:8338
python3 companion.py --wallet bob.json --port 8339     # http://127.0.0.1:8339
```

Pi: the SeedSigner fork (`PYTHONPATH=src .venv/bin/python src/main.py`).

Signing always goes the same way: the app shows an animated request QR. On
the Pi, **Scan** it, check what the screen says, approve. The Pi shows the
signature QR; in the app press **Scan signature** and hold the Pi up to the
webcam (about 20 cm, the Pi's screen filling a third of the picture).

## 0. Two seeds, backed up on paper, paired with the app

Bob first, so Alice can send to his key later.

1. Pi: Tools → New seed (camera image or dice) → 12 words. Write the words
   down only to verify them on the Pi, then **Export as SeedQR** and copy it
   onto a paper SeedQR template. This paper is Bob's backup.
2. Pi: Tools → Cashu → Show pubkey.
3. Bob's app (`:8339`) opens on **Device**: hold the Pi up to the webcam. It
   pairs: "Paired. Ecash will be locked to this device."
4. Pi: Seeds → Bob's fingerprint → Discard seed.

Then the same for Alice: a new seed, her own paper SeedQR, Show pubkey, scanned
into Alice's app (`:8338`). Leave Alice's seed loaded.

## 1. Alice mints 100 sat, locked to her device

Alice's app: **Fund** → 100 → Get invoice. The invoice is shown as QR and
text; testnut pays it by itself, so after a few seconds: "Funded 100 sat".
**Wallet** shows the balance, the device key and the history.

## 2. Alice sends 21 sat locked to Bob's device

Bob's app: **Device** → Copy (Bob's key). Alice's app: **Send** → 21, paste
Bob's key as recipient → Create request.

Pi shows `Send 21 sat / 21 to key <Bob's key>..`, then `Change 42 sat / Fee 1 sat`.
Approve, then **Scan signature** in the app. Alice's app shows the token
locked to Bob, with Copy and a QR. Copy it, then "I have it, clear".

## 3. Switch seeds; Bob receives

Pi: Seeds → Alice's fingerprint → Discard seed. Then Scan → Bob's paper
SeedQR (or Seeds → Load a seed → Scan a SeedQR).

Bob's app: **Receive** → paste the token → Create request. Pi shows
`Receive 20 sat / Fee 1 sat / Mint testnut.cashu.sp`. Approve and scan the
signature: "Received 20 sat, locked to your device". (Optional shot: paste the
same token again; the app refuses it, already spent.)

## 4. Bob pays a Lightning invoice

Any testnut invoice works; this prints one for 5 sat:

```bash
python3 -c "from tests.fake_mint import make_invoice; print(make_invoice(5))"
```

Bob's app: **Pay invoice** → paste → Get quote. The app shows `Pay 5 sat`,
the fee reserve and the inputs at risk, then the request QR.
Pi page 1: `Pay 5 sat (Lightning) / Max fee 1 sat / Mint testnut.cashu.sp`.
Page 2 (warning): `Up to 16 sat at risk / Payee not verified`. Approve, scan the
signature: "Invoice paid", with the preimage. The history shows the melt and
its fee.

## 5. A tampered request is rejected on the Pi

Play a compromised laptop: Bob's app, **Pay invoice** with a fresh invoice →
Get quote, but don't show that QR to the Pi. Instead show a copy of the same
request whose amount the "malware" changed:

```bash
python3 -c "import json; from nutsigner import qr, qrview, wire; r = json.load(open('bob.json'))['pending']['request']; r['amount'] -= 1; qrview.show(qr.frames(wire.encode_request(r)), caption='TAMPERED')"
```

Pi: `Request rejected: invoice is for 5 sat but the quote says 4`. Press OK.
Nothing was signed. In the app press **Cancel**: the pending request is
dropped and the balance is unchanged.

## 6. A plain token, redeemed in cashu.me

Bob's app: **Send** → 5, recipient empty → Create request, approve on the Pi,
scan the signature. Copy the token (or scan its QR with a phone) into
cashu.me (add the mint `https://testnut.cashu.space`) and redeem it.

## If something is interrupted

The app runs **recover** when it starts, and on demand (Wallet → Recover, or
Pending → Recover). If the mint's reply is lost, the app says so: "The outcome
is unknown, but nothing is lost: everything needed is saved in the wallet
file. Run recover." Pending shows the open request with Cancel.

The fees above are testnut's on 2026-11-12 (100 ppk per input, 1 sat Lightning
fee reserve for 5 sat); the screens show the live numbers.

## Appendix: the same demo from the CLI

No browser: `host_cli.py` with OpenCV windows. Here the seeds are typed in on
the Pi (Seeds → Enter 12-word seed), and the keys are scanned with
`pubkey --scan`.

```bash
APK=$(python3 host_cli.py pubkey --scan)    # Pi: Alice's seed, Tools → Cashu → Show pubkey
BPK=$(python3 host_cli.py pubkey --scan)    # the same with Bob's seed
```

Every `--qr` opens a window with the animated request QR; `submit --scan`
reads the signature QR with the webcam.

```bash
# 1. Alice mints 100 sat
python3 host_cli.py --wallet alice.json fund 100 --pubkey $APK
# 2. Alice sends 21 sat locked to Bob
python3 host_cli.py --wallet alice.json send 21 --to $BPK --qr
python3 host_cli.py --wallet alice.json submit --scan | tee alice-out.txt
TOKEN=$(grep cashuA alice-out.txt)
# 3. Switch seeds; Bob receives
python3 host_cli.py --wallet bob.json receive $TOKEN --pubkey $BPK --qr
python3 host_cli.py --wallet bob.json submit --scan      # Received 20 sat, locked to your device
# 4. Bob pays an invoice
INVOICE=$(python3 -c "from tests.fake_mint import make_invoice; print(make_invoice(5))")
python3 host_cli.py --wallet bob.json melt $INVOICE      # writes request.json, no QR yet
python3 -c "import json; from nutsigner import qr, qrview, wire; r = json.load(open('request.json')); r['amount'] -= 1; qrview.show(qr.frames(wire.encode_request(r)), caption='TAMPERED')"   # 5. rejected
python3 -c "import json; from nutsigner import qr, qrview, wire; qrview.show(qr.frames(wire.encode_request(json.load(open('request.json')))))"
python3 host_cli.py --wallet bob.json submit --scan      # Invoice paid. Preimage: ... Change kept: ...
# 6. A plain token for cashu.me
python3 host_cli.py --wallet bob.json send 5 --qr
python3 host_cli.py --wallet bob.json submit --scan
```

If anything is interrupted, `recover` on the same wallet file finishes it.
