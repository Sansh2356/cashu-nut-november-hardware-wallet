#!/usr/bin/env python3
"""NutSigner device, command-line edition.

Runs the same code that will run on the Pi, but with terminal input/output
instead of the screen and camera. Requests and signatures move as JSON files
for now; in Week 2-3 they move as QR codes.

  python3 device_cli.py init            # create a new seed (dev only!)
  python3 device_cli.py pubkey          # show the key ecash gets locked to
  python3 device_cli.py sign request.json -o signature.json

  python3 device_cli.py --ask-seed sign request.json   # type the words; never stored

`--ask-seed` is the device flow: the seed lives only in memory for this run
and the derived pubkey is shown so you can check it. The seed file is for
laptop development only.
"""

import argparse
import getpass
import json
import os
import sys

from embit import bip39

from nutsigner.device import DeviceSigner, Rejected, summary_lines

SEED_FILE = os.environ.get("NUTSIGNER_SEED", "device_seed.json")


def load_device(args, max_send=None):
    if args.ask_seed:
        mnemonic = " ".join(getpass.getpass("Seed words (kept in memory only): ").lower().split())
        if not bip39.mnemonic_is_valid(mnemonic):
            sys.exit("Invalid mnemonic.")
        dev = DeviceSigner.from_mnemonic(mnemonic, max_send=max_send)
        print(f"Device pubkey: {dev.pubkey_hex}  <- check this matches", file=sys.stderr)
        return dev
    if not os.path.exists(SEED_FILE):
        sys.exit(f"No seed at {SEED_FILE}. Run: python3 device_cli.py init, or use --ask-seed")
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
    print(load_device(args).pubkey_hex)


def scan_request(source) -> dict:
    """Read an animated request QR from a camera index or video file."""
    from nutsigner import qr, qrview, wire  # OpenCV only needed for --scan/--qr
    a = qr.Assembler()
    if not qrview.scan(a.add, source, caption=lambda: f"Scanning request: {int(a.progress * 100)}%"):
        sys.exit("No complete request QR was read.")
    try:
        return wire.decode_request(a.result())
    except ValueError as e:
        sys.exit(f"REJECTED: unreadable request ({e})")


def cmd_sign(args):
    dev = load_device(args, args.max_send)
    if args.scan is not None:
        req = scan_request(args.scan)
    elif args.request:
        with open(args.request) as f:
            req = json.load(f)
    else:
        sys.exit("Give a request file or --scan.")
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
    if args.qr:
        from nutsigner import qr, qrview, wire
        print("Showing the signature QR. Close the window once the laptop has it.")
        qrview.show(iter(lambda: qr.single(wire.encode_signature(sig)), None),
                    caption="Signature - scan with host_cli submit --scan")


def main():
    ap = argparse.ArgumentParser(description="NutSigner device (CLI)")
    ap.add_argument("--ask-seed", action="store_true",
                    help="type the seed words instead of reading the seed file; nothing is written")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="create or import a seed")
    p.add_argument("--words", type=int, choices=(12, 24), default=12)
    p.add_argument("--mnemonic", help="import these words instead of generating")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("pubkey", help="print the P2PK public key")
    p.set_defaults(fn=cmd_pubkey)
    p = sub.add_parser("sign", help="review and sign a request")
    p.add_argument("request", nargs="?", help="request file (or use --scan)")
    p.add_argument("--scan", nargs="?", const="0", metavar="SOURCE",
                   type=lambda s: int(s) if s.isdigit() else s,
                   help="read the animated request QR from a camera (default 0) or a video file")
    p.add_argument("--qr", action="store_true", help="also show the signature as a QR")
    p.add_argument("-o", "--out", default="signature.json")
    p.add_argument("--max-send", type=int, help="refuse to send more than this")
    p.add_argument("--yes", action="store_true", help="skip the prompt (tests only)")
    p.set_defaults(fn=cmd_sign)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
