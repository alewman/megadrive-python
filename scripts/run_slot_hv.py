#!/usr/bin/env python3
"""Where the VDP's slots, HBlank and read-ahead fall against each machine's own HV counter.

A test cartridge (H40) repeats a pair of accesses: a data-port VRAM write with the FIFO
kept full (``write``), a status read (``status``) or a data-port CRAM read (``read``),
each followed by a read of the HV counter. The H the 68000 reads next places the event
on each machine against its own counter, so Nuked-MD (die-derived) and the board need no
lining up -- the question the VDPs' offset and the /HSYNC frame of
``run_read_slots.py`` disagreed on (docs/worklog.md, "The active-line slots"). Printed:
the H read after each stalled write or read, or after each change of the HBlank bit,
with how often, on both machines.

    python scripts/run_slot_hv.py write|status|read [--display-off]
"""

from __future__ import annotations

import argparse
import collections
import os
import subprocess
import sys
import tempfile
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

import nuked_md  # noqa: E402
from run_vscroll_latch import _w, nuked_md_cartridge  # noqa: E402

FRAME_MCLK = 262 * 3420  # NTSC (PAL with --pal: 313 lines)
FIRST, LAST = 6, 10  # the frames measured
PAL = {"on": False}  # --pal: the die run with -pal, the board as "megadrive"
DATA, STATUS, HV = 0xC00000, 0xC00004, 0xC00008

#: Per mode: the address set before the loop, and the access paired with the HV read.
MODES = {
    "write": (_w(0x22BC, 0x6000, 0x0003), _w(0x3081)),  # VRAM $E000; move.w d1,(a0)
    "status": (b"", _w(0x3011)),  # move.w (a1),d0
    "read": (_w(0x22BC, 0x0000, 0x0020), _w(0x3610)),  # CRAM 0; move.w (a0),d3
}


def cartridge(mode: str, display: bool) -> bytes:
    """The test cartridge, hand-assembled; each encoding is in its comment."""
    address, access = MODES[mode]
    code = bytearray()
    code += _w(0x46FC, 0x2700)  # move.w #$2700,sr
    code += bytes.fromhex("23fc5345474100a14000")  # move.l #'SEGA',$A14000
    code += _w(0x41F9, 0x00C0, 0x0000)  # lea $C00000,a0 (data)
    code += _w(0x43F9, 0x00C0, 0x0004)  # lea $C00004,a1 (control)
    code += _w(0x45F9, 0x00C0, 0x0008)  # lea $C00008,a2 (HV counter)
    mode1 = (0x44 if display else 0x04) | (0x08 if PAL.get("v30") else 0)
    for reg, value in ((0x00, 0x04), (0x01, mode1), (0x02, 0x30), (0x04, 0x07),
                       (0x05, 0x78), (0x0C, 0x81), (0x0F, 0x02), (0x10, 0x01)):  # fmt: skip
        code += _w(0x32BC, 0x8000 | reg << 8 | value)  # move.w #$8rvv,(a1)
    code += address
    loop = bytearray()
    for _ in range(32):
        loop += access + _w(0x3412)  # the access, then move.w (a2),d2
    loop += _w(0x6000, (-(len(loop) + 2)) & 0xFFFF)  # bra.w back
    code += loop
    return nuked_md_cartridge(bytes(code))


def referee(rom: bytes) -> list[tuple[int, str, int, int]]:
    """The die's (address, r/w, held MCLK, data) for every VDP access in the window."""
    nuked_md.ensure_built()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "rom.bin").write_bytes(rom)
        span = (FIRST * FRAME_MCLK * 2, LAST * FRAME_MCLK * 2)
        env = dict(os.environ, NMD_STOP=str(span[1] + 1000), NMD_TRACE=str(work / "t.bin"),
                   NMD_TRACE_MASK="1", NMD_TRACE_FROM=str(span[0]),
                   NMD_TRACE_TO=str(span[1]))  # fmt: skip
        argv = ["nice", str(nuked_md.BINARY), *(["-pal"] if PAL["on"] else [])]
        subprocess.run([*argv, str(work / "rom.bin")], env=env, cwd=work, check=True,
                       capture_output=True)  # fmt: skip
        events = nuked_md.m68k_cycles(nuked_md.read_trace(work / "t.bin"), dma=False)
    return [(e.address & 0xFFFFFE, "r" if e.read else "w", e.length - nuked_md.AS_LOW, e.data)
            for e in events if e.address >= DATA]  # fmt: skip


def board(rom: bytes) -> list[tuple[int, str, int, int]]:
    """The same for the board."""
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES
    from megadrive_python.system import System

    system = System(MACHINES["megadrive" if PAL["on"] else "genesis"], Cartridge(rom))
    system.board.fm.enabled = False
    cpu = system.m68k
    out: list[tuple[int, str, int, int]] = []
    read, write = cpu._read_word, cpu._write_word

    def logged_read(address: int) -> int:
        before = system.board.stall
        value = read(address)
        if address >= DATA and FIRST * FRAME_MCLK <= system.mclk < LAST * FRAME_MCLK:
            out.append((address & 0xFFFFFE, "r", system.board.stall - before, value))
        return value

    def logged_write(address: int, value: int) -> None:
        before = system.board.stall
        write(address, value)
        if address >= DATA and FIRST * FRAME_MCLK <= system.mclk < LAST * FRAME_MCLK:
            out.append((address & 0xFFFFFE, "w", system.board.stall - before, value))

    cpu._read_word, cpu._write_word = logged_read, logged_write
    while system.mclk < LAST * FRAME_MCLK:
        system.step()
    return out


def placed(cycles: list[tuple], mode: str) -> collections.Counter:
    """The H read after each event of the mode's kind."""
    found: collections.Counter = collections.Counter()
    last = last_v = None
    for (address, _kind, held, data), nxt in pairwise(cycles):
        if nxt[0] != HV:
            continue
        h = nxt[3] & 0xFF
        if mode == "status" and address == STATUS:
            bit = data & 4
            if last is not None and bit != last:
                found[("set " if bit else "clear ") + f"${h:02X}"] += 1
            last = bit
            vbit = data & 8  # VBLANK, by the V and H read next
            if last_v is not None and vbit != last_v:
                where = f"V${nxt[3] >> 8:02X} H${h:02X}"
                found[("vblank set " if vbit else "vblank clear ") + where] += 1
            last_v = vbit
        elif mode != "status" and address == DATA and held > 0:
            found[f"${h:02X}"] += 1
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mode", choices=sorted(MODES))
    parser.add_argument("--display-off", action="store_true")
    parser.add_argument("--pal", action="store_true", help="a PAL console, 313 lines")
    parser.add_argument("--frames", type=int, default=4, help="frames measured")
    parser.add_argument("--v30", action="store_true", help="30 cell rows (with --pal)")
    args = parser.parse_args()
    global FRAME_MCLK, LAST
    if args.pal:
        PAL["on"] = True
        FRAME_MCLK = 313 * 3420
    PAL["v30"] = args.v30
    LAST = FIRST + args.frames
    rom = cartridge(args.mode, not args.display_off)
    for name, cycles in (("referee", referee(rom)), ("board", board(rom))):
        found = placed(cycles, args.mode)
        common = max(found.values(), default=0) / 10
        print(f"{name}: {sum(found.values())} placed;",
              ", ".join(f"{k} {n}" for k, n in sorted(found.items())
                        if n >= common or k.startswith("vblank")))  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
