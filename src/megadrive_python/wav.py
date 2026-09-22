"""A 16-bit stereo WAV writer, for ``--wav`` and the validation scripts.

Written here rather than with :mod:`wave` so the core keeps no import that a
frontend would have to work around, the same reason :mod:`megadrive_python.png`
exists.
"""

from __future__ import annotations

import struct
import sys
from array import array
from pathlib import Path


def write_wav(path: Path, samples: list[tuple[int, int]], rate: int) -> None:
    """``samples`` as (left, right) pairs of signed 16-bit values."""
    data = array("h")
    for left, right in samples:
        data.append(max(-32768, min(32767, int(left))))
        data.append(max(-32768, min(32767, int(right))))
    if sys.byteorder == "big":
        data.byteswap()
    payload = data.tobytes()
    path.write_bytes(
        b"RIFF"
        + struct.pack("<I", 36 + len(payload))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 2, rate, rate * 4, 4, 16)
        + b"data"
        + struct.pack("<I", len(payload))
        + payload
    )
