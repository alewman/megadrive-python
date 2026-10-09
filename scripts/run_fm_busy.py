#!/usr/bin/env python3
"""When the YM2612's BUSY flag clears after a data write, on Nuked-MD and on the board.

A test cartridge takes the Z80's bus (the Z80 and the FM out of reset) and, over and
over: waits for BUSY to clear, writes an address ($40), waits a delay that grows by one
pass each time (161 MCLK a pass, so the data write walks through every phase of the
FM's 42-MCLK internal cycle), writes the data byte, waits a second delay (a pass of 98
MCLK, 0 to 8 of them, so the polls walk through every 7 MCLK of their own 63-MCLK
spacing), and polls the status port 40 times with ``move.b (a0),d0``. For each data
write, on each machine: where its /AS falls in that machine's FM cycle (the die's from
its slot-23 markers, ``NMD_FMSIG``; the board's from its cycle count), and the last poll
that read BUSY set and the first that read it clear, from the write's /AS. Tabulated by
the phase: the earliest "clear" and the latest "busy" over the passes bracket when BUSY
clears to 7 MCLK (docs/worklog.md, "The busy flag").

Each machine is measured against its own FM's cycle, so the two tables agree only as
far as the two FMs' phases against the 68000's clock happen to; what decides is the
same cartridge run from power-on with every phase put on the die's
(``scripts/cold_boot_drift.py``), where each poll is compared with the die's at the same
MCLK: ``--rom PATH`` writes the cartridge out for that.

    python scripts/run_fm_busy.py [--frames 12] [--rom busy.bin]
"""

from __future__ import annotations

import argparse
import bisect
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
CYCLE = 42  # MCLK in one of the FM's internal cycles
SAMPLE = 24 * CYCLE
POLLS = 40


def cartridge() -> bytes:
    """The test cartridge, hand-assembled; each encoding is in its comment."""
    code = bytearray()
    code += _w(0x46FC, 0x2700)  # move.w #$2700,sr
    code += bytes.fromhex("23fc5345474100a14000")  # move.l #'SEGA',$A14000
    code += _w(0x33FC, 0x0100, 0x00A1, 0x1100)  # move.w #$0100,$A11100: the Z80's bus
    code += _w(0x33FC, 0x0100, 0x00A1, 0x1200)  # move.w #$0100,$A11200: Z80 and FM run
    code += _w(0x0839, 0x0000, 0x00A1, 0x1100, 0x66F6)  # btst #0,$A11100; bne.s *-8
    code += _w(0x41F9, 0x00A0, 0x4000)  # lea $A04000,a0 (the FM)
    code += _w(0x43F9, 0x00A0, 0x1000)  # lea $A01000,a1 (Z80 RAM)
    code += _w(0x7400, 0x7C00)  # moveq #0,d2; moveq #0,d6
    loop = len(code)
    code += _w(0x0810, 0x0007, 0x66FA)  # btst #7,(a0); bne.s *-4: BUSY clear
    code += _w(0x10BC, 0x0040)  # move.b #$40,(a0): the address
    code += _w(0x5242, 0x0242, 0x003F, 0x3602)  # addq.w #1,d2; andi.w #63,d2; move.w d2,d3
    code += _w(0x4E71, 0x1280, 0x51CB, 0xFFFA)  # nop; move.b d0,(a1); dbra d3,*-4: 161 MCLK
    code += _w(0x117C, 0x007F, 0x0001)  # move.b #$7F,1(a0): the data, BUSY from here
    code += _w(0x3806, 0xE84C, 0xC87C, 0x0007)  # move.w d6,d4; lsr.w #4,d4; and.w #7,d4
    code += _w(0x4E71, 0x51CC, 0xFFFC)  # nop; dbra d4,*-2: 98 MCLK a pass
    code += _w(0x1010) * POLLS  # move.b (a0),d0
    code += _w(0x5246)  # addq.w #1,d6
    code += _w(0x6000, (loop - (len(code) + 2)) & 0xFFFF)  # bra.w back
    return nuked_md_cartridge(bytes(code))


def measure(accesses: list[tuple[int, int, str, int]], phase) -> collections.Counter:
    """From (MCLK, byte address, r/w, byte) for the 68000's FM accesses, and a function
    giving a time's phase in the FM's cycle: per data write, (phase, last busy poll,
    first clear poll), both from the write."""
    found: collections.Counter = collections.Counter()
    write = None
    busy = clear = None
    for t, address, kind, value in accesses:
        if kind == "w" and address == FM + 1:
            if write is not None and clear is not None:
                found[(phase(write), busy, clear)] += 1
            write, busy, clear = t, None, None
        elif kind == "r" and address == FM and write is not None and clear is None:
            if value & 0x80:
                busy = t - write
            else:
                clear = t - write
    return found


def referee(rom: bytes, frames: int) -> collections.Counter:
    nuked_md.ensure_built()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "rom.bin").write_bytes(rom)
        env = dict(os.environ, NMD_STOP=str((2 + frames) * PAL_FRAME_MCLK * 2),
                   NMD_TRACE=str(work / "t.bin"), NMD_TRACE_MASK="1",
                   NMD_TRACE_FROM=str(2 * PAL_FRAME_MCLK * 2), NMD_FMSIG=str(work / "fm.txt"))  # fmt: skip
        subprocess.run(["nice", str(nuked_md.BINARY), "-pal", str(work / "rom.bin")], env=env,
                       cwd=work, check=True, capture_output=True)  # fmt: skip
        events = nuked_md.read_trace(work / "t.bin")
        markers, last = [], 0
        for line in open(work / "fm.txt"):
            fields = line.split()
            sel23 = int(fields[4])
            if sel23 and not last:
                markers.append(int(fields[0]) / 2)
            last = sel23
    accesses = []
    for e in events:
        if e.type == nuked_md.EV_M68K and e.by_m68k:
            address = e.address & 0xFFFFFF  # the byte address, from /UDS and /LDS
            if address in (FM, FM + 1):
                byte = (e.data >> 8) & 0xFF if address == FM else e.data & 0xFF
                accesses.append((e.t, address, "r" if e.read else "w", byte))

    def phase(t: int) -> int:
        i = bisect.bisect_right(markers, t) - 1  # slot 23 began here: a cycle boundary
        return int(t - markers[i]) % CYCLE

    return measure(accesses, phase)


def board(rom: bytes, frames: int) -> collections.Counter:
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
        fm = system.board.fm
        while system.mclk < (2 + frames) * PAL_FRAME_MCLK:
            system.step()
        # The FM's cycles begin at a fixed phase of the board's clock: where one began.
        start = fm.mclk - fm.cycles * CYCLE  # a sample's slot 0
    finally:
        Board.read_byte, Board.write_byte = read_byte, write_byte

    def phase(t: int) -> int:
        return (t - start) % CYCLE

    return measure([a for a in accesses if a[0] >= 2 * PAL_FRAME_MCLK], phase)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--frames", type=int, default=12)
    parser.add_argument("--rom", type=Path, help="write the cartridge here and stop")
    args = parser.parse_args()
    rom = cartridge()
    if args.rom:
        args.rom.write_bytes(rom)
        return 0
    tables = {"referee": referee(rom, args.frames), "board": board(rom, args.frames)}
    print("the data write's /AS in its machine's FM cycle (MCLK): BUSY seen set up to,"
          " clear from, MCLK after it (writes)")  # fmt: skip
    for name, table in tables.items():
        print(f"{name}:")
        for p in sorted({k[0] for k in table}):
            rows = [(k, n) for k, n in table.items() if k[0] == p and k[1] is not None]
            if not rows:
                continue
            last_busy = max(k[1] for k, _ in rows)
            first_clear = min(k[2] for k, _ in rows)
            print(f"  phase {p:2d}: busy to {last_busy}, clear from {first_clear} "
                  f"({sum(n for _, n in rows)} writes)")  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
