import { useState } from "react";
import { api } from "../api.js";
import { isSignature } from "../qr.js";
import { AnimatedQR } from "./AnimatedQR.jsx";
import { Scanner } from "./Scanner.jsx";
import { Summary } from "./Summary.jsx";

// Request QR → the device approves → scan its signature QR → submit.
export function SignFlow({ view, state, run, onDone }) {
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
            {!state.pending?.submitted && /* outcome unknown: recover only, as on the Pending tab */
              <button onClick={() => run(() => api("cancel", {})).then((o) => o && onDone(null))}>Cancel</button>}
          </div>
        </>
      )}
    </section>
  );
}

// A screen with a form that builds a request, then the signing flow, then the result.
export function useSign(state, run) {
  const [view, setView] = useState(null);
  const [result, setResult] = useState(null);
  const build = async (path, body) => { const v = await run(() => api(path, body)); if (v) setView(v); };
  const flow = view && <SignFlow view={view} state={state} run={run} onDone={(r) => { setView(null); setResult(r); }} />;
  return { build, flow, result };
}
