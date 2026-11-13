import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { AnimatedQR, Copy, QR, Scanner, api, sat } from "./components.jsx";
import { isSignature, parsePubkey } from "./qr.js";
import "./style.css";

const NOTHING_LOST = "The outcome is unknown, but nothing is lost: everything needed is saved in the wallet file. Recover finishes it.";
const TABS = [["home", "Wallet"], ["fund", "Fund"], ["send", "Send"], ["receive", "Receive"], ["pay", "Pay invoice"],
  ["pending", "Pending"], ["pair", "Device"]];

function App() {
  const [state, setState] = useState(null);
  const [tab, setTab] = useState("home");
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  // Every action: clear messages, call, adopt the returned state; errors go to the banner.
  const run = async (fn) => {
    setError(null);
    setNotice(null);
    try {
      const out = await fn();
      if (out?.state) setState(out.state);
      return out;
    } catch (e) {
      setError(e);
      api("state").then(setState, () => {});
    }
  };
  const recover = () => run(async () => {
    const out = await api("recover", {});
    const what = Object.entries(out.result).map(([k, v]) => `${k.replace("_", " ")}: ${v}`).join(", ");
    setNotice(what ? `Recover: ${what}` : "Recover: nothing to finish.");
    return out;
  });
  const go = (t) => { setTab(t); setError(null); setNotice(null); };

  useEffect(() => { recover().then((out) => out || api("state").then(setState, setError)); }, []);

  if (!state) return <main><h1>NutSigner</h1>{error ? <Banner error={error} recover={recover} /> : <p>Loading…</p>}</main>;
  const screen = state.device_pubkey ? tab : "pair";
  const p = { state, run, go, recover, setNotice };
  const Screen = { home: Home, fund: Fund, send: Send, receive: Receive, pay: Pay, pending: Pending, pair: Pair }[screen];
  return (
    <main>
      <header>
        <h1>NutSigner</h1>
        <span className="balance">{sat(state.balance)}</span>
      </header>
      <nav>
        {TABS.map(([t, label]) => (
          <button key={t} className={screen === t ? "on" : ""} onClick={() => go(t)}>
            {label}{t === "pending" && (state.pending || state.pending_fund) ? " •" : ""}
          </button>
        ))}
      </nav>
      <Banner error={error} recover={recover} />
      {notice && <p className="notice">{notice}</p>}
      <Tokens {...p} />
      <Screen {...p} />
    </main>
  );
}

function Banner({ error, recover }) {
  if (!error) return null;
  return (
    <div className="error">
      <strong>{error.message}</strong>
      {error.hint && <p>{error.hint}</p>}
      {error.in_flight && <button onClick={recover}>Recover now</button>}
    </div>
  );
}

// Tokens for the recipient stay in the wallet file until the user has them.
function Tokens({ state, run }) {
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

// ---------------------------------------------------------------- wallet

function Home({ state, recover }) {
  const pend = state.pending;
  return (
    <>
      <section className="stats">
        <Stat label="Balance" value={sat(state.balance)} big />
        <Stat label="In a pending request" value={sat(pend ? pend.inputs : 0)} />
        <Stat label="Reserved (cancelled, may still land)" value={sat(state.reserved)} />
        <Stat label="Proofs" value={state.proofs} />
        <Stat label="Device key" value={state.fingerprint ? <code>{state.fingerprint}…</code> : "not paired"} />
        <Stat label="Mint" value={<code>{state.mint.replace(/^https?:\/\//, "")}</code>} />
      </section>
      <section className="card">
        <div className="row spread"><h2>History</h2><button onClick={recover}>Recover</button></div>
        {state.history.length ? (
          <table>
            <thead><tr><th>When</th><th>What</th><th className="num">Amount</th><th className="num">Fee</th><th>Status</th></tr></thead>
            <tbody>
              {[...state.history].reverse().map((h, i) => (
                <tr key={i}>
                  <td className="muted">{new Date(h.time * 1000).toLocaleString()}</td>
                  <td>{h.type}{h.to ? <span className="muted"> to {h.to.slice(2, 10)}…</span> : ""}</td>
                  <td className="num">{sat(h.amount)}</td>
                  <td className="num">{h.fee ?? ""}</td>
                  <td className={h.status === "done" ? "ok" : "bad"}>{h.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="muted">Nothing yet. Fund the wallet to start.</p>}
      </section>
    </>
  );
}

const Stat = ({ label, value, big }) => (
  <div className={big ? "stat big" : "stat"}><span className="muted">{label}</span><span>{value}</span></div>
);

// ---------------------------------------------------------------- device

function Pair({ state, run, setNotice }) {
  const [scan, setScan] = useState(!state.device_pubkey);
  return (
    <section className="card">
      <h2>Device</h2>
      {state.device_pubkey && (
        <>
          <div className="row">Paired with device key <code className="wrap">{state.device_pubkey}</code><Copy text={state.device_pubkey} /></div>
          <p className="muted">Others send to this key to lock ecash to your device.</p>
        </>
      )}
      <ol>
        <li>On the NutSigner, load your seed (SeedQR or words). The seed never touches this laptop.</li>
        <li>Tools → Cashu → Show pubkey.</li>
        <li>Hold the Pi's screen up to the webcam.</li>
      </ol>
      {scan ? (
        <Scanner what="the device pubkey QR" accept={(t) => !!parsePubkey(t)} onText={async (t) => {
          setScan(false);
          if (await run(() => api("pair", { pubkey: parsePubkey(t) }))) setNotice("Paired. Ecash will be locked to this device.");
        }} />
      ) : <button onClick={() => setScan(true)}>Scan the pubkey QR</button>}
    </section>
  );
}

// ---------------------------------------------------------------- fund

function Fund({ state, run, go, setNotice }) {
  const [amount, setAmount] = useState(100);
  const [done, setDone] = useState(null);
  const pf = state.pending_fund;

  // Poll until the invoice is paid; recover() mints the locked ecash then.
  useEffect(() => {
    if (!pf) return;
    let alive = true, t;
    const poll = async () => {
      try {
        const out = await api("recover", {});
        if (!alive) return;
        if (out.result.fund === "done") return setDone(pf.amount), run(async () => out);
        if (out.result.fund === "expired") return run(async () => { throw new Error("The invoice expired unpaid. Nothing was minted."); });
      } catch (e) {
        if (!alive) return;
        run(async () => { throw e; });
      }
      t = setTimeout(poll, 2500);
    };
    t = setTimeout(poll, 1500);
    return () => { alive = false; clearTimeout(t); };
  }, [pf?.quote]);

  if (done && !pf) return (
    <section className="card">
      <h2>Funded {sat(done)}</h2>
      <p>New balance: <strong>{sat(state.balance)}</strong>, locked to device <code>{state.fingerprint}…</code></p>
      <button onClick={() => go("home")}>Back to wallet</button>
    </section>
  );
  if (pf) return (
    <section className="card">
      <h2>Pay this invoice: {sat(pf.amount)}</h2>
      <QR text={pf.invoice.toUpperCase()} />
      <div className="row"><code className="wrap">{pf.invoice}</code><Copy text={pf.invoice} /></div>
      <p className="muted"><span className="spinner" /> Waiting for payment… The ecash is minted locked to your device.</p>
      <button onClick={async () => {
        const out = await run(() => api("abandon-fund", {}));
        if (out) setNotice({ funded: `The invoice was paid, so the funding was finished (quote ${out.quote}).`,
          archived: `Funding archived (quote ${out.quote}): the mint may still issue it; recover keeps checking.`,
          forgotten: `Invoice given up (quote ${out.quote}). It is unpaid: don't pay it now.` }[out.outcome]);
      }}>Give up on this invoice</button>
    </section>
  );
  return (
    <section className="card">
      <h2>Fund</h2>
      <form className="row" onSubmit={(e) => { e.preventDefault(); setDone(null); run(() => api("fund", { amount: +amount })); }}>
        <Amount value={amount} set={setAmount} />
        <button className="primary">Get invoice</button>
      </form>
    </section>
  );
}

const Amount = ({ value, set }) => (
  <label>Amount (sat) <input type="number" min="1" required value={value} onChange={(e) => set(e.target.value)} /></label>
);

// ---------------------------------------------------------------- signing (send, receive, pay)

// What the request asks the device to sign, as the device will show it.
function Summary({ p }) {
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

// Request QR → the device approves → scan its signature QR → submit.
function SignFlow({ view, run, onDone }) {
  const [scanning, setScanning] = useState(false);
  const submit = async (qr) => {
    const out = await run(() => api("submit", { qr }));
    if (out) onDone(out.result);
    else setScanning(false); // the error is in the banner; show the request again
  };
  return (
    <section className="card">
      <h2>Sign on the NutSigner</h2>
      <Summary p={view.pending} />
      {scanning ? (
        <>
          <p>Show the signature QR from the NutSigner to the webcam.</p>
          <Scanner what="a NutSigner signature QR" accept={isSignature} onText={submit} />
          <button onClick={() => setScanning(false)}>Back to the request QR</button>
        </>
      ) : (
        <>
          <p>On the NutSigner: <b>Scan</b>, point it at this QR, check the screen, approve.</p>
          <AnimatedQR frames={view.frames} />
          <div className="row">
            <button className="primary" onClick={() => setScanning(true)}>Scan signature</button>
            <button onClick={() => run(() => api("cancel", {})).then((o) => o && onDone(null))}>Cancel</button>
          </div>
        </>
      )}
    </section>
  );
}

function Result({ r, go }) {
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

// A screen with a form that builds a request, then the signing flow, then the result.
function useSign(run) {
  const [view, setView] = useState(null);
  const [result, setResult] = useState(null);
  const build = async (path, body) => { const v = await run(() => api(path, body)); if (v) setView(v); };
  const flow = view && <SignFlow view={view} run={run} onDone={(r) => { setView(null); setResult(r); }} />;
  return { build, flow, result };
}

function Busy({ state, go }) {
  return (
    <section className="card">
      <p>A {state.pending.kind} of {sat(state.pending.amount)} is waiting for its signature. Finish or cancel it first.</p>
      <button onClick={() => go("pending")}>Go to pending</button>
    </section>
  );
}

function Send({ state, run, go }) {
  const [amount, setAmount] = useState(21);
  const [to, setTo] = useState("");
  const s = useSign(run);
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

function Receive({ state, run, go }) {
  const [token, setToken] = useState("");
  const s = useSign(run);
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

function Pay({ state, run, go }) {
  const [invoice, setInvoice] = useState("");
  const s = useSign(run);
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

// ---------------------------------------------------------------- pending

function Pending({ state, run, go, recover }) {
  const [view, setView] = useState(null);
  const [result, setResult] = useState(null);
  const pend = state.pending;
  if (view) return <SignFlow view={view} run={run} onDone={(r) => { setView(null); setResult(r); }} />;
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

createRoot(document.getElementById("root")).render(<StrictMode><App /></StrictMode>);
