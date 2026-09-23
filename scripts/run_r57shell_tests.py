#!/usr/bin/env python3
"""r57shell's VDP tests, which the 6-button pad unblocks, and what can judge them.

Milestones 3 and 5 could not get past this ROM's first screen and wrote that down as
"it polls the pad seven times a frame, which is the 6-button handshake". That was the
right observation and the wrong conclusion: the polling is the handshake, but the ROM
is navigated with the **D-pad**, as its author says in the thread ("even if you don't
see screen, you can change tests by arrows keys on pad", forum t=1585), and RIGHT
moves through its eight screens with a 3-button pad just as well as with a 6-button
one. This script walks them and measures each.

**What can judge them, exactly.** The thread (``reference/forum/spritesmind-t1585.html``)
is the only hardware evidence, and it is thin. r57shell himself never ran the ROM on
hardware ("I didn't because I can't do that") and asked for results; the one report is
MrTamk1s's, on **an MD2 VA1.8 with a Sega 315-5708-01 ASIC** (photographed;
``reference/images/t1585-18.jpg``) plus a 32X, captured to a VCR and watched on two
CRTs. What he wrote is one sentence about one test:

    "Interestingly, VDP Redraw Test #4 acted differently on my smaller CRT and on the
    Big CRT. On the smaller, the screen completely rolled over, while on the larger,
    only the first few rows were flickering/rolling."

His video is on YouTube and is not pinned; his three photographs are (two are of the
console and the VCR, one is a CRT too dark and too small to read a test off). So the
MD2 report judges **test 4 and nothing else**, and it judges it as "the first rows
move" -- a statement about a CRT losing and regaining sync, which a frame buffer
cannot show at all.

For the other seven the ROM prints its own expectation ("You MUST SEE ...", "You MUST
NOT SEE ..."), which r57shell wrote from what he believed and explicitly wanted
confirmed. Those are checked here mechanically -- a sphere either is drawn or is not,
a screen either changes between frames or does not -- and reported as what they are:
the ROM's author's expectation, not a hardware capture.

    python scripts/run_r57shell_tests.py
    python scripts/run_r57shell_tests.py --png /tmp/r57
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from megadrive_python.cartridge import Cartridge  # noqa: E402
from megadrive_python.io import RIGHT, Pad6  # noqa: E402
from megadrive_python.machine import MACHINES  # noqa: E402
from megadrive_python.png import write_png  # noqa: E402
from megadrive_python.system import System  # noqa: E402

ROM = ROOT / "tests" / "test_roms" / "r57shell" / "vdp_tests.bin"

#: The ROM's eight screens, in the order RIGHT walks them, with the expectation it
#: prints on each (its strings are in the image; ``strings vdp_tests.bin`` shows them).
SCREENS = (
    ("VDP Redraw Time Test 1", "MUST SEE right column of spheres"),
    ("VDP Redraw Time Test 2", "MUST NOT SEE right column of spheres"),
    ("VDP Redraw Time Test 3", "MUST NOT SEE glitches"),
    ("VDP Redraw Time Test 4", "MUST NOT SEE blinking of whole screen, only first rows"),
    ("VDP Blink Test (Reg 0 bit 0)", "MUST SEE blinking of whole screen"),
    ("VDP Colors Test 1", "MUST SEE only same colors within solid block"),
    ("VDP Colors Test 2", "(interactive: shadow/highlight priority)"),
    ("VDP Colors Test 3", "(interactive: shadow/highlight priority)"),
)
#: Where the ROM's own text ends and its pictures begin, in lines.
TEXT_LINES = 40


def measure(system: System, frames: int) -> str:
    """Run ``frames`` frames and say what the screen did over them.

    The first four tests switch the display mode inside the frame, which is the whole
    point of them, so a frame is not always the same size as the one before it and
    "how many pixels changed" only means something between two frames of one size.
    What is reported is therefore: the sizes seen, how many pixels of each frame are
    not its background colour in the right 40 columns and below the ROM's own text,
    and how many consecutive same-size pairs differ.
    """
    sizes: dict[tuple[int, int], int] = {}
    right: list[int] = []
    below: list[int] = []
    pairs = differing = 0
    previous: list[list[int]] | None = None
    for _frame in range(frames):
        system.run_frame()
        rows = system.vdp.renderer.frame_rgb()
        height, width = len(rows), len(rows[0])
        sizes[(width, height)] = sizes.get((width, height), 0) + 1
        counts: dict[int, int] = {}
        for row in rows:
            for pixel in row:
                counts[pixel] = counts.get(pixel, 0) + 1
        background = max(counts, key=lambda pixel: counts[pixel])
        right.append(sum(
            1 for y in range(height) for x in range(max(0, width - 40), width)
            if rows[y][x] != background
        ))  # fmt: skip
        below.append(sum(
            1 for y in range(TEXT_LINES, height) for x in range(width)
            if rows[y][x] != background
        ))  # fmt: skip
        if previous is not None and len(previous) == height and len(previous[0]) == width:
            pairs += 1
            differing += any(
                rows[y][x] != previous[y][x] for y in range(height) for x in range(width)
            )
        previous = rows
    shape = ", ".join(f"{w}x{h} on {n} frames" for (w, h), n in sorted(sizes.items()))
    return (f"{shape}; right 40 columns {min(right)}-{max(right)} pixels drawn, "
            f"below the text {min(below)}-{max(below)}; "
            f"{differing} of {pairs} same-size frame pairs differ")  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--png", type=Path, help="write each screen here")
    parser.add_argument("--frames", type=int, default=12, help="frames measured per screen")
    args = parser.parse_args(argv)
    if not ROM.exists():
        print(f"{ROM} is not fetched")
        return 1

    started = time.perf_counter()
    pad = Pad6()
    system = System(MACHINES["genesis"], Cartridge.from_image(ROM.read_bytes()), pads=(pad, None))
    system.board.fm.enabled = False
    for _frame in range(90):
        system.run_frame()
    if args.png:
        args.png.mkdir(parents=True, exist_ok=True)
    for index, (title, expectation) in enumerate(SCREENS):
        if index:
            pad.pressed = RIGHT
            for _frame in range(6):
                system.run_frame()
            pad.pressed = 0
            for _frame in range(24):
                system.run_frame()
        report = measure(system, args.frames)
        if args.png:
            write_png(args.png / f"{index}.png", system.vdp.renderer.frame_rgb())
        print(f"{index}: {title}")
        print(f"     the ROM says: {expectation}")
        print(f"     board: {report}")
    print(f"({time.perf_counter() - started:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
