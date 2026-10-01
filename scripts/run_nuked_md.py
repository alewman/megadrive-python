#!/usr/bin/env python3
"""Milestone 6: judge the board's bus and device timing against Nuked-MD.

Nuked-MD is a gate-level model of the whole chipset from decapped-die photographs
(``scripts/nuked_md.py`` has its tier and its caveats).  It is *slow* -- about a third
of an emulated frame a second on this machine -- so every comparison here is sized to
fit: small purpose-built cartridges where the question is a timing rule, and short
windows of the real test ROMs where the question is whether a whole program's bus
traffic agrees.

The subcommands, each with what it costs:

  probes    eight probe loops (ROM, work RAM, VDP, mixed), the 68000's whole bus
            stream compared cycle for cycle.  ~15 s of wall time each.
  dma       the cost of a DMA transfer against its word count, for four targets:
            the slope and the *intercept* Kabuto measured (+5.6 cycles) and the
            board does not reproduce (milestone 5's open item 15).  ~1 min.
  arbiter   MoD's three Z80-bank-window experiments (including the ``pop`` pair the
            board charges 2.53 wait states where he measured 3.5).  ~2 min each.
  busack    the Z80's bus grant: /BUSREQ to /BUSAK, which no hardware source in the
            pinned material measures at all.  ~30 s.
  sound     the board's *wiring* of the two sound chips: when a Z80 write reaches
            the FM, how long BUSY lasts, and what the four ports read back.  ~2 min.
  roms      the test ROMs of milestones 2-5, a window of each compared cycle for
            cycle.  Minutes to tens of minutes each; ``--parallel`` runs them at
            once.

Every run says what it pinned: the referee's revision, the ROM's SHA-256 where it is
a fetched one, the window in MCLK and the wall time.

    python scripts/run_nuked_md.py probes
    python scripts/run_nuked_md.py dma --words 1,2,4,8,16,32,64
    python scripts/run_nuked_md.py roms --only vdpfifotesting --frames 200
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import subprocess
import sys
import time
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import nuked_md as nmd  # noqa: E402

from megadrive_python.board import (  # noqa: E402
    RAM_REFRESH_AFTER_MCLK,
    RAM_REFRESH_SELF_MCLK,
    ram_refresh,
)
from megadrive_python.machine import M68K_DIVIDER  # noqa: E402

TEST_ROMS = ROOT / "tests" / "test_roms"

#: The referee's I/O chip reports hardware version 1 ($A1 at $A10001: overseas, NTSC,
#: no Mega CD, version 1), as an FC1004 board does; the board's ``genesis`` machine is
#: the pre-TMSS VA-early one and reports version 0.  The comparisons below give the
#: board the referee's value, which with TMSS off is the combination the referee's own
#: README calls one no retail console shipped.
REFEREE_VERSION = 0xA1

#: The board's cartridge refresh, as ``megadrive_python.board`` has it; the work RAM's
#: is ``board.ram_refresh`` itself.
CART_PERIOD, CART_STALL = 896, 14


# -- cartridges built here ------------------------------------------------------------


def cartridge(body: bytes, tail: bytes = b"", *, size: int = 0x1000) -> bytes:
    """A minimal cartridge: vectors, a Mega Drive header, then ``body`` at $200.

    The header matters to Nuked-MD only through TMSS, which this build has off
    (``validation/build_nuked_md.sh``), but a real board's TMSS wants it, so the board
    and the referee are given the same image.
    """
    rom = bytearray(size)
    rom[0:4] = (0x00FFFE00).to_bytes(4, "big")  # initial SSP
    rom[4:8] = (0x00000200).to_bytes(4, "big")  # reset vector
    for vector in range(2, 64):  # every exception lands on a halt, so a fault is loud
        rom[vector * 4 : vector * 4 + 4] = (0x000001F0).to_bytes(4, "big")
    rom[0x1F0:0x1F4] = bytes.fromhex("4e722700")  # stop #$2700
    rom[0x100:0x110] = b"SEGA GENESIS    "
    rom[0x200 : 0x200 + len(body)] = body
    rom[0x200 + len(body) : 0x200 + len(body) + len(tail)] = tail
    return bytes(rom)


def loop(setup: bytes, body: bytes) -> bytes:
    """``setup``, then ``body`` for ever (a ``bra.w`` back to its start)."""
    start = 0x200 + len(setup)
    end = 0x200 + len(setup) + len(body)
    displacement = (start - (end + 2)) & 0xFFFF
    return setup + body + b"\x60\x00" + displacement.to_bytes(2, "big")


UNLOCK = bytes.fromhex("23fc5345474100a14000")  # move.l #'SEGA',$A14000
NOP = bytes.fromhex("4e71")


# -- the refresh phases, the one thing fitted -----------------------------------------
#
# Both refreshes are free-running grids whose phase at power-on nothing models: the
# board's ``cart_refresh_due``/``ram_refresh_due`` start at one arbitrary point and
# the referee's counters at another.  Every comparison below therefore fits the two
# phases -- and *only* the phases; the rules (the cartridge's 896-MCLK grid and 14,
# the work RAM's ``board.ram_refresh``) stay the board's own, and a wrong period
# would show as drift no single phase could hide.


def referee_refresh(reference) -> tuple[int, int, int, int]:
    """Read the two refresh grids straight out of the referee's trace.

    The referee says how long it held the 68000 for each bus cycle (/AS low, 18 MCLK
    with nothing stretching it), so the cartridge refresh's ticks (+14 on a cartridge
    access) and the work RAM's (+21 on a work-RAM access) are *observed*, not
    inferred.  Fitting a grid to them gives the phase the board's own counters should
    start at -- which is the only thing about either refresh that power-on decides.

    Returns (the cartridge grid's next due time, how many of its ticks the board's
    rule misplaces, the work RAM's next due time, likewise), in the referee's clock.
    """
    cycles = [(e.t, e.address, e.length) for e in reference]
    cart = {t for t, a, n in cycles if n == nmd.AS_LOW + CART_STALL and a < 0x800000}
    ram_accesses = [t for t, a, _ in cycles if a >= 0xE00000]

    best = None
    for phase in range(CART_PERIOD):
        due = cycles[0][0] + phase - CART_PERIOD
        hit = set()
        for t, a, _ in cycles:
            if t >= due:
                due += ((t - due) // CART_PERIOD + 1) * CART_PERIOD
                if a < 0x800000:
                    hit.add(t)
        miss = len(hit ^ cart)
        if best is None or miss < best[0]:
            best = (miss, cycles[0][0] + phase - CART_PERIOD)
        if not miss:
            break
    cart_miss, cart_due = best

    # The work RAM's by the board's own rule (``board.ram_refresh``), every access's
    # wait against the referee's, the partial ones included.
    best = None
    if ram_accesses:
        waits = [(t, n - nmd.AS_LOW) for t, a, n in cycles if a >= 0xE00000]
        span = RAM_REFRESH_SELF_MCLK + RAM_REFRESH_AFTER_MCLK
        for phase in range(span):
            due = ram_accesses[0] + phase - span
            start, miss = due, 0
            for t, observed in waits:
                wait, due = ram_refresh(due, t)
                miss += wait != observed
            if best is None or miss < best[0]:
                best = (miss, start)
            if not miss:
                break
    ram_miss, ram_due = best if best else (0, cycles[0][0])
    return cart_due, cart_miss, ram_due, ram_miss


def compare_rom(reference, rom, *, machine="genesis", version=None, accesses=None):
    """Compare one program's whole 68000 bus stream, with both refresh phases taken
    from the referee rather than searched for."""
    cart_due, cart_miss, ram_due, ram_miss = referee_refresh(reference)
    accesses = accesses or len(reference) + 400

    def setup(system, cart=0, ram=0):
        if version is not None:
            system.board.io.version = version
        system.board.cart_refresh_due = cart
        system.board.ram_refresh_due = ram

    # The two clocks differ by a constant (the referee's 68000 comes out of reset
    # 693,230 MCLK after the board's), and the refresh phases have to be translated by
    # it.  Finding it takes a board run, and a board run needs the phases, so it is
    # iterated: the offset moves at most once, because a stall before the alignment
    # point is all that can change it.
    delta, start, log = 0, 0, None
    for _ in range(4):
        cart, ram = dues_at(reference, start, cart_due, ram_due)
        _, log = nmd.board_trace(
            rom,
            machine=machine,
            accesses=accesses,
            offset=REFEREE_RESET,
            setup=lambda s, c=cart - delta, r=ram - delta: setup(s, c, r),
        )
        r0, b0 = nmd.align(reference, log)
        moved = reference[r0].t - log[b0].t
        if moved == delta and r0 == start:
            break
        delta, start = moved, r0
    n, diffs = nmd.compare(reference, log)
    return n, diffs, cart_miss, ram_miss, len(reference)


def dues_at(reference, index: int, cart_due: int, ram_due: int) -> tuple[int, int]:
    """Where the referee's two refresh counters stand just before ``reference[index]``.

    The board's log begins at the first access it makes *after* the harness has set
    its counters -- its reset-vector reads are made by the core's own ``reset()``,
    before that -- so the counters have to be set to what the referee's were at that
    same point, not to their power-on value.
    """
    for e in reference[:index]:
        if e.t >= cart_due:
            cart_due += ((e.t - cart_due) // CART_PERIOD + 1) * CART_PERIOD
        if e.address >= 0xE00000:
            ram_due = ram_refresh(ram_due, e.t)[1]
    return cart_due, ram_due


# -- probes ---------------------------------------------------------------------------

#: Each probe is (name, setup words, the loop's body).  They are chosen to exercise one
#: kind of bus cycle at a time, because a difference then names itself.
#: Mode 5, display off, DMA enabled, H40, auto-increment 2: what every Mega Drive
#: program writes before it touches the VDP.  Without it the VDP is still in Mode 4,
#: where the referee's HV port reads a constant and the board's does not (a named
#: difference, docs/worklog.md).
VDP_MODE5 = b"".join(
    bytes.fromhex("33fc") + bytes([0x80 | reg, value]) + b"\x00\xc0\x00\x04"
    for reg, value in ((0x00, 0x04), (0x01, 0x14), (0x0C, 0x81), (0x0F, 0x02))
)
#: ``clr.l $FF0000`` and ``clr.l $FF0004``: work RAM powers up with whatever the
#: referee's DRAM held, so a probe that reads it writes it first.
CLEAR_RAM = bytes.fromhex("42b900ff000042b900ff0004")

PROBES: list[tuple[str, bytes, bytes]] = [
    ("nop loop (ROM fetches only)", UNLOCK, NOP * 12),
    ("ROM reads", UNLOCK + bytes.fromhex("41f900000300"), bytes.fromhex("3010") * 8),
    (
        "work RAM reads",
        UNLOCK + CLEAR_RAM + bytes.fromhex("41f900ff0000"),
        bytes.fromhex("3010") * 8,
    ),
    ("work RAM writes", UNLOCK + bytes.fromhex("41f900ff0000"), bytes.fromhex("3080") * 8),
    (
        "work RAM read-modify-write",
        UNLOCK + CLEAR_RAM + bytes.fromhex("41f900ff0000"),
        bytes.fromhex("5250") * 8,
    ),
    (
        "VDP status reads",
        UNLOCK + VDP_MODE5 + bytes.fromhex("41f900c00004"),
        bytes.fromhex("3010") * 8,
    ),
    (
        "VDP HV counter reads",
        UNLOCK + VDP_MODE5 + bytes.fromhex("41f900c00008"),
        bytes.fromhex("3010") * 8,
    ),
    # A control write that sets VRAM write at $0000, then eight data writes: the FIFO
    # and its access slots, which is milestone 2's and 5's subject.
    (
        "VDP data writes",
        UNLOCK
        + VDP_MODE5
        + bytes.fromhex("41f900c00000")
        + bytes.fromhex("337c400000043c7c000000024e71"),
        bytes.fromhex("3092") * 8,
    ),
    (
        "VDP HV counter reads, still in Mode 4",
        UNLOCK + bytes.fromhex("41f900c00008"),
        bytes.fromhex("3010") * 8,
    ),
]


def cmd_probes(args) -> int:
    print(f"# probe loops, the 68000's whole bus stream ({args.cycles} cycles each)")
    print(f"# referee: Nuked-MD @ {referee_revision()}, from its 68000's reset "
          f"({REFEREE_RESET} MCLK) for {args.window} MCLK")  # fmt: skip
    total = agree = 0
    for name, setup, body in PROBES:
        rom = cartridge(loop(setup, body))
        began = time.monotonic()
        run = nmd.run(rom, stop=REFEREE_RESET + args.window + 5000,
                      trace=(REFEREE_RESET, REFEREE_RESET + args.window), trace_mask=1)  # fmt: skip
        reference = run.m68k(dma=False)[: args.cycles]
        n, diffs, cart_miss, ram_miss, _ = compare_rom(reference, rom, version=REFEREE_VERSION)
        total += n
        agree += n - len(diffs)
        print(f"\n{name}: {n - len(diffs)} of {n} bus cycles agree "
              f"({time.monotonic() - began:.0f} s; the board's own refresh rules "
              f"misplace {cart_miss} cartridge and {ram_miss} work-RAM ticks of the "
              f"referee's)")  # fmt: skip
        for d in diffs[: args.show]:
            print(f"    [{d.index}] {d.what}: {d.detail}")
        if len(diffs) > args.show:
            print(f"    ... and {len(diffs) - args.show} more")
    print(f"\ntotal: {agree} of {total} 68000 bus cycles agree")
    return 0


# -- the DMA's cost -------------------------------------------------------------------

#: The MCLK at which the referee's 68000 fetches its reset vector.  Its power-on
#: sequence (/SRES released at half-MCLK 10,000, then the FC1004 holding the CPU
#: reset) takes 693,230 MCLK, where the board's 68000 starts at 0; every window below
#: is quoted in the referee's own time.
REFEREE_RESET = 693_230

#: The transfer targets: the two control words a 68000-memory DMA to each one writes.
DMA_TARGETS = {
    "VRAM": (0x4000, 0x0080),
    "CRAM": (0xC000, 0x0080),
    "VSRAM": (0x4000, 0x0090),
}


#: ``btst #3,$C00005`` then ``bne``/``beq`` back to it: wait while VBlank is set / until
#: it is.
WAIT_VBLANK_CLEAR = bytes.fromhex("0839000300c0000566f6")
WAIT_VBLANK_SET = bytes.fromhex("0839000300c0000567f6")


def dma_rom(words: int, target: str, *, display_off: bool = True, h40: bool = True,
            when: str | None = None, delay: int = 0) -> bytes:  # fmt: skip
    """A cartridge that sets the VDP up, starts one 68000-memory DMA of ``words``
    words from $FF0000, and then runs a long NOP chain.

    What is measured is the gap the transfer leaves in the 68000's own bus stream: the
    second control write starts it, and the 68000 gets the bus back only when it is
    over -- which is exactly Kabuto's "on top of what the actual 68k instructions
    cost".
    """
    first, second = DMA_TARGETS[target]
    source = 0xFF0000 >> 1
    regs = bytearray()
    for reg, value in (
        (0x00, 0x04),
        (0x01, 0x14 if display_off else 0x54),  # M5 + DMA enable, display off/on
        (0x0C, 0x81 if h40 else 0x00),
        (0x0F, 0x02),
        (0x13, words & 0xFF), (0x14, (words >> 8) & 0xFF),
        (0x15, source & 0xFF), (0x16, (source >> 8) & 0xFF),
        (0x17, (source >> 16) & 0x7F),  # bit 7 clear: from the 68000's memory
    ):  # fmt: skip
        regs += bytes.fromhex("33fc") + bytes([0x80 | reg, value]) + b"\x00\xc0\x00\x04"
    start = bytes.fromhex("33fc") + first.to_bytes(2, "big") + b"\x00\xc0\x00\x04"
    start += bytes.fromhex("33fc") + second.to_bytes(2, "big") + b"\x00\xc0\x00\x04"
    # With the display on, where the transfer runs decides its cost: ``when`` starts it
    # just after VBlank begins ("vblank") or just after it ends ("active"), and
    # ``delay`` NOPs move it along the line.
    wait = b""
    if when == "vblank":
        wait = WAIT_VBLANK_CLEAR + WAIT_VBLANK_SET
    elif when == "active":
        wait = WAIT_VBLANK_SET + WAIT_VBLANK_CLEAR
    body = UNLOCK + bytes(regs) + NOP * 4 + wait + NOP * delay + start
    return cartridge(loop(body, NOP * 16))


def dma_hold(cycles, *, control_address: int = 0xC00004) -> int | None:
    """The MCLK the 68000 lost to the transfer.

    The transfer is the one control-port write with by far the longest gap after it;
    what the 68000 lost is that gap less the 28 MCLK the write itself takes.
    """
    best = None
    for e, nxt in pairwise(cycles):
        if e.addr == control_address and e.kind == "ww":
            gap = nxt.t - e.t
            if best is None or gap > best:
                best = gap
    return None if best is None else best - 28


def cmd_dma(args) -> int:
    words = [int(w) for w in args.words.split(",")]
    print("# the cost of a 68000-memory DMA beyond the write that starts it,")
    display = "display on" if args.display_on else "display off"
    where = f", started at {args.when} + {args.delay} NOPs" if args.when else ""
    print(f"# {display}, H40{where}, in 68000 cycles")
    print(f"# referee: Nuked-MD @ {referee_revision()}")
    print(f"\n{'target':7} {'words':>6} {'referee':>9} {'board':>9} {'board-ref':>10}")
    fits: dict[str, tuple[list[int], list[float], list[float]]] = {}
    for target in args.targets.split(","):
        used, ref_costs, board_costs = [], [], []
        for n in words:
            rom = dma_rom(n, target, display_off=not args.display_on, when=args.when,
                          delay=args.delay)  # fmt: skip
            window = 20_000 + n * 40 + (2_400_000 if args.when else 0) + args.delay * 28
            run = nmd.run(rom, stop=REFEREE_RESET + window + 5000,
                          trace=(REFEREE_RESET, REFEREE_RESET + window), trace_mask=1)  # fmt: skip
            ref = dma_hold(run.m68k(dma=False))
            _, log = nmd.board_trace(rom, accesses=90_000 if args.when else 1500 + args.delay)
            board = dma_hold(log)
            if ref is None or board is None:
                print(f"{target:7} {n:6}   no transfer seen")
                continue
            ref_c, board_c = ref / M68K_DIVIDER, board / M68K_DIVIDER
            used.append(n)
            ref_costs.append(ref_c)
            board_costs.append(board_c)
            print(f"{target:7} {n:6} {ref_c:9.1f} {board_c:9.1f} {board_c - ref_c:+10.1f}")
        if len(ref_costs) >= 2:
            fits[target] = (used, ref_costs, board_costs)
    print(f"\n{'target':7} {'referee slope':>14} {'intercept':>10} "
          f"{'board slope':>12} {'intercept':>10}")  # fmt: skip
    for target, (used, ref_costs, board_costs) in fits.items():
        rs, ri = line_fit(used, ref_costs)
        bs, bi = line_fit(used, board_costs)
        print(f"{target:7} {rs:14.2f} {ri:10.1f} {bs:12.2f} {bi:10.1f}")
    print("\n# Kabuto (hardware-measured research, 'The bus system'): a fast transfer")
    print("# (CRAM, VSRAM, VRAM in 128 KB mode) 2.4 cycles a word with a +5.6 intercept,")
    print("# a slow one (VRAM) 4.7 with -6.  Milestone 5's open item 15 is that the")
    print("# board reproduces both slopes and neither intercept.")
    return 0


def line_fit(xs, ys) -> tuple[float, float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs) or 1.0
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / denom
    return slope, my - slope * mx


# -- the arbiter ----------------------------------------------------------------------

#: MoD's experiments (``scripts/run_bus_arbiter.py`` has the quotations), with one
#: change: each Z80 loop jumps back to $0000 rather than into the middle of itself, so
#: that it re-runs its own set-up every iteration.  The referee's Z80 comes out of
#: reset with a stack pointer of its own and, with MoD's ``jp $0003``, never executes
#: the ``ld sp,$8002`` at all -- its ``pop`` then reads Z80 RAM and the experiment
#: measures nothing (found by tracing: the reads took three T-states and /WAIT never
#: moved).  Jumping to $0000 makes the loop self-correcting on both machines, and both
#: are given the same image here.
M68K_STOP = bytes.fromhex("4e722700")
M68K_SPIN = bytes.fromhex("41fa00024ed0")
#: ``ld hl,$8000`` / ``ld a,(hl)`` / ``jp $0000``
Z80_READ = bytes.fromhex("2100807ec30000")
#: ``ld sp,$8002`` / ``pop hl`` / ``jp $0000``: two bank reads in one instruction,
#: the case MoD measured on the sound drivers.
Z80_POP = bytes.fromhex("310280e1c30000")


def arbiter_rom(z80_program: bytes, m68k_loop: bytes) -> bytes:
    """``scripts/run_bus_arbiter.py``'s ``_rom``, rebuilt here so that the referee and
    the board are given the same image byte for byte."""
    setup = bytearray()
    setup += UNLOCK
    setup += bytes.fromhex("33fc010000a11100")  # request the Z80's bus
    setup += bytes.fromhex("33fc010000a11200")  # release its reset
    lea_at = len(setup)
    setup += bytes.fromhex("43fa0000")
    setup += bytes.fromhex("247c00a00000")
    setup += bytes([0x70, len(z80_program) - 1])
    setup += bytes.fromhex("14d951c8fffc")
    setup += bytes.fromhex("33fc000000a11100")  # release the bus
    setup += m68k_loop
    setup[lea_at + 2 : lea_at + 4] = (len(setup) - (lea_at + 2)).to_bytes(2, "big")
    return cartridge(bytes(setup) + z80_program)


#: A Z80 memory cycle with no wait state holds /MREQ low for at most 30 MCLK in the
#: referee's traces (an M1 opcode fetch; a data read is 22 and an M1 refresh 15).  An
#: access through the bank window is held by the arbiter and is always longer, so this
#: is how one is recognised -- the *address* cannot be used, because the arbiter takes
#: the Z80's address bus over for the access and ``md.zaddress`` then reads back
#: whatever was left on it.
Z80_CYCLE_MAX = 45


def z80_bank_reads(run) -> list[tuple[nmd.Event, int]]:
    """Each Z80 access the arbiter held, with the wait states it was charged.

    A Z80 memory cycle is three T-states with no wait, and the next machine cycle
    begins the moment it ends, so ``(next cycle's first ZCLK) - (this one's) - 3`` is
    the wait count -- exact, because ZCLK edges are counted, not inferred from a
    length.
    """
    cycles = run.z80()
    out = []
    for e, nxt in pairwise(cycles):
        if e.length > Z80_CYCLE_MAX and e.z80_read:
            out.append((e, (nxt.zt - e.zt) - 3))
    return out


def board_bank_delays(rom: bytes, accesses: int) -> list[tuple[int, int]]:
    """The board's own model on the same image: each bank access's (Z80, 68000) cost."""
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES
    from megadrive_python.system import System

    system = System(MACHINES["genesis"], Cartridge(rom))
    board = system.board
    board.vdp.rendering = False
    board.fm.enabled = False
    log: list[tuple[int, int]] = []
    charged = board._bank_delay

    def record() -> None:
        before = (board.bank_z80_cycles, board.bank_m68k_cycles)
        charged()
        log.append(
            (
                board.bank_z80_cycles - before[0],
                board.bank_m68k_cycles - before[1],
                board.step_start,
            )
        )

    board._bank_delay = record  # type: ignore[method-assign]
    while len(log) < accesses and system.m68k_instructions < 4_000_000:
        system.step()
    return log[:accesses]


def board_pop_split(board_log):
    """The board's two reads of one ``pop``: both fall in the same Z80 step, which is
    exactly the limitation docs/polish.md records (z80-python reports only a step's
    total, so the board cannot place the second read at its own T-state)."""
    firsts, seconds = [], []
    for a, b in pairwise(board_log):
        if a[2] == b[2]:
            firsts.append(a[0])
            seconds.append(b[0])
    return firsts, seconds


def summarise(values) -> str:
    counts = ", ".join(f"{v}: {values.count(v)}" for v in sorted(set(values)))
    return f"{min(values)}-{max(values)}, mean {sum(values) / len(values):.2f}  ({counts})"


def cmd_arbiter(args) -> int:
    print("# what a Z80 access through the bank window costs: the referee, the board")
    print("# and the two hardware reports, on the same three images")
    print(
        f"# referee: Nuked-MD @ {referee_revision()}, window {args.start}-"
        f"{args.start + args.window} MCLK"
    )
    experiments = [
        ("68000 stopped, Z80 reading", Z80_READ, M68K_STOP, 'MoD: "2-3" wait states'),
        (
            "68000 in jmp (a0), Z80 reading",
            Z80_READ,
            M68K_SPIN,
            'MoD: 2-4, "seemed about 2.93"; Kabuto: 2-5, mean 3.3',
        ),
        (
            "68000 in jmp (a0), Z80 popping",
            Z80_POP,
            M68K_SPIN,
            'MoD: the second read "at least 4 wait states", mean 3.5 over 10',
        ),
    ]
    for name, z80_program, m68k_loop, hardware in experiments:
        rom = arbiter_rom(z80_program, m68k_loop)
        began = time.monotonic()
        run = nmd.run(
            rom,
            stop=args.start + args.window + 2000,
            trace=(args.start, args.start + args.window),
            trace_mask=7,
        )
        held = z80_bank_reads(run)
        board = board_bank_delays(rom, args.accesses)
        print(f"\n{name}")
        if held:
            waits = [w for _, w in held]
            print(f"  referee  ({len(waits):4} accesses): Z80 wait states {summarise(waits)}")
        else:
            print("  referee  : no bank access in the window")
        if board:
            print(
                f"  board    ({len(board):4} accesses): Z80 wait states "
                f"{summarise([z for z, _, _ in board])}"
            )
            m = [k for _, k, _ in board]
            print(
                f"                              68000 delay {min(m)}-{max(m)} cycles, "
                f"mean {sum(m) / len(m):.2f}"
            )
        print(f"  hardware : {hardware}")
        if z80_program is Z80_POP and held:
            firsts, seconds = [], []
            for i in range(len(held) - 1):
                if held[i + 1][0].zt - held[i][0].zt <= 3 + held[i][1]:
                    firsts.append(held[i][1])
                    seconds.append(held[i + 1][1])
            if firsts:
                print(
                    f"  referee, the pop's two reads: first {summarise(firsts)}; "
                    f"second {summarise(seconds)} ({len(firsts)} pairs)"
                )
            bf, bs = board_pop_split(board)
            if bf:
                print(
                    f"  board,   the pop's two reads: first {summarise(bf)}; "
                    f"second {summarise(bs)} ({len(bf)} pairs)"
                )
        print(f"  ({time.monotonic() - began:.0f} s)")
    return 0


# -- the bus grant --------------------------------------------------------------------


def back(body: bytes) -> bytes:
    """``body`` followed by a ``bra.w`` to its own first byte."""
    return body + b"\x60\x00" + (-(len(body) + 2) & 0xFFFF).to_bytes(2, "big")


#: Nuked-MD's Z80 drives /BUSAK *high* when it has granted the bus (md.c gives the
#: arbiter ``ext_zbak = !z80.o_busak``), so a grant is this value, not 0.
BUSACK_GRANTED = 1


def cmd_busack(args) -> int:
    """/BUSREQ to /BUSAK, which no hardware source in the pinned material measures.

    The 68000 asks for the Z80's bus, holds it, gives it back and asks again, for
    ever, while the Z80 runs a loop of its own; every grant the referee makes is timed
    from the clock the request reached the Z80.
    """
    print("# the Z80's bus grant: /BUSREQ asserted to /BUSAK asserted")
    print(
        f"# referee: Nuked-MD @ {referee_revision()}, window {args.start}-"
        f"{args.start + args.window} MCLK"
    )
    request = bytes.fromhex("33fc010000a11100")
    release = bytes.fromhex("33fc000000a11100")
    rom = arbiter_rom(Z80_READ, back(request + NOP * args.hold + release + NOP * args.gap))
    run = nmd.run(
        rom,
        stop=args.start + args.window + 2000,
        trace=(args.start, args.start + args.window),
        trace_mask=7,
    )
    events = sorted(
        (e.t, e.addr, e.data) for e in run.signals() if e.addr in (nmd.SIG_BUSREQ, nmd.SIG_BUSACK)
    )
    grants, pending = [], None
    for t, which, value in events:
        if which == nmd.SIG_BUSREQ:
            pending = t if value == 0 else None
        elif which == nmd.SIG_BUSACK and pending is not None and value == BUSACK_GRANTED:
            grants.append(t - pending)
            pending = None
    if not grants:
        print("  the referee granted the bus no times in the window")
        print(f"  (busreq/busack changes seen: {len(events)})")
        return 1
    print(
        f"  referee, /ZBR to /BUSAK        : {len(grants)} grants, "
        f"{min(grants)}-{max(grants)} MCLK "
        f"({min(grants) / M68K_DIVIDER:.2f}-{max(grants) / M68K_DIVIDER:.2f} 68000 "
        f"cycles), mean {sum(grants) / len(grants) / M68K_DIVIDER:.2f}"
    )
    # The quantity the board models is the whole thing: from the 68000's own write to
    # $A11100 to the moment the Z80 has let go.
    writes = [
        e.t for e in run.m68k(dma=False) if e.addr == 0xA11100 and not e.read and e.data & 0x0100
    ]
    acks = [e.t for e in run.signals(nmd.SIG_BUSACK) if e.data == BUSACK_GRANTED]
    whole = []
    for t in writes:
        later = [a for a in acks if a >= t]
        if later:
            whole.append(later[0] - t)
    if whole:
        print(
            f"  referee, the $A11100 write to /BUSAK: {len(whole)} grants, "
            f"{min(whole)}-{max(whole)} MCLK ({min(whole) / M68K_DIVIDER:.2f}-"
            f"{max(whole) / M68K_DIVIDER:.2f} 68000 cycles), mean "
            f"{sum(whole) / len(whole) / M68K_DIVIDER:.2f}"
        )
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES
    from megadrive_python.system import System

    system = System(MACHINES["genesis"], Cartridge(rom))
    system.board.vdp.rendering = False
    system.board.fm.enabled = False
    board = system.board
    board_grants: list[int] = []
    original = board.z80_busack

    def watch(mclk: int) -> int:
        at = original(mclk)
        board_grants.append(at - mclk)
        return at

    board.z80_busack = watch
    while len(board_grants) < max(len(grants), 200) and system.m68k_instructions < 2_000_000:
        system.step()
    if board_grants:
        print(
            f"  board  : {len(board_grants)} grants, {min(board_grants)}-"
            f"{max(board_grants)} MCLK ({min(board_grants) / M68K_DIVIDER:.2f}-"
            f"{max(board_grants) / M68K_DIVIDER:.2f} 68000 cycles), mean "
            f"{sum(board_grants) / len(board_grants) / M68K_DIVIDER:.2f}"
        )
    print(
        "  hardware: nothing in the pinned material measures this direction "
        "(docs/worklog.md, milestone 5)"
    )
    return 0


# -- the sound chips' wiring ----------------------------------------------------------


def sound_rom(register: int, value: int, polls: int = 8) -> bytes:
    """A cartridge that puts the FM through the board's own wiring and times BUSY.

    The 68000 releases the Z80's reset (the FM shares that line -- CMD 2.2, "for as
    long as the Z80 is reset, the YM2612 cannot be used"), takes the Z80's bus, writes
    one register and then polls $A04000 until bit 7 clears, counting the polls.  Eight
    such rounds are stored at $FF0000 on.  Everything the comparison needs is on the
    68000's bus, so the trace alone answers it: when the write reaches the chip, how
    long BUSY stands, and what each of the four ports reads back.
    """
    body = bytearray(UNLOCK)
    body += bytes.fromhex("33fc010000a11200")  # release the Z80's reset (and the FM's)
    body += bytes.fromhex("33fc010000a11100")  # take the Z80's bus
    body += NOP * 64
    body += bytes.fromhex("41f900ff0000")  # lea $00FF0000,a0
    for round_ in range(polls):
        # move.b #reg,$A04000 / move.b #data,$A04001  (MOVE.B #x is 8 bytes: opcode,
        # an immediate *word* whose low byte carries the data, then the long address)
        body += bytes.fromhex("13fc00") + bytes([register]) + bytes.fromhex("00a04000")
        body += (bytes.fromhex("13fc00") + bytes([(value + round_) & 0xFF])
                 + bytes.fromhex("00a04001"))  # fmt: skip
        body += bytes.fromhex("7000")  # moveq #0,d0
        # poll: addq.w #1,d0 / move.b $A04000,d1 / btst #7,d1 / bne.s poll
        body += bytes.fromhex("5240") + bytes.fromhex("123900a04000")
        body += bytes.fromhex("08010007") + bytes.fromhex("66f2")
        body += bytes.fromhex("30c0")  # move.w d0,(a0)+
    # what the other three ports read back, stored after the counts
    for port in range(4):
        body += bytes.fromhex("1239") + (0x00A04000 + port).to_bytes(4, "big")
        body += bytes.fromhex("10c1")  # move.b d1,(a0)+
    body += bytes.fromhex("4e722700")  # stop #$2700
    return cartridge(bytes(body))


def cmd_sound(args) -> int:
    """The board's *wiring* of the FM, which milestone 4's referees could not see."""
    print("# the FM through the board's wiring: BUSY after a data write, and what the")
    print("# four status ports read back, compared with the referee's whole bus stream")
    print(f"# referee: Nuked-MD @ {referee_revision()}.  Its FM is the CMOS YM3438 of")
    print("# an FC1004, so the board is run as `genesis2` (the ASIC FM variant) as well")
    print("# as `genesis` (the discrete YM2612), and both are reported.")
    rom = sound_rom(0x22, 0x00)
    run = nmd.run(rom, stop=REFEREE_RESET + args.window + 5000,
                  trace=(REFEREE_RESET, REFEREE_RESET + args.window),
                  trace_mask=1, dump_ram=True)  # fmt: skip
    reference = run.m68k(dma=False)
    print(f"\n  referee: {len(reference)} bus cycles in {args.window} MCLK")
    if run.ram:
        counts = [int.from_bytes(run.ram[i * 2 : i * 2 + 2], "big") for i in range(8)]
        ports = list(run.ram[16:20])
        print(f"  referee, polls of $A04000 until BUSY cleared: {counts}")
        print(f"  referee, $A04000-3 read back: {' '.join(f'{p:02X}' for p in ports)}")
    for machine in ("genesis", "genesis2"):
        n, diffs, _, _, _ = compare_rom(reference, rom, machine=machine, version=REFEREE_VERSION)
        print(f"\n  board as {machine}: {n - len(diffs)} of {n} bus cycles agree")
        for key, count in sorted(classify(diffs).items(), key=lambda kv: -kv[1]):
            print(f"    {count:5}  {key}")
        for d in diffs[: args.show]:
            print(f"    [{d.index}] {d.what}: {d.detail}")
    print("\n  hardware: Eke (forum t=386 start=810, a VA4 Model 1 and a VA0 Model 2)")
    print("  measured BUSY as 32 internal FM clocks -- 32 x 6 68000 cycles -- and found")
    print("  the discrete YM2612 answering on port 0 only, the ASIC on all four.")
    return 0


# -- the test ROMs --------------------------------------------------------------------


def _from_zip(archive: Path, member: str):
    """A ROM that lives inside one of the fetched archives (never unpacked into the
    tree): returns a callable so that nothing is read until the ROM is wanted."""

    def read() -> bytes:
        import zipfile

        with zipfile.ZipFile(archive) as zf:
            return zf.read(member)

    read.exists = archive.exists  # type: ignore[attr-defined]
    return read


#: The test ROMs of milestones 2-5, each with the machine to run it as.  A ROM here is
#: either a path or a reader for a member of a fetched archive.
NEMESIS = TEST_ROMS / "nemesis"
ROM_WINDOWS: dict[str, tuple[object, str, bool]] = {
    # milestone 2: the ports, the FIFO, the DMA, the counters
    "vdpfifotesting": (
        _from_zip(NEMESIS / "VDPFIFOTesting.zip", "VDPFIFOTesting/VDPFIFOTesting.bin"),
        "genesis",
        False,
    ),
    "hv_capture": (
        _from_zip(NEMESIS / "sr logic analyser roms.zip", "sr logic analyser noint - H40V28.bin"),
        "genesis",
        False,
    ),
    # milestone 3: the picture
    "sprite_masking": (NEMESIS / "SpriteMaskingTestRom.bin", "genesis", False),
    "vdp_tests": (TEST_ROMS / "r57shell" / "vdp_tests.bin", "genesis", False),
    "wtest": (TEST_ROMS / "eke" / "md_test" / "wtest.bin", "genesis", False),
    "stetest": (TEST_ROMS / "eke" / "md_test" / "stetest.bin", "genesis", False),
    "vctest": (TEST_ROMS / "eke" / "md_test" / "vctest.bin", "genesis", False),
    # milestone 3/5: the Z80's bus
    "gen_test_z80_bus": (TEST_ROMS / "mask-of-destiny" / "gen_test_z80_bus.md", "genesis", False),
    # milestone 4: the FM
    "ym2612csmmode": (NEMESIS / "ym2612csmmode.bin", "genesis", False),
    "ym2612detune": (NEMESIS / "ym2612detune.bin", "genesis", False),
    "ym2612multiplexing": (NEMESIS / "ym2612multiplexing.bin", "genesis", False),
}


def rom_bytes(source) -> bytes:
    return source() if callable(source) else Path(source).read_bytes()


def rom_exists(source) -> bool:
    return source.exists() if callable(source) else Path(source).exists()


def region(address: int) -> str:
    top = address >> 16
    if top < 0x40:
        return "cartridge"
    if top >= 0xE0:
        return "work RAM"
    if top >= 0xC0:
        return "VDP"
    if top == 0xA0:
        return "Z80 space"
    if top == 0xA1:
        return "I/O and control"
    return "open bus"


def classify(diffs) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in diffs:
        address = int(d.detail.split()[0], 16) if d.what != "stream" else 0
        key = f"{d.what} in {region(address)}" if d.what != "stream" else "stream"
        out[key] = out.get(key, 0) + 1
    return out


def cmd_roms(args) -> int:
    names = args.only.split(",") if args.only else sorted(ROM_WINDOWS)
    jobs = [(n, *ROM_WINDOWS[n]) for n in names if rom_exists(ROM_WINDOWS[n][0])]
    missing = [n for n in names if not rom_exists(ROM_WINDOWS[n][0])]
    print("# the test ROMs of milestones 2-5: every 68000 bus cycle from the reset")
    print("# vector on, compared with the referee's -- address, direction, size, the")
    print("# gap to the cycle before it, and the data on a word read.")
    print(
        f"# referee: Nuked-MD @ {referee_revision()}; its 68000 starts at "
        f"{REFEREE_RESET} MCLK and the window is {args.window} MCLK from there,"
    )
    print(f"# at most {args.cycles} bus cycles.  The board's clock is put at the same")
    print("# point so that its VDP is at the same place in the frame.")
    if missing:
        print(f"# not fetched, skipped: {', '.join(missing)}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
        for line in pool.map(lambda j: rom_report(j, args), jobs):
            print(line)
    return 0


def rom_report(job, args) -> str:
    name, source, machine, pal = job
    image = rom_bytes(source)
    digest = hashlib.sha256(image).hexdigest()[:16]
    began = time.monotonic()
    try:
        run = nmd.run(image, stop=REFEREE_RESET + args.window + 5000,
                      trace=(REFEREE_RESET, REFEREE_RESET + args.window),
                      trace_mask=1, pal=pal)  # fmt: skip
        reference = run.m68k(dma=False)[: args.cycles]
        if len(reference) < 32:
            return (
                f"\n{name} ({digest}): only {len(reference)} bus cycles in the "
                f"window -- nothing to compare"
            )
        n, diffs, cart_miss, ram_miss, seen = compare_rom(
            reference, image, machine=machine, version=REFEREE_VERSION
        )
        lines = [
            f"\n{name} ({digest}): {n - len(diffs)} of {n} bus cycles agree, "
            f"{seen} in the window ({time.monotonic() - began:.0f} s)"
        ]
        lines.append(
            f"    the board's refresh rules misplace {cart_miss} of the "
            f"referee's cartridge ticks and {ram_miss} of its work-RAM ticks"
        )
        lines.extend(
            f"    {count:5}  {key}"
            for key, count in sorted(classify(diffs).items(), key=lambda kv: -kv[1])
        )
        lines.extend(f"    [{d.index}] {d.what}: {d.detail}" for d in diffs[: args.show])
        if len(diffs) > args.show:
            lines.append(f"    ... and {len(diffs) - args.show} more")
        return "\n".join(lines)
    except Exception as exc:
        return f"\n{name} ({digest}): {type(exc).__name__}: {exc}"


# -- plumbing -------------------------------------------------------------------------


def referee_revision() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT / "reference" / "src" / "Nuked-MD",
            capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip
    except Exception:
        return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, *, start=3_000_000, window=6000):
        p.add_argument("--start", type=int, default=start, help="window start, MCLK")
        p.add_argument("--window", type=int, default=window, help="window length, MCLK")
        p.add_argument("--show", type=int, default=8, help="differences to print")

    p = sub.add_parser("probes", help="probe loops, cycle for cycle")
    common(p, window=20_000)
    p.add_argument("--cycles", type=int, default=400)
    p.set_defaults(func=cmd_probes)

    p = sub.add_parser("dma", help="the cost of a DMA transfer")
    p.add_argument("--words", default="1,2,4,8,16,32,64,128")
    p.add_argument("--targets", default="VRAM,CRAM,VSRAM")
    p.add_argument("--display-on", action="store_true", help="the display enabled")
    p.add_argument("--when", choices=("vblank", "active"), help="wait for this first")
    p.add_argument("--delay", type=int, default=0, help="NOPs after the wait")
    p.set_defaults(func=cmd_dma)

    p = sub.add_parser("arbiter", help="MoD's bank-window experiments")
    common(p, start=3_000_000, window=40_000)
    p.add_argument("--accesses", type=int, default=2000)
    p.set_defaults(func=cmd_arbiter)

    p = sub.add_parser("busack", help="the Z80's bus grant")
    common(p, start=3_000_000, window=40_000)
    p.add_argument("--hold", type=int, default=20, help="NOPs the 68000 holds the bus")
    p.add_argument("--gap", type=int, default=20, help="NOPs between two requests")
    p.set_defaults(func=cmd_busack)

    p = sub.add_parser("sound", help="the FM's and PSG's wiring")
    common(p, start=0, window=2_000_000)
    p.set_defaults(func=cmd_sound)

    p = sub.add_parser("roms", help="the test ROMs of milestones 2-5")
    common(p, start=0, window=200_000)
    p.add_argument("--only", help="comma-separated names")
    p.add_argument("--cycles", type=int, default=400)
    p.add_argument("--steps", type=int, default=128, help="refresh-phase search steps")
    p.add_argument("--fit", type=int, default=1200, help="cycles the phase fit uses")
    p.add_argument("--parallel", type=int, default=8)
    p.set_defaults(func=cmd_roms)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
