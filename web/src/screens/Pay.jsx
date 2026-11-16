import { useState } from "react";
import { Busy, Result, useSign } from "../components";

export function Pay({ state, run, go }) {
  const [invoice, setInvoice] = useState("");
  const s = useSign(state, run);
  if (s.flow) return s.flow;
  if (s.result) return <Result r={s.result} go={go} />;
  if (state.pending) return <Busy state={state} go={go} />;
  return (
    <section className="card">
      <h2>Pay a Lightning invoice</h2>
      <form onSubmit={(e) => { e.preventDefault(); s.build("melt", { invoice: invoice.trim() }); }}>
        <label>Invoice <textarea rows={4} required value={invoice} onChange={(e) => setInvoice(e.target.value)} placeholder="lnbc…" /></label>
        <button className="primary">Get quote</button>
      </form>
    </section>
  );
}
