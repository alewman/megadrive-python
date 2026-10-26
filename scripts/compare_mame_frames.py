#!/usr/bin/env python3
"""Compare the board's picture with MAME's exit frame, as a detector.

MAME is the weakest oracle here (docs/mame-oracle.md) and never a judge, but for a
whole game it is the only reference picture available: a divergence in a plane, the
window, a sprite or a colour shows up at once. Each trace directory
``scripts/mame_trace.py`` makes keeps MAME's exit snapshot at ``snap/exit.png``, taken
after ``--seconds`` emulated seconds with no input.

This runs the same cartridge on the board for the same emulated time and reports how
many pixels agree. It also scores the frames on either side, because MAME's exit frame
and the board's need not be the same field of an animation; the best offset is reported
beside the exact one. Colours are compared as they stand: MAME 0.285 builds its mode-5
palette from the same measured ramp this board uses (``video/315_5124.cpp``,
``sega315_5313_palette``: "non-linear (reference: ... t=2188)"), shadow and highlight
included.

    python scripts/compare_mame_frames.py tests/mame_traces/genesis-sonic1
    python scripts/compare_mame_frames.py tests/mame_traces/genesis-* --png /tmp/frames
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from megadrive_python.cartridge import Cartridge  # noqa: E402
from megadrive_python.machine import MACHINES, MAME_DRIVERS  # noqa: E402
from megadrive_python.png import read_png, write_png  # noqa: E402
from megadrive_python.system import System  # noqa: E402

IMAGE_SUFFIXES = (".md", ".gen", ".bin", ".smd")
MAME = "/usr/games/mame"
#: MAME's BIOS/system romsets (MAME_ROMPATH; MAME's own default if unset).
ROMPATH = os.environ.get("MAME_ROMPATH", "roms")


def record(trace: Path, seconds: int, driver: str, mame: str, rompath: str) -> Path:
    """Run MAME for ``seconds`` emulated seconds and keep its exit snapshot.

    The same run ``scripts/mame_trace.py`` makes, without the debugger: MAME writes a
    snapshot as it exits, which is the frame to compare against. Snapshots are kept in
    the trace directory (ignored), one per length.
    """
    import shutil
    import subprocess

    trace = trace.resolve()
    images = sorted(p for p in trace.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    if not images:
        raise SystemExit(f"{trace}: no cartridge image to record")
    target = trace / "snap" / f"{seconds}s.png"
    snapshots = trace / f"snap-{seconds}s"
    if snapshots.exists():
        shutil.rmtree(snapshots)
    subprocess.run(
        [mame, driver, "-cart", str(images[0]), "-rompath", rompath,
         "-homepath", str(trace), "-snapshot_directory", str(snapshots),
         "-video", "none", "-sound", "none", "-nothrottle", "-noreadconfig",
         "-skip_gameinfo", "-seconds_to_run", str(seconds)],
        cwd=trace, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    found = sorted(snapshots.glob("*/*.png"))
    if not found:
        raise SystemExit(f"{trace}: MAME wrote no snapshot")
    target.parent.mkdir(exist_ok=True)
    shutil.copyfile(found[0], target)
    shutil.rmtree(snapshots)
    return target


def agreement(board: list[list[int]], mame: list[list[int]]) -> float:
    """The fraction of pixels that are the same colour in both pictures."""
    height = min(len(board), len(mame))
    width = min(len(board[0]), len(mame[0]))
    if not height or not width:
        return 0.0
    equal = sum(board[y][x] == mame[y][x] for y in range(height) for x in range(width))
    return equal / (height * width)


def compare(trace: Path, seconds: int, window: int, machine_name: str, png: Path | None,
            snapshot: Path | None = None) -> tuple[float, float, int] | None:  # fmt: skip
    if snapshot is None:
        # A snapshot recorded for this length if there is one, else the trace's own
        # exit frame (``scripts/mame_trace.py`` keeps that one as ``snap/exit.png``).
        snapshot = trace / "snap" / f"{seconds}s.png"
        if not snapshot.exists():
            snapshot = trace / "snap" / "exit.png"
    images = sorted(p for p in trace.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    if not snapshot.exists() or not images:
        return None
    machine = MACHINES[machine_name]
    mame = read_png(snapshot)
    system = System(machine, Cartridge(images[0].read_bytes()))
    target = seconds * machine.mclk
    frame_mclk = 3420 * machine.lines_per_frame
    while system.mclk < target - window * frame_mclk:
        system.run_frame()
    exact = -1.0
    best = (0.0, 0)
    offset = -window
    while offset <= window:
        system.run_frame()
        fraction = agreement(system.vdp.renderer.frame_rgb(), mame)
        if fraction > best[0]:
            best = (fraction, offset)
        if exact < 0 and system.mclk >= target:
            exact = fraction
            if png is not None:
                png.mkdir(parents=True, exist_ok=True)
                write_png(png / f"{trace.name}.png", system.vdp.renderer.frame_rgb())
        offset += 1
    return exact, best[0], best[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("traces", nargs="+", type=Path)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--driver", default="genesis", choices=sorted(MAME_DRIVERS))
    parser.add_argument("--window", type=int, default=4, help="frames either side to try")
    parser.add_argument("--png", type=Path, help="write each board frame into this folder")
    parser.add_argument("--record", action="store_true",
                        help="run MAME first for --seconds and use that frame")  # fmt: skip
    parser.add_argument("--mame", default=MAME)
    parser.add_argument("--rompath", default=ROMPATH)
    args = parser.parse_args()
    started = time.perf_counter()
    for trace in args.traces:
        snapshot = None
        if args.record:
            snapshot = record(trace, args.seconds, args.driver, args.mame, args.rompath)
        result = compare(trace, args.seconds, args.window, MAME_DRIVERS[args.driver],
                         args.png, snapshot)  # fmt: skip
        if result is None:
            print(f"{trace.name:28} no snapshot or no cartridge image")
            continue
        exact, best, offset = result
        print(f"{trace.name:28} {exact * 100:6.2f} % of pixels at {args.seconds} s, "
              f"best {best * 100:6.2f} % ({offset:+d} frames)")  # fmt: skip
    print(f"{time.perf_counter() - started:.0f} s, {sys.implementation.name} "
          f"{sys.version.split()[0]}")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
