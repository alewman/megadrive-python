#!/usr/bin/env python3
"""Where the board's 68000 gets ahead of the referee's, bus cycle by bus cycle.

Overdrive 2's boot, from the Z80's final start (the 68000's write releasing the bus
after the sound driver is loaded) to a later point, runs on both machines; this walks
their 68000 bus cycles together (``drift_walk``) and reports the time difference as it
goes, so the first cycle where the board runs ahead is named rather than guessed
(docs/worklog.md, "The boot offset"), and where the two first take different paths.

The referee's cycles come from a Nuked-MD bus trace (``validation/nuked_md_headless.c``
``NMD_TRACE``) covering the window, the board's from :meth:`Board._bus_cycle`, which
every 68000 access passes through. The board runs as ``megadrive-tmss`` by default: the
referee reports version 1 at $A10001 (a TMSS Model 1, its README's VA7), and Overdrive 2
takes a different path at boot on a version 0 console (docs/worklog.md, "The console
model"). Both are anchored at the same event: the 68000's
second write of $0000 to $A11100 after the final $A11200 release (the Z80's start).
For each cycle walked the report gives the referee's time since the anchor, the
difference (board minus referee, MCLK), and what the two held the 68000 for; a line
is printed wherever the difference changes.

    python scripts/od2_boot_drift.py ROM TRACE --reset-mclk 26601719 --span 200000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

import drift_walk  # noqa: E402
import nuked_md  # noqa: E402

from megadrive_python.board import Board  # noqa: E402
from megadrive_python.cartridge import Cartridge, load_image  # noqa: E402
from megadrive_python.machine import MACHINES  # noqa: E402
from megadrive_python.system import System  # noqa: E402

AS_LOW = 18


def anchor_index(cycles: list[tuple], which: int = 1) -> int:
    """The ``which``-th (from 0) write of $0000 to $A11100 after the last $A11200
    release; the second by default, the Z80's start."""
    release = max(i for i, c in enumerate(cycles)
                  if c[1] == 0xA11200 and c[2] == "w" and c[4] & 0x100)  # fmt: skip
    zeros = [i for i, c in enumerate(cycles[release:], release)
             if c[1] == 0xA11100 and c[2] == "w" and not c[4] & 0x100]  # fmt: skip
    return zeros[which]


def reset_anchor(cycles: list[tuple], after: int) -> int:
    """The 68000's first bus cycle after ``after`` MCLK: its reset vector fetch after
    the console's reset button."""
    return next(i for i, c in enumerate(cycles) if c[0] > after and c[1] == 0)


def vblank_anchor(cycles: list[tuple], before: int, k: int) -> int:
    """The ``k``-th last vblank edge before index ``before``: a status read ($C00004)
    with bit 3 set after one with it clear -- where a vblank wait lets the 68000 go,
    so where it is in step with the VDP again."""
    edges, last = [], None
    for i, c in enumerate(cycles[:before]):
        if c[1] == 0xC00004 and c[2] == "r":
            bit = c[4] & 8
            if bit and last == 0:
                edges.append(i)
            last = bit
    return edges[-k]


def referee(trace: Path) -> list[tuple]:
    """(t, word address, r/w, held MCLK, data) for the referee's 68000-driven cycles."""
    return [(e.t, e.addr & 0xFFFFFE, "r" if e.read else "w", e.length - AS_LOW, e.data)
            for e in nuked_md.read_trace(trace)
            if e.type == nuked_md.EV_M68K and e.by_m68k]  # fmt: skip


def log_bus() -> list[list]:
    """Patch :class:`Board` (before a system is built: the 68000 keeps the bound methods
    it is given) so that every 68000 bus cycle is logged as ``[t, word address, r/w,
    held MCLK, data]``; return the log, which fills as the system runs."""
    log: list[list] = []
    bus_cycle = Board._bus_cycle
    writing = {"flag": False}

    def logged(self, address):
        # The cycle starts before any refresh stretches it: stamped first.
        before, start = self.stall, self.m68k_access_clock()
        bus_cycle(self, address)
        waited = getattr(self, "grant_wait", 0)  # for the Z80's grant, before /AS
        log.append([start + waited, address & 0xFFFFFE, "w" if writing["flag"] else "r",
                    self.stall - before - waited, 0])  # fmt: skip

    def wrapped(original, byte):
        def wrap(self, address, value):
            writing["flag"] = True
            try:
                original(self, address, value)
            finally:
                writing["flag"] = False
            if log and log[-1][1] == address & 0xFFFFFE:
                log[-1][4] = value << 8 if byte and not address & 1 else value

        return wrap

    Board._bus_cycle = logged
    Board.write_word = wrapped(Board.write_word, False)
    Board.write_byte = wrapped(Board.write_byte, True)

    def read_logged(original, byte):
        def read(self, address):
            value = original(self, address)
            if log and log[-1][1] == address & 0xFFFFFE:
                # A byte read of an odd address comes on D7-D0, as the referee logs it.
                log[-1][4] = value << 8 if byte and not address & 1 else value
            return value

        return read

    Board.read_word = read_logged(Board.read_word, False)
    Board.read_byte = read_logged(Board.read_byte, True)
    return log


def board(rom: Path, reset_mclk: int, until: int, machine: str = "megadrive-tmss",
          refresh_restart: bool = True) -> list[tuple]:  # fmt: skip
    """The same for the board, from power-on to ``until`` MCLK, reset pressed at
    ``reset_mclk`` as in docs/worklog.md Session 13's aligned run."""
    import megadrive_python.system

    megadrive_python.system.RESET_RESTARTS_REFRESH = refresh_restart  # A or B, Session 14
    log = log_bus()
    # The last instruction boundary before the press, from a first run (the press is
    # made there, at the exact clock: ``System.press_reset(at=...)``).
    probe = System(MACHINES[machine], Cartridge.from_image(load_image(rom)))
    stop = 0
    while probe.mclk <= reset_mclk:
        stop = probe.mclk
        probe.step()
    del log[:]
    system = System(MACHINES[machine], Cartridge.from_image(load_image(rom)))
    system.board.fm.enabled = True
    pressed = False
    while system.mclk < until:
        if not pressed and system.mclk + 1_000_000 >= reset_mclk:
            while system.mclk < stop:
                system.step()
            system.press_reset(at=reset_mclk)
            pressed = True
        system.step()
    # Stalls the board charges *after* the access (the Z80-space clock, a VDP hold)
    # land in the next cycle's start; the held figure above counts only refresh.
    return [tuple(c) for c in log]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("rom", type=Path)
    parser.add_argument("trace", type=Path, help="Nuked-MD trace covering the window")
    parser.add_argument("--reset-mclk", type=int, default=26_601_719)
    parser.add_argument("--machine", default="megadrive-tmss",
                        help="the referee is a TMSS Model 1 (version 1)")  # fmt: skip
    parser.add_argument("--span", type=int, default=200_000, help="MCLK after the anchor")
    parser.add_argument("--until", type=int,
                        help="board run length (default: past the reset and the span)")  # fmt: skip
    parser.add_argument("--changes", type=int, default=60, help="difference changes shown")
    parser.add_argument("--no-refresh-restart", action="store_true",
                        help="the board without system.RESET_RESTARTS_REFRESH (A)")  # fmt: skip
    parser.add_argument("--zero", type=int, default=1, help="which $A11100 release anchors")
    parser.add_argument("--from-reset", type=int, nargs=2, metavar=("REFEREE", "BOARD"),
                        help="anchor at the reset vector fetch after these clocks")  # fmt: skip
    parser.add_argument("--min-step", type=int, default=1, help="smallest change shown")
    parser.add_argument("--vblank", type=int, default=0,
                        help="anchor at the K-th vblank edge before the Z80's start")  # fmt: skip
    parser.add_argument("--show", type=int, nargs=2, metavar=("FROM", "TO"),
                        help="print both streams side by side over this window")  # fmt: skip
    parser.add_argument("--save", type=Path, help="pickle both streams and anchors here")
    args = parser.parse_args()
    theirs = referee(args.trace)
    if args.until is None:
        # The board must run past the window, or the judge stops where its stream does.
        after = args.from_reset[1] if args.from_reset else args.reset_mclk
        args.until = max(42_800_000, after + args.span + 1_000_000)
    ours = board(args.rom, args.reset_mclk, args.until, args.machine, not args.no_refresh_restart)
    if args.from_reset:
        a_r = reset_anchor(theirs, args.from_reset[0])
        a_b = reset_anchor(ours, args.from_reset[1])
    else:
        a_r, a_b = anchor_index(theirs, args.zero), anchor_index(ours, args.zero)
        if args.vblank:
            a_r = vblank_anchor(theirs, a_r, args.vblank)
            a_b = vblank_anchor(ours, a_b, args.vblank)
    print(f"anchor: referee {theirs[a_r][0]}, board {ours[a_b][0]}")
    if args.save:
        import pickle

        with open(args.save, "wb") as out:
            pickle.dump((theirs, ours, a_r, a_b), out)
    drift_walk.walk(theirs, ours, a_r, a_b, args.span, changes=args.changes,
                    min_step=args.min_step, show=args.show)  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
