import { useEffect, useState } from "react";
import { api, sat } from "../api.js";
import { Amount, Copy, QR } from "../components";

export function Fund({ state, run, go, setNotice }) {
  const [amount, setAmount] = useState("");
  const [done, setDone] = useState(null);
  const pf = state.pending_fund;

  // Poll until the invoice is paid; recover() mints the locked ecash then.
  useEffect(() => {
    if (!pf) return;
    let alive = true, t;
    const poll = async () => {
      try {
        const out = await api("recover", {});
        if (!alive) return;
        if (out.result.fund === "done") return setDone(pf.amount), run(async () => out);
        if (out.result.fund === "expired") return run(async () => { throw new Error("The invoice expired unpaid. Nothing was minted."); });
      } catch (e) {
        if (!alive) return;
        run(async () => { throw e; });
      }
      t = setTimeout(poll, 2500);
    };
    t = setTimeout(poll, 1500);
    return () => { alive = false; clearTimeout(t); };
  }, [pf?.quote]);

  if (done && !pf) return (
    <section className="card">
      <h2>Funded {sat(done)}</h2>
      <p>New balance: <strong>{sat(state.balance)}</strong>, locked to device <code>{state.fingerprint}…</code></p>
      <button onClick={() => go("home")}>Back to wallet</button>
    </section>
  );
  if (pf) return (
    <section className="card">
      <h2>Pay this invoice: {sat(pf.amount)}</h2>
      <QR text={pf.invoice.toUpperCase()} />
      <div className="row"><code className="wrap">{pf.invoice}</code><Copy text={pf.invoice} /></div>
      <p className="muted"><span className="spinner" /> Waiting for payment… The ecash is minted locked to your device.</p>
      <button onClick={async () => {
        const out = await run(() => api("abandon-fund", {}));
        if (out) setNotice({ funded: `The invoice was paid, so the funding was finished (quote ${out.quote}).`,
          archived: `Funding archived (quote ${out.quote}): the mint may still issue it; recover keeps checking.`,
          forgotten: `Invoice given up (quote ${out.quote}). It is unpaid: don't pay it now.` }[out.outcome]);
      }}>Give up on this invoice</button>
    </section>
  );
  return (
    <section className="card">
      <h2>Fund</h2>
      <form className="row" onSubmit={(e) => { e.preventDefault(); setDone(null); run(() => api("fund", { amount: +amount })); }}>
        <Amount value={amount} set={setAmount} />
        <button className="primary">Get invoice</button>
      </form>
    </section>
  );
}
