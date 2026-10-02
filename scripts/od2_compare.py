#!/usr/bin/env python3
"""Overdrive 2 against the referee, from the reset button to the Z80's sound driver:
the numbers Session 14 reports for every experiment (docs/worklog.md).

The referee's run (``reference/z80-reset``: Nuked-MD from power-on, the reset button
pressed at frame 25 as ``reference/nuked-od2/launch.sh`` does, the bus traced) and the
board's are compared on:

- **the boot offset**: the Z80's start (the 68000's second release of the Z80's bus
  after the final $A11200 release) against the start of the next vblank line; positive
  when the board's 68000 is late against its VDP;
- **the matching Z80 reads**: how many of the Z80's reads after its start have the
  referee's addresses, in order, before the first that does not.

The board's reset button is pressed with ZRES at ``--reset-mclk`` exactly (the
referee's pulse, 26,841,996 half-MCLK/2, less the two VDPs' offset; 26,601,694 is the
clock stepping found best: Session 14 found 26,601,687, and once the 68000's holds were
whole clocks the first interrupt after the reset matched the die 7 MCLK later; docs/worklog.md
"Handover by handover").

``--align-fm`` is **specific to this ROM and this referee run**: at the Z80's start the
YM2612's slot counter is set to the phase the referee's has there (its slot-23 marker
41 MCLK after the Z80's start, modulo 1,008; the board's cycle 0 falls 85 MCLK after
the referee's marker, calibrated at the Z80's start on a run whose reads match). It removes one
variable -- the 68000's exact timing over the 1.2M MCLK between the final sound reset
and the Z80's start, to which the Z80 reads are sensitive at 10 MCLK -- so that the
sound driver can be judged. It is a harness setting, never board behaviour.

    python scripts/od2_compare.py ROM [--align-fm] [--src DIR] [--frames 80]
"""

from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "reference" / "z80-reset"

#: The referee's run: the Z80's start (MCLK), its vblank after it, and the YM2612's
#: slot-23 marker phase against the Z80's start (``fmsig2.txt``; Session 14).
REFEREE_Z80_START = 42_570_940
REFEREE_VBLANK_AFTER = 42_754_904
REFEREE_MARKER_PHASE = 41
#: Where the board's cycle 0 falls against the referee's slot-23 marker, calibrated at
#: the Z80's start on main (69b9e86), whose Z80 reads match 4,738 without alignment.
BOARD_CYCLE0_LAG = 85
FM_PERIOD = 24 * 42


def referee_reads() -> list[int]:
    """The referee's Z80 read addresses from its start, cached next to the trace."""
    cache = REFERENCE / "z80reads.pkl"
    if cache.exists():
        return pickle.loads(cache.read_bytes())
    sys.path.insert(0, str(ROOT / "scripts"))
    import nuked_md

    since = REFEREE_Z80_START - 800
    reads = [e.addr & 0xFFFF for e in nuked_md.read_trace(REFERENCE / "t.bin")
             if e.type == nuked_md.EV_Z80 and e.flags & 1 and e.t >= since]  # fmt: skip
    # The reads start with the Z80's fetch of $0001 after its reset's opcode fetch.
    reads = reads[reads.index(1) :]
    cache.write_bytes(pickle.dumps(reads))
    return reads


def run_board(
    src: Path,
    rom: Path,
    reset_mclk: int,
    align_fm: bool,
    frames: int,
    machine: str = "megadrive-tmss",
    refresh_restart: bool = True,
) -> dict:
    sys.path.insert(0, str(src))
    from megadrive_python import system as system_module
    from megadrive_python.cartridge import Cartridge, load_image
    from megadrive_python.machine import MACHINES
    from megadrive_python.system import System

    # A and B of Session 14: the board with and without the refresh restart at the
    # reset button (system.RESET_RESTARTS_REFRESH), kept reproducible.
    system_module.RESET_RESTARTS_REFRESH = refresh_restart

    def make() -> System:
        system = System(MACHINES[machine], Cartridge.from_image(load_image(rom)))
        system.board.fm.enabled = True
        return system

    probe, stop = make(), 0
    while probe.mclk <= reset_mclk:
        stop = probe.mclk
        probe.step()
    system = make()
    board, fm, cpu = system.board, system.board.fm, system.m68k
    state = {"released": False, "zeros": 0, "z80_start": None, "vblank": None}
    reads: list[int] = []
    write_word, z80_read = cpu._write_word, board.z80_read

    def on_write(address, value):
        address &= 0xFFFFFF
        if address == 0xA11200 and value & 0x100 and system.mclk > reset_mclk:
            state["released"], state["zeros"] = True, 0
        elif address == 0xA11100 and not value & 0x100 and state["released"]:
            state["zeros"] += 1
            if state["zeros"] == 2 and state["z80_start"] is None:
                t = board.m68k_access_clock()
                state["z80_start"] = t
                if align_fm:
                    align(t)
        return write_word(address, value)

    def align(t: int) -> None:
        board.sound_sync(t)
        boundary = fm.mclk + (-fm.cycles % 24) * 42
        target = t + REFEREE_MARKER_PHASE + BOARD_CYCLE0_LAG
        shift = (target - boundary) % FM_PERIOD
        fm.cycles = (fm.cycles - shift // 42) % 24
        fm.mclk += shift % 42

    def on_z80_read(address):
        if state["released"]:
            reads.append(address)
        return z80_read(address)

    cpu._write_word = on_write
    system.z80.read_byte = on_z80_read
    vdp_next = type(system.vdp)._next_line

    def next_line(vdp):
        vdp_next(vdp)
        if state["z80_start"] is not None and state["vblank"] is None and vdp.v == 224:
            state["vblank"] = vdp.line_start

    type(system.vdp)._next_line = next_line
    pressed = False
    while system.vdp.frame < frames:
        if not pressed and system.mclk + 1_000_000 >= reset_mclk:
            while system.mclk < stop:
                system.step()
            system.press_reset(at=reset_mclk)
            pressed = True
        system.run_frame()
        board.fm.take_samples()
        board.psg.take_samples()
    # The Z80's reads from its fetch of $0001 after the start's reset.
    start = max(i for i, a in enumerate(reads[:40]) if a == 0) + 1 if 0 in reads[:40] else 0
    return {**state, "reads": reads[start:]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("rom", type=Path)
    parser.add_argument("--src", type=Path, default=ROOT / "src", help="the board to run")
    parser.add_argument("--reset-mclk", type=int, default=26_601_719)
    parser.add_argument("--align-fm", action="store_true", help="ROM-specific; see above")
    parser.add_argument("--frames", type=int, default=80)
    parser.add_argument("--machine", default="megadrive-tmss", help="the referee is version 1")
    parser.add_argument("--no-refresh-restart", action="store_true",
                        help="the board without system.RESET_RESTARTS_REFRESH (A)")  # fmt: skip
    args = parser.parse_args()
    theirs = referee_reads()
    ours = run_board(args.src.resolve(), args.rom, args.reset_mclk, args.align_fm, args.frames,
                     args.machine, not args.no_refresh_restart)  # fmt: skip
    offset = (REFEREE_VBLANK_AFTER - REFEREE_Z80_START) - (ours["vblank"] - ours["z80_start"])
    n = 0
    while n < min(len(theirs), len(ours["reads"])) and theirs[n] == ours["reads"][n]:
        n += 1
    print(
        f"src {args.src}; {args.machine}; reset {args.reset_mclk}; FM aligned: {args.align_fm}; "
        f"refresh restart: {not args.no_refresh_restart}"
    )
    print(f"boot offset {offset:+d} MCLK; matching Z80 reads {n} "
          f"(of {len(ours['reads'])}; first difference at ${theirs[n]:04X} / "
          f"${ours['reads'][n]:04X})")  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
