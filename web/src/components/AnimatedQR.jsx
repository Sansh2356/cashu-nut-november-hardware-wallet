import QRCode from "qrcode";
import { useEffect, useState } from "react";
import { QR_OPTS } from "./QR.jsx";

// The request as an animated BC-UR QR, frames from the Python side (nutsigner/qr.py), 5 fps.
export function AnimatedQR({ frames }) {
  const [svgs, setSvgs] = useState([]);
  const [i, setI] = useState(0);
  useEffect(() => {
    Promise.all(frames.map((f) => QRCode.toString(f, QR_OPTS))).then(setSvgs);
  }, [frames]);
  useEffect(() => {
    const t = setInterval(() => setI((i) => i + 1), 200);
    return () => clearInterval(t);
  }, []);
  if (!svgs.length) return null;
  return (
    <figure>
      <div className="qr" dangerouslySetInnerHTML={{ __html: svgs[i % svgs.length] }} />
      <figcaption className="muted">frame {(i % svgs.length) + 1} of {svgs.length}</figcaption>
    </figure>
  );
}
