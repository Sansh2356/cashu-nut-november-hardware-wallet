import { sat } from "../api.js";

export function Busy({ state, go }) {
  return (
    <section className="card">
      <p>A {state.pending.kind} of {sat(state.pending.amount)} is waiting for its signature. Finish or cancel it first.</p>
      <button onClick={() => go("pending")}>Go to pending</button>
    </section>
  );
}
