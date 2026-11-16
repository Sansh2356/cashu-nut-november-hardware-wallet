import { useState } from "react";
import { Amount, Busy, Result, useSign } from "../components";

export function Send({ state, run, go }) {
  const [amount, setAmount] = useState("");
  const [to, setTo] = useState("");
  const s = useSign(state, run);
  if (s.flow) return s.flow;
  if (s.result) return <Result r={s.result} go={go} />;
  if (state.pending) return <Busy state={state} go={go} />;
  return (
    <section className="card">
      <h2>Send</h2>
      <form onSubmit={(e) => { e.preventDefault(); s.build("send", { amount: +amount, to: to.trim() || null }); }}>
        <Amount value={amount} set={setAmount} />
        <label>Recipient device pubkey (optional)
          <input value={to} onChange={(e) => setTo(e.target.value)} placeholder="02… or 03…: locks the coins to their NutSigner" />
        </label>
        <p className="muted">Empty: a plain token anyone can redeem, e.g. in cashu.me.</p>
        <button className="primary">Create request</button>
      </form>
    </section>
  );
}
