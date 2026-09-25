#!/usr/bin/env python3
"""When a 2-cell column's vertical scroll is taken, measured on the die model.

The full-screen scroll is latched once a line at the end of the line before
(``run_vscroll_latch.py``). In 2-cell column mode each column has its own word, and
nothing hardware-captured says when it is read: BlastEm latches it at the column's
fetch and says "Further research on vscroll latch behavior" is needed (``vdp.c``
1375), Nemesis "believe[d]" it is read for the next column and had not tested it
(forum t=1291). So Nuked-MD judges.

A cartridge built here: plane A (or B) red over its top 128 rows, green below; 2-cell
column scroll; all 40 VSRAM words 0; on each line the 68000 waits (V & 63) ``dbra``
loops, sweeping across the line, then writes the plane's word for one column k as
``(V & 1) << 7``. Only that word ever changes, so the die's column-scroll latch
(``l185``) takes 128 exactly when it latches column k (``NMD_VDPSIG``, with
``NMD_VSRAM_WATCH``), and the column's colour in the picture (``NMD_PLOT``) says which
value each line used.

For each plane, mode and column it prints the die H counter of the latch, how many
lines show the value latched on their own counter line, and the board's lines that
match the referee's and that split the column in two.

    python scripts/run_vscroll_columns.py
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
RED, GREEN = (255, 0, 0), (0, 255, 0)
CASES = [("A", True, 0), ("A", True, 10), ("A", True, 19), ("B", True, 0), ("B", True, 10),
         ("A", False, 0), ("A", False, 8), ("A", False, 15), ("B", False, 8)]  # fmt: skip


def cartridge(k: int, plane: str, h40: bool) -> bytes:
    code = bytearray()
    code += _w(0x46FC, 0x2700)
    code += bytes.fromhex("23fc5345474100a14000")
    code += _w(0x41F9, 0x00C0, 0x0000, 0x43F9, 0x00C0, 0x0004, 0x45F9, 0x00C0, 0x0008)
    for reg, value in ((0x00, 0x04), (0x01, 0x04), (0x02, 0x30 if plane == "A" else 0x38),
                       (0x04, 0x07 if plane == "A" else 0x06),
                       (0x05, 0x78), (0x07, 0x00), (0x0A, 0xFF), (0x0B, 0x04),
                       (0x0C, 0x81 if h40 else 0x00), (0x0D, 0x3F), (0x0F, 0x02),
                       (0x10, 0x01), (0x11, 0x00), (0x12, 0x00)):  # fmt: skip
        code += _w(0x32BC, 0x8000 | reg << 8 | value)
    code += _w(0x22BC, 0xC000, 0x0000)
    code += _w(0x30BC, 0x0000, 0x30BC, 0x000E, 0x30BC, 0x00E0)
    code += _w(0x22BC, 0x4000, 0x0000, 0x303C, 0x7FFF)  # clear VRAM
    code += _w(0x30BC, 0x0000, 0x51C8, 0xFFFA)
    code += _w(0x22BC, 0x4020, 0x0000)
    code += _w(0x303C, 0x000F, 0x30BC, 0x1111, 0x51C8, 0xFFFA)
    code += _w(0x303C, 0x000F, 0x30BC, 0x2222, 0x51C8, 0xFFFA)
    code += _w(0x22BC, 0x4000, 0x0003)
    code += _w(0x303C, 0x03FF, 0x30BC, 0x0001, 0x51C8, 0xFFFA)
    code += _w(0x303C, 0x03FF, 0x30BC, 0x0002, 0x51C8, 0xFFFA)
    code += _w(0x22BC, 0x4000, 0x0010, 0x303C, 0x0027)  # VSRAM 0, 40 words
    code += _w(0x30BC, 0x0000, 0x51C8, 0xFFFA)
    code += _w(0x32BC, 0x8144)
    code += _w(0x7800, 0x7200)
    address = 4 * k + (0 if plane == "A" else 2)  # the plane's word for column k
    loop = bytearray()
    loop += _w(0x1012, 0xB001, 0x67FA, 0x1200, 0x3400, 0x0242, 0x003F, 0x51CA, 0xFFFE)
    loop += _w(0x22BC, 0x4000 | address, 0x0010)
    loop += _w(0x1801, 0x0244, 0x0001, 0xEF4C, 0x3084)
    assert len(loop) == 34
    loop += _w(0x60DC)
    return nuked_md_cartridge(bytes(code + loop))


def referee(rom: bytes, k: int, plane: str, h40: bool) -> tuple[dict, dict, list]:
    """The column's colours by V, the die H counts of its latch, and per log frame the
    lines whose column k was latched as 128."""
    binary = ROOT / "reference" / "build-nuked-md" / "nukedmd-headless"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "rom.bin").write_bytes(rom)
        env = dict(os.environ, NMD_STOP=str(12 * FRAME * 2), NMD_PLOT=str(work / "p.bin"),
                   NMD_PLOT_FRAME="11", NMD_VDPSIG=str(work / "s.txt"),
                   NMD_VDPSIG_FROM="0", NMD_VDPSIG_TO=str(12 * FRAME * 2),
                   NMD_VSRAM_WATCH=str(2 * k + (0 if plane == "A" else 1)))  # fmt: skip
        subprocess.run(["nice", str(binary), str(work / "rom.bin")], env=env, cwd=work,
                       check=True, capture_output=True)  # fmt: skip
        plot = (work / "p.bin").read_bytes()
        sig = (work / "s.txt").read_text().splitlines()
    lines, samples = int.from_bytes(plot[4:8], "little"), int.from_bytes(plot[8:12], "little")
    counts = [int.from_bytes(plot[12 + 2 * i : 14 + 2 * i], "little") for i in range(lines)]
    base = 12 + 2 * lines
    vbase = base + lines * samples * 3
    vcount = [
        int.from_bytes(plot[vbase + 2 * i : vbase + 2 * i + 2], "little") for i in range(lines)
    ]
    first = 154 if h40 else 130
    column: dict[int, str] = {}
    for i in range(lines):
        if vcount[i] >= 224 or counts[i] < 600:
            continue
        seen = []
        for x in range(16 * k, 16 * k + 16):
            at = base + (i * samples + first + 2 * x) * 3
            pixel = tuple(plot[at : at + 3])
            seen.append("R" if pixel == RED else "G" if pixel == GREEN else "?")
        column[vcount[i]] = "".join(seen)
    latch_h: collections.Counter[int] = collections.Counter()
    frames: list[set[int]] = []
    current: set[int] = set()
    last_v = None
    for line in sig:
        field = line.split()
        if field[1] == "H":
            v = int(field[4])
            if last_v is not None and v < last_v and v < 5 and last_v > 200:
                frames.append(current)
                current = set()
            last_v = v
        elif field[1] == "L185" and int(field[2]) == 128:
            latch_h[int(field[3])] += 1
            current.add(int(field[4]))
    frames.append(current)
    return column, dict(latch_h), frames


def board(rom: bytes, k: int, column: dict) -> tuple[int, int, int]:
    """The board's best frame against the referee's column: (lines matching, frame,
    lines where the column is split)."""
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES
    from megadrive_python.system import System

    system = System(MACHINES["genesis"], Cartridge(rom))
    system.board.fm.enabled = False
    best = (0, 0, 0)
    for frame in range(1, 15):
        system.run_frame()
        if frame < 9:
            continue
        ours = {
            y: "".join("R" if p == 0xFF0000 else "G" if p == 0x00FF00 else "?"
                       for p in row[16 * k : 16 * k + 16])
            for y, row in enumerate(system.vdp.renderer.frame_rgb()[:224])
        }  # fmt: skip
        split = sum(1 for c in ours.values() if "R" in c and "G" in c)
        match = sum(1 for y, c in column.items() if ours.get(y) == c)
        best = max(best, (match, frame, split))
    return best


def main() -> int:
    failed = False
    for plane, h40, k in CASES:
        rom = cartridge(k, plane, h40)
        column, latch_h, frames = referee(rom, k, plane, h40)

        def colour(v: int, latched: set[int]) -> str:
            return "R" if ((v + (128 if v in latched else 0)) & 0xFF) < 128 else "G"

        own = max(sum(1 for v, c in column.items() if c[8] == colour(v, f)) for f in frames)
        split = sum(1 for c in column.values() if "R" in c and "G" in c)
        match, frame, board_split = board(rom, k, column)
        top = max(latch_h, key=latch_h.get)
        print(f"plane {plane} {'H40' if h40 else 'H32'} column {k:>2}: die latches at H "
              f"{top} ({latch_h[top]}x); line v shows counter line v's latch on {own}/224; "
              f"referee splits {split}; board {match}/224 as the referee (frame {frame}), "
              f"{board_split} split")  # fmt: skip
        failed |= own != 224 or split != 0 or board_split != 0
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
