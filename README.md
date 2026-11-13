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
| Pi Zero UI, QR transport | Week 2–3 |

## Setup (laptop)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 -m unittest discover -s tests -t . -v     # 61 tests should pass
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
via NUT-07 that the swap never happened. In that case, `submit` the same
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
- **Physical attacks.** The Pi Zero is not a secure element, and `curve.py` is
  not constant-time (used only for blinding, never for the signing key).
- `device_seed.json` is a plain file. On the real device, the seed should only
  live in RAM, as SeedSigner does.

## Layout

```
nutsigner/curve.py    secp256k1 point math, pure Python (blinding)
nutsigner/crypto.py   hash_to_curve, P2PK secrets, SIG_ALL message, Schnorr (embit)
nutsigner/device.py   review + sign. This is what runs on the Pi.
nutsigner/mint.py     mint HTTP client (stdlib only)
nutsigner/host.py     host wallet: fund, send, submit
device_cli.py         device as a terminal app
host_cli.py           host wallet CLI
tests/                spec vectors, flow + attack tests, crash-recovery tests, offline test mint
```

## On the Pi Zero 1.3

`device.py`, `crypto.py` and `curve.py` need only `embit`, which ships a
prebuilt libsecp256k1 for armv6l. Copy the `nutsigner/` folder and
`device_cli.py` to the Pi and run the same commands over SSH.

Week 2 replaces the terminal with the 240×240 screen and buttons, and the JSON
files with QR codes.

An iterative demo for progress till now - 



https://github.com/user-attachments/assets/1389ff44-1923-4f79-9fd8-399096adaa37




