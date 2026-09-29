"""The board's picture of chosen frames, as ``fNNNNN.png``: the other half of
``scripts/blastem_frames.py``, for ``scripts/compare_frames.py``.

A frame is the VDP's count since power-on (the window's title bar shows the same
number). The FM generator is off unless ``--fm``: nothing a game can observe changes
(docs/worklog.md, milestone 4) and it is about three times faster.

For a run to set against the referee's (``reference/nuked-od2``, docs/worklog.md
Session 13), three more things:

- ``--reset-mclk T`` presses the console's reset button at master clock T, stepping
  the 68000 up to it so it lands where the referee's did (Overdrive 2's referee run
  presses it at frame 25 of a PAL run, T = 25 x 1,070,460).
- ``--audio DIR`` records the sound chips' own output: ``fm.bin``, the YM2612's
  samples (one per 24 internal cycles, the sum of the 24 multiplexed DAC values) as
  int32 left, right; ``psg.bin``, the PSG's digital state per sample (four
  attenuations, one byte each). Needs ``--fm`` for the FM half.
- ``--snapshots DIR --snapshot-every N`` pickles the whole system every N frames, so a
  later look at one scene starts from the snapshot before it.

    python scripts/board_frames.py ROM OUT --range 4100:5200:25 [--machine megadrive]
    python scripts/board_frames.py ROM OUT --range 1:20400:1 --fm --reset-mclk 26761500 \\
        --audio OUT --snapshots OUT --snapshot-every 1000
"""

from __future__ import annotations

import argparse
import pickle
import struct
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
    parser.add_argument("--reset-mclk", type=int, default=-1, help="press reset here")
    parser.add_argument("--audio", type=Path, help="record fm.bin and psg.bin here")
    parser.add_argument("--snapshots", type=Path, help="pickle the system here")
    parser.add_argument("--snapshot-every", type=int, default=1000)
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
    fm_file = psg_file = None
    if args.audio:
        args.audio.mkdir(parents=True, exist_ok=True)
        fm_file = (args.audio / "fm.bin").open("wb")
        psg_file = (args.audio / "psg.bin").open("wb")
    if args.snapshots:
        args.snapshots.mkdir(parents=True, exist_ok=True)
    reset_pending = args.reset_mclk >= 0
    while vdp.frame < max(wanted):
        if reset_pending and system.mclk + 1_000_000 >= args.reset_mclk:
            # Step up to the press (inside this frame), then let the frame finish.
            while system.mclk < args.reset_mclk:
                system.step()
            system.press_reset()
            reset_pending = False
        system.run_frame()
        fm = system.board.fm.take_samples()
        if psg_file:
            psg_file.write(b"".join(bytes(a) for a in system.board.psg.take_attenuations()))
            fm_file.write(b"".join(struct.pack("<ii", left, right) for left, right in fm))
        else:
            system.board.psg.take_samples()
        rows = vdp.renderer.frame_rgb()
        if vdp.frame in wanted and rows:
            write_png(args.out / f"f{vdp.frame:05d}.png", rows)
        if args.snapshots and vdp.frame % args.snapshot_every == 0:
            with (args.snapshots / f"s{vdp.frame:05d}.pkl").open("wb") as file:
                pickle.dump(system, file)
    for file in (fm_file, psg_file):
        if file:
            file.close()
    return 0


if __name__ == "__main__":
    sys.setrecursionlimit(100_000)  # pickling the system goes deep
    raise SystemExit(main())
