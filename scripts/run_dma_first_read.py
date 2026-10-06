#!/usr/bin/env python3
"""When a 68000-to-VDP transfer from work RAM makes its first read, on Nuked-MD and on
the board.

A test cartridge sets up a 16-word transfer from $FF0000 to CRAM and starts it, over and
over, with a ``dbra`` delay that grows by one pass each time (and wraps at 64), so the
trigger write walks through every phase of the line and of the transfer's DMA ticks.
Printed for each machine: how many transfers made their first read how long after the
trigger write's /AS -- on the die its first memory select (``NMD_TRACE_MASK`` bit 3),
on the board its first read. The die's selects come 5 MCLK after the board's DMA ticks
(every cartridge transfer of the Overdrive 2 boot), so the two sets should be that far
apart and no more (docs/worklog.md, "The first read from work RAM").

    python scripts/run_dma_first_read.py [--display-on]
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


def cartridge(display: bool) -> bytes:
    """The test cartridge, hand-assembled; each encoding is in its comment."""
    code = bytearray()
    code += _w(0x46FC, 0x2700)  # move.w #$2700,sr
    code += bytes.fromhex("23fc5345474100a14000")  # move.l #'SEGA',$A14000
    code += _w(0x43F9, 0x00C0, 0x0004)  # lea $C00004,a1 (control)
    for reg, value in ((0x00, 0x04), (0x01, 0x54 if display else 0x14), (0x0C, 0x81),
                       (0x0F, 0x02)):  # fmt: skip
        code += _w(0x32BC, 0x8000 | reg << 8 | value)  # move.w #$8rvv,(a1); DMA enabled
    code += _w(0x7400)  # moveq #0,d2
    loop = len(code)
    for word in (0x9310, 0x9400, 0x9500, 0x9680, 0x977F):  # 16 words from $FF0000
        code += _w(0x32BC, word)  # move.w #word,(a1)
    code += _w(0x22BC, 0xC000, 0x0080)  # move.l #$C0000080,(a1): CRAM 0, the transfer
    code += _w(0x5242, 0x0242, 0x003F, 0x3602)  # addq.w #1,d2; andi.w #63,d2; move.w d2,d3
    code += _w(0x4E71, 0x51CB, 0xFFFC)  # nop; dbra d3,*-2
    code += _w(0x6000, (loop - (len(code) + 2)) & 0xFFFF)  # bra.w back
    return nuked_md_cartridge(bytes(code))


def referee(rom: bytes, frames: int) -> collections.Counter:
    """The die's first memory select after each trigger write, from the write's /AS."""
    nuked_md.ensure_built()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "rom.bin").write_bytes(rom)
        env = dict(os.environ, NMD_STOP=str((2 + frames) * PAL_FRAME_MCLK * 2),
                   NMD_TRACE=str(work / "t.bin"), NMD_TRACE_MASK="9",
                   NMD_TRACE_FROM=str(2 * PAL_FRAME_MCLK * 2))  # fmt: skip
        subprocess.run(["nice", str(nuked_md.BINARY), "-pal", str(work / "rom.bin")], env=env,
                       cwd=work, check=True, capture_output=True)  # fmt: skip
        events = nuked_md.read_trace(work / "t.bin")
    triggers = [e.t for e in events if e.type == nuked_md.EV_M68K and e.by_m68k and not e.read
                and (e.addr & 0xFFFFFE) == 0xC00006]  # fmt: skip
    selects = sorted(e.t for e in events if e.type == nuked_md.EV_MEM and e.bus_granted)
    found: collections.Counter = collections.Counter()
    for t in triggers:
        i = bisect.bisect_right(selects, t)
        if i < len(selects):
            found[selects[i] - t] += 1
    return found


def board(rom: bytes, frames: int) -> collections.Counter:
    """The board's first read after each trigger write, from the write's start."""
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES
    from megadrive_python.system import System
    from megadrive_python.vdp import VDP

    found: collections.Counter = collections.Counter()
    started: dict[str, int | None] = {"at": None}
    run_transfer, transfer_read = VDP._run_transfer, VDP._transfer_read

    def logged_run(self: VDP, mclk: int) -> int:
        started["at"] = mclk
        return run_transfer(self, mclk)

    def logged_read(self: VDP) -> None:
        if started["at"] is not None:
            found[self.transfer_next_read + self._read_inc - started["at"]] += 1
            started["at"] = None
        transfer_read(self)

    VDP._run_transfer, VDP._transfer_read = logged_run, logged_read
    try:
        system = System(MACHINES["nuked-md"], Cartridge(rom))
        system.board.fm.enabled = False
        while system.mclk < frames * PAL_FRAME_MCLK:
            system.step()
    finally:
        VDP._run_transfer, VDP._transfer_read = run_transfer, transfer_read
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--display-on", action="store_true")
    parser.add_argument("--frames", type=int, default=6)
    args = parser.parse_args()
    rom = cartridge(args.display_on)
    for name, found in (("referee", referee(rom, args.frames)), ("board", board(rom, args.frames))):
        common = [t for t, n in found.items() if n >= max(found.values()) / 10]
        print(f"{name}: {sum(found.values())} transfers; first read {min(common)}-{max(common)} "
              f"MCLK after the trigger's /AS for most (all: {min(found)}-{max(found)})")  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
