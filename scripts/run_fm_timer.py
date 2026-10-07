#!/usr/bin/env python3
"""When the YM2612's Timer A flag shows on the status port, on Nuked-MD and on the board.

A test cartridge takes the Z80's bus (with the Z80 and the FM out of reset) and, over
and over: sets Timer A to $3FF (it overflows on its next count), writes $27 = $15
(load, enable, reset A) and polls the status port; then writes $27 = $15 again, which
resets the flag, and polls again; then stops the timer and waits a delay that grows by
one pass each time, so the writes walk through every phase of the FM's sample. Each
poll is ``move.b (a0),d0``, 56 MCLK apart. Printed for each machine: how long after
each of the two $27 writes (from its /AS) the first poll sees the flag set, and the
first sees it clear -- the shape of the two distributions is the timer's pipeline as
the CPU sees it (docs/worklog.md, "The timer flag on the die").

    python scripts/run_fm_timer.py [--frames 6] [--polls 30]
"""

from __future__ import annotations

import argparse
import collections
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

import nuked_md  # noqa: E402
from run_vscroll_latch import _w, nuked_md_cartridge  # noqa: E402

PAL_FRAME_MCLK = 313 * 3420
FM = 0xA04000
BUSY_WAIT = _w(0x0810, 0x0007, 0x66FA)  # btst #7,(a0); bne.s *-4


def write(register: int, value: int) -> bytes:
    """Wait for BUSY to clear, then write ``value`` to FM register ``register``."""
    return (BUSY_WAIT + _w(0x10BC, register)  # move.b #register,(a0)
            + _w(0x117C, value, 0x0001))  # move.b #value,1(a0)  # fmt: skip


def cartridge(polls: int, timer: int = 0x3FF) -> bytes:
    """The test cartridge, hand-assembled; each encoding is in its comment."""
    code = bytearray()
    code += _w(0x46FC, 0x2700)  # move.w #$2700,sr
    code += bytes.fromhex("23fc5345474100a14000")  # move.l #'SEGA',$A14000
    code += _w(0x33FC, 0x0100, 0x00A1, 0x1100)  # move.w #$0100,$A11100: the Z80's bus
    code += _w(0x33FC, 0x0100, 0x00A1, 0x1200)  # move.w #$0100,$A11200: Z80 and FM run
    code += _w(0x0839, 0x0000, 0x00A1, 0x1100, 0x66F6)  # btst #0,$A11100; bne.s *-8
    code += _w(0x41F9, 0x00A0, 0x4000)  # lea $A04000,a0
    code += _w(0x7400)  # moveq #0,d2
    loop = len(code)
    poll = _w(0x1010) * polls  # move.b (a0),d0
    code += write(0x24, timer >> 2) + write(0x25, timer & 3)  # Timer A
    code += write(0x27, 0x15) + poll  # load, enable, reset A: then watch it set
    code += write(0x27, 0x15) + poll  # reset A again: watch it clear
    code += write(0x27, 0x00)  # stop
    code += _w(0x5242, 0x0242, 0x003F, 0x3602)  # addq.w #1,d2; andi.w #63,d2; move.w d2,d3
    code += _w(0x4E71, 0x51CB, 0xFFFC)  # nop; dbra d3,*-2
    code += _w(0x6000, (loop - (len(code) + 2)) & 0xFFFF)  # bra.w back
    return nuked_md_cartridge(bytes(code))


def measure(accesses: list[tuple[int, int, str, int]]) -> tuple[collections.Counter, collections.Counter]:
    """From (MCLK, address, r/w, byte) for the 68000's accesses to the FM: the first
    poll after each $27 load write that sees the flag set, and after each reset write
    that sees it clear, from the write. The load write resets the flag too, so its set is
    the first poll showing it after one has shown it clear."""
    sets: collections.Counter = collections.Counter()
    clears: collections.Counter = collections.Counter()
    register = None
    order = 0  # the $27 writes of a pass: 1 the load, 2 the reset, 3 the stop
    pending: tuple[int, int] | None = None
    seen_clear = False
    for t, address, kind, value in accesses:
        if kind == "w" and address == FM:
            register = value
        elif kind == "w" and address == FM + 1 and register == 0x27:
            order = order % 3 + 1 if value == 0x15 or order == 2 else 0
            if value == 0x00:
                order = 0
            pending = (order, t) if order in (1, 2) else None
            seen_clear = False
        elif kind == "r" and address == FM and pending is not None:
            which, at = pending
            if which == 1 and not value & 1:
                seen_clear = True  # the load's reset has shown; now wait for the overflow
            elif which == 1 and value & 1 and seen_clear:
                sets[t - at] += 1
                pending = None
            elif which == 2 and not value & 1:
                clears[t - at] += 1
                pending = None
    return sets, clears


def referee(rom: bytes, frames: int) -> tuple[collections.Counter, collections.Counter]:
    nuked_md.ensure_built()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "rom.bin").write_bytes(rom)
        env = dict(os.environ, NMD_STOP=str((2 + frames) * PAL_FRAME_MCLK * 2),
                   NMD_TRACE=str(work / "t.bin"), NMD_TRACE_MASK="1",
                   NMD_TRACE_FROM=str(2 * PAL_FRAME_MCLK * 2))  # fmt: skip
        subprocess.run(["nice", str(nuked_md.BINARY), "-pal", str(work / "rom.bin")], env=env,
                       cwd=work, check=True, capture_output=True)  # fmt: skip
        events = nuked_md.read_trace(work / "t.bin")
    accesses = []
    for e in events:
        if e.type != nuked_md.EV_M68K or not e.by_m68k:
            continue
        address = e.address & 0xFFFFFF  # the byte address, from /UDS and /LDS
        if address in (FM, FM + 1):
            byte = (e.data >> 8) & 0xFF if address == FM else e.data & 0xFF
            accesses.append((e.t, address, "r" if e.read else "w", byte))
    return measure(accesses)


def board(rom: bytes, frames: int) -> tuple[collections.Counter, collections.Counter]:
    from megadrive_python.board import Board
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES
    from megadrive_python.system import System

    accesses: list[tuple[int, int, str, int]] = []
    read_byte, write_byte = Board.read_byte, Board.write_byte

    def logged_read(self: Board, address: int) -> int:
        value = read_byte(self, address)
        if (address & 0xFFFFFF) in (FM, FM + 1):
            accesses.append((self.m68k_access_clock(), address & 0xFFFFFF, "r", value))
        return value

    def logged_write(self: Board, address: int, value: int) -> None:
        if (address & 0xFFFFFF) in (FM, FM + 1):
            accesses.append((self.m68k_access_clock(), address & 0xFFFFFF, "w", value & 0xFF))
        write_byte(self, address, value)

    Board.read_byte, Board.write_byte = logged_read, logged_write
    try:
        system = System(MACHINES["nuked-md"], Cartridge(rom))
        while system.mclk < (2 + frames) * PAL_FRAME_MCLK:
            system.step()
    finally:
        Board.read_byte, Board.write_byte = read_byte, write_byte
    return measure([a for a in accesses if a[0] >= 2 * PAL_FRAME_MCLK])


def show(name: str, found: collections.Counter) -> None:
    total = sum(found.values())
    if not total:
        print(f"  {name}: none")
        return
    low, high = min(found), max(found)
    print(f"  {name}: {total}, {low}-{high} MCLK after the write; by 56 MCLK from {low}: "
          + " ".join(str(sum(n for t, n in found.items() if low + k * 56 <= t < low + (k + 1) * 56))
                     for k in range((high - low) // 56 + 1)))  # fmt: skip


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--frames", type=int, default=6)
    parser.add_argument("--polls", type=int, default=30)
    parser.add_argument("--timer", type=lambda v: int(v, 0), default=0x3FF,
                        help="Timer A's value ($3FF overflows on its next count)")
    args = parser.parse_args()
    rom = cartridge(args.polls, args.timer)
    for name, (sets, clears) in (("referee", referee(rom, args.frames)),
                                  ("board", board(rom, args.frames))):  # fmt: skip
        print(f"{name}:")
        show("flag set after the load", sets)
        show("flag clear after the reset", clears)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
