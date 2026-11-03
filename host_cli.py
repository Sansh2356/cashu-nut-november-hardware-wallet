#!/usr/bin/env python3
"""NutSigner host wallet, command-line edition. Runs on your laptop.

  python3 host_cli.py info
  python3 host_cli.py fund 100 --pubkey <device pubkey>
  python3 host_cli.py balance
  python3 host_cli.py send 21 -o request.json        # then sign on the device
  python3 host_cli.py submit signature.json           # prints the token to give away
  python3 host_cli.py recover                         # finish an interrupted fund/submit
  python3 host_cli.py cancel                          # drop a pending send (not yet submitted)
  python3 host_cli.py abandon-fund                    # give up on (or archive) an unfinished funding

Defaults to the testnut mint, which issues fake, unbacked ecash (input fee 100 ppk).
Each wallet file belongs to one mint: use a different --wallet per mint.
"""

import argparse
import json
import sys

from nutsigner.host import HostWallet, WalletError
from nutsigner.mint import MintClient, MintError, MintUnavailable

DEFAULT_MINT = "https://testnut.cashu.space"


def print_send_result(result):
    token, sent, change = result
    print(f"Swap accepted. Change kept: {sum(p['amount'] for p in change)} sat.")


def print_tokens(w):
    """Show tokens of finished sends, then forget them. They are bearer money."""
    for token in w.state.get("undelivered_tokens", []):
        print(f"\nToken for the recipient:\n{token}\n")
    w.clear_tokens()


def main():
    ap = argparse.ArgumentParser(description="NutSigner host wallet")
    ap.add_argument("--mint", default=DEFAULT_MINT)
    ap.add_argument("--wallet", default="host_wallet.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    p = sub.add_parser("fund")
    p.add_argument("amount", type=int)
    p.add_argument("--pubkey", required=True, help="device pubkey from device_cli.py pubkey")
    sub.add_parser("balance")
    p = sub.add_parser("send")
    p.add_argument("amount", type=int)
    p.add_argument("--to", help="lock the payment to this recipient pubkey (optional)")
    p.add_argument("-o", "--out", default="request.json")
    p = sub.add_parser("submit")
    p.add_argument("signature")
    sub.add_parser("recover")
    sub.add_parser("cancel")
    sub.add_parser("abandon-fund")
    args = ap.parse_args()

    client = MintClient(args.mint)
    w = None
    try:
        w = HostWallet(args.wallet, client, args.mint)
        if args.cmd == "info":
            info = w.check_mint()
            nuts = info.get("nuts", {})
            print(f"{info.get('name', '?')} - NUT-11 supported")
            for n, what in (("7", "check state"), ("9", "restore")):
                ok = (nuts.get(n) or {}).get("supported")
                print(f"  NUT-{n} ({what}): {'yes' if ok else 'NO - interrupted sends cannot be recovered'}")
            for ks in w.keysets():
                print(f"  keyset {ks['id']} unit={ks['unit']} active={ks.get('active')} "
                      f"fee_ppk={ks.get('input_fee_ppk', 0)}")
        elif args.cmd == "fund":
            w.fund(args.amount, args.pubkey,
                   on_invoice=lambda inv: print(f"Invoice (testnut pays it automatically):\n{inv}\n"))
            print(f"Funded. Balance: {w.balance()} sat, all locked to {args.pubkey[:16]}...")
        elif args.cmd == "balance":
            print(f"{w.balance()} sat spendable, in {len(w.state['proofs'])} locked proofs")
            if w.reserved_balance():
                print(f"{w.reserved_balance()} sat reserved by a cancelled send the mint may still "
                      "complete; recover collects or releases it (released an hour after cancel).")
            pf = w.state.get("pending_fund")
            if pf and not w.in_flight():
                print(f"Funding invoice pending (quote {pf['quote']}): run recover once paid, "
                      "or abandon-fund.")
            for old in w.state.get("abandoned_funds", []):
                print(f"Archived funding {old['quote']} ({sum(o['amount'] for o in old['outputs'])} sat): "
                      "recover keeps trying; if it never completes, claim the quote with another wallet.")
            if w.in_flight():
                print("An operation was interrupted. Run: host_cli.py recover")
            elif w.state.get("pending"):
                print("A send is waiting for the device's signature.")
        elif args.cmd == "send":
            req = w.build_send_request(args.amount, args.to)
            try:
                with open(args.out, "w") as f:
                    json.dump(req, f, indent=2)
            except OSError as e:
                raise WalletError(f"could not write {args.out}: {e}\nThe send is pending but not "
                                  "written anywhere; run cancel, then send again.") from None
            print(f"Request written to {args.out} ({len(req['inputs'])} inputs, "
                  f"{len(req['outputs'])} outputs). Sign it on the device.")
        elif args.cmd == "submit":
            try:
                with open(args.signature) as f:
                    sig = json.load(f)
            except (OSError, ValueError) as e:
                raise WalletError(f"cannot read signature file {args.signature}: {e}") from None
            print_send_result(w.submit(sig))
            print(f"\nBalance: {w.balance()} sat.")
        elif args.cmd == "recover":
            result = w.recover()
            archived = len(w.state.get("abandoned_funds", []))
            if archived:
                print(f"{archived} archived funding(s) still waiting; recover will keep checking.")
            elif not result:
                print("Nothing to recover.")
            if result.get("fund") == "done":
                print("Interrupted funding completed.")
            elif result.get("fund") == "waiting":
                print("The funding invoice is not paid yet. Pay it, then run recover again "
                      "(or abandon-fund to give up on it).")
            elif result.get("fund") == "mint_pending":
                print("The mint is still processing this funding; run recover again later.")
            elif result.get("fund") == "expired":
                print("The funding invoice expired unpaid; cleared it.")
            for quote in result.get("archived_stuck", []):
                print(f"Archived funding {quote} could not be finished (refused by the mint, or "
                      "issued elsewhere); recover will retry, or contact the mint.")
            if result.get("late_funds"):
                print(f"An archived funding was issued after all: {result['late_funds']} sat added.")
            if result.get("released"):
                print(f"Released {result['released']} sat reserved by a cancelled send.")
            for late in result.get("late_sends", []):
                print("A send you cancelled was completed by the mint after all.")
                print_send_result(late)
            if result.get("send") == "done":
                print_send_result(result["send_result"])
            elif result.get("send") == "not_sent":
                print("The swap never reached the mint. Your funds are untouched; "
                      "submit the same signature again or cancel.")
            elif result.get("send") == "failed":
                print("The pending send can never complete: another transaction spent its "
                      "inputs. It was cleared.")
            print(f"Balance: {w.balance()} sat.")
        elif args.cmd == "abandon-fund":
            quote, archived = w.abandon_fund()
            if archived:
                print(f"Funding archived (quote {quote}): the mint may still issue it, so "
                      "recover keeps checking. You can fund again now.")
            else:
                print(f"Funding forgotten. If you paid it, claim quote {quote} at "
                      f"{args.mint} with another wallet.")
        elif args.cmd == "cancel":
            w.cancel_pending()
            print("Pending send cleared. If the mint ever saw it, recover will still "
                  "collect the change should it complete later.")
    except (MintError, WalletError, OSError, ValueError) as e:
        msg = f"Error: {e}"
        if w is not None and w.in_flight():
            msg += ("\nThe outcome is unknown, but nothing is lost: everything needed is saved. "
                    "Run: host_cli.py recover")
        elif isinstance(e, MintUnavailable):
            msg += "\nCheck your connection and the mint URL, then try again."
        sys.exit(msg)
    finally:
        if w is not None:
            if w.state.get("undelivered_tokens"):
                print_tokens(w)  # even if a later step failed
            w.close()


if __name__ == "__main__":
    main()
