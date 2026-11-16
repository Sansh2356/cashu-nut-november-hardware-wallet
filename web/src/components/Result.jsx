import { sat } from "../api.js";

export function Result({ r, go }) {
  const fee = r.fee ? `, fee ${sat(r.fee)}` : "";
  const text = { send: `Sent ${sat(r.amount)}${fee}. Hand over the token above.`,
    receive: `Received ${sat(r.amount)}${fee}, locked to your device.`,
    melt: `Invoice paid: ${sat(r.amount)}${fee}.` }[r.type] || `${r.type}: ${r.status}`;
  return (
    <section className="card">
      <h2>Done</h2>
      <p>{text}</p>
      {r.preimage && <p className="muted">Preimage <code className="wrap">{r.preimage}</code></p>}
      <button onClick={() => go("home")}>Back to wallet</button>
    </section>
  );
}
