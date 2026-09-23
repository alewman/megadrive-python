#!/usr/bin/env python3
"""Milestone 8: Sega's own MD Soft Checker, read off the screen.

`MD Soft Checker (USA) (Proto) (Unl)` is Sega's factory diagnostic (its header:
"MD Soft Checker     Version 0.30"), so it is a **judge**, not a detector: it was
written by the people who made the machine and its verdicts are about the hardware,
not about an emulator. It is pinned in ``scripts/pinned_files.json`` from the local
No-Intro mirror, because the romset's own symlink for it dangles -- No-Intro dropped
the ``(v0.30)`` from the name (docs/worklog.md, milestone 7's decision 8).

What it does: it checks the 68000's registers and its whole instruction set, work
RAM, the Z80's bus request and its RAM, VRAM, CRAM and VSRAM, the I/O chip's
registers, every kind of DMA, and the VRAM fill and copy, printing one line per
check with its verdict; then it moves on to a pad screen naming the pad it finds on
each port. The verdicts are read here the way ``run_r57shell_tests.py`` reads its
screens -- off the rendered picture, not out of memory, because the ROM is a
proprietary program with no published result table:

- the result lines are the 8-pixel rows of green text below the white header;
- each line's verdict is the last two glyphs on it, so the 16x8 block of pixels
  ending at its rightmost lit column;
- every line whose verdict block equals the others' says the same thing, and what
  it says is read once by eye from ``--png`` and recorded in the work log ("OK").

    .venv-pypy/bin/python scripts/run_soft_checker.py
    .venv-pypy/bin/python scripts/run_soft_checker.py --machine genesis3 --png /tmp/mdsc
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from megadrive_python.cartridge import Cartridge, load_image  # noqa: E402
from megadrive_python.machine import MACHINES  # noqa: E402
from megadrive_python.png import write_png  # noqa: E402
from megadrive_python.system import System  # noqa: E402

ROM = ROOT / "tests" / "test_roms" / "sega" / "MD Soft Checker (USA) (Proto) (Unl).zip"
#: Where the ROM puts its report: the header's row, then one result line every 8
#: pixels. Measured from the picture itself (the bands are 7 pixels of an 8-pixel
#: line), not from the program.
HEADER_ROW = 64
FIRST_LINE = 72
LINE_HEIGHT = 8
VERDICT_WIDTH = 16
#: The report starts at the left margin; the pad screen's panels do not.
MARGIN = 48


def lines(picture: list[list[int]]) -> list[tuple[int, tuple]]:
    """(rightmost lit column, the verdict block) for each result line drawn.

    A result line is an 8-pixel band of text in one colour -- the ROM prints the
    whole report in one -- starting at the left margin, below the white header. The
    background is the commonest colour of the frame rather than a corner pixel: the
    ROM shows other screens (a yellow bar, the pad screen's panels) where a corner is
    not the backdrop.
    """
    counts: dict[int, int] = {}
    for row in picture:
        for pixel in row:
            counts[pixel] = counts.get(pixel, 0) + 1
    background = max(counts, key=lambda c: counts[c])
    found: list[tuple[int, tuple]] = []
    colour = None
    row = FIRST_LINE
    while row + LINE_HEIGHT <= len(picture):
        band = picture[row : row + LINE_HEIGHT]
        lit = [x for x in range(len(picture[0])) if any(r[x] != background for r in band)]
        drawn = {p for r in band for p in r if p != background}
        if not lit or len(drawn) != 1 or lit[0] > MARGIN:
            break
        if colour is None:
            colour = next(iter(drawn))
        elif drawn != {colour}:
            break
        right = lit[-1]
        found.append((right, tuple(tuple(r[right - VERDICT_WIDTH + 1 : right + 1]) for r in band)))
        row += LINE_HEIGHT
    return found


def run(machine_name: str, frames: int, png: Path | None) -> tuple[int, int, tuple | None]:
    image = load_image(ROM)
    system = System(MACHINES[machine_name], Cartridge.from_image(image))
    system.board.fm.enabled = False
    best: list[tuple[int, tuple]] = []
    picture: list[list[int]] = []
    for frame in range(1, frames + 1):
        system.run_frame()
        if frame < 60 or frame % 4:
            continue
        # The report is on screen until the ROM moves to its pad screen; keep the
        # frame with the most result lines, which is the complete report.
        current = lines(system.vdp.renderer.frame_rgb())
        if len(current) > len(best):
            best, picture = current, system.vdp.renderer.frame_rgb()
    if png is not None and picture:
        png.mkdir(parents=True, exist_ok=True)
        write_png(png / f"{machine_name}.png", picture)
    if not best:
        return 0, 0, None
    verdicts = {block for _, block in best}
    majority = max(verdicts, key=lambda b: sum(1 for _, x in best if x == b))
    return len(best), sum(1 for _, block in best if block == majority), majority


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--machine", action="append", choices=sorted(MACHINES))
    parser.add_argument("--frames", type=int, default=280)
    parser.add_argument("--png", type=Path, help="write each machine's report frame here")
    args = parser.parse_args()
    if not ROM.exists():
        raise SystemExit(f"{ROM} is missing: python scripts/fetch_references.py")
    started = time.perf_counter()
    machines = args.machine or sorted(MACHINES)
    reference = None
    for name in machines:
        count, agreeing, verdict = run(name, args.frames, args.png)
        if reference is None:
            reference = verdict
        same = (
            " (the same verdict as the first machine)"
            if verdict == reference
            else " (a DIFFERENT verdict glyph)"
        )
        print(f"{name:18} {count:3d} result lines, {agreeing:3d} with the same verdict"
              + (same if count else ""))  # fmt: skip
    print(f"{time.perf_counter() - started:.0f} s, {sys.implementation.name} "
          f"{sys.version.split()[0]}")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
