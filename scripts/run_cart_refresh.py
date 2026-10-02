#!/usr/bin/env python3
"""What the cartridge refresh costs a read after the bus has been free, on Nuked-MD.

A test cartridge reads the cartridge densely (eight ``move.w (a0),d0`` a pass, a0 in
ROM), which places the die's own 896-MCLK refresh grid from the reads it holds, and
between bursts spends 70 clocks in a ``mulu.w #$FFFF`` with no bus cycle. For each read
after such a gap, printed: how long before it the refresh fell due ("late") and how long
the die held it -- the rule ``board.CART_REFRESH_SELF_MCLK`` models (docs/worklog.md,
"The cartridge refresh when the bus is free"). Die-only: nothing is lined up.

    python scripts/run_cart_refresh.py
"""

from __future__ import annotations

import argparse
import collections
import sys
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

import nuked_md as nmd  # noqa: E402
import run_nuked_md as rn  # noqa: E402

PERIOD = 896  # board.CART_REFRESH_MCLK
READ_ROM = bytes.fromhex("3010")  # move.w (a0),d0
MULU = bytes.fromhex("c2fcffff")  # mulu.w #$FFFF,d1: 70 clocks without a bus cycle


def cartridge() -> bytes:
    setup = rn.UNLOCK + bytes.fromhex("41f900000400")  # lea $400,a0 (ROM)
    return rn.cartridge(rn.loop(setup, READ_ROM * 8 + MULU))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--window", type=int, default=400_000, help="MCLK traced")
    args = parser.parse_args()
    span = (rn.REFEREE_RESET, rn.REFEREE_RESET + args.window)
    run = nmd.run(cartridge(), stop=span[1], trace=span, trace_mask=1)
    cycles = [(e.t, e.addr & 0xFFFFFE, e.length - nmd.AS_LOW) for e in run.m68k(dma=False)]
    # The grid: cartridge reads held 14 after a short gap come at or after a due time.
    held = [t for (pt, _, _), (t, a, h) in pairwise(cycles) if a < 0x800000 and h == 14
            and t - pt <= 70]  # fmt: skip
    phases = sorted(t % PERIOD for t in held)
    if phases[-1] - phases[0] >= PERIOD // 2:  # the grid wraps through 0
        phases = [p for p in phases if p > PERIOD // 2]
    due_phase = phases[0]
    print(f"{len(held)} reads held 14 after short gaps; the die's refresh falls due at "
          f"{due_phase} mod {PERIOD} (held reads seen at {sorted(set(phases))[:4]})")  # fmt: skip
    by_late: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    for (pt, _, _), (t, a, h) in pairwise(cycles):
        if a >= 0x800000 or t - pt < 100:
            continue
        late = (t - due_phase) % PERIOD
        if late < t - pt:  # it fell due inside the gap
            by_late[late // 7 * 7][h] += 1
    print("late (MCLK)  die's hold: count")
    for late in sorted(by_late):
        print(f"  {late:5d}    ", ", ".join(f"{h}: {n}" for h, n in sorted(by_late[late].items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
