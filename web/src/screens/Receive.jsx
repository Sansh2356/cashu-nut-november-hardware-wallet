import { useState } from "react";
import { Busy, Result, useSign } from "../components";

export function Receive({ state, run, go }) {
  const [token, setToken] = useState("");
  const s = useSign(state, run);
  if (s.flow) return s.flow;
  if (s.result) return <Result r={s.result} go={go} />;
  if (state.pending) return <Busy state={state} go={go} />;
  return (
    <section className="card">
      <h2>Receive</h2>
      <form onSubmit={(e) => { e.preventDefault(); s.build("receive", { token: token.trim() }); }}>
        <label>Token locked to your device
          <textarea rows={5} required value={token} onChange={(e) => setToken(e.target.value)} placeholder="cashuA…" />
        </label>
        <p className="muted">Swaps it into fresh coins locked to your device, so the sender can no longer touch it.</p>
        <button className="primary">Create request</button>
      </form>
    </section>
  );
}
