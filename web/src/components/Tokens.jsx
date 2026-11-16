import { api } from "../api.js";
import { Copy } from "./Copy.jsx";
import { QR } from "./QR.jsx";

// Tokens for the recipient stay in the wallet file until the user has them.
export function Tokens({ state, run }) {
  if (!state.tokens.length) return null;
  return (
    <section className="card token">
      <h2>Token to hand over</h2>
      {state.tokens.map((t) => (
        <div key={t}>
          <QR text={t} small />
          <textarea readOnly value={t} rows={4} />
          <Copy text={t} />
        </div>
      ))}
      <p className="muted">Kept in the wallet file until you confirm you have it.</p>
      <button className="primary" onClick={() => run(() => api("tokens/clear", { tokens: state.tokens }))}>I have it, clear</button>
    </section>
  );
}
