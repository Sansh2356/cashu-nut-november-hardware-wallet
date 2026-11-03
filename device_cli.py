#!/usr/bin/env python3
"""NutSigner device, command-line edition.

Runs the same code that will run on the Pi, but with terminal input/output
instead of the screen and camera. Requests and signatures move as JSON files
for now; in Week 2-3 they move as QR codes.

  python3 device_cli.py init            # create a new seed (dev only!)
  python3 device_cli.py pubkey          # show the key ecash gets locked to
  python3 device_cli.py sign request.json -o signature.json
"""

import argparse
import json
import os
import sys

from embit import bip39

from nutsigner.device import DeviceSigner, Rejected, summary_lines

SEED_FILE = os.environ.get("NUTSIGNER_SEED", "device_seed.json")


def load_device(max_send=None):
    if not os.path.exists(SEED_FILE):
        sys.exit(f"No seed at {SEED_FILE}. Run: python3 device_cli.py init")
    with open(SEED_FILE) as f:
        mnemonic = json.load(f)["mnemonic"]
    return DeviceSigner.from_mnemonic(mnemonic, max_send=max_send)


def cmd_init(args):
    if os.path.exists(SEED_FILE) and not args.force:
        sys.exit(f"{SEED_FILE} already exists. Use --force to overwrite.")
    mnemonic = args.mnemonic or bip39.mnemonic_from_bytes(os.urandom(16 if args.words == 12 else 32))
    if not bip39.mnemonic_is_valid(mnemonic):
        sys.exit("Invalid mnemonic.")
    with open(SEED_FILE, "w") as f:
        json.dump({"mnemonic": mnemonic}, f)
    os.chmod(SEED_FILE, 0o600)
    dev = DeviceSigner.from_mnemonic(mnemonic)
    print("WARNING: the seed is stored in a plain file. Testnut ecash only!")
    print(f"Seed words: {mnemonic}")
    print(f"Device pubkey: {dev.pubkey_hex}")


def cmd_pubkey(args):
    print(load_device().pubkey_hex)


def cmd_sign(args):
    dev = load_device(args.max_send)
    with open(args.request) as f:
        req = json.load(f)
    try:
        review = dev.review(req)
    except Rejected as e:
        sys.exit(f"REJECTED: {e}")

    width = 24
    print("+" + "-" * width + "+")
    for line in summary_lines(review):
        print("|" + line[:width].ljust(width) + "|")
    print("+" + "-" * width + "+")

    if not args.yes:
        answer = input("Approve and sign? [y/N] ").strip().lower()
        if answer != "y":
            sys.exit("Not signed.")
    sig = dev.sign(req)
    with open(args.out, "w") as f:
        json.dump(sig, f, indent=2)
    print(f"Signed. Signature written to {args.out}")


def main():
    ap = argparse.ArgumentParser(description="NutSigner device (CLI)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="create or import a seed")
    p.add_argument("--words", type=int, choices=(12, 24), default=12)
    p.add_argument("--mnemonic", help="import these words instead of generating")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("pubkey", help="print the P2PK public key")
    p.set_defaults(fn=cmd_pubkey)
    p = sub.add_parser("sign", help="review and sign a request")
    p.add_argument("request")
    p.add_argument("-o", "--out", default="signature.json")
    p.add_argument("--max-send", type=int, help="refuse to send more than this")
    p.add_argument("--yes", action="store_true", help="skip the prompt (tests only)")
    p.set_defaults(fn=cmd_sign)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
