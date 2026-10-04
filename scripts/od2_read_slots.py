#!/usr/bin/env python3
"""Overdrive 2's boot, read by read: how the die lets data-port reads go, slot by slot.

The board and the referee (Nuked-MD's bus trace from the reset, ``reference/z80-reset``)
are lined up as ``od2_boot_drift`` lines them up (by the reset vector fetch, then cycle
by cycle). For every data-port read whose bus cycle starts at the same time on both
(relative to the anchor) and that waited for the read-ahead, the difference in when the
*next* bus cycle starts is the die's release against the board's. Printed by the H of
the slot the board filled the buffer at; with ``--slot`` also, for that slot, by how long
before the read the buffer was filled, with the two reads' lengths (docs/worklog.md,
"The slot at $140").

    python scripts/od2_read_slots.py [--span 16000000] [--slot 0x140]
"""

from __future__ import annotations

import argparse
import bisect
import collections
import difflib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

import od2_boot_drift as drift  # noqa: E402

from megadrive_python.vdp import VDP  # noqa: E402

REFERENCE = ROOT / "reference"
DATA_PORT = 0xC00000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--span", type=int, default=16_000_000, help="MCLK after the reset")
    parser.add_argument("--slot", type=lambda s: int(s, 0), help="detail for this slot's H")
    parser.add_argument("--reset-mclk", type=int, default=26_601_719)
    args = parser.parse_args()

    fills: list[tuple[int, int, bool]] = []  # (MCLK, H, active line)
    read_cache = VDP._read_cache

    def logged_read_cache(self) -> None:
        mode = self.hmode
        h = mode.hvals[bisect.bisect_right(mode.starts, self.mclk - self.line_start) - 1]
        fills.append((self.mclk, h, self.slots is mode.slots_active))
        read_cache(self)

    VDP._read_cache = logged_read_cache
    theirs = drift.referee(REFERENCE / "z80-reset" / "t.bin")
    ours = drift.board(REFERENCE / "nuked-od2" / "od2.bin", args.reset_mclk,
                       args.reset_mclk + args.span + 60_000, "nuked-md")  # fmt: skip
    a_r, a_b = drift.reset_anchor(theirs, 26_800_000), drift.reset_anchor(ours, 26_500_000)
    t_r, t_b = theirs[a_r][0], ours[a_b][0]
    r = [c for c in theirs[a_r:] if c[0] - t_r <= args.span]
    b = [c for c in ours[a_b:] if c[0] - t_b <= args.span + 20_000]
    matcher = difflib.SequenceMatcher(None, [(c[1], c[2]) for c in r],
                                      [(c[1], c[2]) for c in b], autojunk=False)  # fmt: skip
    pairs = [(m.a + k, m.b + k) for m in matcher.get_matching_blocks() for k in range(m.size)]

    fill_times = [f[0] for f in fills]
    by_slot: dict[tuple[int, bool], collections.Counter] = collections.defaultdict(
        collections.Counter
    )
    detail: collections.Counter = collections.Counter()
    for (i0, _), (i1, j1), (i2, j2) in zip(pairs, pairs[1:], pairs[2:], strict=False):
        if r[i1][1] != DATA_PORT or r[i1][2] != "r" or i1 != i0 + 1 or i2 != i1 + 1:
            continue
        if r[i1][0] - t_r != b[j1][0] - t_b:
            continue  # not in step at the read
        k = bisect.bisect_right(fill_times, b[j2][0]) - 1
        if k < 0 or fill_times[k] < b[j1][0] - 2000:
            continue  # served from the buffer without a wait
        fill, h, active = fills[k]
        diff = (r[i2][0] - t_r) - (b[j2][0] - t_b)
        by_slot[(h, active)][diff] += 1
        if h == args.slot:
            detail[(fill - b[j1][0], r[i2][0] - r[i1][0], b[j2][0] - b[j1][0])] += 1

    print("slot H (A active, i idle)   die minus board at the next cycle: reads")
    for (h, active), counts in sorted(by_slot.items()):
        print(f"  ${h:03X} {'A' if active else 'i'}    {dict(sorted(counts.items()))}")
    if args.slot is not None:
        print(f"slot ${args.slot:03X}: fill minus read start, die's read length, board's: reads")
        for (offset, die, board), n in sorted(detail.items()):
            print(f"  {offset:6d}  {die:4d}  {board:4d}   {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
