import { sat } from "../api.js";

// What the request asks the device to sign, as the device will show it.
export function Summary({ p }) {
  if (p.kind === "melt") return (
    <dl>
      <dt>Pay</dt><dd>{sat(p.amount)} (Lightning)</dd>
      <dt>Fee reserve</dt><dd>{sat(p.fee_reserve)}, unused part comes back as change</dd>
      <dt>At risk</dt><dd>up to {sat(p.inputs)} (the inputs)</dd>
      <dt>Invoice</dt><dd><code className="wrap">{p.invoice}</code></dd>
    </dl>
  );
  return (
    <dl>
      <dt>{p.kind === "send" ? "Send" : "Receive"}</dt><dd>{sat(p.amount)}</dd>
      {p.kind === "send" && <><dt>To</dt><dd>{p.to ? <>device <code>{p.to.slice(2, 10)}…</code> (locked)</> : "anyone holding the token"}</dd></>}
      <dt>Inputs</dt><dd>{sat(p.inputs)}</dd>
    </dl>
  );
}
