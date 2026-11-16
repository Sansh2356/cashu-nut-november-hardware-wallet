import { sat } from "../api.js";

export function HistoryTable({ history }) {
  if (!history.length) return <p className="muted">Nothing yet. Fund the wallet to start.</p>;
  return (
    <table>
      <thead><tr><th>When</th><th>What</th><th className="num">Amount</th><th className="num">Fee</th><th>Status</th></tr></thead>
      <tbody>
        {[...history].reverse().map((h, i) => (
          <tr key={i}>
            <td className="muted">{new Date(h.time * 1000).toLocaleString()}</td>
            <td className="kind">{h.type}{h.to ? <span className="muted"> to {h.to.slice(2, 10)}…</span> : ""}</td>
            <td className="num">{sat(h.amount)}</td>
            <td className="num">{h.fee ?? ""}</td>
            <td><span className={h.status === "done" ? "pill ok" : "pill bad"}>{h.status}</span></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
