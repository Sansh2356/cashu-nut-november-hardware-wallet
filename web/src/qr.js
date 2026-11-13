// QR helpers shared by the scanner and the screens. Pure functions, unit-tested in qr.test.js.
import jsQR from "jsqr";

// Grayscale and stretch contrast to 0..255, in place on RGBA pixels.
// The Pi's 240x240 LCD is dim and small in a webcam frame; jsQR binarizes better after this.
export function grayStretch(rgba) {
  let lo = 255, hi = 0;
  for (let i = 0; i < rgba.length; i += 4) {
    const g = (rgba[i] * 77 + rgba[i + 1] * 150 + rgba[i + 2] * 29) >> 8;
    rgba[i] = g;
    if (g < lo) lo = g;
    if (g > hi) hi = g;
  }
  const k = hi > lo ? 255 / (hi - lo) : 1;
  for (let i = 0; i < rgba.length; i += 4) {
    const g = (rgba[i] - lo) * k;
    rgba[i] = rgba[i + 1] = rgba[i + 2] = g;
  }
  return rgba;
}

// Text of the QR in an RGBA frame, or "". Tries normal and inverted (light-on-dark) codes.
export function decodePixels(rgba, width, height) {
  const hit = jsQR(grayStretch(rgba), width, height, { inversionAttempts: "attemptBoth" });
  return hit ? hit.data : "";
}

// The device's pubkey QR (Tools > Cashu > Show pubkey): 66 hex chars, uppercase on the Pi.
export function parsePubkey(text) {
  const key = text.trim().toLowerCase();
  return /^0[23][0-9a-f]{64}$/.test(key) ? key : null;
}

// The device's signature QR is one single-part UR ("UR:BYTES/<words>"); the
// animated request frames ("UR:BYTES/3-9/...") are not, so a scanner that
// still sees the laptop's own screen ignores them.
export function isSignature(text) {
  return /^ur:bytes\/[a-z]+$/i.test(text.trim());
}
