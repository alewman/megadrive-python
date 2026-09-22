#!/usr/bin/env python3
"""Run Nemesis's sprite masking and overflow test ROM and compare it with his MD1600.

The ROM (2009; forum t=541 start=30; ``tests/test_roms/nemesis/SpriteMaskingTestRom.bin``)
is not self-checking: it shows nine tests of the sprite limits, the dot overflow and
masking, each with two result cells drawn by sprites over a background. A cell shows a
green tick or "PASS" when the VDP behaves as the hardware does and a red cross or
"FAIL" when it does not, because the sprites that would draw the red cross are the ones
the limits and the masking must drop.

The judge is what Nemesis photographed on his MD1600 (hardware-captured):
``MD1600SpriteMaskH32.png`` and ``MD1600SpriteMaskH40.png``, linked from his post and
fetched into ``reference/images/`` (ignored, like everything else fetched). Both
captures contain **no red pixel at all** in the result block, so on the hardware all
nine tests pass in both modes. This script classifies each of the 9 x 2 cells of the
board's frame and of the capture (its result block found by its own colours and mapped
on to the board's) and reports every cell where the two disagree.

    python scripts/run_sprite_masking.py            # both modes, a line per test
    python scripts/run_sprite_masking.py --png out  # also write out-h32.png, out-h40.png

The mode switch: the ROM polls bit 5 of $A10003 without making TH an output, so on a
3-button pad it is reading C rather than Start ("PRESS START BUTTON" notwithstanding).
The harness presses both.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from megadrive_python.cartridge import Cartridge  # noqa: E402
from megadrive_python.io import START, C  # noqa: E402
from megadrive_python.machine import MACHINES  # noqa: E402
from megadrive_python.png import read_png, write_png  # noqa: E402
from megadrive_python.system import System  # noqa: E402

ROM = ROOT / "tests" / "test_roms" / "nemesis" / "SpriteMaskingTestRom.bin"
CAPTURES = ROOT / "reference" / "images"
TESTS = (
    "max sprites per line",
    "max sprite dots - basic",
    "max sprite dots - complex",
    "sprite mask",
    "sprite mask in slot 1",
    "mask in slot 1 on dot overflow",
    "mask S1, X=1 (mode 2 must not exist)",
    "mask S1 X=$40, S2 X=0",
    "max sprites per frame",
)
#: Where the result block sits in the board's frame: its left edge (the ROM puts it at
#: the same pixel in both modes), its top line, and the size of one cell. Taken from a
#: rendered frame's coloured bounding box, and fixed here so that a run whose cells are
#: all blank is still read at the right place.
RESULT_LEFT = {256: 216, 320: 216}
RESULT_TOP = 48
CELL_WIDTH, CELL_HEIGHT = 16, 8


def is_red(pixel: int) -> bool:
    return (pixel >> 16) & 0xFF > 110 and (pixel >> 8) & 0xFF < 90 and pixel & 0xFF < 90


def is_green(pixel: int) -> bool:
    return (pixel >> 8) & 0xFF > 110 and (pixel >> 16) & 0xFF < 90 and pixel & 0xFF < 90


def classify(rows: list[list[int]], left: float, top: float, width: float,
             height: float) -> str:  # fmt: skip
    """ "pass", "fail" or "blank" for one result cell of a picture."""
    red = green = 0
    for y in range(int(top), int(top + height)):
        for x in range(int(left), int(left + width)):
            pixel = rows[y][x]
            red += is_red(pixel)
            green += is_green(pixel)
    if red > green:
        return "fail"
    return "pass" if green else "blank"


def board_frame(machine: str, mode: str, png: Path | None) -> list[list[int]]:
    """The ROM's screen in ``mode`` ("h32" or "h40") as the board draws it."""
    system = System(MACHINES[machine], Cartridge(ROM.read_bytes()))
    pad = system.board.io.ports[0].device
    for _ in range(12):
        system.run_frame()
    if mode == "h40":
        pad.pressed = START | C
        for _ in range(6):
            system.run_frame()
        pad.pressed = 0
        for _ in range(20):
            system.run_frame()
    rows = system.vdp.renderer.frame_rgb()
    if png is not None:
        write_png(png, rows)
    return rows


def capture_block(mode: str) -> tuple[list[list[int]], float, float, float, float]:
    """The MD1600 capture and its result block (left, top, cell width, cell height).

    The block is found by its own pixels: the capture is an analogue frame of unknown
    scale, so the nine rows and two columns are taken from the bounding box of the
    coloured result area rather than from fixed coordinates.
    """
    rows = read_png(CAPTURES / f"MD1600SpriteMask{mode.upper()}.png")
    points = [
        (x, y)
        for y, row in enumerate(rows)
        for x, pixel in enumerate(row)
        if is_red(pixel) or is_green(pixel)
    ]
    if not points:
        raise SystemExit(f"{mode}: no result block found in the capture")
    left = min(x for x, _ in points)
    right = max(x for x, _ in points)
    top = min(y for _, y in points)
    bottom = max(y for _, y in points)
    return rows, left, top, (right + 1 - left) / 2, (bottom + 1 - top) / 9


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--machine", default="genesis", choices=sorted(MACHINES))
    parser.add_argument("--png", type=Path, help="write <arg>-h32.png and <arg>-h40.png")
    args = parser.parse_args()
    started = time.perf_counter()
    differences = 0
    for mode in ("h32", "h40"):
        width = 256 if mode == "h32" else 320
        png = Path(f"{args.png}-{mode}.png") if args.png else None
        frame = board_frame(args.machine, mode, png)
        if len(frame[0]) != width:
            raise SystemExit(f"{mode}: the board is showing {len(frame[0])} pixels a line")
        capture, left, top, cell_w, cell_h = capture_block(mode)
        print(f"-- {mode.upper()}: the board, then Nemesis's MD1600 capture")
        for index, name in enumerate(TESTS):
            board = [
                classify(
                    frame,
                    RESULT_LEFT[width] + cell * CELL_WIDTH,
                    RESULT_TOP + index * CELL_HEIGHT,
                    CELL_WIDTH,
                    CELL_HEIGHT,
                )
                for cell in range(2)
            ]
            hardware = [
                classify(capture, left + cell * cell_w, top + index * cell_h, cell_w, cell_h)
                for cell in range(2)
            ]
            agree = board == hardware
            differences += not agree
            print(f"{index + 1:>2}. {name:38} {board[0]:5} {board[1]:5}   "
                  f"{hardware[0]:5} {hardware[1]:5}   {'' if agree else 'DIFFERS'}")  # fmt: skip
    print(f"{differences} of 18 result cells differ from the MD1600 captures; "
          f"{time.perf_counter() - started:.0f} s, {sys.implementation.name} "
          f"{sys.version.split()[0]}")  # fmt: skip
    return 0 if differences == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
