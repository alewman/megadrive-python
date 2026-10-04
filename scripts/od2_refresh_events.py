#!/usr/bin/env python3
"""Overdrive 2's boot: every work-RAM refresh on the die against the board's.

The die's refreshes come from a Nuked-MD trace made with ``NMD_TRACE_MASK`` bit 5 (type 6:
the VDP's refresh counter going back to 0, with the count it reached and whether a
work-RAM access took the refresh or it ran by itself), from the reset button at frame
25, e.g.

    NMD_STOP=64600000 NMD_RESET=53523000:64227600 NMD_TRACE=ref.bin NMD_TRACE_MASK=33 \\
    NMD_TRACE_FROM=53600000 NMD_TRACE_TO=64600000 nukedmd-headless -pal od2.bin

The board's are reconstructed from its own decisions (``board.ram_refresh`` and
``ram_refresh_paused``): taken by an access, run by itself 112 MCLK after falling due and
every 931 after while nothing takes them, the timer stopped for a transfer. Both are put
on the reset anchor's clock (``od2_boot_drift``) and each die refresh is matched to the
nearest board one: the report gives the time differences by kind (fixed per kind: the die
logs the counter's reset, which comes a different time into each kind of refresh) and
lists those whose kind differs (docs/worklog.md, "The refresh timer stops for a
transfer").

    python scripts/od2_refresh_events.py TRACE [--span 5200000]
"""

from __future__ import annotations

import argparse
import bisect
import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

import nuked_md  # noqa: E402
import od2_boot_drift as drift  # noqa: E402

from megadrive_python import board as board_module  # noqa: E402

REFERENCE = ROOT / "reference"
FULL_COUNT = 100  # a real refresh resets the counter from near 127; the reset is repeated


def board_refreshes(span: int, reset_mclk: int) -> tuple[list[tuple[int, str]], int, int]:
    """The board's refresh start times (since its reset anchor) and kinds, and the last
    time its reconstruction is complete to."""
    self_mclk = board_module.RAM_REFRESH_SELF_MCLK
    period = self_mclk + board_module.RAM_REFRESH_AFTER_MCLK
    ran = self_mclk + board_module.RAM_REFRESH_STALL
    began: list[tuple[int, str]] = []
    last_seen = [0]

    def self_runs(due: int, until: int) -> int:
        while until >= due + ran:
            began.append((due + self_mclk, "self"))
            due += period
        return due

    ram_refresh, paused = board_module.ram_refresh, board_module.ram_refresh_paused

    def logged_refresh(due: int, t: int) -> tuple[int, int]:
        wait, next_due = ram_refresh(due, t)
        last_seen[0] = t
        if t >= due:
            due = self_runs(due, t)
            if t >= due:
                start = next_due - board_module.RAM_REFRESH_AFTER_MCLK
                began.append((start, "taken" if start == t else "self"))
        return wait, next_due

    def logged_paused(due: int, start: int, end: int) -> int:
        return paused(self_runs(due, start), start, end)

    board_module.ram_refresh = logged_refresh
    board_module.ram_refresh_paused = logged_paused
    cycles = drift.board(REFERENCE / "nuked-od2" / "od2.bin", reset_mclk,
                         reset_mclk + span + 60_000, "nuked-md")  # fmt: skip
    b0 = cycles[drift.reset_anchor(cycles, 26_500_000)][0]
    unique = {t - b0: kind for t, kind in began}  # a refresh may be reconstructed twice
    return sorted(unique.items()), b0, last_seen[0] - b0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("trace", type=Path, help="Nuked-MD trace with NMD_TRACE_MASK bit 5")
    parser.add_argument("--span", type=int, default=5_200_000, help="MCLK after the reset")
    parser.add_argument("--reset-mclk", type=int, default=26_601_719)
    args = parser.parse_args()
    board, _, complete = board_refreshes(args.span, args.reset_mclk)
    events = nuked_md.read_trace(args.trace)
    cycles = [(e.t, e.addr & 0xFFFFFE, "r" if e.read else "w", 0, 0) for e in events
              if e.type == nuked_md.EV_M68K and e.by_m68k]  # fmt: skip
    r0 = cycles[drift.reset_anchor(cycles, 26_800_000)][0]
    span = min(args.span, complete)
    die = [(e.t - r0, e.flags) for e in events if e.type == nuked_md.EV_REFRESH
           and e.data >= FULL_COUNT and 0 <= e.t - r0 <= span]  # fmt: skip
    times = [t for t, _ in board]
    by_kind: dict[tuple[str, str], collections.Counter] = collections.defaultdict(
        collections.Counter
    )
    differ = []
    for t, flags in die:
        k = bisect.bisect_left(times, t)
        j = min((j for j in (k - 1, k) if 0 <= j < len(times)), key=lambda j: abs(times[j] - t))
        kind = {1: "taken", 2: "self"}.get(flags & 3, "both")
        by_kind[(kind, board[j][1])][times[j] - t] += 1
        if kind != "both" and kind != board[j][1]:
            differ.append((t, kind, times[j], board[j][1]))
    print(f"refreshes to +{span}: die {len(die)}, board "
          f"{sum(1 for t, _ in board if 0 <= t <= span)}")  # fmt: skip
    print("die kind / board kind: board minus die (MCLK) -> refreshes")
    for (die_kind, board_kind), counts in sorted(by_kind.items()):
        print(f"  {die_kind:5s} / {board_kind:5s}  {dict(counts.most_common(6))}")
    print(f"{len(differ)} whose kind differs:")
    for t, kind, bt, bkind in differ[:20]:
        print(f"  die +{t} {kind}, board +{bt} {bkind}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
