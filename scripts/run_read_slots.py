#!/usr/bin/env python3
"""Where in a line a data-port read can be served, measured on the die model.

The 68000 reads the data port from a read-ahead buffer the VDP fills in one of its
access slots; a read that finds the buffer empty waits for the next slot that fills
it (Exodus ``PerformReadCacheOperation``; the board's ``VDP._read_cache``). So a burst
of back-to-back reads, each emptying the buffer, ends one read per usable slot, and
where in the line those reads end maps the slots. Overdrive 2's start-up reads CRAM
that way in H40 with the display on, and the board catches a slot at the end of the
line the die never does (docs/worklog.md, "The access-timing table, and what the slip
turned into", item 6). Nothing hardware-captured covers it, so by the tier rule
(docs/validation.md) Nuked-MD, the die-derived referee, judges.

A cartridge built here: mode 5, the display on, a CRAM read address set. The 68000
polls the V counter; on each new line it waits (V & 63) ``dbra`` loops, which sweeps
the burst across the whole line over 64 lines, then makes 16 word reads of the data
port. On the referee (``validation/nuked_md_headless.c``: ``NMD_TRACE`` for the bus
cycles, ``NMD_VDPSIG`` for each /HSYNC fall with the die's H counter) each read's end
is placed in its line: the time since the line's /HSYNC fall plus where the board puts
that H counter, three below the die's in H40 (docs/undocumented-behavior.md VDP item
20). On the board the same cartridge runs and each read's end is taken from the VDP's
own line start. Both are printed as a table of where stalled reads end, which is the
slot map shifted by the read's own latency.

    python scripts/run_read_slots.py [--h32] [--display-off]
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
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import nuked_md  # noqa: E402
from run_vscroll_latch import _w, nuked_md_cartridge  # noqa: E402

FRAME_MCLK = 262 * 3420  # NTSC
FIRST, LAST = 6, 10  # the frames measured, after the set-up
READS = 16
BIN = 7  # MCLK: one 68000 clock


def cartridge(
    h40: bool = True, display: bool = True, reads: int = READS, target: str = "cram"
) -> bytes:
    """The test cartridge, hand-assembled; each encoding is in its comment."""
    code = bytearray()
    code += _w(0x46FC, 0x2700)  # move.w #$2700,sr
    code += bytes.fromhex("23fc5345474100a14000")  # move.l #'SEGA',$A14000
    code += _w(0x41F9, 0x00C0, 0x0000)  # lea $C00000,a0 (data)
    code += _w(0x43F9, 0x00C0, 0x0004)  # lea $C00004,a1 (control)
    code += _w(0x45F9, 0x00C0, 0x0008)  # lea $C00008,a2 (HV counter)
    for reg, value in (
        (0x00, 0x04), (0x01, 0x04), (0x02, 0x30), (0x04, 0x07), (0x05, 0x78),
        (0x07, 0x00), (0x0A, 0xFF), (0x0B, 0x00), (0x0C, 0x81 if h40 else 0x00),
        (0x0D, 0x3F), (0x0F, 0x02), (0x10, 0x01), (0x11, 0x00), (0x12, 0x00),
    ):  # fmt: skip
        code += _w(0x32BC, 0x8000 | reg << 8 | value)  # move.w #$8rvv,(a1)
    code += _w(0x22BC, 0xC000, 0x0000)  # move.l #$C0000000,(a1): CRAM write 0
    code += _w(0x303C, 0x003F, 0x30BC, 0x0ACE, 0x51C8, 0xFFFA)  # 64 words of $0ACE
    code += _w(0x32BC, 0x8144 if display else 0x8104)  # the display on (or off)
    low = {"cram": 0x0020, "vsram": 0x0010, "vram": 0x0000}[target]
    code += _w(0x22BC, 0x0000, low)  # move.l #$000000xx,(a1): a read of address 0
    code += _w(0x7200)  # moveq #0,d1
    loop = bytearray()
    loop += _w(0x1012)  # L0: move.b (a2),d0      the V counter
    loop += _w(0xB001)  # cmp.b d1,d0
    loop += _w(0x67FA)  # beq.s L0
    loop += _w(0x1200)  # move.b d0,d1
    loop += _w(0x3400)  # move.w d0,d2
    loop += _w(0x0242, 0x003F)  # andi.w #63,d2
    loop += _w(0x51CA, 0xFFFE)  # D: dbra d2,D
    loop += _w(0x3610) * reads  # move.w (a0),d3, back to back
    loop += _w(0x6000, (-(len(loop) + 2)) & 0xFFFF)  # bra.w L0
    code += loop
    return nuked_md_cartridge(bytes(code))


def referee(rom: bytes, h40: bool) -> list[tuple[int, int]]:
    """The die's (line position, stall) for every data-port read in the window."""
    from megadrive_python import vdp as vdp_module

    mode = vdp_module.H40 if h40 else vdp_module.H32
    lag = 3 if h40 else 0  # the die's H counter against the board's
    nuked_md.ensure_built()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "rom.bin").write_bytes(rom)
        span = (FIRST * FRAME_MCLK * 2, LAST * FRAME_MCLK * 2)
        env = dict(
            os.environ,
            NMD_STOP=str(span[1] + 1000),
            NMD_TRACE=str(work / "t.bin"), NMD_TRACE_MASK="1",
            NMD_TRACE_FROM=str(span[0]), NMD_TRACE_TO=str(span[1]),
            NMD_VDPSIG=str(work / "sig.txt"),
            NMD_VDPSIG_FROM=str(span[0] - 20000), NMD_VDPSIG_TO=str(span[1]),
        )  # fmt: skip
        subprocess.run(["nice", str(nuked_md.BINARY), str(work / "rom.bin")], env=env,
                       cwd=work, check=True, capture_output=True)  # fmt: skip
        events = nuked_md.read_trace(work / "t.bin")
        sig = (work / "sig.txt").read_text().splitlines()
    hsync = []  # (MCLK of the fall, where the board puts that H counter)
    for line in sig:
        field = line.split()
        if field[1] == "H":
            h = (int(field[3]) - lag) & 0x1FF
            if h in mode.offset_of:
                hsync.append((int(field[0]) // 2, mode.offset_of[h]))
    out = []
    k = 0
    for e in nuked_md.m68k_cycles(events, dma=False):
        if not e.read or e.address & 0xFFFFFE != 0xC00000:
            continue
        end = e.t + e.length
        while k + 1 < len(hsync) and hsync[k + 1][0] <= end:
            k += 1
        if hsync and hsync[k][0] <= end:
            fall, at = hsync[k]
            out.append(((end - fall + at) % mode.length, e.length - nuked_md.AS_LOW))
    return out


def board(rom: bytes) -> list[tuple[int, int, int]]:
    """The board's (line position, stall, V counter) for every data-port read in the
    window."""
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES
    from megadrive_python.system import System

    system = System(MACHINES["genesis"], Cartridge(rom))
    system.board.fm.enabled = False
    cpu, vdp = system.m68k, system.vdp
    out: list[tuple[int, int, int]] = []
    read = cpu._read_word

    def logged(address: int) -> int:
        start = system.board.m68k_access_clock()
        before = system.board.stall
        value = read(address)
        if address & 0xFFFFFE == 0xC00000 and FIRST * FRAME_MCLK <= start < LAST * FRAME_MCLK:
            stall = system.board.stall - before
            end = start + nuked_md.AS_LOW + stall
            out.append(((end - vdp.line_start) % vdp.hmode.length, stall, vdp.v))
        return value

    cpu._read_word = logged
    while system.mclk < LAST * FRAME_MCLK:
        system.step()
    return out


def table(reads: list[tuple]) -> dict[int, collections.Counter]:
    """Stalled reads by where they end, in one-clock bins."""
    by: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    for at, stall, *_ in reads:
        if stall > 0:
            by[at // BIN * BIN][stall // BIN * BIN] += 1
    return by


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--h32", action="store_true")
    parser.add_argument("--display-off", action="store_true")
    args = parser.parse_args()
    rom = cartridge(not args.h32, not args.display_off)
    die, ours = referee(rom, not args.h32), board(rom)
    dt, bt = table(die), table(ours)
    print(f"reads: referee {len(die)}, board {len(ours)}")
    print("where stalled reads end (MCLK into the board's line): referee | board")
    for at in sorted(set(dt) | set(bt)):
        d, b = sum(dt[at].values()), sum(bt[at].values())
        print(f"  {at:5d}  {d:5d} | {b:5d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
