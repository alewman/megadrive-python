#!/usr/bin/env python
"""Milestone 4's PSG acceptance: the board's PSG against Nuked-PSG.

    python scripts/run_psg_compare.py            # the synthetic streams
    python scripts/run_psg_compare.py --games    # and five cartridges

Nuked-PSG (``reference/src/Nuked-PSG``, die-derived, GPL-2.0, pinned at d15a168) is
the judge here: no hardware capture of a Mega Drive PSG is pinned, and the tier rule
makes a die-derived model the judge where nothing hardware covers the question
(docs/validation.md, "The tier rule").

What is compared is the chip's **digital** output: the four attenuations its four
channels are driving, at **every input clock** (MCLK / 15, 3.58 MHz), not only at the
one clock in sixteen a sample is taken on.  That is the whole state a listener could
hear, sampled 16 times more finely than the chip's own output rate.  Nuked's analogue
level is not compared, because the board uses Nuked's own measured attenuation curve
(:data:`megadrive_python.psg.VOLUME_LEVELS`) and comparing it would be circular; the
curve is checked in the unit tests instead.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import audio_refs

from megadrive_python.psg import PSG

CARTS = Path("/data/emu/roms/megadrive")

GAMES = [
    ("Sonic The Hedgehog (USA, Europe).zip", 120, 400),
    ("Streets of Rage 2 (USA).zip", 200, 400),
    ("Gunstar Heroes (USA).zip", 200, 400),
    ("Castlevania - Bloodlines (USA).zip", 200, 400),
    ("Phantasy Star IV (USA).zip", 200, 400),
]


def stream_random(seed: int, count: int) -> list[tuple[int, int]]:
    """Latch and data bytes at random, over all eight registers."""
    rng = random.Random(seed)
    events: list[tuple[int, int]] = []
    clock = 40
    for _ in range(count):
        events.append((clock, 0x80 | (rng.randrange(8) << 4) | rng.randrange(16)))
        clock += rng.randrange(20, 400)
        if rng.random() < 0.6:
            events.append((clock, rng.randrange(0x40)))
            clock += rng.randrange(20, 400)
    return events


def stream_tones() -> list[tuple[int, int]]:
    """Every tone period from 0 to 1023 in steps, at full volume: the periods 0 and 1
    behave differently from the rest (sms-python's note from Maxim's wiki), and the
    counter's compare-then-reload makes a rewrite mid-period visible."""
    events: list[tuple[int, int]] = []
    clock = 40
    for channel in range(3):
        events.append((clock, 0x90 | (channel << 5)))
        clock += 32
    for period in list(range(0, 8)) + list(range(8, 1024, 37)):
        for channel in range(3):
            events.append((clock, 0x80 | (channel << 5) | (period & 0x0F)))
            clock += 32
            events.append((clock, (period >> 4) & 0x3F))
            clock += 32
        clock += 4000
    return events


def stream_noise() -> list[tuple[int, int]]:
    """All eight noise settings, white and periodic, each with tone 3 as its clock at
    a few periods, and the LFSR reset a write brings."""
    events: list[tuple[int, int]] = []
    clock = 40
    events.append((clock, 0xF0))  # noise at full volume
    clock += 32
    for period in (0x010, 0x080, 0x200):
        events.append((clock, 0xC0 | (period & 0x0F)))
        clock += 32
        events.append((clock, (period >> 4) & 0x3F))
        clock += 32
        for setting in range(8):
            events.append((clock, 0xE0 | setting))
            clock += 20000
    return events


def stream_volumes() -> list[tuple[int, int]]:
    """Every attenuation on every channel, with a tone running."""
    events: list[tuple[int, int]] = []
    clock = 40
    for channel in range(3):
        events.append((clock, 0x80 | (channel << 5) | 0x0A))
        clock += 32
        events.append((clock, 0x08 + channel))
        clock += 32
    events.append((clock, 0xE4))
    clock += 32
    for level in range(16):
        for channel in range(4):
            events.append((clock, 0x90 | (channel << 5) | level))
            clock += 600
        clock += 3000
    return events


def load_image(path: Path) -> bytes:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if Path(name).suffix.lower() in (".md", ".gen", ".bin", ".smd"):
                    return archive.read(name)
    return path.read_bytes()


def compare(name: str, events: list[tuple[int, int]], clocks: int, tmp: Path) -> bool:
    started = time.perf_counter()
    reference, _levels = audio_refs.run_psg(events, clocks, tmp)
    psg = PSG()
    index = 0
    count = len(events)
    differing = 0
    first = None
    for clock in range(clocks):
        while index < count and events[index][0] <= clock:
            psg.write(events[index][1])
            index += 1
        psg.run(1)
        mine = psg.attenuations()
        if mine != reference[clock]:
            differing += 1
            if first is None:
                first = (clock, reference[clock], mine)
    wall = time.perf_counter() - started
    if differing == 0:
        print(f"  {clocks:>12,} input clocks  IDENTICAL (four attenuations)  {wall:.1f} s")
        return True
    print(
        f"  {differing:,} of {clocks:,} input clocks differ, first at {first[0]:,} "
        f"(referee {first[1]}, board {first[2]})"
    )
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--games", action="store_true")
    parser.add_argument("--tmp", type=Path, default=Path("/tmp"))
    parser.add_argument("--tail", type=int, default=16 * 2000)
    args = parser.parse_args(argv)
    args.tmp.mkdir(parents=True, exist_ok=True)

    streams = [
        ("random (seed 1, 3000 latches)", stream_random(1, 3000)),
        ("random (seed 2, 3000 latches)", stream_random(2, 3000)),
        ("every tone period", stream_tones()),
        ("every noise setting", stream_noise()),
        ("every attenuation", stream_volumes()),
    ]
    if args.games:
        from megadrive_python.io import BUTTONS

        for name, press_at, frames in GAMES:
            path = CARTS / name
            if not path.exists():
                print(f"{name}: missing")
                continue
            image = load_image(path)
            digest = hashlib.sha256(image).hexdigest()[:16]
            _fm, writes, _cycles, _clock = audio_refs.capture_streams(
                image, frames, press=BUTTONS["start"], press_at=press_at
            )
            streams.append((f"{name} ({digest}, {frames} frames, {len(writes):,} writes)", writes))

    failures = 0
    for name, events in streams:
        clocks = (events[-1][0] if events else 0) + args.tail
        print(f"{name}: {len(events):,} writes, {clocks:,} input clocks")
        if not compare(name, events, clocks, args.tmp):
            failures += 1
    print(f"\n{len(streams)} streams, {failures} not identical")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
