import QRCode from "qrcode";
import { useEffect, useState } from "react";

export const QR_OPTS = { type: "svg", errorCorrectionLevel: "L", margin: 4 };

export function QR({ text, small }) {
  const [svg, setSvg] = useState(null);
  useEffect(() => {
    QRCode.toString(text, QR_OPTS).then(setSvg, () => setSvg(""));
  }, [text]);
  if (svg === "") return <p className="muted">Too long for one QR code: copy the text instead.</p>;
  return <div className={small ? "qr small" : "qr"} dangerouslySetInnerHTML={{ __html: svg || "" }} />;
}
