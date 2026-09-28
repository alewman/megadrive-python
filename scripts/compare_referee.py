"""Set the board's frames against the referee's (Nuked-MD), frame by frame: the judge,
where ``scripts/compare_frames.py`` sets them against BlastEm, a detector.

The referee's frames come from a run with ``NMD_PICS`` (``validation/
nuked_md_headless.c``): each record of ``pics.z`` inflates to one frame's raw capture of
the VDP's RGB pins, a row per /HSYNC with the die's V counter. A display pixel x is
sample 154 + 2x in H40 (the offset ``scripts/run_output_delay.py`` measured), and the
pins' 0-255 levels are the 15 steps of the DAC ramp (normal colours on the even steps,
shadow and highlight between), so both sides are compared as ramp steps.

``--offset`` is referee frame minus board frame: -1 for Overdrive 2's aligned runs
(docs/worklog.md, Session 13). Each board frame is scored at that offset and at the
best offset within ``--window`` of it, so a picture a frame early or late shows as
such rather than as a difference.

    python scripts/compare_referee.py REFEREE_DIR BOARD_DIR OUT.csv [--offset -1] [--jobs 8]
"""

from __future__ import annotations

import argparse
import csv
import struct
import sys
import zlib
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from megadrive_python.png import read_png
from megadrive_python.render import RAMP

LINES, SAMPLES, FIRST, WIDTH, HEIGHT = 400, 2048, 154, 320, 224
BOARD_STEP = {level: step for step, level in enumerate(RAMP)}


def load_index(directory: Path) -> dict[int, tuple[int, int, int]]:
    raw = (directory / "pics.idx").read_bytes()
    index = {}
    for at in range(0, len(raw) - 23, 24):
        frame, offset, size, t = struct.unpack("<IQIQ", raw[at : at + 24])
        index[frame] = (offset, size, t)
    return index


def referee_frame(directory: Path, entry: tuple[int, int, int]) -> list | None:
    offset, size, _ = entry
    with (directory / "pics.z").open("rb") as file:
        file.seek(offset)
        body = zlib.decompress(file.read(size))
    counts = struct.unpack(f"<{LINES}H", body[: 2 * LINES])
    base = 2 * LINES
    vcount = struct.unpack(f"<{LINES}H", body[base + LINES * SAMPLES * 3 :])
    rows: dict[int, bytes] = {}
    for line in range(LINES):
        v = vcount[line]
        if v < HEIGHT and counts[line] >= FIRST + 2 * WIDTH and v not in rows:
            start = base + (line * SAMPLES + FIRST) * 3
            samples = body[start : start + WIDTH * 6]
            rows[v] = bytes(round(samples[6 * x + c] * 14 / 255) for x in range(WIDTH)
                            for c in range(3))  # fmt: skip
    return [rows.get(v) for v in range(HEIGHT)]


def board_frame(path: Path) -> list:
    rows = read_png(path)
    return [bytes(BOARD_STEP.get((p >> s) & 255, 99) for p in row for s in (16, 8, 0))
            for row in rows[:HEIGHT]]  # fmt: skip


def same(board: list, referee: list) -> int:
    count = 0
    for ours, theirs in zip(board, referee, strict=False):
        if theirs is None or len(ours) != len(theirs):
            continue
        count += sum(1 for x in range(0, len(ours), 3) if ours[x : x + 3] == theirs[x : x + 3])
    return count


def work(task: tuple) -> dict:
    referee_dir, board_dir, index, n, offset, window = task
    path = board_dir / f"f{n:05d}.png"
    row = {"frame": n, "at_offset": "", "best": "", "best_offset": "", "pixels": ""}
    if not path.exists():
        return row
    board = board_frame(path)
    pixels = sum(len(r) // 3 for r in board)
    row["pixels"] = pixels
    scores = {}
    for d in range(offset - window, offset + window + 1):
        if n + d in index:
            scores[d] = same(board, referee_frame(referee_dir, index[n + d]))
    if scores:
        best = max(scores, key=lambda d: (scores[d], d == offset))
        row.update(at_offset=scores.get(offset, ""), best=scores[best], best_offset=best)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("referee", type=Path)
    parser.add_argument("board", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--offset", type=int, default=-1)
    parser.add_argument("--window", type=int, default=3)
    parser.add_argument("--first", type=int, default=1)
    parser.add_argument("--last", type=int, default=10**9)
    parser.add_argument("--every", type=int, default=1)
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()
    index = load_index(args.referee)
    last = min(args.last, max(index) - args.offset - args.window)
    tasks = [(args.referee, args.board, index, n, args.offset, args.window)
             for n in range(args.first, last + 1, args.every)]  # fmt: skip
    with Pool(args.jobs) as pool, args.out.open("w", newline="") as file:
        writer = csv.DictWriter(file, ["frame", "at_offset", "best", "best_offset", "pixels"])
        writer.writeheader()
        for row in pool.imap(work, tasks, chunksize=4):
            writer.writerow(row)
    print(f"{len(tasks)} board frames scored into {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
