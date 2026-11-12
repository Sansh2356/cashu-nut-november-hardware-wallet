"""Laptop QR window and camera reader, using OpenCV. Not used on the device.

`show()` loops QR frames in a window; `scan()` reads QR codes from a webcam
or from a video file (e.g. a phone recording of the screen).
"""

import cv2

WINDOW = "NutSigner"
QUIET_ZONE = 4  # modules of white border the QR spec requires


def render(text: str, px: int = 600):
    """QR image (grayscale numpy array) about `px` pixels wide."""
    p = cv2.QRCodeEncoder.Params()
    p.mode = cv2.QRCODE_ENCODER_MODE_ALPHANUMERIC  # UR strings are uppercase alphanumeric
    img = cv2.QRCodeEncoder.create(p).encode(text)
    img = cv2.copyMakeBorder(img, *[QUIET_ZONE] * 4, cv2.BORDER_CONSTANT, value=255)
    scale = max(1, px // img.shape[0])
    return cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)


def _caption(img, text):
    img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR) if img.ndim == 2 else img.copy()
    cv2.putText(img, text, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    return img


def _window_closed() -> bool:
    try:
        return cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1
    except cv2.error:
        return True


def show(frames, fps: float = 5, caption: str = "Esc or q to close"):
    """Loop QR frames (any iterator of strings) until Esc/q or the window closes."""
    cv2.namedWindow(WINDOW, cv2.WINDOW_AUTOSIZE)
    try:
        for text in frames:
            cv2.imshow(WINDOW, _caption(render(text), caption))
            if cv2.waitKey(int(1000 / fps)) & 0xFF in (27, ord("q")) or _window_closed():
                return
    finally:
        try:
            cv2.destroyWindow(WINDOW)
        except cv2.error:  # OpenCV 5 raises if the user already closed it
            pass


def scan(done, source=0, preview=None, caption=lambda: "Point the camera at the QR"):
    """Feed every decoded QR string to `done(text)` until it returns True.

    `source` is a camera index or a video file path; a live camera gets a
    preview window unless `preview` says otherwise. Returns True when done,
    False if the user pressed Esc/q, closed the preview or the video ended.
    """
    if preview is None:
        preview = isinstance(source, int)
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise OSError(f"cannot open camera or video {source!r}")
    det = cv2.QRCodeDetector()
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                return False
            try:
                text, _, _ = det.detectAndDecode(frame)
            except cv2.error:  # OpenCV's decoder can assert on damaged codes
                text = ""
            if text and done(text):
                return True
            if preview:
                cv2.imshow(WINDOW, _caption(frame, caption()))
                if cv2.waitKey(1) & 0xFF in (27, ord("q")) or _window_closed():
                    return False
    finally:
        cap.release()
        if preview:
            cv2.destroyAllWindows()
