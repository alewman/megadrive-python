#!/usr/bin/env python3
"""What an /IC pulse does to the YM2612's sample clock, on Nuked-MD and on the board.

A test cartridge, with the Z80's bus taken and the FM running, writes $A11200 = 0 (the
Z80's and the FM's reset, /IC), holds it for one of four lengths, writes $0100 to
release it, and waits long enough for the FM to settle; a delay that grows by one pass
each time walks the assertion through every phase of the FM's sample. For each pulse the
FM's phase is taken before the assertion and after the release -- on the die from its
slot-23 markers (``NMD_FMSIG``), on the board from its slot counter -- and the shift the
pulse made, modulo a sample (1,008 MCLK), is tabulated against where in the FM's slot
the assertion's bus cycle began and against the pulse's length (docs/worklog.md, "The
FM's restart, swept").

    python scripts/run_fm_ic.py [--frames 10] [--short]

``--short``: pulses of 56 to 224 MCLK instead, each made by two register writes
(``move.w d0,(a1)``, 0 to 6 ``nop``s, ``move.w d1,(a1)``), the length of the /IC pulse
a game's start-up may give the chip (Gunstar Heroes, +13,454: 112 MCLK).
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
SAMPLE = 1008  # MCLK in the FM's 24 slots
SLOT = 42
RESET = 0xA11200
HOLDS = (0, 6, 30, 120)  # dbra passes the /IC write is held for (10 68000 clocks each)


def cartridge() -> bytes:
    """The test cartridge, hand-assembled; each encoding is in its comment."""
    code = bytearray()
    code += _w(0x46FC, 0x2700)  # move.w #$2700,sr
    code += bytes.fromhex("23fc5345474100a14000")  # move.l #'SEGA',$A14000
    code += _w(0x33FC, 0x0100, 0x00A1, 0x1100)  # move.w #$0100,$A11100: the Z80's bus
    code += _w(0x33FC, 0x0100, 0x00A1, 0x1200)  # move.w #$0100,$A11200: Z80 and FM run
    code += _w(0x43F9, 0x00A0, 0x1000)  # lea $A01000,a1 (Z80 RAM)
    code += _w(0x7400, 0x7C00)  # moveq #0,d2; moveq #0,d6
    loop = len(code)
    # The hold for this pass, HOLDS[(d6 >> 4) & 3] from a table after the code: each
    # length for 16 passes in turn.
    code += _w(0x3006, 0xE848, 0x0240, 0x0003, 0xD040)  # move.w d6,d0; lsr.w #4,d0; andi.w #3,d0; add.w d0,d0
    table_at = len(code)
    code += _w(0x41FA, 0x0000)  # lea table(pc),a0 (patched below)
    code += _w(0x3830, 0x0000)  # move.w (a0,d0.w),d4
    code += _w(0x33FC, 0x0000, 0x00A1, 0x1200)  # move.w #$0000,$A11200: assert /IC
    code += _w(0x51CC, 0xFFFE)  # dbra d4,*: hold
    code += _w(0x33FC, 0x0100, 0x00A1, 0x1200)  # move.w #$0100,$A11200: release
    code += _w(0x3A3C, 0x0300, 0x51CD, 0xFFFE)  # move.w #$300,d5; dbra d5,*: settle
    code += _w(0x5242, 0x0242, 0x003F, 0x3602)  # addq.w #1,d2; andi.w #63,d2; move.w d2,d3
    code += _w(0x4E71, 0x1280, 0x51CB, 0xFFFA)  # nop; move.b d0,(a1); dbra d3,*-4: the walk,
    # 23 68000 clocks a pass (the write to Z80 RAM is held a clock), so it reaches every
    # position in the FM's 6-clock slot
    code += _w(0x5246)  # addq.w #1,d6
    code += _w(0x6000, (loop - (len(code) + 2)) & 0xFFFF)  # bra.w back
    table = len(code)
    for hold in HOLDS:
        code += _w(hold)
    disp = table - (table_at + 2)
    code[table_at + 2:table_at + 4] = disp.to_bytes(2, "big")
    return nuked_md_cartridge(bytes(code))


def short_cartridge() -> bytes:
    """The ``--short`` cartridge: every pass, a pulse of each length in turn."""
    code = bytearray()
    code += _w(0x46FC, 0x2700)  # move.w #$2700,sr
    code += bytes.fromhex("23fc5345474100a14000")  # move.l #'SEGA',$A14000
    code += _w(0x33FC, 0x0100, 0x00A1, 0x1100)  # move.w #$0100,$A11100: the Z80's bus
    code += _w(0x33FC, 0x0100, 0x00A1, 0x1200)  # move.w #$0100,$A11200: Z80 and FM run
    code += _w(0x43F9, 0x00A1, 0x1200)  # lea $A11200,a1
    code += _w(0x45F9, 0x00A0, 0x1000)  # lea $A01000,a2 (Z80 RAM)
    code += _w(0x7000, 0x323C, 0x0100)  # moveq #0,d0; move.w #$0100,d1
    code += _w(0x7400)  # moveq #0,d2
    loop = len(code)
    for nops in range(7):
        code += _w(0x3280) + _w(0x4E71) * nops + _w(0x3281)  # move.w d0,(a1); nops; move.w d1,(a1)
        code += _w(0x3A3C, 0x0300, 0x51CD, 0xFFFE)  # move.w #$300,d5; dbra d5,*: settle
        code += _w(0x5242, 0x0242, 0x003F, 0x3602)  # addq.w #1,d2; andi.w #63,d2; move.w d2,d3
        code += _w(0x4E71, 0x1480, 0x51CB, 0xFFFA)  # nop; move.b d0,(a2); dbra d3,*-4: the walk
    code += _w(0x6000, (loop - (len(code) + 2)) & 0xFFFF)  # bra.w back
    return nuked_md_cartridge(bytes(code))


def shifts(pulses: list[tuple[int, int, int, int]]) -> collections.Counter:
    """From (assertion's /AS, release's /AS, phase before, phase after) for each pulse,
    with the phases the time of a slot 0's start modulo a sample: the count of each
    (hold bucket, position in the slot in MCLK, shift)."""
    found: collections.Counter = collections.Counter()
    for assert_at, release_at, before, after in pulses:
        position = (assert_at - before) % SAMPLE
        shift = (after - before) % SAMPLE
        restart = (after - assert_at) % SAMPLE  # the new phase, from the assertion
        found[(release_at - assert_at, position % SLOT, shift, restart)] += 1
    return found


def referee(rom: bytes, frames: int) -> list[tuple[int, int, int, int]]:
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
                markers.append(int(fields[0]) // 2)
            last = sel23
    writes = [(e.t, e.data) for e in events if e.type == nuked_md.EV_M68K and e.by_m68k
              and not e.read and (e.addr & 0xFFFFFE) == RESET]  # fmt: skip
    pulses = []
    for (assert_at, low), (release_at, high) in zip(writes, writes[1:]):
        if low & 0x100 or not high & 0x100:
            continue
        i = bisect.bisect_left(markers, assert_at) - 1  # the last marker before
        j = bisect.bisect_left(markers, release_at + 2 * SAMPLE)  # one well after
        if i < 0 or j >= len(markers):
            continue
        pulses.append((assert_at, release_at, (markers[i] + SLOT) % SAMPLE,
                       (markers[j] + SLOT) % SAMPLE))  # fmt: skip
    return pulses


def board(rom: bytes, frames: int) -> list[tuple[int, int, int, int]]:
    from megadrive_python.board import Board
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES
    from megadrive_python.system import System

    pulses: list[tuple[int, int, int, int]] = []
    pending: dict[str, int] = {}
    write_word = Board.write_word

    def phase(self: Board, t: int) -> int:
        self.sound_sync(t)
        fm = self.fm
        return (fm.mclk - fm.cycles * SLOT) % SAMPLE  # when the current sample's slot 0 began

    def logged(self: Board, address: int, value: int) -> None:
        t = self.m68k_access_clock()
        if (address & 0xFFFFFE) == RESET:
            if not value & 0x100:
                pending.update(assert_at=t, before=phase(self, t))
            elif "assert_at" in pending:
                pending.update(release_at=t)
        write_word(self, address, value)

    Board.write_word = logged
    try:
        system = System(MACHINES["nuked-md"], Cartridge(rom))
        while system.mclk < (2 + frames) * PAL_FRAME_MCLK:
            system.step()
            if "release_at" in pending and system.mclk >= pending["release_at"] + 2 * SAMPLE:
                pulses.append((pending["assert_at"], pending["release_at"], pending["before"],
                               phase(system.board, system.mclk)))  # fmt: skip
                pending.clear()
    finally:
        Board.write_word = write_word
    return [p for p in pulses if p[0] >= 2 * PAL_FRAME_MCLK]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--frames", type=int, default=10)
    parser.add_argument("--short", action="store_true", help="pulses of 56-224 MCLK")
    args = parser.parse_args()
    rom = short_cartridge() if args.short else cartridge()
    tables = {"referee": shifts(referee(rom, args.frames)), "board": shifts(board(rom, args.frames))}
    bucket = 28 if args.short else 100
    lengths = sorted({k[0] // bucket * bucket for t in tables.values() for k in t})
    for length in lengths:
        print(f"pulses {length}-{length + bucket - 1} MCLK long: shift (MCLK, mod {SAMPLE}) by "
              f"the assertion's /AS, MCLK into its slot")
        for position in range(0, SLOT, 7):
            row = []
            for name, table in tables.items():
                picked = [(k, n) for k, n in table.items()
                          if k[0] // bucket * bucket == length and k[1] // 7 * 7 == position]
                by_shift = collections.Counter()
                by_restart = collections.Counter()
                for k, n in picked:
                    by_shift[k[2]] += n
                    by_restart[k[3]] += n
                # whichever is constant names the kind: a pause shifts by a fixed amount,
                # a restart puts slot 0 a fixed time after the assertion
                if len(by_shift) <= len(by_restart):
                    row.append(f"{name} shift " + ",".join(f"{v}x{n}" for v, n in sorted(by_shift.items())))
                else:
                    row.append(f"{name} restart+" + ",".join(f"{v}x{n}" for v, n in sorted(by_restart.items())))
            print(f"  {position:2d}-{position + 6:2d}:  " + "   ".join(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
