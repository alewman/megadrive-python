#!/usr/bin/env python3
"""Run the board's two CPUs in lockstep with a MAME 0.285 trace of both.

    python scripts/mame_trace.py genesis GAME.zip --seconds 30   # records the logs
    python scripts/lockstep_mame.py tests/mame_traces/genesis-GAME

The logs are ``scripts/mame_trace.py``'s ``m68k.log`` and ``z80.log``
(scripts/mame_log.py reads them). The lockstep builds the whole board around both
cores and steps each CPU once per logged instruction, the two streams merged by
master clock:

- the 68000's records are placed at ``totalcycles`` x 7;
- the Z80's ``totalcycles`` stands still while it is held (docs/mame-oracle.md), so
  its records are placed from the moment the board lets it run: when the 68000
  releases the Z80's bus or reset, at the clock of that very write inside its
  instruction (``Board.m68k_access_clock``), the next Z80 record is anchored there
  and the Z80's later records follow at 15 master clocks per cycle. The Z80 runs
  the records that fall before each 68000 record, and before every 68000 access
  that changes what the Z80 can see (the board's catch-up points). So the board's
  own bus-request and reset logic decides when the Z80 runs, and a MAME Z80
  instruction that falls where the board holds the Z80 is a divergence.

Before every instruction each CPU's registers are compared with MAME's. Every
watched access the core makes must be the next one MAME logged for that
instruction (kind, address, size), and:

- every write is checked, value and all;
- reads of devices that are stubs in this milestone are replayed from the log: the
  VDP ($C00000-$C0001F; the Z80's $7F00-$7F1F) and the YM2612's status;
- every other read (the I/O chip, the bus request register, Z80 RAM, the Z80's
  bank window) is computed by the board and compared. Where the board and MAME
  differ for a reason with a source, the difference is counted under that named
  class (MAME is a detector, never a judge: docs/validation.md) and MAME's value is
  given to the CPU so the run stays on MAME's path; any other difference is a
  divergence and stops the run (``--patch`` counts it and goes on).

Interrupts are taken where MAME took them (docs/mame-oracle.md, trap 8): the VDP is a
stub in milestone 1. The clock count of every instruction is compared too, and every
difference sorted into a class.
"""

from __future__ import annotations

import argparse
import collections
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(Path(__file__).resolve().parent)]

from mame_log import Access, Record, m68k_records, z80_records  # noqa: E402

from megadrive_python.board import Lockup  # noqa: E402
from megadrive_python.cartridge import Cartridge, load_image  # noqa: E402
from megadrive_python.machine import MACHINES, MAME_DRIVERS  # noqa: E402
from megadrive_python.system import System  # noqa: E402

M68K_NAMES = ("pc", "sr", *(f"d{i}" for i in range(8)), *(f"a{i}" for i in range(7)),
              "usp", "ssp")  # fmt: skip
Z80_NAMES = ("pc", "af", "bc", "de", "hl", "ix", "iy", "sp", "af'", "bc'", "de'", "hl'", "r")


DEBUG = bool(__import__("os").environ.get("LOCKSTEP_DEBUG"))


@dataclass
class Inflight:
    """A Z80 record the 68000's accesses fell inside (MAME's markers), kept so the
    other orders can be tried: the Z80's state and RAM before it, the order it was
    run in ("before" or "after" the 68000's accesses, or "cut": not run, a reset came
    first), and the 68000's writes to Z80 RAM and resets of the Z80 among those
    accesses, in order."""

    record: Record
    state: object
    ram: bytes
    order: str
    events: list[tuple[int, int] | None] = field(default_factory=list)  # None: a reset
    closed: bool = False


class Divergence(Exception):
    """The board and MAME disagree without a named reason."""


def watch_68k(address: int) -> str | None:
    """The tag MAME's 68000 run watches ``address`` under (scripts/mame_trace.py)."""
    if 0xC00000 <= address < 0xC00020:
        return "V"
    if 0xA00000 <= address < 0xA10000:
        return "A"
    if 0xA10000 <= address < 0xA10020:
        return "I"
    if 0xA11100 <= address < 0xA11102 or 0xA11200 <= address < 0xA11202:
        return "B"
    return None


class Lockstep:
    def __init__(self, machine_name: str, image: bytes, trace: Path, patch: bool) -> None:
        self.system = System(MACHINES[machine_name], Cartridge.from_image(image))
        self.board = board = self.system.board
        board.refresh_enabled = False  # MAME models neither refresh (docs/worklog.md)
        self.m68k = cpu = self.system.m68k
        self.z80 = z80 = self.system.z80
        self.patch = patch
        cpu.tas_write = board.tas_write
        cpu.attach_bus(self.m68k_read_byte, self.m68k_read_word,
                       self.m68k_write_byte, self.m68k_write_word)  # fmt: skip
        z80.read_byte = self.z80_read
        z80.write_byte = self.z80_write
        # The markers order the Z80 against the 68000's accesses to its side: the
        # board's own catch-up points are not needed here.
        board.catch_up_z80 = lambda mclk, stopping=False: None
        self.pending_68k: list[Access] = []
        self.pending_z80: list[Access] = []
        # Accesses the Z80 made through its bank to windows MAME's 68000 run watches:
        # they appear in m68k.log too (docs/mame-oracle.md, trap 5) and are dropped.
        self.foreign: deque[tuple[str, int, int]] = deque()
        self.z80_stream = z80_records(trace / "z80.log")
        self.zrec: Record | None = None
        self.zrec_ran = True
        self.zmarker = 0
        self.zpeek: Record | None = None
        self.anchor_time = 0
        self.anchor_cycles = 0
        self.z80_previous: tuple[int, int] | None = None  # (MAME cycles, our T-states)
        self.z80_powered = False
        self.z80_cut = 0  # records cut short by a Z80 reset
        self.z80_deferred = 0  # records MAME finished after a 68000 access
        self.z80_while_held = 0  # records MAME ran after the request, catching up
        self.releases = 0  # the 68000's releases of the Z80's bus (markers), counted
        self.releases_at_previous = self.releases_before_previous = 0
        self.last_z80_writes: list[tuple[int, int]] = []  # (address, old value)
        self.inflight: Inflight | None = None
        # Counters.
        self.m68k_count = self.z80_count = 0
        self.m68k_interrupts = self.z80_interrupts = 0
        self.classes: collections.Counter[str] = collections.Counter()
        self.examples: dict[str, str] = {}
        self.failures: list[str] = []
        self.m68k_clocks: collections.Counter[str] = collections.Counter()
        self.z80_clocks: collections.Counter[str] = collections.Counter()
        self.clock_examples: dict[str, list[str]] = collections.defaultdict(list)
        self.history: deque[str] = deque(maxlen=12)

    # -- bookkeeping ---------------------------------------------------------

    def note(self, name: str, detail: str) -> None:
        self.classes[name] += 1
        self.examples.setdefault(name, detail)

    def fail(self, detail: str) -> None:
        self.failures.append(detail)
        if not self.patch or len(self.failures) > 1000:
            raise Divergence(detail)

    # -- the 68000's bus, checked against MAME --------------------------------

    def pop_68k(self, kind: str, tag: str, address: int, size: int) -> Access:
        want = tag + kind
        pending = self.pending_68k
        while pending:
            head = pending[0]
            if head.tag == want and head.address == address and head.size == size:
                return pending.pop(0)
            if self.drop_foreign(head):
                pending.pop(0)
                continue
            raise Divergence(f"68000 {want} {address:06X}.{size}; MAME's next access is {head}")
        raise Divergence(f"68000 {want} {address:06X}.{size} that MAME did not make")

    def drop_foreign(self, access: Access) -> bool:
        key = (access.tag, access.address, access.value)
        if key in self.foreign:
            self.foreign.remove(key)
            return True
        return False

    def judge_68k_read(self, tag: str, address: int, size: int, ours: int, theirs: int) -> int:
        if ours == theirs:
            return theirs
        where = f"{tag}R {address:06X}.{size}: board {ours:X}, MAME {theirs:X}"
        board = self.board
        if tag == "V":
            self.note("VDP read replayed (the VDP is a stub in milestone 1)", where)
        elif tag == "A":
            offset = address & 0x7FFF
            if 0x4000 <= offset < 0x6000:
                self.note("YM2612 status replayed (the YM2612 is a stub until milestone 4)", where)
            elif not board.m68k_has_z80_bus:
                self.note(
                    "Z80 space read without the bus: MAME returns rand(); the board the "
                    "next opcode's MSB (CMD gen-hw.txt 2.2)", where)  # fmt: skip
            elif offset < 0x4000 and self.interleaved(address, size, theirs):
                self.note(
                    "Z80 RAM read by the 68000 inside a Z80 instruction: MAME's timeslices "
                    "put the instruction's write on the other side of the read than the "
                    "board's machine-cycle estimate (the value matches it on that side)",
                    where)  # fmt: skip
            elif size == 16 and ours >> 8 == theirs >> 8 and offset < 0x4000:
                self.note(
                    "Z80 RAM word read: MAME returns both bytes; the board duplicates the "
                    "even byte (CMD gen-hw.txt 1.2)", where)  # fmt: skip
            else:
                self.fail(where)
        elif (
            tag == "I"
            and 2 <= address & 0x1F < 8
            and (ours ^ theirs) & 0xFF == 0x80
            and not board.io.ports[((address & 0x1F) >> 1) - 1].data_written
        ):
            self.note(
                "I/O data register bit 7 before any write: MAME starts the latch at 1 "
                "(sega_md_ioport.cpp, m_data = 0xff); the board at 0 (CMD gen-hw.txt 3.0: "
                "the data registers read $7F)", where)  # fmt: skip
        elif tag == "B":
            bit = 0x100 if size == 16 else 0x01 if not address & 1 else 0
            if (ours ^ theirs) & bit:
                self.fail(where + " (the bus-grant bit)")
            else:
                self.note(
                    "$A11100 read, bits other than the grant: MAME returns rand(); the "
                    "board the next opcode's MSB and a zero LSB (CMD gen-hw.txt 1 note 4)",
                    where,
                )
        else:
            self.fail(where)
        return theirs

    def interleaved(self, address: int, size: int, theirs: int) -> bool:
        """Whether MAME's value is the byte as it was on the other side of the Z80
        instruction in flight: before the last instruction run, or after the one
        left for later."""
        byte = theirs >> 8 if size == 16 and not address & 1 else theirs & 0xFF
        where = address & 0x1FFF
        for written, old in self.last_z80_writes:
            if written & 0x1FFF == where and old == byte:
                return True
        if not self.zrec_ran and self.zrec is not None and self.zrec.registers:
            _, writes = self.system.z80_probe()
            return any(a & 0x1FFF == where and v == byte for a, v in writes.items())
        return False

    def m68k_read_word(self, address: int) -> int:
        tag = watch_68k(address)
        if tag is None:
            return self.board.read_word(address)
        if tag in "AB":
            self.z80_until_marker("R", address, 16)
        ours = self.board.read_word(address)
        theirs = self.pop_68k("R", tag, address, 16).value
        return self.judge_68k_read(tag, address, 16, ours, theirs)

    def m68k_read_byte(self, address: int) -> int:
        tag = watch_68k(address)
        if tag is None:
            return self.board.read_byte(address)
        if tag in "AB":
            self.z80_until_marker("R", address, 8)
        ours = self.board.read_byte(address)
        theirs = self.pop_68k("R", tag, address, 8).value
        return self.judge_68k_read(tag, address, 8, ours, theirs)

    def note_68k_ram_write(self, address: int, value: int) -> None:
        inflight = self.inflight
        if (inflight is not None and not inflight.closed and address & 0x7FFF < 0x4000
                and self.board.m68k_has_z80_bus):  # fmt: skip
            inflight.events.append((address & 0x1FFF, value))

    def note_68k_reset_write(self, address: int, value: int) -> None:
        inflight = self.inflight
        if (inflight is not None and not inflight.closed and address & 0xFFFF00 == 0xA11200
                and not value & 0x101 and not self.board.z80_reset):  # fmt: skip
            inflight.events.append(None)
            if inflight.order == "after" and self.zrec is inflight.record and not self.zrec_ran:
                # A reset under way cannot be followed by the old instruction: it is cut
                # (MAME's count need not restart for that, as Phantasy Star IV shows).
                inflight.order = "cut"
                self.zrec_ran = True
                self.z80_cut += 1
                self.z80_previous = None

    def m68k_write_word(self, address: int, value: int) -> None:
        self.check_68k_write(address, value, 16)
        self.board.write_word(address, value)

    def m68k_write_byte(self, address: int, value: int) -> None:
        self.check_68k_write(address, value, 8)
        self.board.write_byte(address, value)

    def check_68k_write(self, address: int, value: int, size: int) -> None:
        tag = watch_68k(address)
        if tag is None:
            return
        if tag in "AB":
            self.z80_until_marker("W", address, size)
            if tag == "A":
                self.note_68k_ram_write(address & ~1 if size == 16 else address,
                                        value >> 8 if size == 16 else value)  # fmt: skip
            else:
                self.note_68k_reset_write(address, value)
        theirs = self.pop_68k("W", tag, address, size).value
        if theirs != value:
            self.fail(f"{tag}W {address:06X}.{size}: board {value:X}, MAME {theirs:X}")

    # -- the Z80's bus, checked against MAME ------------------------------------

    def pop_z80(self, tag: str, address: int) -> Access:
        pending = self.pending_z80
        if pending:
            head = pending[0]
            if head.tag == tag and head.address == address:
                return pending.pop(0)
            raise Divergence(f"Z80 {tag} {address:04X}; MAME's next access is {head}")
        raise Divergence(f"Z80 {tag} {address:04X} that MAME did not make")

    def z80_read(self, address: int) -> int:
        board = self.board
        ours = board.z80_read(address)
        if address < 0x4000:
            return ours
        tag = "YR" if address < 0x8000 else "KR"
        theirs = self.pop_z80(tag, address).value
        if address >= 0x8000:
            target = board.bank | (address & 0x7FFF)
            if watch_68k(target):
                self.foreign.append((watch_68k(target) + "R", target, theirs))
        if ours == theirs:
            return theirs
        where = f"{tag} {address:04X}: board {ours:X}, MAME {theirs:X}"
        if 0x4000 <= address < 0x6000:
            self.note("YM2612 status replayed (the YM2612 is a stub until milestone 4)", where)
        elif 0x7F00 <= address < 0x7F20:
            self.note("VDP read by the Z80 replayed (the VDP is a stub in milestone 1)", where)
        elif address >= 0x8000 and (board.bank | (address & 0x7FFF)) >= 0xE00000:
            self.note(
                "Z80 read of 68000 RAM through the bank: MAME returns the RAM; the board "
                "$FF (MoD's gen_test_z80_bus: reads fail; CMD gen-hw.txt 2.3)", where)  # fmt: skip
        else:
            self.fail(where)
        return theirs

    def z80_write(self, address: int, value: int) -> None:
        board = self.board
        if address < 0x4000:
            self.last_z80_writes.append((address, board.z80_ram[address & 0x1FFF]))
        if address >= 0x4000:
            tag = "YW" if address < 0x8000 else "KW"
            theirs = self.pop_z80(tag, address).value
            if theirs != value:
                self.fail(f"{tag} {address:04X}: board {value:X}, MAME {theirs:X}")
            if address >= 0x8000:
                target = board.bank | (address & 0x7FFF)
                if watch_68k(target):
                    self.foreign.append((watch_68k(target) + "W", target, value))
        board.z80_write(address, value)

    # -- the Z80's records ------------------------------------------------------

    def z80_registers(self) -> list[int]:
        z = self.z80
        return [z.pc, (z.a << 8) | int(z.f), (z.b << 8) | z.c, (z.d << 8) | z.e, (z.h << 8) | z.l,
                z.ix, z.iy, z.sp, z.af_, z.bc_, z.de_, z.hl_, z.r]  # fmt: skip

    def set_z80_registers(self, values: list[int]) -> None:
        z = self.z80
        (z.pc, af, bc, de, hl, z.ix, z.iy, z.sp, z.af_, z.bc_, z.de_, z.hl_, z.r) = values
        z.a, z.f, z.b, z.c = af >> 8, af & 0xFF, bc >> 8, bc & 0xFF
        z.d, z.e, z.h, z.l = de >> 8, de & 0xFF, hl >> 8, hl & 0xFF

    # -- the Z80's stream, interleaved as MAME ran it --------------------------------

    def load_next(self) -> Record | None:
        record = self.zrec = next(self.z80_stream, None)
        self.zrec_ran = record is None or not record.registers
        self.zmarker = 0
        return record

    def lookahead(self) -> Record | None:
        if self.zpeek is None:
            self.zpeek = next(self.z80_stream, None)
        return self.zpeek

    def mapped(self, record: Record) -> int:
        return self.anchor_time + (record.cycles - self.anchor_cycles) * 15

    def run_current(self) -> None:
        """Run the current record, unless a reset cut it short in MAME."""
        record = self.zrec
        self.zrec_ran = True
        following = self.lookahead()
        if following is not None and following.registers and following.cycles < record.cycles:
            # MAME restarts the Z80's count when it resets it, and the instruction logged
            # last before the reset never finished: it is not run.
            self.z80_cut += 1
            self.z80_previous = None
            if self.inflight is not None and self.inflight.record is record:
                self.inflight.order = "cut"
            return
        if not self.board.z80_running:
            # MAME catches the Z80 up to the 68000's time before the request takes
            # effect (the timer the write sets fires at the end of that catch-up).
            self.z80_while_held += 1
        self.run_z80(record)

    def next_record(self) -> Record | None:
        """Move past the current record (run already) to the next one."""
        if self.inflight is not None and self.inflight.record is self.zrec:
            self.inflight.closed = True
        if self.zpeek is not None:
            record, self.zpeek = self.zpeek, None
            self.zrec = record
            self.zrec_ran = not record.registers
            self.zmarker = 0
            return record
        return self.load_next()

    def z80_until_marker(self, kind: str, address: int, size: int) -> Access:
        """Advance the Z80's stream to the marker of this 68000 access and return it.

        Every record before the marker's has run. The record the marker fell in ran
        only partly before it in MAME if the marker's count lies inside it: it is run
        now if the board's model of where its accesses fall
        (``System.z80_access_offsets``) has them all started by then, and after its
        last marker otherwise.
        """
        while True:
            record = self.zrec
            if record is None:
                record = self.next_record()
                if record is None:
                    raise Divergence(f"68000 M{kind} {address:06X} with no Z80-side marker left")
                continue
            if self.zmarker < len(record.markers):
                _, marker, cycles = record.markers[self.zmarker]
                if not self.zrec_ran and self.zmarker == 0:
                    elapsed = cycles - record.cycles
                    offsets = self.system.z80_access_offsets()
                    before = all(t < elapsed for t in offsets)
                    # Kept so the other order can be tried if the Z80's next registers
                    # say MAME ran it on the other side (see run_z80).
                    self.inflight = Inflight(record, self.z80.capture_state(),
                                             bytes(self.board.z80_ram),
                                             "before" if before else "after")  # fmt: skip
                    if before:
                        self.run_current()
                    else:
                        self.z80_deferred += 1
                if marker.tag != "M" + kind or marker.address != address:
                    raise Divergence(
                        f"68000 {kind} {address:06X}.{size}; the Z80's run logged {marker} next"
                    )
                self.zmarker += 1
                self.anchor_time, self.anchor_cycles = self.board.m68k_access_clock(), cycles
                if kind == "W" and address & 0xFFFF00 == 0xA11100 and not marker.value & 0x101:
                    self.releases += 1  # the record under way's count jumps by the hold
                return marker
            if not self.zrec_ran:
                self.run_current()
            self.next_record()

    def catch_up_z80(self, mclk: int) -> None:
        """Run the Z80's records whose count places them before ``mclk``, stopping at
        a record that holds a marker the 68000 has not reached."""
        while True:
            record = self.zrec
            if record is None:
                record = self.next_record()
                if record is None:
                    return
                continue
            if self.zmarker < len(record.markers):
                return
            if self.zrec_ran:
                self.next_record()
                continue
            if self.mapped(record) >= mclk:
                return
            self.run_current()

    def try_other_order(self, record: Record) -> bool:
        """Replay the last instruction the 68000's accesses fell inside in another
        order against them; keep the first order after which the Z80 reaches MAME's
        registers."""
        inflight, self.inflight = self.inflight, None
        if inflight is None or not inflight.closed:
            return False
        board, z80 = self.board, self.z80
        after_state, after_ram = z80.capture_state(), bytes(board.z80_ram)
        resets = None in inflight.events
        orders = ["before", "cut"] if resets else ["before", "after"]
        for order in orders:
            if order == inflight.order:
                continue
            z80.restore_state(inflight.state)
            board.z80_ram[:] = inflight.ram
            if order == "before":
                self.pending_z80 = list(inflight.record.accesses)
                z80.step()
            for event in inflight.events:
                if event is None:
                    self.system._z80_reset_line(True)  # the board's reset, I and R cleared
                else:
                    board.z80_ram[event[0]] = event[1]
            if order == "after":
                self.pending_z80 = list(inflight.record.accesses)
                z80.step()
            if self.z80_registers() == record.registers:
                self.note(
                    "Z80 instruction the 68000's accesses fell inside: MAME's timeslices "
                    "ran it on another side of them than the board's machine-cycle "
                    "estimate, or finished it before a reset (the Z80's next registers "
                    "match that order)",
                    f"Z80 {inflight.record.registers[0]:04X} @ {inflight.record.cycles}: "
                    f"board {inflight.order}, MAME {order}")  # fmt: skip
                return True
        z80.restore_state(after_state)
        board.z80_ram[:] = after_ram
        return False

    def run_z80(self, record: Record) -> int:
        z80 = self.z80
        if not self.z80_powered:
            # The Z80's registers at power-on are undefined on hardware: copy MAME's.
            self.set_z80_registers(record.registers)
            self.z80_powered = True
        ours = self.z80_registers()
        if ours != record.registers and record.registers[0] == 0x38 and z80.iff1:
            z80.request_maskable_interrupt(0xFF)
            self.board.step_start = self.anchor_time + (record.cycles - self.anchor_cycles) * 15
            spent = z80.step()
            z80.clear_maskable_interrupt()
            self.z80_interrupts += 1
            if self.z80_previous is not None:
                self.z80_previous = (self.z80_previous[0], self.z80_previous[1] + spent)
            ours = self.z80_registers()
        if ours != record.registers and self.try_other_order(record):
            ours = self.z80_registers()
        if ours != record.registers:
            raise Divergence(
                "Z80 registers before MAME cycle "
                f"{record.cycles}: "
                + ", ".join(
                    f"{n} board {a:X} MAME {b:X}"
                    for n, a, b in zip(Z80_NAMES, ours, record.registers, strict=True)
                    if a != b
                )
            )
        if self.z80_previous is not None:
            before, spent = self.z80_previous
            difference = record.cycles - before - spent
            if difference > 0 and self.releases > self.releases_before_previous:
                self.z80_clocks["held by the 68000's bus request (MAME counts the wait)"] += 1
            elif difference:
                name = f"Z80 {difference:+d}"
                self.z80_clocks[name] += 1
                if len(self.clock_examples["z80 " + name]) < 3:
                    self.clock_examples["z80 " + name].append(
                        f"{record.registers[0]:04X} @ {record.cycles}"
                    )
            else:
                self.z80_clocks["agree"] += 1
        self.pending_z80 = list(record.accesses)
        self.last_z80_writes = []
        self.board.step_start = self.anchor_time + (record.cycles - self.anchor_cycles) * 15
        spent = z80.step()
        if self.pending_z80:
            raise Divergence(f"Z80 at {record.registers[0]:04X} left MAME accesses unmade: "
                             + ", ".join(map(str, self.pending_z80)))  # fmt: skip
        self.z80_previous = (record.cycles, spent)
        self.releases_before_previous, self.releases_at_previous = (
            self.releases_at_previous,
            self.releases,
        )
        self.z80_count += 1
        self.history.append(f"Z80 {record.registers[0]:04X} @ {record.cycles}")
        return spent

    # -- the 68000's records --------------------------------------------------------

    def m68k_registers(self) -> list[int]:
        cpu = self.m68k
        return [cpu.PC & 0xFFFFFF, cpu.SR, *cpu.R[0:15], cpu.usp, cpu.ssp]

    m68k_start = 0

    def run_68k(self, record: Record) -> None:
        cpu, board = self.m68k, self.board
        start = record.cycles * 7
        self.catch_up_z80(start)
        self.m68k_start = start
        board.step_start = start
        board.stall = 0
        record.registers[0] &= 0xFFFFFF  # MAME's curpc can carry the top byte of a jump
        ours = self.m68k_registers()
        interrupt_clocks = 0
        if ours != record.registers:
            level = (record.registers[1] >> 8) & 7
            if ours[0] != record.registers[0] and level > (cpu.SR >> 8) & 7:
                # MAME took an interrupt here: its line, held for one step. The entry
                # began where the previous instruction ended on MAME's count (its own
                # clocks plus MAME's one-clock delay per $A0xxxx access), which is what
                # the E-clock phase of the autovectored acknowledge follows
                # (m68000-python's _e_clock_wait, MAME's vpa_sync).
                if self.previous_68k is not None:
                    before, spent = self.previous_68k
                    cpu.clock = (
                        before.cycles + spent + sum(1 for a in before.accesses if a.tag[0] == "A")
                    )
                cpu.set_ipl(level)
                interrupt_clocks = cpu.step()
                cpu.set_ipl(0)
                self.m68k_interrupts += 1
                ours = self.m68k_registers()
        if ours != record.registers:
            raise Divergence(
                f"68000 registers before MAME cycle {record.cycles}: "
                + ", ".join(
                    f"{n} board {a:X} MAME {b:X}"
                    for n, a, b in zip(M68K_NAMES, ours, record.registers, strict=True)
                    if a != b
                )
            )
        # The E-clock phase follows the clock count: keep it on MAME's.
        cpu.clock = record.cycles
        if self.previous_68k is not None:
            before, spent = self.previous_68k
            self.sort_68k_clocks(before, record, spent + interrupt_clocks, interrupt_clocks)
        self.pending_68k = list(record.accesses)
        clocks = cpu.step()
        while self.pending_68k and self.drop_foreign(self.pending_68k[0]):
            self.pending_68k.pop(0)
        if self.pending_68k:
            raise Divergence(f"68000 at {record.registers[0]:06X} left MAME accesses unmade: "
                             + ", ".join(map(str, self.pending_68k)))  # fmt: skip
        self.m68k_count += 1
        self.history.append(f"68000 {record.registers[0]:06X} @ {record.cycles}")
        self.previous_68k = (record, clocks)

    previous_68k: tuple[Record, int] | None = None

    def sort_68k_clocks(self, record: Record, following: Record, clocks: int,
                        interrupt: int) -> None:  # fmt: skip
        """Sort the difference between MAME's and the core's clocks for one step."""
        mame = following.cycles - record.cycles
        difference = mame - clocks
        z80_area = sum(1 for a in record.accesses if a.tag[0] == "A")
        if difference == 0:
            name = "agree" if not interrupt else "agree (with an interrupt entry)"
        elif difference == z80_area:
            name = "MAME's before_delay(1) on each $A00000-$A0FFFF access (megadriv.cpp)"
        elif difference > 0 and any(
            a.tag == "VW" and a.address & 0x1C == 4 for a in record.accesses
        ):
            name = (
                "MAME's 68000-to-VDP DMA stall, length / 3.5 ns (315_5313.cpp "
                "insta_68k_to_*_dma: 'a hack until real DMA timings are implemented')"
            )
        else:
            name = f"68000 {difference:+d} (unsorted)"
        self.m68k_clocks[name] += 1
        if len(self.clock_examples[name]) < 3:
            self.clock_examples[name].append(
                f"{record.registers[0]:06X} @ {record.cycles}: MAME {mame}, core {clocks}"
            )

    def run(self, trace: Path, limit: int | None) -> int:
        stream = m68k_records(trace / "m68k.log")
        record = next(stream, None)
        started = time.perf_counter()
        status = 0
        try:
            while record is not None:
                following = next(stream, None)
                if following is None:
                    break  # MAME stopped before this instruction finished
                self.run_68k(record)
                record = following
                if limit and self.m68k_count >= limit:
                    break
        except (Divergence, Lockup) as error:
            print(f"DIVERGENCE after {self.m68k_count:,} 68000 and {self.z80_count:,} Z80 "
                  f"instructions: {error}")  # fmt: skip
            print("  last instructions:", *self.history, sep="\n    ")
            status = 1
        elapsed = time.perf_counter() - started
        self.report(elapsed)
        return status

    def report(self, elapsed: float) -> None:
        print(f"68000: {self.m68k_count:,} instructions identical ({self.m68k_interrupts:,} "
              f"interrupts taken where MAME took them)")  # fmt: skip
        print(f"Z80:   {self.z80_count:,} instructions identical ({self.z80_interrupts:,} "
              f"interrupts)")  # fmt: skip
        print(f"       {self.z80_cut:,} instructions MAME began and a reset cut short (not "
              f"run); {self.z80_deferred:,} finished after a 68000 access that fell inside "
              f"them; {self.z80_while_held:,} run after the 68000's bus request, as MAME "
              "caught the Z80 up to it")  # fmt: skip
        print(f"unexplained differences: {len(self.failures)}")
        for line in self.failures[:10]:
            print("   ", line)
        print("named differences from MAME (MAME's value given to the CPU):")
        for name, count in self.classes.most_common():
            print(f"  {count:>10,}  {name}\n              e.g. {self.examples[name]}")
        print("68000 clocks, MAME minus core, by class:")
        for name, count in self.m68k_clocks.most_common():
            print(f"  {count:>10,}  {name}")
            for example in self.clock_examples.get(name, [])[:3]:
                print(f"              {example}")
        print("Z80 T-states, MAME minus core:")
        for name, count in self.z80_clocks.most_common():
            print(f"  {count:>10,}  {name}")
            for example in self.clock_examples.get("z80 " + name, [])[:3]:
                print(f"              before {example}")
        print(f"wall {elapsed:,.0f} s, {sys.implementation.name} {sys.version.split()[0]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("trace", type=Path, help="a directory scripts/mame_trace.py made")
    parser.add_argument("--driver", default="genesis", choices=sorted(MAME_DRIVERS))
    parser.add_argument("--cartridge", type=Path, help="default: the image beside the logs")
    parser.add_argument("--limit", type=int, help="stop after this many 68000 instructions")
    parser.add_argument(
        "--patch",
        action="store_true",
        help="count unexplained read differences and go on with MAME's value",
    )
    args = parser.parse_args()
    cart = args.cartridge or next(
        p for p in sorted(args.trace.iterdir()) if p.suffix.lower() in (".md", ".bin", ".gen")
    )
    image = load_image(cart)
    print(f"{cart.name}: {len(image):,} bytes; machine {MAME_DRIVERS[args.driver]}")
    lockstep = Lockstep(MAME_DRIVERS[args.driver], image, args.trace, args.patch)
    print(f"board: {lockstep.board.cartridge.slot}"
          + (f" (unsupported: {lockstep.board.cartridge.unsupported})"
             if lockstep.board.cartridge.unsupported else ""))  # fmt: skip
    return lockstep.run(args.trace, args.limit)


if __name__ == "__main__":
    sys.exit(main())
