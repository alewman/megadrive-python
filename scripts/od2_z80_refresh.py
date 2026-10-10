#!/usr/bin/env python3
"""Which cycle uses up the cartridge refresh when the Z80 is on the 68000's bus, on
Nuked-MD in Overdrive 2's boot.

The die's refresh grid (every 896 MCLK) is placed from the 68000's held cartridge
accesses: each falls due after the access before it and by the held one's start. Then
for every Z80 cycle on the 68000's bus with a tick from 28 MCLK before to 35 after it,
the table counts what the 68000's next access is and whether it was held -- by the
Z80 cycle's target and the tick's offset from its start (docs/worklog.md, "The refresh
a Z80 cycle uses up").

    python scripts/od2_z80_refresh.py [--span 58700000]
"""

from __future__ import annotations

import argparse
import bisect
import collections
import itertools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

import nuked_md  # noqa: E402
import od2_boot_drift as drift  # noqa: E402

TRACE = ROOT / "reference/z80-reset/t.bin"
PERIOD = 896  # board.CART_REFRESH_MCLK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--span", type=int, default=58_700_000)
    args = parser.parse_args()
    theirs = drift.referee(TRACE)
    first = drift.reset_anchor(theirs, 26_800_000)
    r0 = theirs[first][0]
    cycles = [(c[0] - r0, c[1], c[3]) for c in theirs[first:] if c[0] - r0 <= args.span]
    # The grid's phase: after the access before each held cartridge access, and by it.
    lo, hi = -PERIOD, PERIOD
    for (before, _, _), (t, address, held) in itertools.pairwise(cycles):
        if held == 14 and address < 0x800000:
            x, y = before % PERIOD, t % PERIOD
            if x > y:
                x -= PERIOD
            if y > PERIOD // 2:
                x, y = x - PERIOD, y - PERIOD
            lo, hi = max(lo, x), min(hi, y)
    print(f"the die's refresh falls due at a phase in ({lo}, {hi}] MCLK, mod {PERIOD}")
    z80 = [(e.t - r0, e.addr & 0xFFFFFE) for e in nuked_md.read_trace(TRACE)
           if e.type == nuked_md.EV_M68K and not e.by_m68k
           and 0 <= e.t - r0 <= args.span]  # fmt: skip
    times = [c[0] for c in cycles]
    table: collections.Counter = collections.Counter()
    for t, address in z80:
        last = hi + (t - hi) // PERIOD * PERIOD  # the last tick by the Z80 cycle's start
        for tick in (last, last + PERIOD):
            offset = tick - t
            i = bisect.bisect_right(times, t)  # the 68000's next access
            if -28 <= offset <= 35 and i < len(cycles) and cycles[i][0] - tick < 112:
                target = "cartridge" if address < 0x800000 else f"${address:06X}"
                after = "cartridge" if cycles[i][1] < 0x800000 else f"${cycles[i][1]:06X}"
                table[(target, offset, after, cycles[i][2])] += 1
    print(
        "Z80 cycle's target, tick from its start (MCLK), the 68000's next access, its hold: count"
    )
    for key in sorted(table):
        print(f"  {key[0]:>10} {key[1]:+4d}  {key[2]:>10} held {key[3]:2d}: {table[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
