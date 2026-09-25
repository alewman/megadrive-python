#!/usr/bin/env python3
"""Where a mid-line change to the colour output shows, measured on the die model.

Kabuto places H40's second mid-tiles access slot "at the pixel with X=-3 (display area
coordinates)" for palette writes ("Access timing"): the colour output runs behind the
H counter. That is hardware-measured research for one slot; Nuked-MD, the die-derived
referee, gives every slot, both modes, and register writes too.

Two cartridges built here, both with the planes transparent so the display is the
backdrop. On each new line the 68000 waits (V & 63) ``dbra`` loops, sweeping across the
line over 64 lines, then changes the backdrop's colour one of two ways, fixed by V:

- ``cram``: writes CRAM entry 0 (the backdrop) -- $000E on even V, $008E on odd;
- ``reg7``: writes register 7, the backdrop's CRAM index -- 1 (red) or 2 (green).

For each line it finds the output pixel where the colour changes, on the referee
(``validation/nuked_md_headless.c``'s ``NMD_PLOT``; display pixel 0 is sample 154 in
H40 and 130 in H32, two samples a pixel) and on the board, and prints referee minus
board per line. For CRAM it also maps the die's H counter at the commit (``NMD_VDPSIG``,
``C0``) to the pixel where it shows. Whole-slot differences (multiples of 16) are the
68000's polling phase, which differs by a slot between the two runs on some lines.

    python scripts/run_output_delay.py
"""

from __future__ import annotations

import collections
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from run_vscroll_latch import _w, nuked_md_cartridge  # noqa: E402

FRAME = 262 * 3420
FIRST_SAMPLE = {True: 154, False: 130}


def cartridge(h40: bool = True, kind: str = "cram") -> bytes:
    code = bytearray()
    code += _w(0x46FC, 0x2700)
    code += bytes.fromhex("23fc5345474100a14000")
    code += _w(0x41F9, 0x00C0, 0x0000, 0x43F9, 0x00C0, 0x0004, 0x45F9, 0x00C0, 0x0008)
    for reg, value in ((0x00, 0x04), (0x01, 0x04), (0x02, 0x30), (0x04, 0x07),
                       (0x05, 0x78), (0x07, 0x00), (0x0A, 0xFF), (0x0B, 0x00),
                       (0x0C, 0x81 if h40 else 0x00), (0x0D, 0x3F), (0x0F, 0x02),
                       (0x10, 0x01), (0x11, 0x00), (0x12, 0x00)):  # fmt: skip
        code += _w(0x32BC, 0x8000 | reg << 8 | value)
    code += _w(0x22BC, 0x4000, 0x0000, 0x303C, 0x7FFF)  # clear VRAM
    code += _w(0x30BC, 0x0000, 0x51C8, 0xFFFA)
    code += _w(0x22BC, 0xC000, 0x0000, 0x30BC, 0x000E)  # CRAM 0 red
    code += _w(0x30BC, 0x000E, 0x30BC, 0x00E0)  # CRAM 1 red, 2 green (the reg 7 test)
    code += _w(0x32BC, 0x8144)  # display on
    code += _w(0x7800, 0x7200)
    loop = bytearray()
    loop += _w(0x1012, 0xB001, 0x67FA)  # L0: move.b (a2),d0; cmp.b d1,d0; beq.s L0
    loop += _w(0x1200, 0x3400, 0x0242, 0x003F)  # move.b d0,d1; move.w d0,d2; andi #63
    loop += _w(0x51CA, 0xFFFE)  # D: dbra d2,D
    if kind == "cram":
        loop += _w(0x22BC, 0xC000, 0x0000)  # CRAM write 0
        loop += _w(0x1801, 0x0244, 0x0001, 0xEF4C)  # d4 = (V & 1) << 7
        loop += _w(0x0044, 0x000E)  # ori.w #$E,d4
        loop += _w(0x3084)  # move.w d4,(a0)
        assert len(loop) == 38
        loop += _w(0x60D8)  # bra.s L0
    else:  # register 7, the backdrop's CRAM index: 1 (red) on even V, 2 (green) on odd
        loop += _w(0x1801, 0x0244, 0x0001)  # move.b d1,d4; andi.w #1,d4
        loop += _w(0x5244)  # addq.w #1,d4
        loop += _w(0x0044, 0x8700)  # ori.w #$8700,d4
        loop += _w(0x3284)  # move.w d4,(a1)
        assert len(loop) == 32
        loop += _w(0x60DE)  # bra.s L0
    return nuked_md_cartridge(bytes(code + loop))


def referee(rom: bytes, first: int):
    binary = ROOT / "reference" / "build-nuked-md" / "nukedmd-headless"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "rom.bin").write_bytes(rom)
        env = dict(
            os.environ,
            NMD_STOP=str(13 * FRAME * 2),
            NMD_PLOT=str(work / "p.bin"),
            NMD_PLOT_FRAME="11",
            NMD_VDPSIG=str(work / "s.txt"),
            NMD_VDPSIG_FROM=str(10 * FRAME * 2),
            NMD_VDPSIG_TO=str(13 * FRAME * 2),
        )
        subprocess.run(["nice", str(binary), str(work / "rom.bin")], env=env, cwd=work,
                       check=True, capture_output=True)  # fmt: skip
        plot = (work / "p.bin").read_bytes()
        sig = (work / "s.txt").read_text().splitlines()
    lines, samples = int.from_bytes(plot[4:8], "little"), int.from_bytes(plot[8:12], "little")
    counts = [int.from_bytes(plot[12 + 2 * i : 14 + 2 * i], "little") for i in range(lines)]
    base = 12 + 2 * lines
    vbase = base + lines * samples * 3
    vc = [int.from_bytes(plot[vbase + 2 * i : vbase + 2 * i + 2], "little") for i in range(lines)]
    change = {}  # V -> output pixel where the line's colour changes
    for i in range(lines):
        if vc[i] >= 224 or counts[i] < 600:
            continue
        row = [tuple(plot[base + (i * samples + x) * 3 : base + (i * samples + x) * 3 + 3])
               for x in range(counts[i])]  # fmt: skip
        for x in range(first + 1, first + 640):
            if row[x] != row[x - 1]:
                change[vc[i]] = (x - first) / 2
                break
    commit = collections.defaultdict(list)  # V -> die H of the CRAM commit
    for line in sig:
        f = line.split()
        if f[1] == "C0":
            commit[int(f[4])].append(int(f[3]))
    return change, commit


def board(rom: bytes):
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES
    from megadrive_python.system import System

    system = System(MACHINES["genesis"], Cartridge(rom))
    system.board.fm.enabled = False
    vdp = system.vdp
    commits = collections.defaultdict(list)
    for _ in range(12):
        system.run_frame()
    cram = vdp.cram

    class Rec(list):
        def __setitem__(self, i, v):
            if i == 0 and vdp.frame == 12:
                h = vdp.hmode.hvals[vdp.hmode.position(vdp.mclk - vdp.line_start)]
                commits[vdp.v].append(h)
            super().__setitem__(i, v)

    vdp.cram = Rec(cram)
    system.run_frame()
    rows = system.vdp.renderer.frame_rgb()
    change = {}
    for y, row in enumerate(rows[:224]):
        for x in range(1, len(row)):
            if row[x] != row[x - 1]:
                change[y] = x
                break
    return change, commits


def main() -> int:
    for h40 in (True, False):
        width = 320 if h40 else 256
        for kind in ("cram", "reg7"):
            rom = cartridge(h40, kind)
            ref_change, ref_commit = referee(rom, FIRST_SAMPLE[h40])
            board_change, _ = board(rom)
            lines = [y for y in range(224) if y in ref_change and y in board_change
                     and ref_change[y] < width and board_change[y] < width]  # fmt: skip
            diffs = collections.Counter(round(ref_change[y] - board_change[y]) for y in lines)
            name = f"{'H40' if h40 else 'H32'} {kind}"
            print(f"{name}: referee minus board, per line: {sorted(diffs.items())}")
            if kind == "cram":
                shown = collections.Counter(
                    round(ref_change[v] - ref_commit[v][0]) for v in ref_change
                    if ref_commit.get(v) and ref_change[v] < width
                )  # fmt: skip
                print(f"{name}: referee pixel minus die H of the commit: "
                      f"{sorted(shown.items())}")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
