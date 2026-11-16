import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { api, sat } from "./api.js";
import { Banner, Tokens } from "./components";
import { Fund, Home, Pair, Pay, Pending, Receive, Send } from "./screens";
import "./style.css";

const TABS = [["home", "Wallet"], ["fund", "Fund"], ["send", "Send"], ["receive", "Receive"], ["pay", "Pay invoice"],
  ["pending", "Pending"], ["pair", "Device"]];
const SCREENS = { home: Home, fund: Fund, send: Send, receive: Receive, pay: Pay, pending: Pending, pair: Pair };

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

  if (!state) return <main><h1 className="brand">NutSigner</h1>{error ? <Banner error={error} recover={recover} /> : <p>Loading…</p>}</main>;
  const screen = state.device_pubkey ? tab : "pair";
  const p = { state, run, go, recover, setNotice };
  const Screen = SCREENS[screen];
  return (
    <main>
      <header>
        <h1 className="brand"><img src="/logo.svg" alt="" />NutSigner</h1>
        <div className="balance">
          <span className="amount">{sat(state.balance)}</span>
          <span className="muted">{state.mint.replace(/^https?:\/\//, "")}</span>
        </div>
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

createRoot(document.getElementById("root")).render(<StrictMode><App /></StrictMode>);
