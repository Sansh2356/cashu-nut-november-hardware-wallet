import { useState } from "react";
import { api } from "../api.js";
import { Copy, Scanner } from "../components";
import { parsePubkey } from "../qr.js";

export function Pair({ state, run, setNotice }) {
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
