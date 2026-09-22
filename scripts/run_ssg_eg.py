#!/usr/bin/env python
"""Nemesis's SSG-EG test against his MD1600 recording, by measurement.

    python scripts/run_ssg_eg.py --wav "<unpacked>/ssg-eg md1600.wav"

The recording (``tests/test_roms/nemesis/ssg-eg md1600.rar``, fetched with
``--large``) is 83 seconds of a real MD1600's audio output while
``ym2612ssg-eg.bin`` runs: **hardware-captured**, and the judge for SSG-EG
(docs/validation.md).  It cannot be compared sample for sample -- it is an analogue
recording at 96 kHz of an unknown gain, with an unknown start offset, through the
console's own filter -- so what is compared is what can be measured on both:

1. **how many notes sound**, and for how long each does;
2. **the gap between one note's start and the next's**, which is the ROM's own
   timing and must match to the frame;
3. **the envelope's repetition period inside each note**, which is what SSG-EG *is*:
   a shape that repeats at a rate the envelope generator sets.  Measured by
   autocorrelating the amplitude envelope of each note and taking the first strong
   peak.  A shape that does not repeat (the ones that hold or run to silence) has no
   peak, and that is itself a result.

Nothing here is a pass or a fail by itself: the script prints both machines' numbers
side by side and their differences, and docs/worklog.md records what they came to.
The board's own audio never leaves this script: the unpacked recording is deleted by
the caller and nothing is committed.
"""

from __future__ import annotations

import argparse
import math
import sys
import wave
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from megadrive_python.cartridge import Cartridge
from megadrive_python.machine import MACHINES
from megadrive_python.system import System

ROOT = Path(__file__).resolve().parent.parent
ROM = ROOT / "tests" / "test_roms" / "nemesis" / "ym2612ssg-eg.bin"

#: The envelope is measured in blocks of this length, on both machines.
BLOCK_HZ = 20
#: A block counts as sounding above this fraction of the recording's own peak.
THRESHOLD = 0.02


def envelope(samples: list[float], rate: float) -> tuple[list[float], float]:
    """Block RMS **about each block's own mean**, at :data:`BLOCK_HZ`, and the block
    length in seconds.

    The mean is removed because neither signal sits at zero: the MD1600's recording
    has whatever offset its capture chain had, and the board's raw FM samples have
    the discrete chip's standing level (the ladder drives +1 in every gap between
    channels, which is 72 units of every sample even in silence).  A plain RMS would
    then call silence loud on both.
    """
    block = max(1, int(rate / BLOCK_HZ))
    out = []
    for start in range(0, len(samples) - block, block):
        total = 0.0
        squares = 0.0
        for index in range(start, start + block):
            value = samples[index]
            total += value
            squares += value * value
        mean = total / block
        out.append(math.sqrt(max(0.0, squares / block - mean * mean)))
    return out, block / rate


def notes(levels: list[float], seconds: float) -> list[tuple[float, float, int, int]]:
    """The runs of blocks above the threshold: (start s, length s, first, last)."""
    peak = max(levels) if levels else 0.0
    if peak <= 0:
        return []
    limit = peak * THRESHOLD
    found = []
    index = 0
    while index < len(levels):
        if levels[index] > limit:
            end = index
            while end < len(levels) and levels[end] > limit:
                end += 1
            if end - index >= 2:
                found.append((index * seconds, (end - index) * seconds, index, end))
            index = end
        else:
            index += 1
    return found


def repetition(levels: list[float], first: int, last: int, seconds: float) -> float | None:
    """The period of the envelope inside one note, by autocorrelation.

    Returns the lag of the first peak whose correlation is at least 0.8 of the
    zero-lag one, in seconds, or ``None`` when the envelope does not repeat.
    """
    window = levels[first:last]
    if len(window) < 8:
        return None
    mean = sum(window) / len(window)
    window = [value - mean for value in window]
    zero = sum(value * value for value in window)
    if zero <= 0:
        return None
    best = None
    previous = 0.0
    rising = False
    for lag in range(1, len(window) // 2):
        total = sum(window[i] * window[i + lag] for i in range(len(window) - lag))
        score = total / zero
        if score > previous:
            rising = True
        elif rising and previous >= 0.8:
            best = (lag - 1) * seconds
            break
        else:
            rising = False
        previous = score
    return best


def run_board(seconds: float, machine: str) -> tuple[list[float], float]:
    """The board's own FM output at the chip's native rate, mono."""
    system = System(MACHINES[machine], Cartridge(ROM.read_bytes()))
    rate = system.machine.mclk / 1008
    frames = int(seconds * system.machine.mclk / (3420 * system.machine.lines_per_frame))
    mono: list[float] = []
    for _ in range(frames):
        system.run_frame()
        for left, right in system.board.fm.take_samples():
            mono.append((left + right) / 2.0)
        system.board.psg.take_samples()
    return mono, rate


def read_wav(path: Path) -> tuple[list[float], float]:
    with wave.open(str(path)) as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    if width != 2:
        raise SystemExit(f"{path}: expected 16-bit samples, got {width * 8}-bit")
    values = array("h")
    values.frombytes(raw)
    if sys.byteorder == "big":
        values.byteswap()
    if channels == 1:
        return list(values), float(rate)
    return [(values[i] + values[i + 1]) / 2.0 for i in range(0, len(values) - 1, channels)], float(
        rate
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--wav", type=Path, required=True, help="the unpacked MD1600 recording")
    parser.add_argument("--machine", default="genesis")
    parser.add_argument("--seconds", type=float, default=0.0, help="default: the recording's")
    args = parser.parse_args(argv)

    recorded, recorded_rate = read_wav(args.wav)
    seconds = args.seconds or len(recorded) / recorded_rate
    print(f"MD1600 recording: {len(recorded):,} samples at {recorded_rate:.0f} Hz, {seconds:.1f} s")
    board, board_rate = run_board(seconds, args.machine)
    print(f"board:            {len(board):,} samples at {board_rate:.0f} Hz")

    hardware, hardware_block = envelope(recorded, recorded_rate)
    ours, our_block = envelope(board, board_rate)
    hardware_notes = notes(hardware, hardware_block)
    our_notes = notes(ours, our_block)
    print(f"\nnotes: MD1600 {len(hardware_notes)}, board {len(our_notes)}")

    offset_h = hardware_notes[0][0] if hardware_notes else 0.0
    offset_b = our_notes[0][0] if our_notes else 0.0
    print(
        f"first note at {offset_h:.2f} s on the MD1600 and {offset_b:.2f} s on the board; "
        "every time below is relative to it\n"
    )
    print(
        f"{'note':>4} | {'MD1600':>32} | {'board':>32}\n"
        f"{'':>4} | {'start s':>9} {'length s':>10} {'period':>11} "
        f"| {'start s':>9} {'length s':>10} {'period':>11}"
    )

    def cells(table, levels, block, offset, index):
        if index >= len(table):
            return f"{'-':>9} {'-':>10} {'-':>11}"
        start, length, first, last = table[index]
        period = repetition(levels, first, last, block)
        shown = f"{period * 1000:.0f} ms" if period else "none"
        return f"{start - offset:9.2f} {length:10.2f} {shown:>11}"

    for index in range(max(len(hardware_notes), len(our_notes))):
        left = cells(hardware_notes, hardware, hardware_block, offset_h, index)
        right = cells(our_notes, ours, our_block, offset_b, index)
        print(f"{index:>4} | {left} | {right}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
