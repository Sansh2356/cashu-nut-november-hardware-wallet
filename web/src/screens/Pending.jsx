import { useState } from "react";
import { api, sat } from "../api.js";
import { Result, SignFlow, Summary } from "../components";

const NOTHING_LOST = "The outcome is unknown, but nothing is lost: everything needed is saved in the wallet file. Recover finishes it.";

export function Pending({ state, run, go, recover }) {
  const [view, setView] = useState(null);
  const [result, setResult] = useState(null);
  const pend = state.pending;
  if (view) return <SignFlow view={view} state={state} run={run} onDone={(r) => { setView(null); setResult(r); }} />;
  if (result) return <Result r={result} go={go} />;
  return (
    <section className="card">
      <div className="row spread"><h2>Pending</h2><button onClick={recover}>Recover</button></div>
      {state.in_flight && <p className="error">{NOTHING_LOST}</p>}
      {pend ? (
        <>
          <Summary p={pend} />
          <div className="row">
            {!pend.submitted && <button className="primary" onClick={async () => { const v = await run(() => api("frames")); if (v) setView(v); }}>Show request QR</button>}
            {!pend.submitted && <button onClick={() => run(() => api("cancel", {}))}>Cancel</button>}
          </div>
        </>
      ) : state.pending_fund ? (
        <p>An invoice for {sat(state.pending_fund.amount)} is waiting to be paid. <button onClick={() => go("fund")}>Show it</button></p>
      ) : <p className="muted">Nothing pending.</p>}
      {state.archived_funds > 0 && <p className="muted">{state.archived_funds} abandoned funding(s): recover keeps checking whether they land.</p>}
      {state.reserved > 0 && <p className="muted">{sat(state.reserved)} reserved by cancelled requests the mint may still complete; recover releases them an hour after cancel.</p>}
    </section>
  );
}
