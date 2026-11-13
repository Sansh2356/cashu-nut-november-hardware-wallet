import QRCode from "qrcode";
import { describe, expect, it } from "vitest";
import { decodePixels, grayStretch, isSignature, parsePubkey } from "./qr.js";

// From nutsigner/qr.py: single(bytes(range(132))) and the first frame of frames(bytes(range(256)) * 4).
const SIG = "UR:BYTES/HDLRAEADAOAXAAAHAMATAYASBKBDBNBTBABSBEBYBGBWBBBZCMCHCSCFCYCWCECACKCTCXCLCPCNDKDADSDIDEDTDRDNDWDPDMDLDYEHEYEOEEECENEMETESFTFRFNFSFMFHFZFPFWFXFYFEFGFLFDGAGEGRGSGTGLGWGDGYGMGUGHGOHFHGHDHKHTHPHHHLHYHEHNHSIDIAIEIHIYIOISINIMJEJZJNJTJLJOJSJPJKJYKPKOKTKSKKKNKGKEKIKBLBLALYLFLSHKJPIYDR";
const FRAME = "UR:BYTES/1-9/LPADASCFAAAXCYDIJYFDRLHDJKHKAAAEAEADAOAXAAAHAMATAYASBKBDBNBTBABSBEBYBGBWBBBZCMCHCSCFCYCWCECACKCTCXCLCPCNDKDADSDIDEDTDRDNDWDPDMDLDYEHEYEOEEECENEMETESFTFRFNFSFMFHFZFPFWFXFYFEFGFLFDGAGEGRGSGTGLGWGDGYGMGUGHGOHFHGHDHKHTHPHHHLHYHEHNHSIDIAIEIHIYIOISINIMJEJZJNJTJLROSTNNID";
const PK = "02" + "ab".repeat(32);

// A webcam-like RGBA frame: the QR small and low-contrast (a dim LCD) on a grey background.
function frame(text, { dark = 70, light = 110, invert = false, scale = 3 } = {}) {
  const m = QRCode.create(text, { errorCorrectionLevel: "L" }).modules;
  const w = 640, h = 360, x0 = 200, y0 = 60, quiet = 4;
  const px = new Uint8ClampedArray(w * h * 4).fill(90);
  for (let y = 0; y < (m.size + 2 * quiet) * scale; y++)
    for (let x = 0; x < (m.size + 2 * quiet) * scale; x++) {
      const r = Math.floor(y / scale) - quiet, c = Math.floor(x / scale) - quiet;
      const on = !!(r >= 0 && c >= 0 && r < m.size && c < m.size && m.get(r, c));
      const v = on !== invert ? dark : light;
      const i = ((y0 + y) * w + x0 + x) * 4;
      px[i] = v; px[i + 1] = v; px[i + 2] = v + 10; px[i + 3] = 255;
    }
  return [px, w, h];
}

describe("decodePixels", () => {
  it("reads a dim signature QR", () => expect(decodePixels(...frame(SIG))).toBe(SIG));
  it("reads a light-on-dark (inverted) QR", () => expect(decodePixels(...frame(SIG, { invert: true }))).toBe(SIG));
  it("reads an animated request frame", () => expect(decodePixels(...frame(FRAME))).toBe(FRAME));
  it("reads the pubkey QR as the Pi shows it", () => expect(decodePixels(...frame(PK.toUpperCase()))).toBe(PK.toUpperCase()));
  it("returns an empty string without a QR", () => expect(decodePixels(new Uint8ClampedArray(64 * 64 * 4).fill(128), 64, 64)).toBe(""));
});

describe("grayStretch", () => {
  it("stretches a dim range to 0..255 and greys every channel", () => {
    const px = grayStretch(new Uint8ClampedArray([100, 100, 100, 255, 140, 140, 140, 255, 120, 120, 120, 255]));
    expect([...px]).toEqual([0, 0, 0, 255, 255, 255, 255, 255, 128, 128, 128, 255]);
  });
  it("leaves a flat frame alone", () => {
    expect([...grayStretch(new Uint8ClampedArray([7, 7, 7, 255]))]).toEqual([0, 0, 0, 255]);
  });
});

describe("parsePubkey", () => {
  it("accepts the Pi's uppercase key", () => expect(parsePubkey(PK.toUpperCase() + "\n")).toBe(PK));
  it("refuses anything else", () => {
    for (const t of ["04" + "ab".repeat(32), PK.slice(2), "abandon abandon", SIG]) expect(parsePubkey(t)).toBeNull();
  });
});

describe("isSignature", () => {
  it("accepts a single-part UR", () => expect(isSignature(SIG)).toBe(true));
  it("ignores request frames and other QRs", () => {
    for (const t of [FRAME, PK, "lnbc1...", ""]) expect(isSignature(t)).toBe(false);
  });
});
