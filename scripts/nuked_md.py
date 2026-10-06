#!/usr/bin/env python3
"""Drive Nuked-MD, milestone 6's referee, and read its bus trace.

Nuked-MD (nukeykt, ogamespec, andkorzh; GPL-2.0, ``reference/src/Nuked-MD``) is a
gate-level model of the Mega Drive chipset built from decapped-die photographs.  It is
built here by ``validation/build_nuked_md.sh`` with a headless driver of our own
(``validation/nuked_md_headless.c``); nothing of it is copied into this repository.

**Its tier.**  ``docs/validation.md``'s rule: a die-derived model judges only where no
hardware source covers the question, and Nuked-MD has one more caveat of its own --
its README says it models "NMOS 68k, NMOS Z80 and FC1004.  No real mega drive used
such combination, but Model 1 VA7 would be closest".  So a difference that follows
from its chip combination, from its unimplemented parts (``Progress.md``) or from this
build's ``-DTMSS_ENABLE=0`` is a **named cause**, not a board bug.

What this module offers:

- :func:`ensure_built` -- build the referee if it is missing or older than its sources.
- :func:`run` -- run one ROM to a given half-MCLK, optionally tracing a window and
  dumping the memories, and return the parsed trace.
- :func:`m68k_cycles`, :func:`z80_cycles`, :func:`signals` -- views of a trace.
- :func:`board_trace` -- the *board's* own 68000 bus log in the same shape, so that the
  two can be compared cycle for cycle (:func:`align`, :func:`compare`).

Units.  Nuked-MD counts ``mcycles`` in **half** master clocks (two per MCLK); every
time this module hands back is converted to MCLK, the board's unit, and the conversion
is exact (every edge in the traces observed so far falls on an even half-MCLK).
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "reference" / "build-nuked-md"
BINARY = BUILD / "nukedmd-headless"
SOURCES = [
    ROOT / "validation" / "nuked_md_headless.c",
    ROOT / "validation" / "nuked_md_sdl.h",
    ROOT / "validation" / "build_nuked_md.sh",
]

#: Nuked-MD's ``mcycles`` are half master clocks.
HALF = 2
#: Master clocks per 68000 clock, as ``megadrive_python.machine`` has it.
M68K_DIVIDER = 7
#: Master clocks per Z80 clock.
Z80_DIVIDER = 15
#: The referee's /AS-low length for a 68000 bus cycle nothing is stretching.
AS_LOW = 18

EVENT = struct.Struct("<QIIHHBBH")
EV_M68K, EV_Z80, EV_SIGNAL, EV_MEM, EV_VRAM, EV_REFRESH = 1, 2, 3, 4, 5, 6
#: A work-RAM /AS with the refresh counter (``NMD_TRACE_MASK`` bit 6; its ``length``,
#: halved by :func:`read_trace` like every length, is the MCLK the VDP's H counter
#: has held its value), and the refresh taken.
EV_RAM_AS, EV_PENDING = 7, 8

#: ``validation/nuked_md_headless.c``'s signal numbers.
SIG_BUSREQ, SIG_BUSACK, SIG_ZRESET, SIG_BGACK = 0, 1, 2, 3
SIG_REFRESH, SIG_WAIT, SIG_SOUND, SIG_VRES = 4, 5, 6, 7
SIG_NAMES = {
    SIG_BUSREQ: "busreq", SIG_BUSACK: "busack", SIG_ZRESET: "zreset",
    SIG_BGACK: "bgack", SIG_REFRESH: "refresh", SIG_WAIT: "wait",
    SIG_SOUND: "sound", SIG_VRES: "vres",
}  # fmt: skip


@dataclass(frozen=True)
class Event:
    """One record of the referee's trace, times in MCLK."""

    t: int
    zt: int  # ZCLK rising edges since the run began
    addr: int
    data: int
    length: int  # MCLK the cycle held the bus (/AS low, or /MREQ+/IORQ low)
    type: int
    flags: int

    # -- type 1, the 68000's bus ------------------------------------------------
    @property
    def read(self) -> bool:
        return bool(self.flags & 1)

    @property
    def uds(self) -> bool:
        return bool(self.flags & 2)

    @property
    def lds(self) -> bool:
        return bool(self.flags & 4)

    @property
    def by_m68k(self) -> bool:
        """The 68000 drove /AS; False means the VDP did (a 68000-memory DMA cycle)."""
        return bool(self.flags & 8)

    @property
    def size(self) -> str:
        return "w" if self.uds and self.lds else ("b" if self.uds or self.lds else "-")

    @property
    def address(self) -> int:
        """The *byte* address.  The 68000 has no A0 pin: it drives the word address
        and selects the byte with /UDS and /LDS (68000 UM 5.1.2), so an odd byte
        access shows as an even address on the bus and /LDS alone."""
        return self.addr | 1 if self.lds and not self.uds else self.addr

    @property
    def kind(self) -> str:
        """``rw``/``rb``/``ww``/``wb``, the board's own log's key."""
        return ("r" if self.read else "w") + self.size

    # -- type 4, a work-RAM or cartridge select ------------------------------------
    @property
    def work_ram(self) -> bool:
        """Type 4: /RAS0 (work RAM) rather than /CE0 (the cartridge)."""
        return bool(self.flags & 1)

    @property
    def bus_granted(self) -> bool:
        """Type 4: /BGACK was asserted -- the VDP (or the Z80's arbiter) had the bus."""
        return bool(self.flags & 2)

    # -- type 2, the Z80's bus --------------------------------------------------
    @property
    def z80_read(self) -> bool:
        return bool(self.flags & 1)

    @property
    def z80_write(self) -> bool:
        return bool(self.flags & 2)

    @property
    def z80_m1(self) -> bool:
        return bool(self.flags & 16)


@dataclass
class Run:
    """What one run of the referee produced."""

    events: list[Event] = field(default_factory=list)
    ram: bytes = b""
    zram: bytes = b""
    vram: bytes = b""
    stdout: str = ""
    seconds: float = 0.0

    def m68k(self, *, dma: bool | None = None) -> list[Event]:
        return m68k_cycles(self.events, dma=dma)

    def z80(self) -> list[Event]:
        return z80_cycles(self.events)

    def signals(self, which: int | None = None) -> list[Event]:
        return signals(self.events, which)


# -- building ---------------------------------------------------------------------


def ensure_built(*, force: bool = False, quiet: bool = True) -> Path:
    """Build the referee if it is missing or older than the driver, and return it."""
    need = force or not BINARY.exists()
    if not need:
        stamp = BINARY.stat().st_mtime
        need = any(s.exists() and s.stat().st_mtime > stamp for s in SOURCES)
    if need:
        subprocess.run(
            ["sh", str(ROOT / "validation" / "build_nuked_md.sh")],
            check=True,
            cwd=ROOT,
            stdout=subprocess.DEVNULL if quiet else None,
        )
    return BINARY


# -- running ----------------------------------------------------------------------


def run(
    rom: bytes | Path,
    stop: int,
    *,
    trace: tuple[int, int] | None = None,
    trace_mask: int = 7,
    presses: Sequence[tuple[int, int]] = (),
    pal: bool = False,
    japanese: bool = False,
    dump_ram: bool = False,
    dump_zram: bool = False,
    dump_vram: bool = False,
    nice: int = 10,
    tmp: Path | None = None,
) -> Run:
    """Run one ROM on the referee.

    ``stop`` and ``trace`` are in **MCLK** (this module converts); ``presses`` is a
    list of ``(mclk, button mask)`` for pad 1, Nuked-MD's ``controller.h`` bits
    (START = 128).  Nothing is left behind: the ROM, the trace and the dumps live in a
    temporary directory that is removed before returning.
    """
    ensure_built()
    with tempfile.TemporaryDirectory(dir=tmp or os.environ.get("TMPDIR")) as tmpdir:
        work = Path(tmpdir)
        if isinstance(rom, Path):
            rom_path = rom
        else:
            rom_path = work / "rom.bin"
            rom_path.write_bytes(rom)
        env = dict(os.environ, NMD_STOP=str(stop * HALF))
        if trace is not None:
            env["NMD_TRACE"] = str(work / "trace.bin")
            env["NMD_TRACE_FROM"] = str(trace[0] * HALF)
            env["NMD_TRACE_TO"] = str(trace[1] * HALF)
            env["NMD_TRACE_MASK"] = str(trace_mask)
        if presses:
            env["NMD_PRESS"] = ";".join(f"{t * HALF}:{mask}" for t, mask in presses)
        if dump_ram:
            env["NMD_DUMP_RAM"] = str(work / "ram.bin")
        if dump_zram:
            env["NMD_DUMP_ZRAM"] = str(work / "zram.bin")
        if dump_vram:
            env["NMD_DUMP_VRAM"] = str(work / "vram.bin")
        argv = ["nice", "-n", str(nice), str(BINARY)]
        if pal:
            argv.append("-pal")
        if japanese:
            argv.append("-jap")
        argv.append(str(rom_path))
        import time

        began = time.monotonic()
        proc = subprocess.run(argv, env=env, capture_output=True, text=True, cwd=work)
        out = Run(stdout=proc.stdout, seconds=time.monotonic() - began)
        if proc.returncode not in (0, None):
            raise RuntimeError(f"Nuked-MD exited {proc.returncode}: {proc.stderr[-400:]}")
        if trace is not None:
            out.events = read_trace(work / "trace.bin")
        for name, flag in (("ram", dump_ram), ("zram", dump_zram), ("vram", dump_vram)):
            if flag and (work / f"{name}.bin").exists():
                setattr(out, name, (work / f"{name}.bin").read_bytes())
        return out


def read_trace(path: Path) -> list[Event]:
    """Parse a trace file into MCLK-based events, in time order.

    A bus cycle is written when it ends, so the raw file is not quite sorted: a signal
    change inside a bus cycle is written first.  Sorting by ``t`` restores the order.
    """
    raw = path.read_bytes()
    events = []
    for off in range(0, len(raw) - EVENT.size + 1, EVENT.size):
        t, zt, addr, data, length, kind, flags, _ = EVENT.unpack_from(raw, off)
        events.append(Event(t // HALF, zt, addr, data, length // HALF, kind, flags))
    events.sort(key=lambda e: (e.t, e.type))
    return events


# -- views ------------------------------------------------------------------------


def m68k_cycles(events: Iterable[Event], *, dma: bool | None = None) -> list[Event]:
    """The 68000-bus cycles.  ``dma=False`` keeps only the ones the 68000 itself drove,
    ``dma=True`` only the ones the VDP drove (a 68000-memory DMA)."""
    out = [e for e in events if e.type == EV_M68K]
    if dma is True:
        return [e for e in out if not e.by_m68k]
    if dma is False:
        return [e for e in out if e.by_m68k]
    return out


def z80_cycles(events: Iterable[Event]) -> list[Event]:
    return [e for e in events if e.type == EV_Z80]


def memory_cycles(events: Iterable[Event]) -> list[Event]:
    """The work-RAM and cartridge selects (``NMD_TRACE_MASK`` bit 3)."""
    return [e for e in events if e.type == EV_MEM]


def signals(events: Iterable[Event], which: int | None = None) -> list[Event]:
    out = [e for e in events if e.type == EV_SIGNAL]
    return out if which is None else [e for e in out if e.addr == which]


# -- the board's side of the comparison -------------------------------------------


@dataclass(frozen=True)
class Access:
    """One access on the board's 68000 bus: the same shape as :class:`Event`."""

    t: int
    addr: int
    kind: str
    data: int
    #: Master clocks the board held the 68000 for during this access -- a refresh
    #: stretch or a VDP stall.  The referee's own figure is ``length - AS_LOW``.
    stall: int = 0


def board_trace(
    rom: bytes,
    *,
    machine: str = "genesis",
    accesses: int = 20000,
    offset: int = 0,
    stop: Callable[[object], bool] | None = None,
    setup: Callable[[object], None] | None = None,
    z80_log: list[tuple[int, int, str, int]] | None = None,
):
    """Run the board on ``rom`` and return (system, its 68000 bus log).

    The log is collected by wrapping the four bus callables the core holds, through
    m68000-python's public ``attach_bus`` -- the board itself is not modified, and the
    clock recorded is ``Board.m68k_access_clock()`` *before* the access, which is where
    the bus cycle begins (its own refresh stretch is charged inside it).  DMA reads of
    68000 memory are recorded too, as ``rd`` (what Nuked-MD marks "the VDP drove /AS").
    """
    sys.path.insert(0, str(ROOT / "src"))
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES
    from megadrive_python.system import System

    system = System(MACHINES[machine], Cartridge(rom))
    board = system.board
    board.fm.enabled = False
    board.vdp.rendering = False
    if offset:
        # Put the board's clock where the referee's 68000 starts, so that the VDP is
        # at the same point in its frame when the program begins.  The referee holds
        # its 68000 in reset while the FC1004 powers up; the board's starts at 0.
        system.mclk += offset
        system.z80_mclk += offset
        board.step_start = system.mclk
        board.cart_refresh_due += offset
        board.ram_refresh_due += offset
    if setup is not None:
        setup(system)
    log: list[Access] = []

    def wrap_read(fn, kind):
        def f(address, *a, **k):
            t = board.m68k_access_clock()
            before = board.stall
            value = fn(address, *a, **k)
            log.append(Access(t, address, kind, value, board.stall - before))
            return value

        return f

    def wrap_write(fn, kind):
        def f(address, value, *a, **k):
            t = board.m68k_access_clock()
            before = board.stall
            fn(address, value, *a, **k)
            log.append(Access(t, address, kind, value, board.stall - before))

        return f

    system.m68k.attach_bus(
        wrap_read(board.read_byte, "rb"), wrap_read(board.read_word, "rw"),
        wrap_write(board.write_byte, "wb"), wrap_write(board.write_word, "ww"),
    )  # fmt: skip
    dma_read = board.dma_read_word

    def dma(address):
        value = dma_read(address)
        log.append(Access(board.m68k_access_clock(), address, "rd", value))
        return value

    board.dma_read_word = dma  # type: ignore[method-assign]

    if z80_log is not None:
        z80_read, z80_write = board.z80_read, board.z80_write

        def zr(address):
            value = z80_read(address)
            z80_log.append((board.step_start + board.z80_stall, address, "r", value))
            return value

        def zw(address, value):
            z80_write(address, value)
            z80_log.append((board.step_start + board.z80_stall, address, "w", value))

        system.z80.read_byte, system.z80.write_byte = zr, zw

    idle = 0
    while len(log) < accesses:
        if stop is not None and stop(system):
            break
        before = len(log)
        system.step()
        # A program that ends in ``stop #$2700`` makes no more bus cycles; give up
        # rather than spin (the referee's own run ends at its NMD_STOP instead).
        idle = 0 if len(log) > before else idle + 1
        if idle > 2000:
            break
    return system, log


# -- comparing --------------------------------------------------------------------


def key(item) -> tuple[int, str]:
    """The alignment key of a bus cycle or a board access: byte address and kind."""
    return (item.address if isinstance(item, Event) else item.addr, item.kind)


def align(reference: Sequence, board: Sequence, *, run_length: int = 24) -> tuple[int, int]:
    """Find where the two streams begin to say the same thing.

    ``run_length`` consecutive (address, kind) pairs must agree.  Returns the pair of
    indices, or raises if no such point exists.  The referee's trace begins wherever
    its window opened, so this is how the board's own stream is anchored to it.
    """
    want = [key(e) for e in reference[:run_length]]
    have = [key(a) for a in board]
    for i in range(len(have) - run_length + 1):
        if have[i : i + run_length] == want:
            return 0, i
    # The window may have opened part way through an instruction the board began
    # earlier; try later starting points in the referee's stream too.
    for skip in range(1, min(64, len(reference) - run_length)):
        want = [key(e) for e in reference[skip : skip + run_length]]
        for i in range(len(have) - run_length + 1):
            if have[i : i + run_length] == want:
                return skip, i
    raise RuntimeError("the two bus streams never agree on 24 consecutive accesses")


@dataclass
class Difference:
    index: int
    t_ref: int
    t_board: int
    what: str
    detail: str


def compare(
    reference: Sequence[Event],
    board: Sequence[Access],
    *,
    run_length: int = 24,
    limit: int | None = None,
    data: bool = True,
) -> tuple[int, list[Difference]]:
    """Compare two aligned bus streams cycle for cycle.

    Returns (how many cycles were compared, the differences).  Each cycle is compared
    on its address, its kind, the gap to the previous cycle (so a stall shows where it
    is charged) and, optionally, the data on the bus.  Comparison stops at the first
    address or kind difference: after that the two machines are running different
    code and nothing later is evidence of anything.
    """
    r0, b0 = align(reference, board, run_length=run_length)
    n = min(len(reference) - r0, len(board) - b0)
    if limit is not None:
        n = min(n, limit)
    out: list[Difference] = []
    for i in range(n):
        e, a = reference[r0 + i], board[b0 + i]
        if e.address != a.addr or e.kind != a.kind:
            out.append(
                Difference(i, e.t, a.t, "stream",
                           f"{e.address:06X} referee {e.address:06X} {e.kind}, "
                           f"board {a.addr:06X} {a.kind}")
            )  # fmt: skip
            return i, out
        if e.length - AS_LOW != a.stall:
            out.append(
                Difference(i, e.t, a.t, "stall",
                           f"{e.address:06X} {e.kind}: referee held the 68000 "
                           f"{e.length - AS_LOW} MCLK, board {a.stall} "
                           f"({a.stall - (e.length - AS_LOW):+d})")
            )  # fmt: skip
        elif i:
            gap_r = e.t - reference[r0 + i - 1].t
            gap_b = a.t - board[b0 + i - 1].t
            if gap_r != gap_b:
                out.append(
                    Difference(i, e.t, a.t, "gap",
                               f"{e.address:06X} {e.kind}: referee {gap_r} MCLK, "
                               f"board {gap_b} MCLK ({gap_b - gap_r:+d})")
                )  # fmt: skip
        if data and e.read:
            # A byte read drives only half the bus; the referee records the whole
            # sixteen bits, so compare the half the strobes selected.
            if e.kind == "rw":
                seen, mine = e.data & 0xFFFF, a.data & 0xFFFF
            elif e.kind == "rb":
                shift = 0 if e.lds else 8
                seen, mine = (e.data >> shift) & 0xFF, a.data & 0xFF
            else:
                seen = mine = 0
            if seen != mine:
                out.append(
                    Difference(i, e.t, a.t, "data",
                               f"{e.address:06X} read: referee {seen:0{len(e.kind) * 2}X}, "
                               f"board {mine:0{len(e.kind) * 2}X}")
                )  # fmt: skip
    return n, out
