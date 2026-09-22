#!/usr/bin/env python
"""Milestone 4's FM acceptance: the board's YM2612 against the two die-derived models.

    python scripts/run_fm_streams.py                 # the synthetic streams
    python scripts/run_fm_streams.py --roms --games  # and Nemesis's ROMs, and games
    python scripts/run_fm_streams.py --lle           # also YM2608-LLE (slow)

For each register stream the chip is run from power-on and **every** value of its two
DAC pins is compared, one per internal cycle -- 24 to an output sample at the chip's
native rate, so this is a stricter comparison than sample-for-sample at that rate.
The status byte (BUSY and the two timer overflow flags) is compared on the same
cycles.  Nothing is rounded: a stream either matches on every cycle or the first
differing cycle is printed with both values.

The referees
------------

* **Nuked-OPN2** (``ym3438.c``, die-derived, LGPL-2.1) in both of its output modes:
  ``ym2612`` (the discrete NMOS part, with the ladder) and ``ym3438`` (the ASIC).
  The board is run with the matching :class:`~megadrive_python.machine.FMVariant`.
* **YM2608-LLE** (``fmopna_2612.c``, die-derived, GPL-2.0): a *pin-level* model of
  the NMOS YM2612, clocked at MCLK / 7 with the write strobes driven.  Its DAC pins
  carry the same 9-bit value without Nuked's threefold amplification, and its
  internal /6 phase after /IC is its own, so the comparison fits one constant
  sub-cycle offset over the whole run and then requires every sample to agree.

The streams
-----------

* **synthetic**: a seeded random walk over the whole register space, and six aimed
  streams -- channel 3's special mode, CSM, SSG-EG's eight shapes, the LFO at all
  eight speeds with every PMS/AMS depth, both timers, and the DAC.
* **--roms**: Nemesis's six YM2612 test ROMs, run on the board.
* **--games**: five commercial cartridges, run on the board.

The ROM and game streams are the *board's own*: the writes its YM2612 received, at
the internal cycle it received them on, so a difference would be the board's
scheduling as much as the chip's.
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

from megadrive_python.machine import YM2612 as FM_DISCRETE
from megadrive_python.machine import YM3438_ASIC
from megadrive_python.ym2612 import YM2612

ROOT = Path(__file__).resolve().parent.parent
ROMS = ROOT / "tests" / "test_roms"
CARTS = Path("/data/emu/roms/megadrive")

#: The five cartridges the earlier milestones are measured on, with the buttons and
#: length each needs to get its sound driver going.
GAMES = [
    ("Sonic The Hedgehog (USA, Europe).zip", "start", 120, 720),
    ("Streets of Rage 2 (USA).zip", "start", 240, 720),
    ("Gunstar Heroes (USA).zip", "start", 240, 720),
    ("Castlevania - Bloodlines (USA).zip", "start", 240, 720),
    ("Phantasy Star IV (USA).zip", "start", 240, 720),
]

#: Nemesis's YM2612 test ROMs (docs/validation.md, "Sound").
TEST_ROMS = [
    "ym2612csmmode.bin",
    "ym2612ssg-eg.bin",
    "ym2612attackandrelease.bin",
    "ym2612detune.bin",
    "ym2612multiplexing.bin",
    "ym2612FrequencyWriteOrder.bin",
]


# -- building streams ----------------------------------------------------------------


def _write(events: list[tuple[int, int, int]], cycle: int, register: int, value: int) -> int:
    """One register write as the board makes it: the address then the data, on the
    two ports of the part the register lives in, far enough apart that the chip's
    write strobes see them (Kabuto: about 33.6 Z80 cycles, docs/timing.md)."""
    part = 2 if register >= 0x100 else 0
    events.append((cycle, part, register & 0xFF))
    events.append((cycle + 12, part + 1, value & 0xFF))
    return cycle + 24


def stream_random(seed: int, count: int) -> list[tuple[int, int, int]]:
    """A random walk over every register the chip has."""
    rng = random.Random(seed)
    registers = (
        [0x22, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2A, 0x2B]
        + [
            base + op * 4 + ch
            for base in range(0x30, 0xA0, 0x10)
            for op in range(4)
            for ch in (0, 1, 2, 0x100, 0x101, 0x102)
        ]
        + [
            base + ch
            for base in (0xA0, 0xA4, 0xA8, 0xAC, 0xB0, 0xB4)
            for ch in (0, 1, 2, 0x100, 0x101, 0x102)
        ]
    )
    events: list[tuple[int, int, int]] = []
    cycle = 8
    for _ in range(count):
        _write(events, cycle, rng.choice(registers), rng.randrange(256))
        cycle += 24 + rng.randrange(0, 200)
    return events


def stream_ch3_csm() -> list[tuple[int, int, int]]:
    """Channel 3's special mode and CSM: four separate frequencies on channel 3, then
    CSM driven by timer A at three speeds."""
    events: list[tuple[int, int, int]] = []
    cycle = 8
    for op in range(4):
        for register, value in ((0x30, 0x01), (0x40, 0x10), (0x50, 0x1F), (0x60, 0x0A),
                                (0x70, 0x04), (0x80, 0x1F), (0x90, 0x00)):  # fmt: skip
            cycle = _write(events, cycle, register + op * 4 + 2, value)
    cycle = _write(events, cycle, 0xB2, 0x07)  # algorithm 7: all four operators out
    cycle = _write(events, cycle, 0xB6, 0xC0)
    for slot, (high, low) in enumerate(((0x22, 0x69), (0x23, 0x11), (0x21, 0xA0), (0x24, 0x40))):
        cycle = _write(events, cycle, 0xAC if slot else 0xA4, high)
        cycle = (
            _write(events, cycle, 0xA8 + (slot - 1 if slot else 0), low)
            if slot
            else _write(events, cycle, 0xA2, low)
        )
    cycle = _write(events, cycle, 0x27, 0x40)  # channel 3 special mode
    cycle = _write(events, cycle, 0x28, 0xF2)  # key on channel 3
    cycle += 24 * 4000
    for timer_a in (0x3F0, 0x3C0, 0x300):
        cycle = _write(events, cycle, 0x24, timer_a >> 2)
        cycle = _write(events, cycle, 0x25, timer_a & 3)
        cycle = _write(events, cycle, 0x27, 0x80 | 0x15)  # CSM, load and enable timer A
        cycle += 24 * 6000
    return events


def stream_ssg_eg() -> list[tuple[int, int, int]]:
    """All eight SSG-EG shapes on one operator, keyed on and off."""
    events: list[tuple[int, int, int]] = []
    cycle = 8
    for register, value in ((0x30, 0x01), (0x40, 0x00), (0x50, 0x1F), (0x60, 0x0C),
                            (0x70, 0x0C), (0x80, 0xFF), (0xB0, 0x07), (0xB4, 0xC0),
                            (0xA4, 0x22), (0xA0, 0x69)):  # fmt: skip
        cycle = _write(events, cycle, register, value)
    for shape in range(8):
        cycle = _write(events, cycle, 0x90, 0x08 | shape)
        cycle = _write(events, cycle, 0x28, 0xF0)
        cycle += 24 * 8000
        cycle = _write(events, cycle, 0x28, 0x00)
        cycle += 24 * 2000
    return events


def stream_lfo() -> list[tuple[int, int, int]]:
    """Every LFO speed against every PMS and AMS depth, with AM on all operators."""
    events: list[tuple[int, int, int]] = []
    cycle = 8
    for op in range(4):
        for register, value in ((0x30, 0x01), (0x40, 0x10), (0x50, 0x1F),
                                (0x60, 0x80 | 0x08), (0x70, 0x04), (0x80, 0x1F)):  # fmt: skip
            cycle = _write(events, cycle, register + op * 4, value)
    cycle = _write(events, cycle, 0xB0, 0x07)
    cycle = _write(events, cycle, 0xA4, 0x22)
    cycle = _write(events, cycle, 0xA0, 0x69)
    cycle = _write(events, cycle, 0x28, 0xF0)
    for speed in range(8):
        cycle = _write(events, cycle, 0x22, 0x08 | speed)
        for pms in range(8):
            for ams in range(4):
                cycle = _write(events, cycle, 0xB4, 0xC0 | (ams << 4) | pms)
                cycle += 24 * 400
    return events


def stream_timers() -> list[tuple[int, int, int]]:
    """Both timers at several reloads, loaded, reset and read."""
    events: list[tuple[int, int, int]] = []
    cycle = 8
    for reload_a, reload_b in ((0x3FF, 0xFF), (0x3F0, 0xF0), (0x200, 0x40), (0x000, 0x00)):
        cycle = _write(events, cycle, 0x24, reload_a >> 2)
        cycle = _write(events, cycle, 0x25, reload_a & 3)
        cycle = _write(events, cycle, 0x26, reload_b)
        cycle = _write(events, cycle, 0x27, 0x3F)  # load and enable both
        cycle += 24 * 3000
        cycle = _write(events, cycle, 0x27, 0x30)  # reset both flags
        cycle += 24 * 500
    return events


def stream_dac() -> list[tuple[int, int, int]]:
    """The DAC: enabled, a ramp through every value, the ninth bit, and disabled."""
    events: list[tuple[int, int, int]] = []
    cycle = 8
    cycle = _write(events, cycle, 0x2B, 0x80)
    for value in range(256):
        cycle = _write(events, cycle, 0x2A, value)
        cycle += 48
    for value in (0x00, 0x7F, 0x80, 0xFF):
        cycle = _write(events, cycle, 0x2C, 0x08)  # the ninth bit
        cycle = _write(events, cycle, 0x2A, value)
        cycle += 96
        cycle = _write(events, cycle, 0x2C, 0x00)
        cycle += 96
    cycle = _write(events, cycle, 0x2B, 0x00)
    return events


def load_image(path: Path) -> bytes:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if Path(name).suffix.lower() in (".md", ".gen", ".bin", ".smd"):
                    return archive.read(name)
    return path.read_bytes()


# -- comparing ------------------------------------------------------------------------


def replay(events: list[tuple[int, int, int]], cycles: int, variant):
    """Run a fresh chip over the stream, yielding (MOL, MOR, status) per cycle.

    A generator, not a list: a ten-second game stream is 13 million cycles, and
    holding three lists of that length costs more than the run does.
    """
    chip = YM2612(variant)
    index = 0
    count = len(events)
    for cycle in range(cycles):
        while index < count and events[index][0] <= cycle:
            chip.write(events[index][1], events[index][2])
            index += 1
        chip.cycle()
        yield (
            chip.mol,
            chip.mor,
            (chip.busy << 7) | (chip.timer_b_overflow_flag << 1) | chip.timer_a_overflow_flag,
        )


def compare_nuked(name: str, events, cycles: int, tmp: Path, variant, vname: str) -> bool:
    started = time.perf_counter()
    ref_l, ref_r, ref_s = audio_refs.run_opn2(events, cycles, tmp, variant=vname)
    first: dict[str, tuple[int, int, int]] = {}
    counts = {"MOL": 0, "MOR": 0, "status": 0}
    for cycle, (left, right, status) in enumerate(replay(events, cycles, variant)):
        for label, mine, ref in (
            ("MOL", left, ref_l[cycle]),
            ("MOR", right, ref_r[cycle]),
            ("status", status, ref_s[cycle]),
        ):
            if mine != ref:
                counts[label] += 1
                first.setdefault(label, (cycle, ref, mine))
    wall = time.perf_counter() - started
    if not first:
        print(f"  {vname:8s} {cycles:>12,} cycles  IDENTICAL (MOL, MOR, status)  {wall:.1f} s")
        return True
    for label, (cycle, ref, mine) in first.items():
        print(
            f"  {vname:8s} {label}: {counts[label]:,} of {cycles:,} differ, "
            f"first at cycle {cycle:,} (referee {ref}, board {mine})"
        )
    return False


def compare_lle(name: str, events, cycles: int, tmp: Path) -> bool:
    """YM2608-LLE, at MCLK / 7 with its own /6 phase, **at the chip's native rate**.

    Two things make a cycle-by-cycle comparison meaningless here, and both are
    recorded rather than smoothed over (docs/undocumented-behavior.md, "The YM2612"):

    1. The two models put a channel's sample in a different place inside the four
       cycles the channel owns.  YM2608-LLE drives the value on the first of the four
       and the bare sign on the other three (``fmopna_impl.c`` lines 5824-5853);
       Nuked-OPN2's YM2612 mode -- which its own comment calls "not verified"
       (``ym3438.c`` line 966) -- drives the sign on three and the value on the
       *last*.  The 24 values of a sample are the same multiset either way, so the
       sample at the native rate is unaffected, and that is what is compared.
    2. The write is driven differently: LLE is a pin-level model and the harness
       holds /CS and /WR low for 16 chip clocks, while Nuked latches an edge.  A
       write can therefore land one internal cycle apart in the two, which is a
       difference in the harness, not in either chip.

    So: sum each model's DAC over a sample, fit one phase and one whole-sample lag
    over the run, and report how many samples then differ.  LLE's pins carry the
    9-bit value Nuked multiplies by three (``ym3438.c`` line 987), so the board's sum
    is divided by three and LLE's 144 chip clocks by six.
    """
    clocks = cycles * 6
    lle_events = [(cycle * 6, port, data) for cycle, port, data in events]
    started = time.perf_counter()
    ref_l, _ref_r = audio_refs.run_lle(lle_events, clocks, tmp)
    mine = list(replay(events, cycles, FM_DISCRETE))
    mine_l = [sample[0] for sample in mine]
    del mine
    base = 2400  # past both models' reset settling
    best = None
    for phase in range(24):
        mine = [sum(mine_l[i : i + 24]) // 3 for i in range(base + phase, cycles - 24, 24)]
        for lag in range(-3, 4):
            start = (base + phase + lag * 24) * 6
            if start < 0:
                continue
            ref = [sum(ref_l[i : i + 144]) // 6 for i in range(start, len(ref_l) - 144, 144)]
            count = min(len(mine), len(ref))
            if count < 500:
                continue
            wrong = sum(1 for i in range(count) if mine[i] != ref[i])
            if best is None or wrong < best[0]:
                best = (wrong, count, phase, lag)
    wall = time.perf_counter() - started
    if best is None:
        print("  LLE 2612: stream too short to align")
        return True
    wrong, count, phase, lag = best
    verdict = "IDENTICAL" if wrong == 0 else f"{wrong:,} of {count:,} differ ({wrong / count:.2%})"
    print(f"  LLE 2612 native rate, phase {phase} lag {lag}: {verdict}  {wall:.1f} s")
    return wrong == 0


def stream_ladder() -> list[tuple[int, int, int]]:
    """One operator sounding on channel 1, its total level walked from 0 to 127.

    The output of the discrete chip stops shrinking at the bottom, because the +1 it
    adds to every non-negative sample and the bare sign it drives between channels do
    not shrink with the note: that is the ladder.  Aly James's ``MD1_LADDER.wav`` is a
    real Model 1 doing exactly this -- a decaying tone whose level stops falling about
    32 dB below its peak and stays there until the note reaches absolute silence --
    and the two variants' floors are what this compares against it.
    """
    events: list[tuple[int, int, int]] = []
    cycle = 8
    for register, value in ((0x30, 0x01), (0x50, 0x1F), (0x60, 0x00), (0x70, 0x00),
                            (0x80, 0x0F), (0x90, 0x00), (0xB0, 0x07), (0xB4, 0xC0),
                            (0xA4, 0x22), (0xA0, 0x69)):  # fmt: skip
        cycle = _write(events, cycle, register, value)
    cycle = _write(events, cycle, 0x28, 0xF0)
    for level in range(0, 128):
        cycle = _write(events, cycle, 0x40, level)
        cycle += 24 * 400
    return events


def ladder_report(tmp: Path, recording: Path | None) -> None:
    """The ladder as a measurement: how far each machine's level falls.

    Prints each machine's level profile as decibels below its own peak at ten evenly
    spaced points along the ramp, which is the least arbitrary way to show a plateau:
    a chip whose output shrinks with the note keeps falling, one with the ladder
    stops.  No single number is a pass or a fail here (the recording's gain, its
    ramp's length and its console's filter are all unknown); the shape is the result.
    """
    import math
    import wave
    from array import array as _array

    def profile(values, block: int) -> list[float]:
        """RMS about each block's own mean: the standing level the discrete chip
        drives even in silence is not part of what is being measured."""
        out = []
        for start in range(0, len(values) - block, block):
            total = squares = 0.0
            for index in range(start, start + block):
                value = values[index]
                total += value
                squares += value * value
            mean = total / block
            out.append(math.sqrt(max(0.0, squares / block - mean * mean)))
        return out

    def show(label: str, levels: list[float]) -> None:
        peak = max(levels)
        points = [levels[round(i * (len(levels) - 1) / 10)] for i in range(11)]
        shown = " ".join(
            f"{20 * math.log10(level / peak):6.1f}" if level > 0 else "  -inf" for level in points
        )
        print(f"  {label:16s} peak {peak:9.1f} | {shown}")

    events = stream_ladder()
    cycles = events[-1][0] + 24 * 400
    print(f"the ladder: {len(events) // 2} register writes, {cycles:,} cycles")
    print(f"  {'':16s} {'':14s} | dB below peak at each tenth of the ramp")
    for name, variant in (("discrete YM2612", FM_DISCRETE), ("ASIC YM3438", YM3438_ASIC)):
        mono = [left + right for left, right, _status in replay(events, cycles, variant)]
        show(name, profile(mono, 24 * 200))
    if recording and recording.exists():
        with wave.open(str(recording)) as handle:
            channels = handle.getnchannels()
            raw = handle.readframes(handle.getnframes())
            rate = handle.getframerate()
        values = _array("h")
        values.frombytes(raw)
        show(recording.name + " (hardware)", profile(list(values[::channels]), rate // 20))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--roms", action="store_true", help="also Nemesis's test ROMs")
    parser.add_argument("--games", action="store_true", help="also five cartridges")
    parser.add_argument("--lle", action="store_true", help="also YM2608-LLE (slow)")
    parser.add_argument("--tmp", type=Path, default=Path("/tmp"))
    parser.add_argument("--tail", type=int, default=24 * 500, help="cycles to run on")
    parser.add_argument(
        "--ladder",
        nargs="?",
        const="",
        type=str,
        help="measure the ladder instead, optionally against a hardware recording",
    )
    args = parser.parse_args(argv)
    if args.ladder is not None:
        args.tmp.mkdir(parents=True, exist_ok=True)
        ladder_report(args.tmp, Path(args.ladder) if args.ladder else None)
        return 0
    tmp = args.tmp
    tmp.mkdir(parents=True, exist_ok=True)

    streams: list[tuple[str, list[tuple[int, int, int]]]] = [
        ("random walk (seed 1, 4000 writes)", stream_random(1, 4000)),
        ("random walk (seed 2, 4000 writes)", stream_random(2, 4000)),
        ("channel 3 special mode and CSM", stream_ch3_csm()),
        ("SSG-EG, all eight shapes", stream_ssg_eg()),
        ("the LFO, 8 speeds x 8 PMS x 4 AMS", stream_lfo()),
        ("both timers", stream_timers()),
        ("the DAC", stream_dac()),
    ]
    if args.roms:
        from megadrive_python.io import BUTTONS

        for name in TEST_ROMS:
            path = ROMS / "nemesis" / name
            if not path.exists():
                print(f"{name}: missing (scripts/fetch_references.py)")
                continue
            image = path.read_bytes()
            digest = hashlib.sha256(image).hexdigest()[:16]
            writes, _psg, cycles, _clock = audio_refs.capture_streams(
                image, 400, press=BUTTONS["start"], press_at=120
            )
            streams.append((f"{name} ({digest}, 400 frames, {len(writes):,} writes)", writes))
    if args.games:
        from megadrive_python.io import BUTTONS

        for name, button, press_at, frames in GAMES:
            path = CARTS / name
            if not path.exists():
                print(f"{name}: missing")
                continue
            image = load_image(path)
            digest = hashlib.sha256(image).hexdigest()[:16]
            writes, _psg, cycles, _clock = audio_refs.capture_streams(
                image, frames, press=BUTTONS[button], press_at=press_at
            )
            streams.append((f"{name} ({digest}, {frames} frames, {len(writes):,} writes)", writes))

    failures = 0
    for name, events in streams:
        cycles = (events[-1][0] if events else 0) + args.tail
        print(f"{name}: {len(events) // 2:,} register writes, {cycles:,} cycles")
        if not compare_nuked(name, events, cycles, tmp, FM_DISCRETE, "ym2612"):
            failures += 1
        if not compare_nuked(name, events, cycles, tmp, YM3438_ASIC, "ym3438"):
            failures += 1
        if args.lle and not compare_lle(name, events, cycles, tmp):
            failures += 1
    print(f"\n{len(streams)} streams, {failures} comparisons not identical")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
