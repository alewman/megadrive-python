#!/usr/bin/env python
"""The MDFourier signal against the hardware recordings, as a detector.

    python scripts/run_mdfourier.py --recording <unpacked>/A-MD1UTVA3-M192.flac

MDFourier's own analyser (``reference/src/MDFourier``) needs FFTW and libFLAC, which
are not installed here, so this script makes the one measurement the board's analogue
model actually has parameters for, and no more (docs: ``src/megadrive_python/audio.py``):

* the **level of each of the signal's 172 tone blocks**, which is a frequency
  response across the FM's range and the PSG's;
* the **PSG's level relative to the FM's**, which is the one mixing number
  :data:`megadrive_python.audio.PSG_GAIN` sets.

The signal comes from the 240p Test Suite, driven here from its menus: down, down, A
(Audio tests), down, down, A (MDFourier), A (start).  Its block structure is
MDFourier's own Genesis profile
(``reference/src/MDFourier/profiles/mdfblocksGEN.mfn``): a 20-frame sync, 5 frames of
decay, 15 of silence, then 96 FM blocks, 40 PSG blocks, 20 PSG ramp blocks and 16
noise blocks of 20 frames each, then decay, silence and a closing 20-frame sync.
Both signals are aligned on those two syncs and mapped linearly between them, so a
recording running at a slightly different rate than nominal does not drift.

This is a **detector**: the recordings are hardware-captured and could judge, but
what they would judge is a filter and a mixing ratio no pinned document gives a
number for, so what is printed is the measurement and the difference, and
docs/worklog.md records which way the board's parameters were set by it.  Nothing
here is a pass or a fail.
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
import wave
import zipfile
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from megadrive_python.cartridge import Cartridge
from megadrive_python.io import BUTTONS
from megadrive_python.machine import MACHINES
from megadrive_python.system import System
from megadrive_python.wav import write_wav

SUITE = Path(
    "/data/emu/roms/megadrive/240p Test Suite (World) (v1.30) (Program) (Aftermarket) (Unl).zip"
)

#: MDFourier's Genesis profile, in order: (name, blocks, frames each).
BLOCKS = [
    ("sync", 1, 20),
    ("decay", 1, 5),
    ("silence", 1, 15),
    ("FM", 96, 20),
    ("PSG", 40, 20),
    ("PSG ramp", 20, 20),
    ("noise", 16, 20),
    ("decay", 1, 5),
    ("silence", 1, 15),
    ("sync", 1, 20),
]

#: Frames from the "press A" to the first sync, and the buttons that get there.
NAVIGATION = [(120, "down"), (140, "down"), (160, "a"), (200, "down"), (220, "down"),
              (240, "a"), (300, "a")]  # fmt: skip
HOLD_FRAMES = 8
SIGNAL_FRAMES = sum(count * frames for _name, count, frames in BLOCKS)


def run_board(rate: int, machine: str, wav: Path | None) -> tuple[array, int]:
    with zipfile.ZipFile(SUITE) as archive:
        name = next(n for n in archive.namelist() if n.lower().endswith((".md", ".bin", ".gen")))
        image = archive.read(name)
    system = System(MACHINES[machine], Cartridge(image))
    system.audio.sample_rate = rate
    pad = system.board.io.ports[0].device
    # Kept as one flat array of mono values: 66 emulated seconds at 48 kHz is six
    # million samples, and a list of pairs of that length costs hundreds of megabytes.
    mono = array("f")
    stereo = array("h") if wav else None
    for frame in range(320 + SIGNAL_FRAMES + 120):
        held = 0
        for at, button in NAVIGATION:
            if at <= frame < at + HOLD_FRAMES:
                held |= BUTTONS[button]
        pad.pressed = held
        system.run_frame()
        for left, right in system.take_audio():
            mono.append((left + right) / 2.0)
            if stereo is not None:
                stereo.extend((left, right))
    if wav and stereo is not None:
        write_wav(wav, [(stereo[i], stereo[i + 1]) for i in range(0, len(stereo), 2)], rate)
    return mono, rate


def decode(path: Path, rate: int, tmp: Path) -> list[float]:
    """Any audio file to mono at ``rate``, through ffmpeg (the recordings are FLAC and
    neither libFLAC nor a pure-Python decoder is available here)."""
    if path.suffix.lower() != ".wav":
        out = tmp / (path.stem + ".wav")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
             "-ac", "1", "-ar", str(rate), "-sample_fmt", "s16", str(out)],
            check=True,
        )  # fmt: skip
        path = out
    with wave.open(str(path)) as handle:
        raw = handle.readframes(handle.getnframes())
        channels, width = handle.getnchannels(), handle.getsampwidth()
        if handle.getframerate() != rate or width != 2:
            raise SystemExit(f"{path}: expected mono 16-bit at {rate} Hz")
    values = array("h")
    values.frombytes(raw)
    if sys.byteorder == "big":
        values.byteswap()
    if channels == 1:
        return list(values)
    return [values[i] for i in range(0, len(values), channels)]


def _ac_rms(samples, start: int, stop: int) -> float:
    """RMS about the window's own mean.

    Neither signal sits at zero: the recordings carry their capture chain's offset,
    and the board's discrete YM2612 drives a standing level even in silence (the
    ladder's +1 sign in every gap between channels).  Removing the mean is what makes
    a silence read as silence on both.
    """
    total = 0.0
    squares = 0.0
    for index in range(start, stop):
        value = samples[index]
        total += value
        squares += value * value
    count = stop - start
    mean = total / count
    return math.sqrt(max(0.0, squares / count - mean * mean))


def loud_regions(samples, rate: int, window: float = 0.05) -> list[tuple[int, int]]:
    block = int(rate * window)
    levels = [
        _ac_rms(samples, start, start + block) for start in range(0, len(samples) - block, block)
    ]
    peak = max(levels) if levels else 0.0
    limit = peak * 0.05
    regions = []
    index = 0
    while index < len(levels):
        if levels[index] > limit:
            end = index
            while end < len(levels) and levels[end] > limit:
                end += 1
            if end - index >= 3:
                regions.append((index * block, end * block))
            index = end
        else:
            index += 1
    return regions


def block_levels(samples, first: int, last: int) -> list[tuple[str, float]]:
    """The RMS of each tone block, with the signal's two syncs at ``first`` and
    ``last`` (in samples)."""
    span = last - first
    total_frames = SIGNAL_FRAMES
    out: list[tuple[str, float]] = []
    frame = 0
    for name, count, frames in BLOCKS:
        for _ in range(count):
            start = first + int(span * frame / total_frames)
            stop = first + int(span * (frame + frames) / total_frames)
            # Trim a frame at each end so a block's attack and release do not count.
            trim = (stop - start) // frames
            start, stop = start + trim, stop - trim
            if stop > start and name not in ("decay", "silence", "sync"):
                out.append((name, _ac_rms(samples, start, stop)))
            frame += frames
    return out


def response(hardware: list[tuple[str, float]], board: list[tuple[str, float]], group: str) -> None:
    """The board's level minus the hardware's, block by block through one group,
    each normalised by its own group mean.

    The FM group's 96 blocks climb through the chip's range, so this is a frequency
    response: a low-pass that is too low shows as a fall to the right.
    """
    ours = [level for name, level in board if name == group]
    theirs = [level for name, level in hardware if name == group]
    if len(ours) != len(theirs) or not ours:
        print(f"  {group}: {len(theirs)} hardware blocks against {len(ours)}; not compared")
        return
    ours_mean = sum(ours) / len(ours)
    theirs_mean = sum(theirs) / len(theirs)
    points = [round(i * (len(ours) - 1) / 11) for i in range(12)]

    def difference(index: int) -> float:
        mine = max(ours[index] / ours_mean, 1e-9)
        theirs_here = max(theirs[index] / theirs_mean, 1e-9)
        return 20 * math.log10(mine / theirs_here)

    shown = " ".join(f"{difference(index):6.1f}" for index in points)
    print(f"  {group} blocks {points[0]}..{points[-1]}, board minus hardware: {shown}")


def summarise(label: str, levels: list[tuple[str, float]]) -> dict[str, float]:
    groups: dict[str, list[float]] = {}
    for name, level in levels:
        groups.setdefault(name, []).append(level)
    reference = sum(groups.get("FM", [1.0])) / max(1, len(groups.get("FM", [1.0])))
    print(f"  {label}")
    summary = {}
    for name, values in groups.items():
        mean = sum(values) / len(values)
        summary[name] = mean / reference
        print(
            f"    {name:9s} {len(values):3d} blocks  mean {mean:10.1f}  "
            f"relative to FM {20 * math.log10(max(mean, 1e-9) / max(reference, 1e-9)):+7.2f} dB"
        )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--machine", default="genesis")
    parser.add_argument("--rate", type=int, default=48_000)
    parser.add_argument("--tmp", type=Path, default=Path("/tmp"))
    parser.add_argument("--wav", type=Path, help="keep the board's own signal here")
    args = parser.parse_args(argv)
    args.tmp.mkdir(parents=True, exist_ok=True)

    recorded = decode(args.recording, args.rate, args.tmp)
    board, _ = run_board(args.rate, args.machine, args.wav)
    print(f"recording {args.recording.name}: {len(recorded) / args.rate:.1f} s")
    print(f"board:                           {len(board) / args.rate:.1f} s")

    results = {}
    blocks = {}
    for label, samples in (("hardware", recorded), ("board", board)):
        regions = loud_regions(samples, args.rate)
        if len(regions) < 2:
            print(f"{label}: found {len(regions)} loud regions; cannot align")
            return 1
        first, last = regions[0][0], regions[-1][0]
        print(
            f"{label}: {len(regions)} loud regions, syncs at "
            f"{first / args.rate:.2f} s and {last / args.rate:.2f} s "
            f"({(last - first) / args.rate:.2f} s apart; the signal is "
            f"{SIGNAL_FRAMES / 59.92:.2f} s at 59.92 Hz)"
        )
        blocks[label] = block_levels(samples, first, last)
        results[label] = summarise(label, blocks[label])

    print("\n  board minus hardware, per group (dB relative to each one's own FM mean):")
    for name in results["hardware"]:
        if name in results["board"]:
            difference = 20 * math.log10(
                max(results["board"][name], 1e-9) / max(results["hardware"][name], 1e-9)
            )
            print(f"    {name:9s} {difference:+7.2f} dB")
    print("\n  the shape of each group, which is where the low-pass shows:")
    for group in ("FM", "PSG", "noise"):
        response(blocks["hardware"], blocks["board"], group)
    return 0


if __name__ == "__main__":
    sys.exit(main())
