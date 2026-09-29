"""Set the board's frames against BlastEm's, pixel for pixel: which frames are the same
picture, and how far off the rest are.

Each board frame ``fNNNNN.png`` (``scripts/board_frames.py``) is scored against the
best of BlastEm's frames N-W..N+W (``scripts/blastem_frames.py``), because frame
numbers are not a common clock between two emulators (docs/handoff-2026-09-24.md):
Overdrive 2 runs one to six frames apart on the two. Three things are put right first,
each checked on Overdrive 2 (docs/worklog.md, Session 13):

- **Geometry**: BlastEm's frame keeps an 8-line top border in V28 and V30 alike, so
  board row y is BlastEm row y + 8; 232 rows at most are compared.
- **Levels**: the two DACs differ (the board's ramp is Plutiedev's measured one), so a
  BlastEm level is mapped to the board's level at the same step of the 15-step ramp,
  shadow and highlight included. A pixel counts as the same only if all three
  channels land on the same step.
- **Width**: an H32 frame is compared over its own width.

A frame BlastEm has no neighbour for is reported as such. With ``--pairs`` the
commonest (board, BlastEm) ramp steps of each differing frame are printed: a shade
fault shows as k against 2k (shadow against normal), a frame out of step as matched
pairs both ways.

    python scripts/compare_frames.py BOARD_DIR BLASTEM_DIR [--window 6] [--pairs]
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from megadrive_python.png import read_png
from megadrive_python.render import RAMP

#: BlastEm's output levels for the 15 ramp steps (``vdp.c``, ``levels[]``).
BLASTEM_LEVELS = (0, 27, 49, 71, 87, 103, 119, 130, 146, 157, 174, 190, 206, 228, 255)
BLASTEM_STEP = {level: step for step, level in enumerate(BLASTEM_LEVELS)}
BOARD_STEP = {level: step for step, level in enumerate(RAMP)}
TOP_BORDER = 8


def steps(pixel: int, table: dict[int, int]) -> tuple[int, int, int]:
    return tuple(table.get((pixel >> shift) & 0xFF, -1) for shift in (16, 8, 0))


def score(board: list[list[int]], blastem: list[list[int]], pairs: Counter | None) -> tuple:
    rows = min(len(board), len(blastem) - TOP_BORDER, 232)
    width = min(len(board[0]), len(blastem[0]))
    same = 0
    for y in range(rows):
        ours, theirs = board[y], blastem[y + TOP_BORDER]
        for x in range(width):
            a, b = steps(ours[x], BOARD_STEP), steps(theirs[x], BLASTEM_STEP)
            if a == b:
                same += 1
            elif pairs is not None:
                pairs[(a, b)] += 1
    return same, rows * width


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("board", type=Path)
    parser.add_argument("blastem", type=Path)
    parser.add_argument("--window", type=int, default=6)
    parser.add_argument("--pairs", action="store_true")
    args = parser.parse_args()
    cache: dict[int, list[list[int]] | None] = {}

    def reference(n: int):
        if n not in cache:
            path = args.blastem / f"f{n:05d}.png"
            cache[n] = read_png(path) if path.exists() else None
        return cache[n]

    identical = total = 0
    for path in sorted(args.board.glob("f*.png")):
        n = int(path.stem[1:])
        board = read_png(path)
        best = None
        for m in range(n - args.window, n + args.window + 1):
            ref = reference(m)
            if ref is not None:
                same, pixels = score(board, ref, None)
                if best is None or same > best[0]:
                    best = (same, pixels, m)
        total += 1
        if best is None:
            print(f"{n:6d}  no BlastEm frame within {args.window}")
            continue
        same, pixels, m = best
        identical += same == pixels
        line = f"{n:6d}  BlastEm {m:6d}  {same:6d} / {pixels}"
        if same != pixels and args.pairs:
            pairs: Counter = Counter()
            score(board, reference(m), pairs)
            line += "  " + ", ".join(f"{a}->{b} x{k}" for (a, b), k in pairs.most_common(3))
        print(line)
    print(f"{identical} of {total} frames identical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
