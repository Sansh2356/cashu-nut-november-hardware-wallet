import { sat } from "../api.js";
import { HistoryTable, Stat } from "../components";

export function Home({ state, recover }) {
  const pend = state.pending;
  return (
    <>
      <section className="stats">
        <Stat label="In a pending request" value={sat(pend ? pend.inputs : 0)} />
        <Stat label="Reserved (cancelled, may still land)" value={sat(state.reserved)} />
        <Stat label="Proofs" value={state.proofs} />
        <Stat label="Device key" value={state.fingerprint ? <code>{state.fingerprint}…</code> : "not paired"} />
      </section>
      <section className="card">
        <div className="row spread"><h2>History</h2><button onClick={recover}>Recover</button></div>
        <HistoryTable history={state.history} />
      </section>
    </>
  );
}
