"""The board's picture of chosen frames, as ``fNNNNN.png``: the other half of
``scripts/blastem_frames.py``, for ``scripts/compare_frames.py``.

A frame is the VDP's count since power-on (the window's title bar shows the same
number). The FM generator is off unless ``--fm``: nothing a game can observe changes
(docs/worklog.md, milestone 4) and it is about three times faster.

    python scripts/board_frames.py ROM OUT --range 4100:5200:25 [--machine megadrive]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from megadrive_python.__main__ import load_image
from megadrive_python.cartridge import Cartridge
from megadrive_python.machine import MACHINES
from megadrive_python.png import write_png
from megadrive_python.system import System


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("rom", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--machine", default="megadrive", choices=sorted(MACHINES))
    parser.add_argument("--frames", default="", help="comma-separated frame numbers")
    parser.add_argument("--range", default="", help="first:last:step, inclusive")
    parser.add_argument("--fm", action="store_true", help="run the FM generator too")
    args = parser.parse_args()
    wanted = {int(n) for n in args.frames.split(",") if n}
    if args.range:
        first, last, step = (int(n) for n in args.range.split(":"))
        wanted |= set(range(first, last + 1, step))
    if not wanted:
        parser.error("no frames asked for")
    args.out.mkdir(parents=True, exist_ok=True)
    system = System(MACHINES[args.machine], Cartridge.from_image(load_image(args.rom)))
    system.board.fm.enabled = args.fm
    vdp = system.vdp
    while vdp.frame < max(wanted):
        system.run_frame()
        system.board.psg.take_samples()
        system.board.fm.take_samples()
        rows = vdp.renderer.frame_rgb()
        if vdp.frame in wanted and rows:
            write_png(args.out / f"f{vdp.frame:05d}.png", rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
