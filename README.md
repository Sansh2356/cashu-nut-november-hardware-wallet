# NutSigner

An offline signer for Cashu ecash, built for Nut November 2026.

Ecash is locked to a key that lives only on the device (NUT-11 P2PK with
`SIG_ALL`). The proofs can sit on any phone or laptop and are worthless without
the device's signature. Because `SIG_ALL` signs every input **and every output**
of a swap, a compromised host cannot reuse a signature to redirect the money.

This is the Week 1 code: the signing core, a host wallet, and a command-line
"device" that runs the same logic the Pi Zero will run.

## Status

| Part | State |
| --- | --- |
| NUT-00 blinding, hash_to_curve | Passes official test vectors |
| NUT-11 `SIG_ALL` message (swap and melt) | Passes official test vectors |
| Device review and signing | Done, with attack tests |
| Host wallet (fund, send, submit) | Done; tested against an in-process mint |
| Crash recovery (NUT-07, NUT-09) | Done; interrupted mints and swaps recover |
| Real testnut mint | Works (2026-11-03, cdk-mintd 0.18.1): fund, SIG_ALL swap, change; unsigned spend refused |
| Compact v2 wire format | Done; a 1-input, 7-output request is 1134 bytes (v1 JSON: 3305) |
| QR transport (BC-UR) | Done on the laptop: animated request QR, single signature QR, webcam/video reader |
| Fast blinding | Done; `sign()` 1.1 ms on a laptop (was 46 ms) via libsecp256k1 |
| Melt (pay a Lightning invoice) | Done: NUT-05 bolt11 + NUT-08 change + `SIG_ALL`; works on testnut (2026-11-12) |
| Receive (locked to this device) | Done: `send --to` locks to the recipient's device with `SIG_ALL`; `receive` swaps it into fresh locked coins |
| Pi Zero UI (SeedSigner fork) | Swap, receive and melt screens; see `docs/DEMO.md` |
| Companion app (browser UI) | Pair, fund, send, receive, pay, pending, history; `companion.py` + `web/` |

## Setup (laptop)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-host.txt               # device-only installs need just requirements.txt
python3 -m unittest discover -s tests -t . -v     # the testnut tests skip by default
NUTSIGNER_TESTNUT=1 python3 -m pytest -q -k testnut   # optional: a real melt on testnut
```

## Companion app

The laptop wallet for the Pi, with a browser UI: pair by scanning the
device's pubkey QR (no seed words on the laptop), fund, send (optionally
locked to another device), receive, pay an invoice, and see the balance,
pending request and history. Every request goes to the Pi as an animated QR
in the browser; the Pi's signature QR comes back through the webcam.

```bash
pip install -r requirements-host.txt         # OpenCV is optional: the fallback reader for the Pi's dim LCD
(cd web && npm install && npm run build)     # builds web/dist (Node 20.19+ or 22.12+)
python3 companion.py --wallet alice.json     # opens http://127.0.0.1:8338
```

`--mint` picks the mint (default testnut), `--port` the port, `--no-browser`
skips opening a tab. The app listens on 127.0.0.1 only and refuses other
sites' requests. It runs `recover` on start; the wallet file is opened for one
API call at a time, so `host_cli.py` can use the same file while it runs.
`docs/DEMO.md` walks through the two-user demo with it.

The webcam reader asks for 1080p, grayscales and contrast-stretches each frame
and decodes it with jsQR (normal and inverted). When that misses, about twice
a second it posts the frame to `/api/decode`, the same OpenCV reader as
`host_cli.py --scan`. Without a camera, paste the QR text instead.

UI code is in `web/src` (React, `qrcode`, `jsqr`; no UI framework):

```bash
cd web && npm test         # vitest: QR decode and parse helpers
npm run build              # then reload the page; companion.py serves web/dist
```

## First run against the real testnut mint

The testnut mint (the default) issues fake, unbacked ecash and pays its own
invoices. It charges a 100 ppk input fee, so a send costs about 1 sat. (The old
no-fee mint, `nofee.testnut.cashu.space`, no longer exists.)

```bash
python3 device_cli.py init                  # creates device_seed.json (dev only!)
PK=$(python3 device_cli.py pubkey)

python3 host_cli.py info                    # must say NUT-11 supported, ideally NUT-7 and NUT-9 too
python3 host_cli.py fund 100 --pubkey $PK   # mints 100 sat locked to the device
python3 host_cli.py send 21 -o request.json
python3 device_cli.py sign request.json -o signature.json   # review, type y
python3 host_cli.py submit signature.json   # prints a cashuA token for the recipient
python3 host_cli.py balance                 # 78 (100 - 21 - 1 sat fee)
```

Paste the printed token into any Cashu wallet (for example cashu.me) to check it
redeems.

To pay a Lightning invoice (testnut's fake Lightning backend "pays" anything):

```bash
python3 host_cli.py melt lnbc100n1... -o request.json      # gets a melt quote; add --qr for QR
python3 device_cli.py sign request.json -o signature.json  # shows: Pay 10 sat (Lightning), max fee, sats at risk
python3 host_cli.py submit signature.json                  # pays, keeps the unused fee reserve as change
```

To pay another NutSigner user, lock the coins to their device key. They
receive the token with their own wallet file and device:

```bash
python3 host_cli.py send 21 --to <their device pubkey>   # sign, submit, hand over the token
# the recipient (a new wallet takes --pubkey once):
python3 host_cli.py --wallet bob.json receive cashuA... --pubkey <bob's pubkey>
python3 device_cli.py sign request.json -o signature.json  # shows: Receive 21 sat, Fee 1 sat
python3 host_cli.py --wallet bob.json submit signature.json
```

`receive` accepts only cashuA tokens from this wallet's mint whose proofs are
all P2PK `SIG_ALL` locked to this wallet's device key and still unspent. Until
the swap, the sender knows the proofs too (but can't spend them without the
recipient's device), so receive promptly. A token that isn't locked is
refused: redeem it in any Cashu wallet. `send --to` locks with `SIG_ALL`,
which a NutSigner device can sign; a regular Cashu wallet may not support it.

**Each wallet file belongs to one mint.** Use a different `--wallet` file per
mint; using the same file with another mint is refused.

If every mint request hangs for about a minute, your machine's IPv6 route is
probably broken: Python tries IPv6 first and waits for it to time out.

### If a command is interrupted

`fund` and `submit` save everything needed to finish before they contact the
mint. `fund` saves the quote before showing the invoice, so an invoice paid
late (after `fund` gave up waiting) is finished by `recover`. If one fails with an unknown outcome (timeout, lost connection, crash),
nothing is lost. Run:

```bash
python3 host_cli.py recover
```

It asks the mint to re-send signatures it already issued (NUT-09) or confirms
via NUT-07 that the swap never happened. For a payment (melt) it asks for the
melt quote's state: paid (keep the change), still pending (run recover again
later) or unpaid with the inputs untouched. In that case, `submit` the same
signature again or `cancel`. Running `submit` again also recovers
automatically. `cancel` is refused while an outcome is unknown. A mint refusal
(4xx) is not trusted to mean "nothing happened": the wallet checks with the mint
the same way before clearing the send.

If you `cancel` a send the mint has seen, its outputs are kept and its inputs
stay reserved (shown by `balance`) so no new send reuses them. Should a slow
mint complete it later, `recover` collects the change and prints the
recipient's token. If it hasn't happened an hour after the cancel, `recover`
releases the inputs. A send whose inputs another transaction spent can never
complete; `recover` clears it instead of leaving it stuck.

Recipient tokens are saved in the wallet file until they have been printed, so
a crash or error after a swap can't lose them.

`abandon-fund` gives up on a funding whose invoice is unpaid, or paid but never
sent to the mint, and prints the quote id so you can claim it elsewhere. If a mint
request was sent, it archives the funding instead and `recover` keeps trying to finish it.

**One command at a time per wallet file.** The wallet holds a lock
(`<wallet>.lock`, POSIX `flock`) while a command runs; a second command on the
same file is refused with "in use by another process".

**If `submit` fails with a witness or signature error**, the mint may build the
`SIG_ALL` message differently from the current spec. Note the mint's software
and version from `info`, and check its changelog for NUT-11 changes.

### Offline development

No network? Run the built-in test mint:

```bash
python3 -m tests.fake_mint_server            # http://127.0.0.1:3338
python3 host_cli.py --mint http://127.0.0.1:3338 --wallet local.json info
```

## How it works

```
Host (laptop/phone, online, untrusted)          Device (offline)
  holds proofs locked to device key
  builds swap: inputs + outputs     ── request ──▶  checks every input is its own,
  + "reveals" (secret, r) per output                verifies each reveal against B_,
                                                    shows: send / change / fee / mint
                                    ◀─ signature ─  user approves → Schnorr sig (SIG_ALL)
  attaches witness, POST /v1/swap
  keeps change (locked to device again)
```

### Request format (v1)

```json
{
  "v": 1, "type": "swap", "mint": "https://...", "unit": "sat",
  "inputs":  [{"amount": 64, "id": "00..", "secret": "[\"P2PK\",...]", "C": "02.."}],
  "outputs": [{"amount": 1, "id": "00..", "B_": "03.."}],
  "reveals": [{"index": 0, "secret": "...", "r": "<hex>"}]
}
```

The device recomputes `B_ = hash_to_curve(secret) + r·G` for each reveal. If it
doesn't match, the request is rejected. Each verified output is classified as:

- **change**: locked back to this device with `SIG_ALL`
- **locked**: locked to another pubkey (shown on screen)
- **bearer**: a plain secret, spendable by whoever holds the token
- **unverified**: no reveal given; the device warns about it

The signature is returned as `{"v":1, "pubkey", "msg_hash", "signature"}`.

### Paying an invoice (melt)

A melt request is the same with `"type": "melt"` plus `"invoice"`, `"quote"`,
`"amount"` and `"fee_reserve"` from the mint's melt quote. Its outputs are
NUT-08 blank outputs (amount 0) that carry back the unused fee reserve; the
mint chooses their amounts. The device:

- decodes the bolt11 invoice itself and requires its amount to equal the
  quote's (invoices without an amount, and non-mainnet `lntb`/`lnbcrt`/... ones,
  are refused),
- requires every output to be a blank output (amount 0) revealed as change
  locked back to itself, and enough of them to return everything not paid to
  the invoice,
- checks the inputs cover amount + fee reserve,
- counts **every input as at risk**: the screen shows "Up to N sat at risk"
  and `--max-send` applies to the input total,
- signs `SIG_ALL` over inputs, outputs and the quote id, so the signature
  can't be used for any other quote.

```
Pay 30 sat (Lightning)
Payee not verified
Max fee 4 sat
Up to 36 sat at risk
Mint testnut.cashu.sp
```

**Limitation:** offline, the device can't check that the quote id belongs to
the invoice it shows, or that the fee reserve is the mint's real one; it
doesn't verify the payee either. The mint pays the invoice it stored with the
quote. A lying host can therefore swap in the quote of **any invoice the
inputs cover**, up to the whole input total, or show a lower max fee than the
mint will charge. That is why the device shows the input total as the amount
at risk and applies its limit to it. The host picks inputs tightly (proofs
that don't overshoot first, then the smallest one that closes the gap), so
the input total stays close to amount + fee reserve. If you cancel a payment
the mint has seen, its inputs stay reserved like a cancelled send's, and
`recover` collects the change should it be paid later.

### Over QR (v2)

For QR codes the same request is packed into a compact binary format
(`nutsigner/wire.py`): varints, a keyset table listed once, raw points, and
compact P2PK/bearer secrets. It travels as an animated BC-UR `ur:bytes` QR,
and the 132-byte signature comes back as a single QR (`nutsigner/qr.py`).

```bash
python3 host_cli.py send 21 --qr                  # window loops the animated request QR
python3 device_cli.py --ask-seed sign --scan --qr  # laptop stand-in for the device: webcam in, QR out
python3 host_cli.py submit --scan                  # webcam reads the signature QR
```

`--scan` also accepts a video file, e.g. a phone recording of the screen.

### Key derivation

The P2PK key is derived from a BIP-39 seed at `m/129372'/11'/0'/0/0`. This path
is specific to this project, chosen to stay clear of NUT-13's deterministic
secrets.

## Threat model

**Protects against:** a compromised host (malware, a malicious browser extension,
a stolen unlocked phone). Without the device, locked proofs can't be spent.
After signing, any change to inputs, output amounts or `B_` values makes the
mint reject the swap.

**Does not protect against:**
- **The mint.** It can fail, rug or censor. Keep amounts small.
- **The host keeping the recipient's token.** The host builds the token it hands
  to the recipient, so a malicious host could keep it. Paying to the recipient's
  pubkey (`send --to`) makes that output useless to the host.
- **Keyset ids.** The `SIG_ALL` message covers amounts and `B_`, but not each
  output's keyset `id`. A host could ask the mint to sign change under a
  different keyset of the same mint. It can't steal funds that way.
- **Physical attacks.** The Pi Zero is not a secure element. Blinding uses
  libsecp256k1; `curve.py` (not constant-time) is only a fallback, and never
  touches the signing key.
- `device_seed.json` is a plain file, for laptop development only. Use
  `device_cli.py --ask-seed` to keep the seed in memory; on the Pi, the
  SeedSigner fork loads it from SeedQR or typed words and keeps it in RAM.

## Layout

```
nutsigner/curve.py    secp256k1 point math, pure Python (fallback, test mint)
nutsigner/crypto.py   hash_to_curve, blinding, P2PK secrets, SIG_ALL message, Schnorr (embit)
nutsigner/device.py   review + sign. This is what runs on the Pi.
nutsigner/wire.py     compact v2 request/signature format for QR
nutsigner/bolt11.py   minimal invoice reader (amount, payment hash) for the device
nutsigner/qr.py       BC-UR frames (nutsigner/ur2: vendored from SeedSigner)
nutsigner/qrview.py   laptop QR window and webcam/video reader (OpenCV)
nutsigner/mint.py     mint HTTP client (stdlib only)
nutsigner/host.py     host wallet: fund, send, submit
device_cli.py         device as a terminal app
host_cli.py           host wallet CLI
companion.py          companion app: local JSON API + the UI in web/ (React, Vite)
tests/                spec vectors, flow + attack tests, crash-recovery tests, offline test mint
```

## On the Pi Zero 1.3

`device.py`, `bolt11.py`, `crypto.py`, `curve.py`, `wire.py` and `qr.py` need only `embit`,
which ships a prebuilt libsecp256k1 for armv6l. Copy the `nutsigner/` folder
and `device_cli.py` to the Pi and run the same commands over SSH.

The SeedSigner fork replaces the terminal with the 240×240 screen, buttons and
camera.

An iterative demo for progress till now - 

https://github.com/user-attachments/assets/1389ff44-1923-4f79-9fd8-399096adaa37




