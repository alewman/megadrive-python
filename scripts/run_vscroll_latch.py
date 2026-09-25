#!/usr/bin/env python3
"""When a full-screen vertical scroll takes effect, measured on the die model.

Nothing hardware-captured covers it. BlastEm latches once a line at one H40 slot and
says in its source it has not been tested ("TODO: Develop some tests on hardware to
see when vscroll latch actually happens for full plane mode", ``vdp.c`` 3611 at
542164d); Nemesis believed it is re-read at every mapping slot and had not tested it
either (forum t=1291). So by the tier rule (docs/validation.md) Nuked-MD, the
die-derived referee, judges.

A cartridge built here: plane A (or B) is red over its top 128 pixel rows and green
over the bottom 128 of a 64x32 plane, with full-screen scroll. The 68000 polls the V
counter; on each new line it waits (V & 63) ``dbra`` loops, which sweeps its write
across the whole line over 64 lines, then writes VSRAM word 0 (or 1) as
``(V & 1) << 7``. A value of 128 swaps red and green, so each line shows, pixel by
pixel, which value was in force. The value is fixed by V so that the result does not
depend on which line the loop happened to start on.

On the referee (``validation/nuked_md_headless.c``'s ``NMD_PLOT`` and ``NMD_VDPSIG``)
this checks, for planes A and B in H40 and H32:

- that no line changes colour part way along -- the scroll is latched once a line;
- the internal H counter at which the plane's latch (``l184`` for A, ``l185`` for B)
  changes; and
- that display line v shows the value latched on line v - 1.

Then it runs the same cartridge on the board and counts the lines that match the
referee's picture. That count is not expected to be 224: the board places a slot's
write a few pixels before the die does (the ``X=-3`` pipeline, not modelled:
docs/undocumented-behavior.md), which moves writes that land near a latch point.

    python scripts/run_vscroll_latch.py
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

import nuked_md  # noqa: E402

RED, GREEN = (255, 0, 0), (0, 255, 0)
FRAME_MCLK = 262 * 3420  # NTSC
PLOT_FRAME = 11  # the referee's frame to picture, counted in /VSYNC falls
BOARD_FRAMES = range(9, 15)  # board frames scored against it (the two clocks differ)


def _w(*words: int) -> bytes:
    return b"".join(x.to_bytes(2, "big") for x in words)


def cartridge(plane: str = "A", h40: bool = True) -> bytes:
    """The test cartridge, hand-assembled; each encoding is in its comment."""
    code = bytearray()
    code += _w(0x46FC, 0x2700)  # move.w #$2700,sr
    code += bytes.fromhex("23fc5345474100a14000")  # move.l #'SEGA',$A14000
    code += _w(0x41F9, 0x00C0, 0x0000)  # lea $C00000,a0 (data)
    code += _w(0x43F9, 0x00C0, 0x0004)  # lea $C00004,a1 (control)
    code += _w(0x45F9, 0x00C0, 0x0008)  # lea $C00008,a2 (HV counter)
    for reg, value in (
        (0x00, 0x04), (0x01, 0x04),  # mode 5, display off while loading
        (0x02, 0x30 if plane == "A" else 0x38),  # the plane under test at $C000,
        (0x04, 0x07 if plane == "A" else 0x06),  # the other at an empty table
        (0x05, 0x78), (0x07, 0x00),
        (0x0A, 0xFF), (0x0B, 0x00),  # full-screen H and V scroll
        (0x0C, 0x81 if h40 else 0x00), (0x0D, 0x3F), (0x0F, 0x02), (0x10, 0x01),  # 64x32
        (0x11, 0x00), (0x12, 0x00),
    ):  # fmt: skip
        code += _w(0x32BC, 0x8000 | reg << 8 | value)  # move.w #$8rvv,(a1)
    code += _w(0x22BC, 0xC000, 0x0000)  # move.l #$C0000000,(a1): CRAM write 0
    code += _w(0x30BC, 0x0000, 0x30BC, 0x000E, 0x30BC, 0x00E0)  # black, red, green
    code += _w(0x22BC, 0x4000, 0x0000)  # VRAM write 0
    code += _w(0x303C, 0x7FFF)  # move.w #$7FFF,d0
    code += _w(0x30BC, 0x0000, 0x51C8, 0xFFFA)  # clear: move.w #0,(a0); dbra d0,clear
    code += _w(0x22BC, 0x4020, 0x0000)  # VRAM write $0020: tiles 1 and 2
    code += _w(0x303C, 0x000F, 0x30BC, 0x1111, 0x51C8, 0xFFFA)  # tile 1: colour 1
    code += _w(0x303C, 0x000F, 0x30BC, 0x2222, 0x51C8, 0xFFFA)  # tile 2: colour 2
    code += _w(0x22BC, 0x4000, 0x0003)  # VRAM write $C000: the plane
    code += _w(0x303C, 0x03FF, 0x30BC, 0x0001, 0x51C8, 0xFFFA)  # rows 0-15: tile 1
    code += _w(0x303C, 0x03FF, 0x30BC, 0x0002, 0x51C8, 0xFFFA)  # rows 16-31: tile 2
    code += _w(0x22BC, 0x4000, 0x0010)  # VSRAM write 0
    code += _w(0x30BC, 0x0000, 0x30BC, 0x0000)
    code += _w(0x32BC, 0x8144)  # display on
    code += _w(0x7800, 0x7200)  # moveq #0,d4; moveq #0,d1
    loop = bytearray()
    loop += _w(0x1012)  # L0: move.b (a2),d0      the V counter
    loop += _w(0xB001)  # cmp.b d1,d0
    loop += _w(0x67FA)  # beq.s L0
    loop += _w(0x1200)  # move.b d0,d1
    loop += _w(0x3400)  # move.w d0,d2
    loop += _w(0x0242, 0x003F)  # andi.w #63,d2
    loop += _w(0x51CA, 0xFFFE)  # D: dbra d2,D
    loop += _w(0x22BC, 0x4000 if plane == "A" else 0x4002, 0x0010)  # VSRAM word 0 or 1
    loop += _w(0x1801)  # move.b d1,d4
    loop += _w(0x0244, 0x0001)  # andi.w #1,d4
    loop += _w(0xEF4C)  # lsl.w #7,d4          (V & 1) << 7
    loop += _w(0x3084)  # move.w d4,(a0)
    assert len(loop) == 34
    loop += _w(0x60DC)  # bra.s L0
    code += loop
    return nuked_md_cartridge(bytes(code))


def nuked_md_cartridge(code: bytes) -> bytes:
    rom = bytearray(0x10000)
    rom[0:4] = (0x00FFFE00).to_bytes(4, "big")
    rom[4:8] = (0x00000200).to_bytes(4, "big")
    for vector in range(2, 64):  # every exception halts, so a fault is loud
        rom[vector * 4 : vector * 4 + 4] = (0x000001F0).to_bytes(4, "big")
    rom[0x1F0:0x1F4] = bytes.fromhex("4e722700")  # stop #$2700
    rom[0x100:0x110] = b"SEGA GENESIS    "
    rom[0x200 : 0x200 + len(code)] = code
    return bytes(rom)


def _colour(y: int, value: int) -> tuple[int, int, int]:
    return RED if ((y + value) & 0xFF) < 128 else GREEN


def referee(rom: bytes, plane: str) -> dict:
    """Run the referee; return its picture's lines and its latch log."""
    nuked_md.ensure_built()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "rom.bin").write_bytes(rom)
        env = dict(
            os.environ,
            NMD_STOP=str(13 * FRAME_MCLK * 2),
            NMD_PLOT=str(work / "plot.bin"),
            NMD_PLOT_FRAME=str(PLOT_FRAME),
            NMD_VDPSIG=str(work / "sig.txt"),
            NMD_VDPSIG_FROM=str(10 * FRAME_MCLK * 2),
            NMD_VDPSIG_TO=str(13 * FRAME_MCLK * 2),
        )
        subprocess.run(["nice", str(nuked_md.BINARY), str(work / "rom.bin")], env=env,
                       cwd=work, check=True, capture_output=True)  # fmt: skip
        plot = (work / "plot.bin").read_bytes()
        sig = (work / "sig.txt").read_text().splitlines()
    lines, samples = int.from_bytes(plot[4:8], "little"), int.from_bytes(plot[8:12], "little")
    counts = [int.from_bytes(plot[12 + 2 * i : 14 + 2 * i], "little") for i in range(lines)]
    base = 12 + 2 * lines
    vbase = base + lines * samples * 3
    vcount = [
        int.from_bytes(plot[vbase + 2 * i : vbase + 2 * i + 2], "little") for i in range(lines)
    ]
    picture: dict[int, object] = {}
    for i in range(lines):
        if counts[i] < 600 or vcount[i] >= 224:
            continue
        seen = {
            tuple(plot[base + (i * samples + x) * 3 : base + (i * samples + x) * 3 + 3])
            for x in range(counts[i])
        }
        found = [c for c in (RED, GREEN) if c in seen]
        picture[vcount[i]] = found[0] if len(found) == 1 else "split"
    latch = "L184" if plane == "A" else "L185"
    at_h: collections.Counter[int] = collections.Counter()
    frames: list[dict[int, int]] = []
    current: dict[int, int] = {}
    value, last_v = 0, None
    for line in sig:
        field = line.split()
        if field[1] == latch:
            value = int(field[2])
            at_h[int(field[3])] += 1
        elif field[1] == "H":
            v = int(field[4])
            if last_v is not None and v < last_v and v < 5 and last_v > 200:
                frames.append(current)
                current = {}
            current[v] = value  # what the latch took on the line before v
            last_v = v
    frames.append(current)
    previous = max(sum(1 for y, c in picture.items() if y in f and _colour(y, f[y]) == c)
                   for f in frames)  # fmt: skip
    own = max(sum(1 for y, c in picture.items() if y + 1 in f and _colour(y, f[y + 1]) == c)
              for f in frames)  # fmt: skip
    return {"picture": picture, "latch_h": at_h, "previous": previous, "own": own}


def board(rom: bytes, picture: dict) -> tuple[int, int, int]:
    """The board's best frame against the referee's picture: (lines matching, frame,
    lines split)."""
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES
    from megadrive_python.system import System

    system = System(MACHINES["genesis"], Cartridge(rom))
    system.board.fm.enabled = False
    red, green = 0xFF0000, 0x00FF00
    best = (0, 0, 0)
    for frame in range(1, BOARD_FRAMES.stop):
        system.run_frame()
        if frame not in BOARD_FRAMES:
            continue
        lines: dict[int, object] = {}
        for y, row in enumerate(system.vdp.renderer.frame_rgb()[:224]):
            seen = {p for p in row if p in (red, green)}
            lines[y] = "split" if len(seen) == 2 else (RED if red in seen else GREEN)
        split = sum(1 for c in lines.values() if c == "split")
        match = sum(1 for y, c in picture.items() if lines.get(y) == c)
        best = max(best, (match, frame, split))
    return best


def main() -> int:
    failed = False
    for plane in "AB":
        for h40 in (True, False):
            name = f"plane {plane}, {'H40' if h40 else 'H32'}"
            rom = cartridge(plane, h40)
            ref = referee(rom, plane)
            split = sum(1 for c in ref["picture"].values() if c == "split")
            latch_h = ", ".join(f"${h:03X} x{n}" for h, n in ref["latch_h"].most_common(2))
            match, frame, board_split = board(rom, ref["picture"])
            print(f"{name}: referee latches at {latch_h}; {split} split lines; "
                  f"line v shows the latch of v-1 on {ref['previous']}/224, of v on "
                  f"{ref['own']}/224. Board: {board_split} split lines, "
                  f"{match}/224 as the referee (board frame {frame})")  # fmt: skip
            failed |= split != 0 or ref["previous"] != 224 or board_split != 0
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
