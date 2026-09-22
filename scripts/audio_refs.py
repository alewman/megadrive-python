"""Running milestone 4's three audio referees, and capturing the board's own streams.

The referees are C programs built from the pinned die-derived sources by
``sh validation/build_referees.sh`` into ``reference/build-referees/`` (ignored):

* ``opn2_ref``    -- Nuked-OPN2 (YM3438 from its die, with a YM2612 output mode)
* ``lle2612_ref`` -- YM2608-LLE's ``fmopna_2612.c`` (the NMOS YM2612 from its die)
* ``psg_ref``     -- Nuked-PSG (the PSG block inside the YM7101)

Each takes a binary event file of ``{u64 clock; u8 port; u8 data}`` records and
writes a binary output file; the clock unit differs per referee and is documented
in the corresponding ``validation/*.c``.  This module builds those files, runs the
program and decodes the result, so the run scripts stay short, and it captures a
cartridge's register streams off the board in the units the referees take.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REFEREES = ROOT / "reference" / "build-referees"

#: Master clocks per unit of each referee's clock.  The YM2612 runs at MCLK / 7 and
#: divides that by 6 internally, so one Nuked-OPN2 cycle is 42 MCLK and 24 of them
#: are one output sample (53,267 Hz on NTSC); the PSG runs at MCLK / 15.
MCLK_PER_OPN2_CYCLE = 42
MCLK_PER_CHIP_CLOCK = 7
MCLK_PER_PSG_CLOCK = 15

EVENT = struct.Struct("<QBB")

#: Two of these running at once in the same directory -- two pytest workers, a run
#: beside a test -- must not write each other's event and output files, so every
#: name carries the process id.  (One run of the test suite failed exactly once, on
#: 2026-09-22, while another comparison was running; nothing else explained it.)
_PID = os.getpid()


def referee(name: str) -> Path:
    path = REFEREES / name
    if not path.exists():
        raise SystemExit(
            f"{path} is missing: run `sh validation/build_referees.sh` first "
            "(it needs reference/src/, which scripts/fetch_references.py fetches)"
        )
    return path


def write_events(path: Path, events: list[tuple[int, int, int]]) -> None:
    """``(clock, port, data)`` records, sorted, into a referee's event file."""
    with path.open("wb") as handle:
        for clock, port, data in sorted(events):
            handle.write(EVENT.pack(clock, port & 0xFF, data & 0xFF))


def _run(program: Path, args: list[str]) -> None:
    result = subprocess.run([str(program), *args], capture_output=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr.decode(errors="replace"))
        raise SystemExit(f"{program.name} failed ({result.returncode})")


def run_opn2(
    events: list[tuple[int, int, int]],
    cycles: int,
    tmp: Path,
    *,
    variant: str = "ym2612",
    every: int = 1,
) -> tuple[array, array, array]:
    """Nuked-OPN2 over ``cycles`` internal cycles; returns (mol, mor, status)."""
    event_file, out_file = tmp / f"opn2-events-{_PID}.bin", tmp / f"opn2-out-{_PID}.bin"
    write_events(event_file, events)
    _run(referee("opn2_ref"), [str(event_file), str(out_file), str(cycles), variant, str(every)])
    triples = array("h")
    triples.frombytes(out_file.read_bytes())
    if sys.byteorder == "big":
        triples.byteswap()
    event_file.unlink()
    out_file.unlink()
    return triples[0::3], triples[1::3], triples[2::3]


def run_lle(
    events: list[tuple[int, int, int]], clocks: int, tmp: Path, *, every: int = 1
) -> tuple[array, array]:
    """YM2608-LLE's YM2612 over ``clocks`` chip clocks; returns (o_mol, o_mor)."""
    event_file, out_file = tmp / f"lle-events-{_PID}.bin", tmp / f"lle-out-{_PID}.bin"
    write_events(event_file, events)
    _run(referee("lle2612_ref"), [str(event_file), str(out_file), str(clocks), str(every)])
    raw = out_file.read_bytes()
    both = array("h")
    both.frombytes(raw)
    if sys.byteorder == "big":
        both.byteswap()
    event_file.unlink()
    out_file.unlink()
    return both[0::2], both[1::2]


def run_psg(
    events: list[tuple[int, int]], clocks: int, tmp: Path, *, every: int = 1
) -> tuple[list[tuple[int, int, int, int]], array]:
    """Nuked-PSG over ``clocks`` input clocks.

    Returns the four attenuations each channel drives at each sampled clock and
    Nuked's own analogue level (its measured volume curve times 8192).
    """
    event_file, out_file = tmp / f"psg-events-{_PID}.bin", tmp / f"psg-out-{_PID}.bin"
    # psg_ref's record is {u64 clock; u8 data; u8 pad}: the one port takes no number.
    write_events(event_file, [(clock, data, 0) for clock, data in events])
    _run(referee("psg_ref"), [str(event_file), str(out_file), str(clocks), str(every)])
    raw = out_file.read_bytes()
    count = len(raw) // 4
    attenuations = []
    levels = array("h", bytes(2 * count))
    for i in range(count):
        word = raw[i * 4] | (raw[i * 4 + 1] << 8)
        attenuations.append(((word >> 12) & 15, (word >> 8) & 15, (word >> 4) & 15, word & 15))
        levels[i] = int.from_bytes(raw[i * 4 + 2 : i * 4 + 4], "little", signed=True)
    event_file.unlink()
    out_file.unlink()
    return attenuations, levels


# -- capturing a register stream from the board -------------------------------------


def capture_streams(
    image: bytes,
    frames: int,
    *,
    machine_name: str = "genesis",
    press: int = 0,
    press_at: int | tuple[int, ...] = 0,
    hold: int = 8,
) -> tuple[list[tuple[int, int, int]], list[tuple[int, int]], int, int]:
    """Run a cartridge and record every write that reaches either sound chip.

    Returns the YM2612's writes as ``(cycle, port, value)`` with ``cycle`` counted in
    the chip's internal cycles, the PSG's as ``(clock, value)`` in its input clocks,
    and the two chips' final clock counts.  Those are exactly the units the referees
    take, and the units the board's own chips were at when each write arrived, so a
    replay of the stream into a fresh chip sees each write on the cycle the board's
    did.  Writes made while /RESET is held never reach the chip and are not recorded.

    ``press_at`` is the frame the buttons are pressed at, or several frames -- some
    games need a button more than once to reach the music (Gunstar Heroes wants one
    at its title and another at its mode select).  Each press is held for ``hold``
    frames so that a game reading edges sees one.

    The generator is turned off for the capture: the register stream does not depend
    on it, and it is most of the time.
    """
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "src"))
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES
    from megadrive_python.system import System

    system = System(MACHINES[machine_name], Cartridge(image))
    system.board.fm.enabled = False
    fm, psg, board = system.board.fm, system.board.psg, system.board
    fm_writes: list[tuple[int, int, int]] = []
    psg_writes: list[tuple[int, int]] = []
    fm_write, psg_write = fm.write, psg.write

    def spy_fm(port: int, value: int) -> None:
        if not fm.in_reset:
            fm_writes.append((fm.cycles_run, port & 3, value & 0xFF))
        fm_write(port, value)

    def spy_psg(value: int) -> None:
        psg_writes.append((psg.clock, value & 0xFF))
        psg_write(value)

    fm.write = spy_fm  # type: ignore[method-assign]
    psg.write = spy_psg  # type: ignore[method-assign]
    pad = board.io.ports[0].device
    presses = (press_at,) if isinstance(press_at, int) else tuple(press_at)
    for frame in range(frames):
        pad.pressed = press if press and any(at <= frame < at + hold for at in presses) else 0
        system.run_frame()
        fm.take_samples()
        psg.take_samples()
    return fm_writes, psg_writes, fm.cycles_run, psg.clock
