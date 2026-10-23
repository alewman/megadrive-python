"""The board: both CPUs' memory maps, the Z80's bus and reset lines, and the devices.

A :class:`Board` owns work RAM, the Z80's RAM, the cartridge, the I/O chip, the VDP,
the YM2612 and the bank register, and presents the bus callables each core takes:

    M68000CPU(board.read_byte, board.read_word, board.write_byte, board.write_word,
              tas_write=board.tas_write)
    Z80CPU(board.z80_read, board.z80_write)

It never steps a CPU: :class:`~megadrive_python.system.System` does, and tells the
board when each step starts (:attr:`step_start`, in master clocks) so a device can
place an access in time. The 68000's accesses are placed at their own clock inside
the instruction through :meth:`m68k_access_clock` (see its docstring for the labelled
use of a private core attribute).

Sources (docs/start-here.md has the tables): CMD ``gen-hw.txt`` sections 1-2 for
the maps, the byte/word rules, the lock-ups, the bus request and reset registers
and the bank, all "checked on the real thing"; the Manual for the version register
and TMSS; MoD's ``gen_test_z80_bus`` (forum t=3021, several machines) for the
Z80's failed reads of 68000 RAM.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from sn76489_python import SN76489, YM7101_PSG
from ym2612_python import YM2612
from ym2612_python.chip import IC_SLOT_LAG, IC_WRITE_SLOT_LAG, TIMER_CONTROL_DELAY_CYCLES

from .arbiter import (
    AS_LOW_MCLK,
    BUSACK_PROPAGATION_MCLK,
    CUTOFF_MCLK,
    IDLE_GRANT_MCLK,
    M68K_EDGE_PHASE,
    z80_access_wait,
)
from .cartridge import Cartridge
from .io import IOChip
from .machine import M68K_DIVIDER, Z80_DIVIDER, Machine
from .sockets import CLAIM_PAGE, CLAIMABLE, M68K, Z80
from .vdp import VDP

if TYPE_CHECKING:
    from m68000_python import M68000CPU

    from .sockets import BusDevice, PortDevice, SlotDevice


#: The cartridge-side refresh: every 128 68000 clocks of real time, the next 68000 bus
#: cycle absorbs it, and if that cycle is a cartridge access it takes 2 clocks longer.
#: Kabuto ("The bus system": "every time 128 cycles have passed the next bus access is
#: slowed down by 2 cycles ... if this hits when the 68k is writing to the VDP ... the
#: slowdown doesn't happen"; hardware-measured research), made exact against Nuked-MD
#: (die-derived) in four probe loops, 670 of 670 ticks each (docs/worklog.md).
CART_REFRESH_MCLK = 896
CART_REFRESH_STALL = 14
#: ... unless the bus was free long enough for it to run by itself, from this long after
#: it fell due for its 14 MCLK: an access that starts while it runs waits for its end,
#: and one after it pays nothing. Nuked-MD (die-derived): in Overdrive 2's boot, a
#: refresh that falls due during a long held data-port read costs the next cartridge
#: read 14 MCLK when due up to 112 before it, 7 at 119 and nothing from 126 (1,000 of
#: them, none out of line); and
#: on a test cartridge whose 68000 spends 70 clocks in a ``mulu`` between bursts of
#: cartridge reads, the read after the gap is held 14 up to 112 and not at all from 126
#: (the die's own refresh grid, placed from its held reads; docs/worklog.md, "The
#: cartridge refresh when the bus is free"). The same shape as the work-RAM refresh's
#: self-run (:data:`RAM_REFRESH_SELF_MCLK`).
CART_REFRESH_SELF_MCLK = 112
#: The same refresh falling on a 68000 access to the Z80's space ($A00000-$A0FFFF)
#: stretches it by one 68000 clock, not two -- and by none if it fell due only in the
#: last 68000 clock before /AS, when it is used up all the same. Nothing hardware
#: covers the question, so the die-derived model answers it: Nuked-MD's Overdrive 2
#: start-up copies its sound driver into Z80 RAM a byte at a time, and of the 519
#: refreshes that fell on those writes, 392 due a clock or more before /AS held the
#: write 7 MCLK longer and 127 due within the last clock held it not at all, every
#: one (docs/worklog.md, "The one-frame slip").
Z80_REFRESH_STALL = 7
Z80_REFRESH_LATE = 7
#: A refresh that stretches a cartridge access pulses the cartridge's /REF from 39 to
#: this long after it fell due -- past the end of the stretched access when it fell due
#: at its /AS (42 MCLK), so the next access starts under the pulse. A cartridge access
#: there is not held; one to the Z80's space waits a 68000 clock (as when the refresh
#: falls on it). Die-derived: Nuked-MD's Overdrive 2 boot, the write to Z80 RAM after a
#: cartridge read stretched at +15,263,262, held 14 where every write of the loop is
#: held 7, under the pulse at +15,263,301-308; after a refresh due a clock before /AS
#: the pulse has ended and the write is held 7 (docs/worklog.md, "Under the /REF
#: pulse").
CART_REFRESH_PULSE_END_MCLK = 46
#: The work-RAM refresh (Kabuto: "RAM accesses get their own further slowdowns"). It
#: falls due :data:`RAM_REFRESH_AFTER_MCLK` after the last one began. A work-RAM access
#: starting within :data:`RAM_REFRESH_TRIGGER_MCLK` of that takes it, and waits 3
#: 68000 clocks; if none does, the refresh runs by itself from
#: :data:`RAM_REFRESH_SELF_MCLK` after it fell due, for the same 21 MCLK, and an
#: access starting in the last 14 MCLK before that or while it runs waits for its end.
#: So a 68000 that leaves the RAM alone pays nothing, and the next falls due 819 after
#: whichever began. Die-derived: Nuked-MD's 68000 in Overdrive 2's 3D scene, 135,937
#: work-RAM accesses, every stall predicted exactly but 172 (153 of them a refresh one
#: 68000 clock early, mostly on an odd master clock); the old rule -- the next access
#: always takes it, due again 805 after -- refreshed 20,800 MCLK too much over those
#: four frames (docs/worklog.md "The work-RAM refresh"). ``RAM_REFRESH_HIDDEN`` False
#: keeps the old rule, with the 805 the boot probes left it at.
RAM_REFRESH_STALL = 21
RAM_REFRESH_AFTER_MCLK = 819
RAM_REFRESH_TRIGGER_MCLK = 98
RAM_REFRESH_SELF_MCLK = 112
RAM_REFRESH_HIDDEN = True
#: The clocks the timer counts from a refresh to the next falling due (vdp.c's
#: ``dff23``-``dff29`` reaching 112), in master clocks.
RAM_REFRESH_COUNT_MCLK = 112 * 7
#: A refresh due this long after a work-RAM access's /AS is the die's tie: its counter
#: steps to 112 in the half-MCLK the /AS falls in, and dff13, which latches "count >=
#: 112" on the /AS, sees the step or not by the order Nuked-MD evaluates the two in --
#: it clocks the VDP's logic only on its dot clock's edges, and the access takes the
#: refresh exactly when one falls at the /AS (37,840 ties in scripts/run_refresh_tie.py,
#: every one; :meth:`VDP.dot_clock_edge`). Taken by the nuked-md machine only (an
#: artifact of the die-derived model; docs/worklog.md, "The tie at 112").
RAM_REFRESH_TIE_MCLK = 7
#: Every 68000 access to the Z80's space costs one extra 68000 clock. Nothing hardware
#: covers the question, so the tier rule lets a die-derived model answer it: Nuked-MD
#: holds all 68 of the FM probe's $A0xxxx accesses 7 MCLK where the board held none
#: (milestone 6), and MAME does the same (``megadriv.cpp``, ``before_delay(1)``,
#: "required by pacman2 intro", "TODO: unverified"). Two independent models agreeing
#: against the board is what the user's decision 1 adopted (docs/worklog.md, the
#: timing session). It is charged after the access, so the access itself still lands
#: at the clock the bus cycle began at.
Z80_SPACE_STALL = M68K_DIVIDER
#: A Z80 access to the VDP ($7F00-$7F1F) goes over the 68000's bus, through the same
#: arbitration as one through the bank window (:meth:`Board._bank_delay`): on
#: Nuked-MD the Z80's 3,095 VDP reads in Overdrive 2's first 80 frames are held 60, 75
#: or 90 MCLK, the bank window's three lengths, where its RAM and FM reads take 30
#: (docs/worklog.md, "The Z80 after a hold"). Die-derived for reads; the run has no
#: Z80 VDP writes, which take the same path because the VDP has no other bus.
Z80_VDP_ARBITRATED = True
#: A Z80 access over the 68000's bus delays the 68000 only from the bus cycle it next
#: wants: one inside a long internal operation (a multiply, a divide, a shift) costs it
#: what runs past that operation, not the whole grant. The board charged every access
#: as if the 68000 wanted the bus four clocks after its last cycle, and in Overdrive
#: 2's 3D scene (frames 5,790-5,804, the ninja's arms) that made 28 % of the Z80's
#: 9,710 bank reads cost the 68000 13 clocks where the die charges 13 for 6 % of its
#: reads, and the board's 68000 fell about 10,000 MCLK a frame behind the die's. Die-derived
#: (Nuked-MD's 68000 trace from its frame-5,750 save; docs/worklog.md "The ninja's arm
#: colours"); MoD's ``jmp (a0)`` loop, always on the bus, costs the same either way.
Z80_GRANT_OVERLAPS_IDLE = True
#: The Z80 is brought up to each 68000 bus cycle before it starts, so a bank access
#: the Z80 makes part way through a 68000 instruction holds that instruction's next bus
#: cycle, as on the die, rather than being owed to the next instruction. Overdrive 2's
#: boot, +16,099,290: the die's arbiter takes the 68000's bus for 49 MCLK and its next
#: cycle, in the same instruction, waits 70 MCLK; the board charged the 70 one instruction
#: later (docs/worklog.md, "The Z80's grant inside an instruction"). Die-derived; False
#: owes every such hold to the next instruction.
Z80_CATCH_UP_EACH_CYCLE = 1
#: The cartridge refresh stretches a Z80 access over the 68000's bus only if it goes to
#: the cartridge: a read of the VDP through $7F00-$7F1F is not held by it, and the refresh
#: falls on the 68000's next cycle (the die's +16,155,626: the Z80's VDP cycle lasts 29
#: MCLK with a refresh due inside it, and the 68000's read after the grant is held 14),
#: as Kabuto has it for the 68000's own VDP writes (``CART_REFRESH_STALL``). The board had
#: the Z80's cycle take it, and the 68000's take it again. Die-derived.
Z80_REFRESH_CARTRIDGE_ONLY = 1
#: A 68000 write to the FM is taken this long after its /AS, and a 68000 read gets the
#: status as it stands this long after: so a data write landing in the first half of
#: one of the chip's 42-MCLK cycles is taken by that cycle and one in the second half by
#: the next. Nuked-MD (die-derived; ``scripts/run_fm_busy.py``, 1,408 writes walked
#: through every phase of the cycle and every 7 MCLK of the polls): BUSY clears 1,407
#: MCLK after a write's /AS less its phase in the cycle up to 14, 1,449 less it from 21;
#: the board, taking every write and reading the status at /AS, had 1,435 less it at
#: every phase -- Sonic's and Gunstar Heroes' first parted paths. The sweep puts the
#: write at 15-21 and the read at 22-28; Gunstar's own write at +96,495,063, at phase 21
#: with the two FMs half an MCLK apart, taken a cycle late on the die, needs 21, and its
#: poll at +1,421, the die's BUSY falling at +1,448.5, read busy, needs at most 27
#: (docs/worklog.md, "The busy flag"). The nuked-md machine only.
M68K_FM_WRITE_MCLK = 21
M68K_FM_READ_MCLK = 27
#: Whatever it goes to, a Z80 cycle over the 68000's bus uses up a cartridge refresh
#: that fell due by its start: the 68000's next cartridge access is not held. Due after
#: the Z80's cycle began, the 68000's next cartridge access takes it (14). Die-derived:
#: Nuked-MD's Overdrive 2 boot over +58.7M, the refresh grid placed from the 68000's
#: held reads, 1,809 Z80 cycles with a tick from 28 before to 35 after them and a
#: cartridge access next, every one so
#: (docs/worklog.md, "The refresh a Z80 cycle uses up"). The board had a VDP access
#: leave it for the 68000.
Z80_REFRESH_USED_UP = 1


#: The refresh tie is judged against the refresh due by the access, after the ones no
#: access took have run by themselves: Sonic the Hedgehog from power-on, +51,931,586,
#: the die's counter steps to 112 at a work-RAM /AS after a run of self-run refreshes
#: and the access takes it; the board judged the tie against the due before those
#: self-runs, 7 not being the distance, and missed it (docs/worklog.md, "The tie after
#: the self-runs"). Die-derived. False judges it against the due as it was stored.
RAM_REFRESH_TIE_AFTER_SELF_RUNS = 1


def ram_refresh_self_runs(due: int, t: int) -> int:
    """The work-RAM refresh due at ``t`` once those nobody took since ``due`` have run
    by themselves (:data:`RAM_REFRESH_SELF_MCLK` after each fell due, the next due
    :data:`RAM_REFRESH_AFTER_MCLK` after it began)."""
    ran = RAM_REFRESH_SELF_MCLK + RAM_REFRESH_STALL
    if RAM_REFRESH_HIDDEN and t >= due + ran:
        period = RAM_REFRESH_SELF_MCLK + RAM_REFRESH_AFTER_MCLK
        due += ((t - due - ran) // period + 1) * period
    return due


def ram_refresh(due: int, t: int, tie: bool = False) -> tuple[int, int]:
    """The work-RAM refresh for a work-RAM access starting at ``t`` with the next
    refresh due at ``due``: the MCLK the access waits, and when the next falls due
    (:data:`RAM_REFRESH_HIDDEN`'s rule, or the old one). ``tie``: the access takes a
    refresh due one clock after its /AS (:data:`RAM_REFRESH_TIE_MCLK`). The board and
    the harnesses that model its refresh (``run_hv_captures``, ``run_nuked_md``) share
    it."""
    if t < due and not tie:
        return 0, due
    if not RAM_REFRESH_HIDDEN:
        return RAM_REFRESH_STALL, t + 805
    rolled = ram_refresh_self_runs(due, t)
    if rolled != due:
        due = rolled
        if t < due and not tie:
            return 0, due
    if t < due + RAM_REFRESH_TRIGGER_MCLK:
        return RAM_REFRESH_STALL, t + RAM_REFRESH_AFTER_MCLK
    began = due + RAM_REFRESH_SELF_MCLK
    # Until its end, on the 68000's next clock edge (the die's waits are 7, 14, 28, 35).
    wait = -(-(began + RAM_REFRESH_STALL - t) // M68K_DIVIDER) * M68K_DIVIDER
    return wait, began + RAM_REFRESH_AFTER_MCLK


def ram_refresh_paused(due: int, start: int, end: int, began_by: int) -> int:
    """The work-RAM refresh due time after the VDP held the bus from ``start`` to ``end``
    for a transfer: the refresh timer stops meanwhile and goes on from where it was
    (:data:`RAM_REFRESH_PAUSES`). It counts 68000 clocks, so the pause is the clock
    edges in [``start``, ``end``). A self-run that began by ``began_by`` goes ahead
    under the pause, and the timer counts its 112 clocks to the next from after it, so
    only the pause from then on holds that one up."""
    if began_by >= due + RAM_REFRESH_SELF_MCLK:
        period = RAM_REFRESH_SELF_MCLK + RAM_REFRESH_AFTER_MCLK
        due += ((began_by - due - RAM_REFRESH_SELF_MCLK) // period + 1) * period
        start = max(start, due - RAM_REFRESH_COUNT_MCLK)

    def edges_before(t: int) -> int:
        return -(-(t - M68K_EDGE_PHASE) // M68K_DIVIDER)

    return due + M68K_DIVIDER * max(0, edges_before(end) - edges_before(start))


#: A held 68000 bus cycle lasts whole 68000 clocks: /DTACK is sampled on a clock edge,
#: so the cycle ends on the first edge after the device lets it go. The board added the
#: VDP's (and the bus grant's) hold as it came, in master clocks, and every one that was
#: not a multiple of 7 moved all the 68000's later clock edges -- 4,000 times in Overdrive
#: 2's first 12 frames -- where the die's edges keep one phase from power-on
#: (docs/worklog.md, "Handover by handover"). False keeps the master clocks.
HOLD_ON_68K_EDGE = True
#: How long after a write to $A11200 begins the YM2612 is given /IC. On Nuked-MD the line
#: moves 18 MCLK after the write, but the FM model already samples it later
#: (``ym2612_python.chip.IC_SLOT_LAG``, three FM clocks after the bus cycle): with the clocks lined
#: up as the die's (``system.M68K_POWER_ON_PHASE``), the FM's phase after the boot's
#: resets matches the die's with 0 to 6 here and leaves it 175-440 MCLK out with 9 or more
#: (docs/worklog.md, "The VDPs' offset"). 0.
ZRES_PIN_MCLK = 0


def _on_edge(held: int) -> int:
    """A hold of ``held`` MCLK as the 68000 sees it: rounded up to its clock."""
    if HOLD_ON_68K_EDGE and held % M68K_DIVIDER:
        return held + M68K_DIVIDER - held % M68K_DIVIDER
    return held


class Lockup(Exception):
    """An access that locks up a real console (CMD ``gen-hw.txt`` section 1).

    The board raises it out of the CPU's step; the system stops and reports it
    (docs/undocumented-behavior.md, "The bus and memory map", item 2).
    """


class _LowDecode:
    """$000000-$3FFFFF for the 68000 while a socket's device claims any of it.

    It stands where the cartridge stands (``Board.cartridge``), with the cartridge's
    method set, so the 68000's paths are the same calls with or without a device: a
    claimed page goes to its device at the access's clock, the rest to the slot. Only
    the 68000 comes through here; the Z80's bank window, its peeks and the VDP's DMA
    look the claims up themselves and use their own entries and clocks (socket 3).
    """

    grounds_cart = True

    def __init__(self, board: Board) -> None:
        self.board = board
        self.pages = board._pages
        self.slot = board._slot_bus

    def read_word(self, address: int) -> int:
        device = self.pages[address >> 8]  # type: ignore[index]
        if device is None:
            return self.slot.read_word(address)
        return device.read_word(address, self.board.m68k_access_clock())

    def read_byte(self, address: int) -> int:
        device = self.pages[address >> 8]  # type: ignore[index]
        if device is None:
            return self.slot.read_byte(address)
        return device.read_byte(address, self.board.m68k_access_clock(), M68K)

    def write_word(self, address: int, value: int) -> None:
        device = self.pages[address >> 8]  # type: ignore[index]
        if device is None:
            self.slot.write_word(address, value)
        else:
            device.write_word(address, value, self.board.m68k_access_clock())

    def write_byte(self, address: int, value: int) -> None:
        device = self.pages[address >> 8]  # type: ignore[index]
        if device is None:
            self.slot.write_byte(address, value)
        else:
            device.write_byte(address, value, self.board.m68k_access_clock(), M68K)

    def read_time(self, address: int) -> int | None:
        return self.slot.read_time(address)

    def write_time(self, address: int, value: int) -> None:
        self.slot.write_time(address, value)


class Board:
    """Memory maps and devices for one :class:`Machine`."""

    def __init__(self, machine: Machine, cartridge: Cartridge | None, *,
                 pads: tuple[object, object] | None = None) -> None:  # fmt: skip
        self.machine = machine
        #: The cartridge in the slot -- through a slot device, the one it holds -- or
        #: None: an empty slot is a state of its own, which /CART reads
        #: (:attr:`cartridge_present`; expansion socket 1).
        self.slot_cartridge: Cartridge | None = cartridge
        #: What the slot puts on $000000-$3FFFFF where no device claims: the cartridge,
        #: or for an empty slot a ROM-less stand-in that reads $FF, as the board always
        #: has (nothing hardware here says what an empty slot reads; not claimed).
        self._slot_bus = cartridge if cartridge is not None else Cartridge(b"")
        #: What answers the 68000 at $000000-$3FFFFF now: the slot, the TMSS boot ROM,
        #: or the claims of a socket's device (:meth:`_remap`). The 68000's fetches are
        #: the hottest path in the emulator, so this one object is swapped rather than
        #: anything being tested per access.
        self.cartridge = self._slot_bus
        #: The expansion sockets (docs/expansion-sockets.md, :mod:`megadrive_python.sockets`):
        #: the device in the cartridge slot and the one on the expansion port, both None
        #: on a console with nothing plugged in, and the two as a tuple for the events
        #: every device hears (DMA starts, resets).
        self.slot: SlotDevice | None = None
        self.port: PortDevice | None = None
        self.devices: tuple[BusDevice, ...] = ()
        #: Every claimed 256-byte page of the 68000's space, by ``address >> 8``: the
        #: device that answers it, or None. None as a whole when nothing is attached --
        #: the only paths that look at it are ones that had nothing to do before (open
        #: bus, the 32X area's lock-up, the $A1 page) and the Z80's and DMA's, for one
        #: ``None`` test (:meth:`update_claims`).
        self._pages: list[BusDevice | None] | None = None
        self.ram = bytearray(0x10000)
        self.z80_ram = bytearray(0x2000)
        self.io = IOChip(machine.version_register, pads)  # type: ignore[arg-type]
        self.vdp = VDP(machine, self)
        #: The two sound chips.  Both are lazy: they catch up to the master clock of
        #: an access before it, and to the board's own clock when a caller drains
        #: their samples (:meth:`sound_sync`).  The FM shares /RESET with the Z80,
        #: which is held at power-on ("For as long as the Z80 is reset, the YM2612
        #: cannot be used", CMD ``gen-hw.txt`` 2.2).
        self.fm = YM2612(machine.fm)
        if machine.dma_figures == "die":
            self.fm.control_delay = TIMER_CONTROL_DELAY_CYCLES
        #: When the FM takes a 68000 write and drives its status for a 68000 read, after
        #: the access's /AS (:data:`M68K_FM_WRITE_MCLK`, :data:`M68K_FM_READ_MCLK`).
        self.fm_write_lag, self.fm_read_lag = (
            (M68K_FM_WRITE_MCLK, M68K_FM_READ_MCLK) if machine.dma_figures == "die" else (0, 0)
        )
        self.fm.reset_line(True)
        #: The PSG lives inside the VDP and is written at $C00011 or $7F11; reading
        #: its port locks up a real console (CMD).  It is not held by the Z80's reset.
        self.psg = SN76489(YM7101_PSG)
        self.psg_mclk = 0
        #: 68000 address bits 23-15 of the Z80's bank window; power-on 0 (unknown on
        #: hardware; MAME 0.285 ``machine_reset`` clears it).
        self.bank = 0
        #: "On power-up, the Z80 will be reset and will have the bus" (CMD 2.2).
        self.z80_reset = True
        self.z80_bus_requested = False
        #: TMSS: the VDP is locked until "SEGA" is written to $A14000 (Manual p. 94).
        self.tmss_register = 0
        #: The boot ROM, when one is fitted (:meth:`install_tmss_rom`), and the
        #: cartridge it stands in for. Bit 0 of $A14101 chooses between them.
        self.tmss_rom: object | None = None
        self.tmss_cartridge: Cartridge | None = None
        self.tmss_cart_selected = True
        self.m68k: M68000CPU | None = None  # set by the system; read for open bus
        #: The master clock at which the current CPU step started, set by the system.
        self.step_start = 0
        #: The Z80 instruction under way: its memory accesses' offsets from
        #: :attr:`step_start` in master clocks (z80-python's access-timing table; None
        #: when it does not apply, such as an interrupt acknowledge), and how many of
        #: them it has made so far.
        self.z80_offsets: tuple[int, ...] | None = None
        self.z80_access = 0
        #: The Z80 instruction's accesses over the 68000's bus so far, and those settled
        #: before it ran: (its start, [(the Z80's wait, the 68000's) for each], their offsets in
        #: T-states) (``system.Z80_ARBITRATE_PER_ACCESS``).
        self.z80_bus_access = 0
        self.z80_prearbitrated: tuple[int, list[tuple[int, int]], list[int]] | None = None
        #: Master clocks the 68000 has been held during the current step (stalls).
        self.stall = 0
        #: How much of ``stall`` is the Z80-space hold (:data:`Z80_SPACE_STALL`), for
        #: the lockstep, which compares the board's step with MAME's own clock count.
        self.z80_space_stall = 0
        #: Called with a master clock before the 68000 changes anything the Z80 can
        #: see (its RAM, bus, reset, the YM2612, the bank): the system runs the Z80
        #: up to that time first. ``stopping`` is True when the change about to be
        #: made takes the bus from a running Z80 or resets it: the Z80 then gives up
        #: the bus at the end of the machine cycle in progress, not of the
        #: instruction (``System.catch_up_z80``).
        self.catch_up_z80: Callable[..., None] = lambda mclk, stopping=False: None
        #: The two refreshes that slow the 68000 (``_bus_cycle``); their power-on phases
        #: are unknown, so a caller may set them (the capture harness fits them).
        self.cart_refresh_due = CART_REFRESH_MCLK
        #: When the last stretching refresh's /REF pulse ends (:data:`CART_REFRESH_PULSE_END_MCLK`).
        self.cart_refresh_pulse_end = 0
        #: The refresh tick a Z80 access over the 68000's bus last took (``_bank_delay``).
        self.z80_refresh_taken = -1
        self.ram_refresh_due = RAM_REFRESH_AFTER_MCLK
        self.refresh_enabled = True
        #: The arbiter's delays (milestone 5, :mod:`megadrive_python.arbiter`): the Z80's
        #: bank accesses holding both CPUs, and the bus grant waiting for the Z80's
        #: machine cycle to end.  MAME models neither, so the milestone-1 lockstep turns
        #: this off along with ``refresh_enabled``, which it also needs for the 68000's
        #: access grid.
        self.arbiter_enabled = True
        #: Each 68000 bus cycle started in the step under way, in order, as
        #: ``[master clock /AS fell, master clocks it stays down]``: the arbiter reads
        #: it to see which of the 68000's cycles go before a Z80 access (:meth:`_bank_delay`).
        self.m68k_accesses: list[list[int]] = []
        #: Master clocks the Z80 has been held during its current step, and master
        #: clocks the 68000 owes for Z80 bank accesses made in its wake (they are added
        #: to its next step: the Z80 runs behind it, so the instruction they really fell
        #: inside has already been executed -- ``System.step``).
        self.z80_stall = 0
        self.m68k_pending_stall = 0
        #: How long the 68000's last bus cycle waited for the Z80 to give the bus back
        #: before it began (:data:`Z80_CATCH_UP_EACH_CYCLE`).
        self.grant_wait = 0
        #: Until when the bus is the Z80's for the 68000's next cycle (0: not).
        self.m68k_resume_at = 0
        #: The master clock the 68000's last instruction ended at (``System.step``).
        self.m68k_step_end = 0
        #: Totals for ``scripts/run_bus_arbiter.py``: bank accesses and the cycles they
        #: cost each CPU.
        self.bank_accesses = 0
        self.bank_z80_cycles = 0
        self.bank_m68k_cycles = 0
        #: The master clock the Z80 lets go of the bus after the current request
        #: (BUSACK); set by the system, which alone can probe the Z80's machine cycles.
        self.busack_at = 0
        #: Asks the system for that clock, given the request's (``System.z80_busack``).
        self.z80_busack: Callable[[int], int] = lambda mclk: mclk
        #: Called when the Z80's reset line is released or asserted (True = held).
        self.on_z80_reset: Callable[[bool], None] = lambda held: None
        #: Told when a write gives the Z80 its bus back or releases its reset
        #: (``System._z80_resume``): the clock, and whether it was the reset.
        self.on_z80_resume: Callable[[int, bool], None] = lambda t, reset: None
        #: For the Z80 step resuming after a hold: from which clock its accesses wait,
        #: and for how long.
        self.z80_hold: tuple[int, int] | None = None

    # -- TMSS ------------------------------------------------------------------

    def install_tmss_rom(self, image: bytes) -> None:
        """Fit the TMSS boot ROM, which a TMSS console powers up running.

        A TMSS console has a 16 KB ROM of its own where the cartridge would be, and
        bit 0 of $A14101 chooses between the two; the boot program switches to the
        cartridge to look at its header, back to itself to draw the licence screen,
        and to the cartridge for good before jumping to its start vector
        (``reference/src/TMSS-Disassembly``, ``RAM_Code``). The two are swapped here
        rather than tested for on every access, so a board without a boot ROM costs
        nothing -- which is also why a boot ROM is something a caller asks for: every
        run before milestone 7 put the cartridge on the bus from power-on, and the
        acceptance runs of milestones 1 to 6 would all move if that changed.
        """
        from .cartridge import BootROM

        self.tmss_rom = BootROM(image)
        self.tmss_cartridge = self._slot_bus
        self.tmss_cart_selected = False
        self._remap()

    @property
    def game_cartridge(self) -> Cartridge:
        """The cartridge, whichever of it, the boot ROM and a device is on the bus now
        (the empty slot's stand-in when there is none)."""
        return self._slot_bus

    def set_z80_reset(self, held: bool, t: int, fm_t: int | None = None,
                      write: bool = False) -> None:  # fmt: skip
        """Drive ZRES, the Z80's and YM2612's shared reset line, at MCLK ``t``: from a
        write to $A11200 (``write``), or from the console's reset button
        (``System.press_reset``). ``fm_t``: when the YM2612's /IC sees it, if not at
        ``t``."""
        self.catch_up_z80(t, stopping=held and self.z80_running)
        if held != self.z80_reset:
            self.z80_reset = held
            fm_t = t if fm_t is None else fm_t
            self.sound_sync(fm_t)
            self.fm.reset_line(held, fm_t, IC_WRITE_SLOT_LAG if write else IC_SLOT_LAG)
            self.on_z80_reset(held)
            if not held:
                self.on_z80_resume(t, True)

    def _tmss_select(self, cartridge: bool) -> None:
        """Bit 0 of $A14101: 1 puts the cartridge on the bus, 0 the boot ROM."""
        if self.tmss_rom is None or cartridge == self.tmss_cart_selected:
            return
        self.tmss_cart_selected = cartridge
        self._remap()

    def _remap(self) -> None:
        """Choose what answers the 68000 at $000000-$3FFFFF (:attr:`cartridge`).

        The TMSS boot ROM, while $A14101 bit 0 is clear, sits above everything: in the
        Sega CD's mode 2 the port is what is at $000000, and a TMSS console must still
        run its check first (segacd-python ``docs/sockets.md`` 2: "TMSS ROM selected >
        port in mode 2 > slot"). Then a device's claims there, through
        :class:`_LowDecode`; then the slot. With nothing attached this is exactly the
        swap ``_tmss_select`` always made.
        """
        if self.tmss_rom is not None and not self.tmss_cart_selected:
            self.cartridge = self.tmss_rom  # type: ignore[assignment]
        elif self._pages is not None and any(
            device is not None for device in self._pages[: 0x400000 // CLAIM_PAGE]
        ):
            self.cartridge = _LowDecode(self)  # type: ignore[assignment]
        else:
            self.cartridge = self._slot_bus

    # -- the expansion sockets ---------------------------------------------------

    @property
    def cartridge_present(self) -> bool:
        """/CART: low when the slot -- through a slot device, the device's own slot --
        holds a cartridge that grounds it (``CartridgeSlot.grounds_cart``). The Sega CD
        reads it to choose its mapping; it is told at attach, since hot-plugging is not
        modelled (segacd-python ``docs/sockets.md`` 2)."""
        cartridge = self.slot_cartridge
        return cartridge is not None and cartridge.grounds_cart

    def attach_slot(self, device: SlotDevice) -> None:
        """Plug ``device`` into the cartridge slot (socket 1). The cartridge it holds
        takes the slot's place on the bus wherever the device does not claim."""
        if self.slot is not None:
            raise ValueError("the cartridge slot already holds a device")
        if self.slot_cartridge is not None:
            raise ValueError("a slot device holds its own cartridge: pass the board none")
        self.slot = device
        self.slot_cartridge = device.cartridge
        self._slot_bus = device.cartridge if device.cartridge is not None else self._slot_bus
        if self.tmss_rom is not None:
            self.tmss_cartridge = self._slot_bus
        self.devices = (*self.devices, device)
        device.attach(self)
        self.update_claims()

    def attach_port(self, device: PortDevice) -> None:
        """Plug ``device`` into the expansion port (socket 2). $A10001 bit 5, "no
        expansion unit" (the Manual, PDF page 84; /DISK on the port), now reads 0, as
        ``Machine.expansion`` already makes it for a model that has a unit built in."""
        if self.port is not None:
            raise ValueError("the expansion port already holds a device")
        self.port = device
        self.io.version &= ~0x20
        self.devices = (*self.devices, device)
        device.attach(self)
        self.update_claims()

    def update_claims(self) -> None:
        """Read every attached device's claims again and rebuild the page table: at
        attach, and whenever a device's mapping changes (the 32X's ADEN or RV, the Sega
        CD's mode). A claim is [start, end) in 68000 addresses, page-aligned and inside
        :data:`~megadrive_python.sockets.CLAIMABLE`; the port's claims win over the
        slot's where both claim a page."""
        if not self.devices:
            self._pages = None
            self._remap()
            return
        pages: list[BusDevice | None] = [None] * 0x10000
        for device in (self.slot, self.port):
            if device is None:
                continue
            for start, end in device.claims():
                if start % CLAIM_PAGE or end % CLAIM_PAGE or start >= end:
                    raise ValueError(f"claim ${start:06X}-${end:06X} is not whole pages")
                if not any(low <= start and end <= high for low, high in CLAIMABLE):
                    raise ValueError(f"claim ${start:06X}-${end:06X} is not on a socket's bus")
                for page in range(start // CLAIM_PAGE, end // CLAIM_PAGE):
                    pages[page] = device
        self._pages = pages
        self._remap()

    def hold(self, master: int, mclk: int) -> None:
        """A device's wait states on the access it is answering (socket 4): ``mclk``
        master clocks for the 68000 -- on its own clock edge, as the VDP's holds
        (:data:`HOLD_ON_68K_EDGE`), and as a longer bus cycle in the arbiter's grid -- or
        for the Z80, added to its step as the arbiter's waits are."""
        if master == Z80:
            self.z80_stall += mclk
            return
        held = _on_edge(mclk)
        self.stall += held
        if self.arbiter_enabled and self.m68k_accesses:
            self.m68k_accesses[-1][1] += held

    def dma_started(self, mclk: int) -> None:
        """A 68000-to-VDP DMA started at ``mclk`` (``VDP._run_transfer``): every device is
        told, with the 68000's data bus as it stands (socket 3)."""
        bus = self.open_bus()
        for device in self.devices:
            device.dma_start(mclk, bus)

    # -- time ----------------------------------------------------------------

    def m68k_access_clock(self) -> int:
        """The master clock at which the 68000's current bus access starts.

        m68000-python counts an instruction's clocks as it goes, adding 4 for each
        bus access *before* calling the host, and says so publicly: inside a bus
        callback ``M68000CPU.step_clocks - 4`` is the clock the access began at,
        counted from the start of the step (its PR #1; the board read the private
        ``_cycles`` until the pin reached it, docs/polish.md).
        """
        cpu = self.m68k
        if cpu is None:
            return self.step_start + self.stall
        return self.step_start + self.stall + (cpu.step_clocks - 4) * M68K_DIVIDER

    @property
    def z80_running(self) -> bool:
        """The Z80 runs unless it is held in reset or has granted its bus."""
        return not self.z80_reset and not self.z80_bus_requested

    def m68k_has_z80_bus(self, t: int | None = None) -> bool:
        """ "The Z80 bus can only be accessed by the 68000 when the Z80 is running
        and the 68000 has the bus" (CMD 2.2): requested and out of reset.

        With the arbiter modelled the request is not enough: the Z80 lets go only at
        the end of the machine cycle it is in (:mod:`megadrive_python.arbiter`), and
        until then the arbiter holds the 68000 rather than failing its access -- so an
        access at master clock ``t`` before the grant is stalled to it here.
        """
        if not self.z80_bus_requested or self.z80_reset:
            return False
        if self.arbiter_enabled and t is not None and t < self.busack_at:
            self.stall += _on_edge(self.busack_at - t)
        return True

    def _z80_space_cost(self) -> None:
        """The one extra 68000 clock every access to $A00000-$A0FFFF costs
        (:data:`Z80_SPACE_STALL`), counted apart as well as charged.

        ``scripts/lockstep_mame.py`` adds :attr:`z80_space_stall` to the core's own
        clocks before comparing a step with MAME's, because MAME charges the same
        clock (``before_delay(1)``) and the two have to be compared like with like.
        """
        self.stall += Z80_SPACE_STALL
        self.z80_space_stall += Z80_SPACE_STALL
        if self.arbiter_enabled and self.m68k_accesses:
            # The bus cycle itself is that much longer, as it is for a refresh stretch.
            # (Nothing turns on it: the Z80 is stopped whenever the 68000 reaches its
            # space, so no bank access can be waiting for this one to end.)
            self.m68k_accesses[-1][1] += Z80_SPACE_STALL

    def _bus_cycle(self, address: int) -> None:
        """One 68000 bus cycle about to start at ``address``: its refresh stalls, and
        its place in the grid the arbiter reads (:meth:`_bank_delay`).

        The grid holds ``[start, span]``: the master clock /AS fell and how long it
        stays down, which a refresh stretch lengthens.
        """
        t = self.m68k_access_clock()
        grid = self.m68k_accesses if self.arbiter_enabled else None
        self.grant_wait = 0
        if self.m68k_resume_at:
            # A grant to the Z80 settled after the last instruction (``System.step``).
            if self.m68k_resume_at > t:
                waited = self.m68k_resume_at - t
                self.stall += waited
                t += waited
                self.grant_wait = waited
            self.m68k_resume_at = 0
        if grid is not None:
            grid.append([t, AS_LOW_MCLK])
            if Z80_CATCH_UP_EACH_CYCLE and self.z80_running:
                # The Z80's accesses that win the bus before this cycle, and what they
                # hold it for: the arbiter moves this cycle back in the grid as it moves
                # any 68000 cycle past a grant, so a second Z80 access waits behind it.
                # One asked for within the cut-off of this cycle's start loses to it and
                # is settled at the next cycle, this one then in the grid.
                self.catch_up_z80(t - CUTOFF_MCLK)
                owed = self.m68k_pending_stall
                if owed:
                    self.m68k_pending_stall = 0
                    self.stall += owed
                    t += owed
                # The 68000 drives /AS only once it has the bus back: the cycle begins
                # after this wait rather than being held by it (for logs that stamp it).
                self.grant_wait += owed
        if t >= self.cart_refresh_due:
            late = t - self.cart_refresh_due
            self.cart_refresh_due += (
                (t - self.cart_refresh_due) // CART_REFRESH_MCLK + 1
            ) * CART_REFRESH_MCLK
            stretch = 0
            if late >= CART_REFRESH_SELF_MCLK + CART_REFRESH_STALL:
                pass  # it ran by itself while the bus was free
            elif address < 0x800000:
                stretch = CART_REFRESH_STALL
                self.cart_refresh_pulse_end = t - late + CART_REFRESH_PULSE_END_MCLK
                if late >= CART_REFRESH_SELF_MCLK:
                    # Under way when the access came: it waits for the end, on the
                    # 68000's clock.
                    left = CART_REFRESH_SELF_MCLK + CART_REFRESH_STALL - late
                    stretch = -(-left // M68K_DIVIDER) * M68K_DIVIDER
            elif address >> 16 == 0xA0 and late >= Z80_REFRESH_LATE:
                stretch = Z80_REFRESH_STALL
            if stretch:
                self.stall += stretch
                t += stretch
                if grid is not None:
                    grid[-1][1] += stretch
        elif address >> 16 == 0xA0 and t < self.cart_refresh_pulse_end:
            # Started under the /REF pulse of the refresh that stretched the access
            # before.
            self.stall += Z80_REFRESH_STALL
            if grid is not None:
                grid[-1][1] += Z80_REFRESH_STALL
        if address >= 0xE00000:
            due = self.ram_refresh_due
            if RAM_REFRESH_TIE_AFTER_SELF_RUNS:
                due = ram_refresh_self_runs(due, t)
            tie = due - t == RAM_REFRESH_TIE_MCLK and self.vdp.dot_clock_edge(t)
            wait, self.ram_refresh_due = ram_refresh(due, t, tie)
            if wait:
                self.stall += wait
                if grid is not None:
                    grid[-1][1] += wait

    # -- the arbiter ---------------------------------------------------------

    def _z80_access_time(self) -> int:
        """The master clock of the Z80 access under way: its instruction's start, its
        offset in the instruction (:attr:`z80_offsets`), and what the arbiter has held
        the Z80 for so far in the instruction. Where the offset is not known, the
        instruction's start, as before the table."""
        offsets, k = self.z80_offsets, self.z80_access - 1
        if offsets is not None and 0 <= k < len(offsets):
            t = self.step_start + offsets[k] + self.z80_stall
        else:
            t = self.step_start + self.z80_stall
        hold = self.z80_hold
        return t + hold[1] if hold is not None and t >= hold[0] else t

    def z80_peek(self, address: int) -> int:
        """A byte of the Z80's space without side effects, for looking at code: RAM,
        cartridge ROM and a claimed page's device (``BusDevice.peek_byte``); anything
        else reads $FF. The scheduler's probe reads through it too (``System.z80_probe``),
        so the arbiter's prediction and the access it predicts see one decode."""
        if address < 0x4000:
            return self.z80_ram[address & 0x1FFF]
        if address >= 0x8000:
            full = self.bank | (address & 0x7FFF)
            if self._pages is not None and (device := self._pages[full >> 8]) is not None:
                return device.peek_byte(full)
            if full < 0x400000:
                return self.cartridge.read_byte(full)
        return 0xFF

    def _bank_delay(self, cartridge: bool = True) -> None:
        """What one Z80 access over the 68000's bus -- the bank window, or the VDP --
        costs both CPUs (:func:`~megadrive_python.arbiter.z80_access_wait`).

        The access is placed where z80-python's access-timing table puts it inside the
        instruction (:meth:`_z80_access_time`). The arbiter reads the 68000's bus
        cycles, which are known only as far as the 68000 has run, so the Z80 is not
        stepped into an instruction whose accesses would fall past that
        (``System.catch_up_z80``). The 68000's share is owed rather than charged,
        because the Z80 runs in its wake (``m68k_pending_stall``): the 68000 is
        taken to want the bus again at once, the usual gap after its last cycle (or
        at the cut-off, if the request found it idle), and waits from there.
        """
        index = self.z80_bus_access
        self.z80_bus_access += 1
        done = self.z80_prearbitrated
        if done is not None and done[0] == self.step_start and index < len(done[1]):
            # Settled before the instruction ran, at its own time (``System``'s
            # ``_prearbitrate``): only the Z80's wait remains to be taken.
            wait, m68k_cycles = done[1][index]
        else:
            wait, m68k_cycles = self._arbitrate(self._z80_access_time(), cartridge)
        self.z80_stall += wait * Z80_DIVIDER
        self.bank_accesses += 1
        self.bank_z80_cycles += wait
        self.bank_m68k_cycles += m68k_cycles

    def _arbitrate(self, t: int, cartridge: bool = True) -> tuple[int, int]:
        """A Z80 access over the 68000's bus at ``t``: the 68000's share charged (owed,
        and its later cycles in the grid moved past the grant), the refresh it takes
        (only an access to the cartridge, :data:`Z80_REFRESH_CARTRIDGE_ONLY`); returns
        the Z80's wait states and the 68000's, in their own clocks."""
        # The cartridge refresh grid (``_bus_cycle``): the last tick due by the end of
        # the Z80's grant window, owed if no 68000 cycle since took it and the Z80 has
        # not already.
        due = self.cart_refresh_due
        tick = (
            due + (t + CUTOFF_MCLK + IDLE_GRANT_MCLK - due) // CART_REFRESH_MCLK * CART_REFRESH_MCLK
        )
        taken = tick == self.z80_refresh_taken or any(
            tick <= begin <= t + CUTOFF_MCLK for begin, _span in self.m68k_accesses
        )
        stretched = not taken and (cartridge or not Z80_REFRESH_CARTRIDGE_ONLY)
        wait, resume, own = z80_access_wait(t, self.m68k_accesses, tick if stretched else None)
        if not taken and tick <= own:
            self.z80_refresh_taken = tick
            if Z80_REFRESH_USED_UP and self.cart_refresh_due <= tick:
                self.cart_refresh_due = tick + CART_REFRESH_MCLK
        wanted = t + CUTOFF_MCLK + 1
        nxt = None
        for begin, _span in self.m68k_accesses:
            if begin > t + CUTOFF_MCLK:
                nxt = begin
                break
            wanted = max(wanted, begin + 4 * M68K_DIVIDER)  # its next, four clocks on
        if Z80_GRANT_OVERLAPS_IDLE:
            # The 68000 waits only from when it wants the bus again: its next bus cycle
            # in this instruction, or else where its next instruction starts.
            if nxt is None:
                nxt = self.m68k_step_end + self.m68k_pending_stall
            wanted = max(wanted, nxt)
        resume += -(resume - wanted) % M68K_DIVIDER
        m68k_cycles = max(0, resume - wanted) // M68K_DIVIDER
        if m68k_cycles:
            # The 68000's cycles past the cut-off happen that much later: the one it
            # resumes with is on the bus when the Z80's next access comes (the second
            # read of a ``pop`` waits behind it on the die).
            for cycle in self.m68k_accesses:
                if cycle[0] > t + CUTOFF_MCLK:
                    cycle[0] += m68k_cycles * M68K_DIVIDER
        self.m68k_pending_stall += m68k_cycles * M68K_DIVIDER
        return wait, m68k_cycles

    # -- the sound chips -----------------------------------------------------

    def sound_sync(self, mclk: int) -> None:
        """Run both sound chips up to ``mclk``.

        Called before every access to either chip, so that a register write lands on
        the internal cycle it would land on, and by the system at each frame so that
        the sample queues fill evenly rather than in bursts at the next write.
        """
        self.fm.run(mclk)
        clocks = (mclk - self.psg_mclk) // Z80_DIVIDER
        if clocks > 0:
            self.psg_mclk += clocks * Z80_DIVIDER
            self.psg.run(clocks)

    def _fm_read(self, port: int, mclk: int | None = None) -> int:
        """The YM2612's status byte at $A04000-$A04003 (or $4000-$4003).

        The discrete YM2612 answers on port 0 only; the ASIC answers on all four
        (Eke's counting program on a VA4 Model 1 and a VA0 Model 2, forum t=386
        start=810; hardware-captured, and why Hellfire's music runs slow on a Model
        2).  What a *discrete* chip's other three ports read is not in any pinned
        source: Genesis Plus GX returns 0 (``core/sound/sound.c`` line 152, "invalid
        FM status address"), which is what the board does, labelled emulator-derived
        (docs/undocumented-behavior.md, "The YM2612").
        """
        self.sound_sync(self.m68k_access_clock() if mclk is None else mclk)
        if port and not self.machine.fm.busy_on_all_ports:
            return 0x00
        return self.fm.read(port)

    # -- open bus ------------------------------------------------------------

    def open_bus(self) -> int:
        """The 68000's undriven data bus: the next opcode word it will fetch.

        CMD 1 note 4 and Kabuto ("The VDP ports") read the next instruction's word
        there; at a data access m68000-python's IRC holds it (the prefetch queue has
        moved past the instruction's extension words).
        """
        cpu = self.m68k
        return cpu.irc if cpu is not None else 0xFFFF

    def open_bus_msb(self) -> int:
        """ "Reads return the MSB of the next instruction to be fetched, with the LSB
        set to zero" (CMD 1 note 1: $400000-$7FFFFF; note 4 and 2.2 likewise for the
        Z80 area without the bus, $A11100 and $A11200)."""
        return self.open_bus() & 0xFF00

    # -- the 68000's bus -------------------------------------------------------

    def read_word(self, address: int) -> int:
        if self.refresh_enabled:
            self._bus_cycle(address)
        top = address >> 16
        if top < 0x40:
            return self.cartridge.read_word(address)
        if top >= 0xE0:
            offset = address & 0xFFFF
            return (self.ram[offset] << 8) | self.ram[offset + 1]
        if top >= 0xC0:
            vdp = self._vdp_access(address)
            value = vdp.read_word(address & 0x1F, self.m68k_access_clock())
            self.stall += _on_edge(vdp.hold)
            return value
        if top == 0xA0:
            value = self._z80_area_read(address)
            self._z80_space_cost()
            return value | (value << 8) if value is not None else self.open_bus_msb()
        # The rest is the expansion sockets' (socket 3: a claimed page is the device's,
        # at the access's own clock), and where nothing claims, what it always was.
        if self._pages is not None and (device := self._pages[address >> 8]) is not None:
            return device.read_word(address, self.m68k_access_clock())
        if top == 0xA1:
            return self._system_read_word(address)
        if 0x80 <= top < 0xA0:
            raise Lockup(f"68000 read of ${address:06X} (32X area) without a 32X")
        return self.open_bus_msb()

    def read_byte(self, address: int) -> int:
        if self.refresh_enabled:
            self._bus_cycle(address)
        top = address >> 16
        if top < 0x40:
            return self.cartridge.read_byte(address)
        if top >= 0xE0:
            return self.ram[address & 0xFFFF]
        if top >= 0xC0:
            vdp = self._vdp_access(address)
            value = vdp.read_byte(address & 0x1F, self.m68k_access_clock())
            self.stall += _on_edge(vdp.hold)
            return value
        if top == 0xA0:
            value = self._z80_area_read(address)
            self._z80_space_cost()
            if value is not None:
                return value
            return (self.open_bus() >> 8) if not address & 1 else 0
        if self._pages is not None and (device := self._pages[address >> 8]) is not None:
            return device.read_byte(address, self.m68k_access_clock(), M68K)
        if top == 0xA1:
            word = self._system_read_word(address & ~1)
            return word & 0xFF if address & 1 else word >> 8
        if 0x80 <= top < 0xA0:
            raise Lockup(f"68000 read of ${address:06X} (32X area) without a 32X")
        return 0 if address & 1 else self.open_bus() >> 8

    def write_word(self, address: int, value: int) -> None:
        if self.refresh_enabled:
            self._bus_cycle(address)
        top = address >> 16
        if top >= 0xE0:
            offset = address & 0xFFFF
            self.ram[offset] = value >> 8
            self.ram[offset + 1] = value & 0xFF
        elif top < 0x40:
            self.cartridge.write_word(address, value)
        elif top >= 0xC0:
            self.stall += _on_edge(
                self._vdp_access(address).write_word(
                    address & 0x1F, value, self.m68k_access_clock()
                )
            )
        elif top == 0xA0:
            # "When doing word-wide writes to Z80 RAM, only the MSB is written"
            # (CMD 1.2): the Z80's bus is eight bits wide and takes D15-D8.
            self._z80_area_write(address & ~1, value >> 8)
            self._z80_space_cost()
        elif self._pages is not None and (device := self._pages[address >> 8]) is not None:
            device.write_word(address, value, self.m68k_access_clock())
        elif top == 0xA1:
            self._system_write(address, value >> 8, value & 0xFF, word=True)
        elif 0x80 <= top < 0xA0:
            raise Lockup(f"68000 write to ${address:06X} (32X area) without a 32X")

    def write_byte(self, address: int, value: int) -> None:
        if self.refresh_enabled:
            self._bus_cycle(address)
        top = address >> 16
        if top >= 0xE0:
            self.ram[address & 0xFFFF] = value
        elif top < 0x40:
            self.cartridge.write_byte(address, value)
        elif top >= 0xC0:
            # "Writing to the VDP control or data ports is interpreted as a 16-bit
            # write, with the LSB duplicated in the MSB" (CMD 1.2).
            self.stall += _on_edge(
                self._vdp_access(address).write_byte(
                    address & 0x1F, value, self.m68k_access_clock()
                )
            )
        elif top == 0xA0:
            self._z80_area_write(address, value)
            self._z80_space_cost()
        elif self._pages is not None and (device := self._pages[address >> 8]) is not None:
            device.write_byte(address, value, self.m68k_access_clock(), M68K)
        elif top == 0xA1:
            # The 68000 drives a byte on both halves of the data bus (UM 5.1.2), so
            # a register on D8 sees bit 0 of the byte at either address.
            self._system_write(address, value, value, word=False)
        elif 0x80 <= top < 0xA0:
            raise Lockup(f"68000 write to ${address:06X} (32X area) without a 32X")

    def dma_read_word(self, address: int) -> int:
        """A word the VDP's 68000-to-VDP DMA reads from the 68000's bus (the 68000 is
        held meanwhile): cartridge, work RAM, or anything else as the 68000 would read
        it. A source in the VDP's own range reads $FFFF here: no measurement says what
        it gives (not claimed). A claimed page is its device's, at the VDP's clock for
        the read (socket 3; the device was told of the start, :meth:`dma_started`)."""
        if self._pages is not None and (device := self._pages[address >> 8]) is not None:
            return device.dma_read_word(address, self.vdp.mclk)
        top = address >> 16
        if top < 0x40:
            return self.cartridge.read_word(address)
        if top >= 0xE0:
            offset = address & 0xFFFF
            return (self.ram[offset] << 8) | self.ram[offset + 1]
        if top >= 0xC0:
            return 0xFFFF
        refresh, self.refresh_enabled = self.refresh_enabled, False
        try:
            return self.read_word(address)
        finally:
            self.refresh_enabled = refresh

    def tas_write(self, address: int, value: int) -> None:
        """TAS's write half: the Mega Drive's bus never completes it
        (m68000-python docs/undocumented-behavior.md; MAME ``megadriv_tas_callback``)."""

    # -- the VDP's mirror pattern ------------------------------------------------

    def _vdp_access(self, address: int) -> VDP:
        """The VDP decodes 110n n000 nnnn nnnn 000m mmmm (CMD 1 note 5); anything
        else in $C00000-$DFFFFF locks up. TMSS consoles lock up on a VDP access
        before "SEGA" is written to $A14000 (Manual PDF page 94)."""
        if address & 0xE700E0 != 0xC00000:
            raise Lockup(f"68000 access to ${address:06X}: outside the VDP's mirror pattern")
        if self.machine.tmss and self.tmss_register != 0x53454741:
            raise Lockup(f"68000 access to the VDP at ${address:06X} before TMSS was unlocked")
        return self.vdp

    # -- $A00000-$A0FFFF: the Z80's space from the 68000 -----------------------

    def _z80_area_read(self, address: int) -> int | None:
        """A byte of the Z80's space, or None when the 68000 does not hold the bus
        ("reading $A00000-A0FFFF will return the MSB of the next instruction to be
        fetched, and the LSB will be set to zero. Writes are ignored.", CMD 2.2)."""
        t = self.m68k_access_clock()
        if not self.m68k_has_z80_bus(t):
            return None
        self.catch_up_z80(t)
        offset = address & 0x7FFF  # "A08000-A0FFFFh mirror A00000-A07FFFh" (CMD 1)
        if offset < 0x4000:
            # A word read "has the LSB of the data duplicated in the MSB"; CMD's
            # example reads $AAAA from AA BB, the byte at the even address, which
            # the word paths above duplicate.
            return self.z80_ram[offset & 0x1FFF]
        if offset < 0x6000:
            return self._fm_read(offset & 3, t + self.fm_read_lag)
        if offset >= 0x7F00:
            raise Lockup(f"68000 access to ${address:06X}: the VDP through the Z80's space")
        return 0xFF  # the bank register and $6100-$7EFF read $FF (CMD 2.1)

    def _z80_area_write(self, address: int, value: int) -> None:
        t = self.m68k_access_clock()
        if not self.m68k_has_z80_bus(t):
            return
        self.catch_up_z80(t)
        offset = address & 0x7FFF
        if offset < 0x4000:
            self.z80_ram[offset & 0x1FFF] = value
        elif offset < 0x6000:
            self.sound_sync(t + self.fm_write_lag)
            self.fm.write(offset & 3, value)
        elif offset < 0x6100:
            self.write_bank(value)
        elif offset >= 0x7F00:
            raise Lockup(f"68000 access to ${address:06X}: the VDP through the Z80's space")

    def write_bank(self, value: int) -> None:
        """One bit into the bank register: bits 15-23 of the window's 68000 address
        arrive in that order, each write shifting the others down (CMD 2.3)."""
        self.bank = ((self.bank >> 1) | ((value & 1) << 23)) & 0xFF8000

    # -- $A10000-$A1FFFF: I/O, bus request, reset, /TIME, TMSS -----------------

    def _system_read_word(self, address: int) -> int:
        offset = address & 0xFFFF
        if offset < 0x20:
            value = self.io.read(address | 1, self.m68k_access_clock())
            return value | (value << 8)
        if offset & 0xFF00 == 0x1100:
            # Bit 0 of the MSB is 1 while the 68000 cannot use the Z80's bus; the
            # rest is the next instruction's MSB and a zero LSB (CMD 1 note 4, 2.2).
            t = self.m68k_access_clock()
            self.catch_up_z80(t)
            # The poll itself is not held: it reads "busy" until BUSACK (the arbiter).
            granted = self.z80_bus_requested and not self.z80_reset
            if granted and self.arbiter_enabled:
                granted = t >= self.busack_at
            busy = 0 if granted else 0x100
            return (self.open_bus() & 0xFE00) | busy
        if offset & 0xFF00 == 0x3000:
            value = self.cartridge.read_time(address | 1)
            if value is not None:
                return value | (value << 8)
        return self.open_bus_msb()

    def _system_write(self, address: int, high: int, low: int, *, word: bool,
                      mclk: int | None = None) -> None:  # fmt: skip
        """A write to $A1xxxx: ``high`` is what D15-D8 carry, ``low`` D7-D0.

        ``mclk`` is when the write happens; the I/O chip needs it because a 6-button
        pad counts the *edges* of TH (:mod:`megadrive_python.io`). It defaults to the
        68000's own access clock; the Z80's bank window passes its own.
        """
        offset = address & 0xFFFF
        if offset < 0x20:
            self.io.write(address, low, self.m68k_access_clock() if mclk is None else mclk)
        elif offset & 0xFF00 == 0x1100:
            request = bool(high & 1)
            t = self.m68k_access_clock()
            if not self.arbiter_enabled:
                self.busack_at = t
                self.catch_up_z80(t, stopping=request and self.z80_running)
            elif request and self.z80_running:
                # BUSACK: the Z80 samples BUSREQ at the end of the machine cycle it is
                # in and lets go after it (Zilog UM0080; :mod:`megadrive_python.arbiter`),
                # and the arbiter takes its own propagation to report that.
                # The system probes the Z80 for that clock and runs it up to it.
                self.busack_at = self.z80_busack(t)
            else:
                # The Z80 is held in reset or has already let go, so there is no
                # machine cycle to wait for -- but the arbiter still takes its
                # propagation to answer (:data:`~megadrive_python.arbiter`.
                # ``BUSACK_PROPAGATION_MCLK``), which is what both ROMs that poll
                # $A11100 see the referee do (docs/worklog.md, the timing session).
                self.busack_at = t + BUSACK_PROPAGATION_MCLK
                self.catch_up_z80(t)
            released = self.z80_bus_requested and not request and not self.z80_reset
            self.z80_bus_requested = request
            if released:
                self.on_z80_resume(t, False)
        elif offset & 0xFF00 == 0x1200:
            t = self.m68k_access_clock()
            self.set_z80_reset(not high & 1, t, t + ZRES_PIN_MCLK, write=True)
        elif offset & 0xFF00 == 0x3000:
            self.cartridge.write_time(address | 1, low)
        elif offset & 0xFF00 == 0x4100:
            # $A14101 bit 0: the cartridge (1) or the TMSS boot ROM (0). The boot
            # program reaches it with BSET and BCLR, so only bit 0 of the byte
            # written matters (the disassembly's ``RAM_Code``); MAME watches the
            # same word (``mdconsole.cpp`` ``tmss_swap_w``).
            self._tmss_select(bool(low & 1))
        elif offset & 0xFFFC == 0x4000:
            # The TMSS register takes "SEGA" as a long, two words or four bytes.
            if word:
                shift = 16 - (offset & 2) * 8
                value, mask = (high << 8) | low, 0xFFFF
            else:
                shift = (3 - (offset & 3)) * 8
                value, mask = low, 0xFF
            self.tmss_register = (self.tmss_register & ~(mask << shift)) | (value << shift)

    # -- the Z80's bus ----------------------------------------------------------

    def z80_read(self, address: int) -> int:
        self.z80_access += 1
        if address < 0x4000:
            return self.z80_ram[address & 0x1FFF]
        if address >= 0x8000:
            if self.arbiter_enabled:
                # the arbiter holds both CPUs for this access
                self._bank_delay((self.bank | (address & 0x7FFF)) < 0x800000)
            return self._z80_bank_read(self.bank | (address & 0x7FFF))
        if address < 0x6000:
            return self._fm_read(address & 3, self._z80_access_time())
        if address >= 0x7F00:
            if address >= 0x7F20:
                raise Lockup(f"Z80 access to ${address:04X} (beyond the VDP's $7F00-$7F1F)")
            if self.arbiter_enabled and Z80_VDP_ARBITRATED:
                self._bank_delay(False)  # the VDP is on the 68000's bus
            return self.vdp.z80_read(address & 0x1F, self._z80_access_time())
        return 0xFF  # the bank register and $6100-$7EFF (CMD 2.1)

    def z80_write(self, address: int, value: int) -> None:
        self.z80_access += 1
        if address < 0x4000:
            self.z80_ram[address & 0x1FFF] = value
        elif address >= 0x8000:
            if self.arbiter_enabled:
                self._bank_delay((self.bank | (address & 0x7FFF)) < 0x800000)
            self._z80_bank_write(self.bank | (address & 0x7FFF), value)
        elif address < 0x6000:
            self.sound_sync(self._z80_access_time())
            self.fm.write(address & 3, value)
        elif address < 0x6100:
            self.write_bank(value)
        elif address >= 0x7F00:
            if address >= 0x7F20:
                raise Lockup(f"Z80 access to ${address:04X} (beyond the VDP's $7F00-$7F1F)")
            if self.arbiter_enabled and Z80_VDP_ARBITRATED:
                self._bank_delay(False)
            self.vdp.z80_write(address & 0x1F, value, self._z80_access_time())

    def _z80_bank_read(self, address: int) -> int:
        """A byte of the 68000's space through the Z80's window.

        Reads of 68000 RAM fail ("writes always worked and reads always failed",
        MoD's ``gen_test_z80_bus`` on several machines, forum t=3021); CMD "Read
        banked 68000 RAM. (returns FFh)". The Z80's own space locks up (CMD 2.3, MoD);
        the VDP's unused addresses read $FF and its status's top six bits 1 (CMD 1.1).
        Undriven bits read 1 (CMD 1.1 note 1). A claimed page is its device's, at the
        Z80's own clock (socket 3), before any of that.
        """
        if self._pages is not None and (device := self._pages[address >> 8]) is not None:
            return device.read_byte(address, self._z80_access_time(), Z80)
        top = address >> 16
        if top < 0x40:
            return self.cartridge.read_byte(address)
        if top >= 0xE0:
            return 0xFF
        if top >= 0xC0:
            if address & 0xE700E0 != 0xC00000:
                raise Lockup(f"Z80 bank access to ${address:06X}: outside the VDP's pattern")
            return self.vdp.z80_bank_read(address & 0x1F, self._z80_access_time())
        if top == 0xA0:
            raise Lockup(f"Z80 bank access to ${address:06X}: the Z80's own space")
        if top == 0xA1 and address & 0xFFFF < 0x20:
            return self.io.read(address, self._z80_access_time())
        if 0x80 <= top < 0xA0:
            raise Lockup(f"Z80 bank access to ${address:06X} (32X area) without a 32X")
        return 0xFF

    def _z80_bank_write(self, address: int, value: int) -> None:
        if self._pages is not None and (device := self._pages[address >> 8]) is not None:
            device.write_byte(address, value, self._z80_access_time(), Z80)
            return
        top = address >> 16
        if top >= 0xE0:
            self.ram[address & 0xFFFF] = value
        elif top < 0x40:
            self.cartridge.write_byte(address, value)
        elif top >= 0xC0:
            if address & 0xE700E0 != 0xC00000:
                raise Lockup(f"Z80 bank access to ${address:06X}: outside the VDP's pattern")
            self.vdp.write_byte(address & 0x1F, value, self._z80_access_time())
        elif top == 0xA0:
            raise Lockup(f"Z80 bank access to ${address:06X}: the Z80's own space")
        elif top == 0xA1:
            self._system_write(address, value, value, word=False, mclk=self._z80_access_time())
        elif 0x80 <= top < 0xA0:
            raise Lockup(f"Z80 bank access to ${address:06X} (32X area) without a 32X")
