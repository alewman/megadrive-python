#!/usr/bin/env python3
"""Any cartridge from power-on, the board's nuked-md machine against Nuked-MD, bus cycle
by bus cycle -- the Overdrive 2 judge (``od2_boot_drift``) for a second title.

Nuked-MD runs the cartridge from power-on (PAL, as the nuked-md machine is) and logs
every 68000 bus cycle; the board runs it with its 68000 bus logged (``log_bus``), and
the two streams are walked together (``drift_walk``) from the first cycle after the
vector fetches and the first prefetch.

Two things the board cannot know are set from the referee, as the probe comparisons in
``run_nuked_md.py`` set them, and nothing else is:

- **The clock.** Nuked-MD holds its 68000 in reset for about 693,000 MCLK after
  power-on; the board's starts at once. The board's clock is moved on so that the
  walk's first cycle falls :data:`VDP_OFFSET_MCLK` before the referee's: the relation
  at which the two VDPs, each running from power-on, are at the same point in their
  frame (``system.Z80_CLOCK_PHASE``'s note; docs/worklog.md, "The VDPs' offset") and
  the 68000's and Z80's clock edges fall where the referee's do against its VDP.
- **The E clock.** The board's 68000 counts its E clock from the clocks it has run;
  moving the clock on moves that count with it, as a 68000 clocked from power-on
  through the referee's reset hold would have it (Sonic's first VINT, +55,151,173:
  its autovectored acknowledge waited 7 clocks fewer than the referee's without it).
- **The refresh phases.** Both refreshes run from a power-on phase nothing models. The
  cartridge refresh runs free, so every held cartridge access brackets its phase (due
  after the access before, by the held one's /AS); the tick is placed at the
  bracket's end, on a held access's /AS, as the referee's /REF pulse places it (Sonic
  the Hedgehog, +51,141,633). The work RAM's, which transfers pause, is fitted over
  the first 5M MCLK only (``run_nuked_md.referee_refresh``).

docs/worklog.md, "A second title".

    python scripts/cold_boot_drift.py ROM [--span 30000000] [--save streams.pkl]
"""

from __future__ import annotations

import argparse
import itertools
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

import drift_walk  # noqa: E402
import nuked_md  # noqa: E402
import od2_boot_drift as judge  # noqa: E402
import run_nuked_md  # noqa: E402

from megadrive_python.cartridge import Cartridge, load_image  # noqa: E402
from megadrive_python.machine import M68K_DIVIDER, MACHINES  # noqa: E402
from megadrive_python.system import System  # noqa: E402

#: The vector fetches and the first prefetch: the walk starts at the cycle after them.
FIRST = 6
#: The board's clock at that cycle, from power-on.
BOARD_FIRST_MCLK = 284
#: The board's VDP lines up with Nuked-MD's when the board's clock is this far behind
#: the referee's (every one of 16,427 HBlank flags of the Overdrive 2 boot; the judge's
#: anchors there are as far apart). Putting the two clocks equal instead leaves the
#: VDPs 70 lines and 877 MCLK apart, which nothing the three titles do before their
#: first interrupt shows but their status reads' HBlank bit and the refresh tie's dot
#: clock (docs/worklog.md, "A second title").
VDP_OFFSET_MCLK = 240_277
CART_PERIOD = run_nuked_md.CART_PERIOD
REFRESH_FIT_MCLK = 5_000_000
SAMPLE_MCLK = 24 * 42
#: Where the referee's slot-23 marker (``fsm_sel23`` rising) falls against the board's
#: slot 23 when the two FMs are in step: the /IC sweep's restarts agree to this
#: (``run_fm_ic.py``, 125 on the die against 126 here).
FM_MARKER_MCLK = -1


def align_fm(system: System, markers: list[float], ic: list[tuple[float, int]],
             first: float) -> int:  # fmt: skip
    """Put the board's FM on the referee's sample phase: its slot 23 where the referee's
    marker is, as the referee's FM stands at its first walked cycle (``first``, before
    the cartridge's first /IC). Returns the shift made, MCLK."""
    asserted = [t for t, level in ic if level and t > 0]
    before = min(asserted) if asserted else float("inf")
    reference = next(m for m in markers if m >= first)
    if reference >= before:
        raise SystemExit("the cartridge resets the FM before the walk: no phase to read")
    fm = system.board.fm
    target = int(reference) - VDP_OFFSET_MCLK + FM_MARKER_MCLK
    fm.run(target - SAMPLE_MCLK)
    slot23 = fm.mclk - ((fm.cycles - 23) % 24) * 42
    shift = (target - slot23) % SAMPLE_MCLK
    fm.mclk += shift % 42
    fm.cycles = (fm.cycles - shift // 42) % 24
    return shift


def cartridge_phase(events: list) -> int:
    """The referee's cartridge refresh phase (mod its period): the end of the bracket
    every held cartridge access leaves, up to the first that disagrees."""
    lo, hi = -CART_PERIOD, CART_PERIOD
    for before, held in itertools.pairwise(events):
        if held.length - judge.AS_LOW == run_nuked_md.CART_STALL and held.address < 0x800000:
            x, y = before.t % CART_PERIOD, held.t % CART_PERIOD
            if x > y:
                x -= CART_PERIOD
            if y > CART_PERIOD // 2:
                x, y = x - CART_PERIOD, y - CART_PERIOD
            if max(lo, x) >= min(hi, y):
                break
            lo, hi = max(lo, x), min(hi, y)
    return hi % CART_PERIOD


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("rom", type=Path)
    parser.add_argument("--span", type=int, default=30_000_000, help="MCLK after the first cycle")
    parser.add_argument("--changes", type=int, default=40, help="difference changes shown")
    parser.add_argument("--show", type=int, nargs=2, metavar=("FROM", "TO"),
                        help="print both streams side by side over this window")  # fmt: skip
    parser.add_argument("--save", type=Path, help="pickle both streams and anchors here")
    args = parser.parse_args()
    until = args.span + 2_000_000

    with tempfile.TemporaryDirectory() as tmp:
        run = nuked_md.run(args.rom, until, trace=(0, until), trace_mask=1, pal=True,
                           fm_markers=True, tmp=Path(tmp))  # fmt: skip
    # Two cycles logged at power-on, before the 68000 leaves reset, are left out.
    events = [e for e in run.events if e.type == nuked_md.EV_M68K and e.by_m68k and e.t >= 100]
    theirs = [(e.t, e.addr & 0xFFFFFE, "r" if e.read else "w", e.length - judge.AS_LOW, e.data)
              for e in events]  # fmt: skip
    print(f"referee: {len(theirs):,} 68000 cycles ({run.seconds:.0f} s)")

    fitted = [e for e in events if e.t < events[0].t + REFRESH_FIT_MCLK]
    cart_due, _, ram_due, ram_miss = run_nuked_md.referee_refresh(fitted)
    phase = cartridge_phase(events)
    cart_due -= (cart_due - phase) % CART_PERIOD
    cart_due -= CART_PERIOD * ((cart_due - events[0].t) // CART_PERIOD + 1)
    cart_due, ram_due = run_nuked_md.dues_at(events, FIRST, cart_due, ram_due)
    print(f"refresh phases: cartridge {phase} of {CART_PERIOD}; work RAM fitted on the first "
          f"{REFRESH_FIT_MCLK:,} MCLK, {ram_miss} misplaced")  # fmt: skip

    log = judge.log_bus()
    system = System(MACHINES["nuked-md"], Cartridge.from_image(load_image(args.rom)))
    system.board.fm.enabled = True
    offset = events[FIRST].t - VDP_OFFSET_MCLK - BOARD_FIRST_MCLK
    system.mclk += offset
    system.z80_mclk += offset
    system.board.step_start = system.mclk
    # The 68000's E clock runs from power-on, through the referee's reset hold; the core
    # counts it from the clocks it has run (``M68000CPU.clock``).
    system.m68k.clock += offset // M68K_DIVIDER
    shift = align_fm(system, run.fm_markers, run.fm_ic, events[FIRST].t)
    print(f"the FM's sample phase moved {shift} MCLK onto the referee's")
    system.board.cart_refresh_due = cart_due - VDP_OFFSET_MCLK
    system.board.ram_refresh_due = ram_due - VDP_OFFSET_MCLK
    while system.mclk < until - VDP_OFFSET_MCLK:
        system.step()
    ours = [tuple(c) for c in log]
    a_b = judge.reset_anchor(ours, -1) + FIRST
    if ours[a_b][0] != theirs[FIRST][0] - VDP_OFFSET_MCLK:
        raise SystemExit(f"the board's first cycle is at {ours[a_b][0]}, the referee's at "
                         f"{theirs[FIRST][0]}: BOARD_FIRST_MCLK has moved")  # fmt: skip
    print(
        f"board: {len(ours):,} 68000 cycles; the walk starts at MCLK {theirs[FIRST][0]:,} "
        f"on the referee, {ours[a_b][0]:,} on the board"
    )
    if args.save:
        import pickle

        with open(args.save, "wb") as out:
            pickle.dump((theirs, ours, FIRST, a_b), out)
    drift_walk.walk(theirs, ours, FIRST, a_b, args.span, changes=args.changes, show=args.show)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
