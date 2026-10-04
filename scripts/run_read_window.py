#!/usr/bin/env python3
"""How long Nuked-MD makes a data-port read wait, by where in the line it starts.

The read sweep cartridge (``run_read_slots.py``: bursts of CRAM reads, swept across the
line one ``dbra`` loop per line, the 68000's clock drifting 4 MCLK a line against the
VDP) runs on the die with its /HSYNC falls logged. Each read inside a burst is placed in
the board's line by the die's /HSYNC fall, which the VDPs' offset puts at board offset
327 in Overdrive 2's boot (docs/worklog.md, "The active-line slots"), and its wait is
tabulated by its start against the given slots' board offsets, one master clock at a
time -- the die-only view of the release and of windows like the one after the slot at
H $140 (docs/worklog.md, "The slot at $140").

    python scripts/run_read_window.py [OFFSET ...]     (default 3340, the slot at $140)
"""

from __future__ import annotations

import argparse
import bisect
import collections
import os
import subprocess
import sys
import tempfile
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

import nuked_md  # noqa: E402
import run_read_slots  # noqa: E402

HSYNC_AT = 327  # the board's line offset of the die's /HSYNC fall
LINE_MCLK = 3420
BURST_GAP = 200  # MCLK: reads closer than this are in one burst


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("offsets", nargs="*", type=int, default=[3340],
                        help="slot offsets in the board's line")  # fmt: skip
    parser.add_argument("--frames", type=int, default=34, help="frames traced")
    args = parser.parse_args()
    rom = run_read_slots.cartridge(True, True, reads=16, target="cram")
    nuked_md.ensure_built()
    first, last = 6, 6 + args.frames
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "rom.bin").write_bytes(rom)
        span = (first * run_read_slots.FRAME_MCLK * 2, last * run_read_slots.FRAME_MCLK * 2)
        env = dict(os.environ, NMD_STOP=str(span[1] + 1000), NMD_TRACE=str(work / "t.bin"),
                   NMD_TRACE_MASK="1", NMD_TRACE_FROM=str(span[0]),
                   NMD_TRACE_TO=str(span[1]), NMD_VDPSIG=str(work / "sig.txt"),
                   NMD_VDPSIG_FROM=str(span[0] - 20000), NMD_VDPSIG_TO=str(span[1]))  # fmt: skip
        subprocess.run(["nice", str(nuked_md.BINARY), str(work / "rom.bin")], env=env,
                       cwd=work, check=True, capture_output=True)  # fmt: skip
        cycles = nuked_md.m68k_cycles(nuked_md.read_trace(work / "t.bin"), dma=False)
        falls = [int(line.split()[0]) / 2 for line in (work / "sig.txt").read_text().splitlines()
                 if line.split()[1] == "H"]  # fmt: skip
    reads = [e for e in cycles if e.read and e.address & 0xFFFFFE == 0xC00000]
    by_start: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    for previous, read in pairwise(reads):
        if read.t - previous.t > BURST_GAP:
            continue  # the first of a burst may find the buffer full
        k = bisect.bisect_right(falls, read.t) - 1
        if k >= 0:
            offset = int((read.t - falls[k] + HSYNC_AT) % LINE_MCLK)
            by_start[offset][read.length - nuked_md.AS_LOW] += 1
    for slot in args.offsets:
        print(f"slot at board offset {slot}: read start minus the slot -> die's wait: reads")
        for rel in range(-40, 81):
            counts = by_start.get((slot + rel) % LINE_MCLK)
            if counts:
                print(f"  {rel:+4d}   {dict(sorted(counts.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
