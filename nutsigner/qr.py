"""QR transport: v2 wire bytes <-> BC-UR `ur:bytes` strings.

A request (about 1 KB) goes out as an animated QR: UR fountain-coded frames,
so the camera can miss frames and still recover it. A signature (132 bytes)
goes back as one single-part UR, which fits a single QR code.

Strings are uppercase so QR encoders use alphanumeric mode (smaller codes).
The UR library is Foundation Devices' (BSD-2-Clause-Patent, see ur2/LICENSE),
vendored unchanged from SeedSigner's src/seedsigner/helpers/ur2 at commit
ada81cd, so these frames decode with SeedSigner's own QR decoder. (The `ur`
package on PyPI is an unrelated project.)
"""

from .ur2.cbor_lite import CBORDecoder, CBOREncoder
from .ur2.ur import UR
from .ur2.ur_decoder import URDecoder
from .ur2.ur_encoder import UREncoder

# Payload bytes per animated frame. Smaller frames are easier for the 240x240
# camera to read but take more frames. Tune on the real device.
FRAME_LEN = 120


class QRError(ValueError):
    pass


def _ur(data: bytes) -> UR:
    e = CBOREncoder()
    e.encodeBytes(data)
    return UR("bytes", e.get_bytes())


class frames:
    """Endless iterator of uppercase QR frame strings for `data`.

    The first `count` frames carry the data in order; later ones are
    fountain-coded mixes that fill in whatever the reader missed.
    """

    def __init__(self, data: bytes, max_fragment_len: int = FRAME_LEN):
        self._e = UREncoder(_ur(bytes(data)), max_fragment_len)
        self.count = self._e.fountain_encoder.seq_len()

    def __iter__(self):
        return self

    def __next__(self) -> str:
        return self._e.next_part().upper()


def single(data: bytes) -> str:
    """One-frame UR string, for the 132-byte signature."""
    return UREncoder.encode(_ur(bytes(data))).upper()


class Assembler:
    """Collects scanned frames until the payload is complete."""

    def __init__(self):
        self._d = URDecoder()

    def add(self, frame: str) -> bool:
        """Feed one scanned string. Returns True once the payload is complete."""
        self._d.receive_part(frame)  # ignores frames that don't parse or don't belong
        return self._d.is_complete()

    @property
    def progress(self) -> float:
        """0.0 to 1.0, for a progress bar."""
        return 1.0 if self._d.is_complete() else self._d.estimated_percent_complete()

    def result(self) -> bytes:
        if not self._d.is_complete():
            raise QRError("not all frames received yet")
        if self._d.is_failure():
            raise QRError(f"frames could not be combined: {self._d.result_error()}")
        ur = self._d.result_message()
        if ur.type != "bytes":
            raise QRError(f"expected ur:bytes, got ur:{ur.type}")
        try:
            dec = CBORDecoder(ur.cbor)
            data, n = dec.decodeBytes()
        except Exception:
            raise QRError("ur:bytes payload is not a CBOR byte string") from None
        if n != len(ur.cbor):
            raise QRError("trailing data after the CBOR byte string")
        return data


def read_single(frame: str) -> bytes:
    a = Assembler()
    if not a.add(frame):
        raise QRError("not a complete single-part UR")
    return a.result()
