import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { decodePixels } from "../qr.js";

// Reads QR codes with the webcam until `accept(text)` holds, then calls onText(text) once.
// Each frame: grayscale + contrast stretch + jsQR (normal and inverted). If that misses,
// about twice a second the frame goes to /api/decode (the CLI's OpenCV reader), which
// reads the Pi's dim LCD better. A paste box covers a missing camera.
export function Scanner({ accept, onText, what }) {
  const video = useRef(null);
  const [status, setStatus] = useState("Starting the camera…");
  const [paste, setPaste] = useState("");
  const done = useRef(false);
  const finish = (text) => {
    if (done.current) return;
    if (!accept(text)) return setStatus(`That QR is not ${what}.`);
    done.current = true;
    onText(text.trim());
  };

  useEffect(() => {
    let stream, timer, stopped = false, lastPost = 0, server = true;
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d", { willReadFrequently: true });

    async function tick() {
      const v = video.current;
      if (stopped || done.current) return;
      if (v && v.videoWidth) {
        canvas.width = v.videoWidth;
        canvas.height = v.videoHeight;
        ctx.drawImage(v, 0, 0);
        let text = decodePixels(ctx.getImageData(0, 0, canvas.width, canvas.height).data, canvas.width, canvas.height);
        if (!text && server && Date.now() - lastPost > 500) {
          lastPost = Date.now();
          try {
            text = (await api("decode", { image: canvas.toDataURL("image/jpeg", 0.9) })).text;
          } catch {
            server = false; // no OpenCV on this laptop: the browser decoder alone
          }
        }
        if (text && !stopped) finish(text);
      }
      timer = setTimeout(tick, 80);
    }

    navigator.mediaDevices?.getUserMedia({ audio: false, video: { width: { ideal: 1920 }, height: { ideal: 1080 } } })
      .then((s) => {
        stream = s;
        if (stopped) return s.getTracks().forEach((t) => t.stop());
        video.current.srcObject = s;
        video.current.play();
        const { width, height } = s.getVideoTracks()[0].getSettings();
        setStatus(`Camera ${width}×${height}. Hold ${what} steady, filling about a third of the picture.`);
        tick();
      })
      .catch((e) => setStatus(`No camera (${e?.message || "not available"}). Paste the QR text below instead.`));

    return () => {
      stopped = true;
      clearTimeout(timer);
      stream?.getTracks().forEach((t) => t.stop());
    };
  }, []);

  return (
    <div className="scanner">
      <video ref={video} muted playsInline />
      <p className="muted">{status}</p>
      <form className="row" onSubmit={(e) => { e.preventDefault(); finish(paste); }}>
        <input value={paste} onChange={(e) => setPaste(e.target.value)} placeholder="…or paste the QR text" />
        <button disabled={!paste.trim()}>Use</button>
      </form>
    </div>
  );
}
