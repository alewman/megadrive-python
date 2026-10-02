#!/usr/bin/env python3
"""Overdrive 2's Z80 bus handovers, board against the referee, one by one.

From the sound driver's start (its first read of $00DD, which anchors the two Z80
timelines), every time the 68000 asks for the Z80's bus is matched in order on both
machines, and five moments of it compared: the 68000's request (its write of $0100 to
$A11100), the Z80's last access before it stops, the 68000's first access to Z80 space
once it has the bus, its release (the write of $0000), and the Z80's first access after
it. A difference is printed in MCLK, or as the two addresses where the Z80s were already
somewhere else (docs/worklog.md, "Handover by handover").

The referee is ``reference/z80-reset`` (Nuked-MD from power-on, the reset button at frame
25, 68000 and Z80 buses traced); the board runs as ``nuked-md`` with the button at the
same clock (``od2_compare``). Also printed: each side's 68000 and Z80 clock phases, the
referee's in the board's time (lined up by the VDPs, 240,323 MCLK).

    python scripts/od2_handovers.py [--src DIR] [--count N]
"""

from __future__ import annotations

import argparse
import bisect
import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import nuked_md  # noqa: E402

REFERENCE = ROOT / "reference"
REFEREE_Z80_START = 42_570_940
VDP_OFFSET = 240_323


def board_log(src: Path, reset_mclk: int) -> tuple[list, list, int]:
    """The board's Z80 accesses and 68000 Z80-space accesses and requests, and its Z80
    start."""
    sys.path.insert(0, str(src))
    import od2_compare

    from megadrive_python.board import Board

    z80, m68k = [], []
    z80_read, z80_write, bus_cycle = Board.z80_read, Board.z80_write, Board._bus_cycle
    system_write = Board._system_write

    def read(self, address):
        value = z80_read(self, address)
        z80.append((self._z80_access_time(), address & 0xFFFF))
        return value

    def write(self, address, value):
        z80_write(self, address, value)
        z80.append((self._z80_access_time(), address & 0xFFFF))

    def cycle(self, address):
        t = self.m68k_access_clock()
        bus_cycle(self, address)
        if address >> 16 == 0xA0:
            m68k.append((t, address & 0xFFFFFE))

    def a1(self, address, high, low, *, word, mclk=None):
        if address & 0xFFFF00 == 0xA11100:
            m68k.append((self.m68k_access_clock(), "req" if high & 1 else "rel"))
        return system_write(self, address, high, low, word=word, mclk=mclk)

    Board.z80_read, Board.z80_write, Board._bus_cycle = read, write, cycle
    Board._system_write = a1
    out = od2_compare.run_board(
        src, REFERENCE / "nuked-od2" / "od2.bin", reset_mclk, False, 46, "nuked-md"
    )
    return z80, m68k, out["z80_start"]


def referee_log() -> tuple[list, list]:
    events = [
        e
        for e in nuked_md.read_trace(REFERENCE / "z80-reset" / "t.bin")
        if e.t >= REFEREE_Z80_START - 3000
    ]
    z80 = [(e.t, e.addr & 0xFFFF) for e in events if e.type == nuked_md.EV_Z80 and e.flags & 3]
    m68k = []
    for e in events:
        if e.type == nuked_md.EV_M68K and e.by_m68k:
            if e.addr == 0xA11100 and not e.read:
                m68k.append((e.t, "req" if e.data & 0x100 else "rel"))
            elif e.addr >> 16 == 0xA0:
                m68k.append((e.t, e.addr))
    return z80, m68k


def handovers(m68k: list, z80: list, t0: int) -> list[dict]:
    out = []
    times = [t for t, _ in z80]
    for i, (request, kind) in enumerate(m68k):
        if kind != "req" or request < t0:
            continue
        release = next((t for t, k in m68k[i + 1 :] if k == "rel"), None)
        if release is None:
            break
        first = next((t for t, k in m68k[i + 1 :] if k not in ("req", "rel") and t < release), None)
        k = bisect.bisect_left(times, request)
        before = [x for x in z80[max(0, k - 3) :] if x[0] < (first or release)]
        j = bisect.bisect_left(times, release)
        out.append({
            "req": request - t0,
            "first68": first - t0 if first else None,
            "rel": release - t0,
            "last": (before[-1][0] - t0, before[-1][1]) if before else None,
            "resume": (z80[j][0] - t0, z80[j][1]) if j < len(z80) else None,
        })  # fmt: skip
    return out


def compare(board: dict, referee: dict) -> list[str]:
    diffs = []
    for key in ("req", "first68", "rel"):
        if board[key] != referee[key]:
            diffs.append(f"{key} {(board[key] or 0) - (referee[key] or 0):+d}")
    for key in ("last", "resume"):
        ours, theirs = board[key], referee[key]
        if ours and theirs and ours[1] != theirs[1]:
            diffs.append(f"{key} addr {ours[1]:04X}/{theirs[1]:04X}")
        elif ours and theirs and ours[0] != theirs[0]:
            diffs.append(f"{key} {ours[0] - theirs[0]:+d}")
    return diffs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--src", type=Path, default=ROOT / "src", help="the board to run")
    parser.add_argument("--reset-mclk", type=int, default=26_601_694)
    parser.add_argument("--count", type=int, default=20, help="handovers to show")
    args = parser.parse_args()
    board_z80, board_m68k, start = board_log(args.src.resolve(), args.reset_mclk)
    board_z80 = [x for x in board_z80 if x[0] >= start - 3000]
    ref_z80, ref_m68k = referee_log()

    def anchor(z80):  # the driver's first read of $00DD
        return next(t for t, a in z80 if a == 0xDD)

    ours = handovers(board_m68k, board_z80, anchor(board_z80))
    theirs = handovers(ref_m68k, ref_z80, anchor(ref_z80))
    print(f"handovers: board {len(ours)}, referee {len(theirs)}")
    for k, (b, r) in enumerate(zip(ours[: args.count], theirs[: args.count], strict=False)):
        print(f"{k:3d} referee request t+{r['req']:7d}  {'; '.join(compare(b, r)) or 'same'}")
    for name, rows, offset in (("board", board_m68k, 0), ("referee", ref_m68k, VDP_OFFSET)):
        phases = collections.Counter((t - offset) % 7 for t, _ in rows[:200]).most_common(2)
        print(f"{name}: 68000 phase mod 7 {phases}")
    for name, rows, offset in (("board", board_z80, 0), ("referee", ref_z80, VDP_OFFSET)):
        phases = collections.Counter((t - offset) % 15 for t, _ in rows[:400]).most_common(2)
        print(f"{name}: Z80 phase mod 15 {phases}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
