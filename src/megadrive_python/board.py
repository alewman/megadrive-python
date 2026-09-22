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

from .arbiter import BUS_CYCLE, LOOKAHEAD, MAX_RELEASE, bank_access_delay
from .cartridge import Cartridge
from .io import IOChip
from .machine import M68K_DIVIDER, Z80_DIVIDER, Machine
from .psg import PSG
from .vdp import VDP
from .ym2612 import YM2612

if TYPE_CHECKING:
    from m68000_python import M68000CPU


#: The cartridge-side refresh: every 128 68000 clocks of real time, the next 68000 bus
#: cycle absorbs it, and if that cycle is a cartridge access it takes 2 clocks longer.
#: Kabuto ("The bus system": "every time 128 cycles have passed the next bus access is
#: slowed down by 2 cycles ... if this hits when the 68k is writing to the VDP ... the
#: slowdown doesn't happen"; hardware-measured research), made exact against Nuked-MD
#: (die-derived) in four probe loops, 670 of 670 ticks each (docs/worklog.md).
CART_REFRESH_MCLK = 896
CART_REFRESH_STALL = 14
#: The work-RAM refresh the VDP performs: once due, the next work-RAM access waits 3
#: 68000 clocks; it falls due again 791-819 MCLK after that access started (Nuked-MD
#: probes; Kabuto: "RAM accesses get their own further slowdowns"). The midpoint is
#: used; which value in the window is right is not settled (docs/worklog.md).
RAM_REFRESH_STALL = 21
RAM_REFRESH_AFTER_MCLK = 805


class Lockup(Exception):
    """An access that locks up a real console (CMD ``gen-hw.txt`` section 1).

    The board raises it out of the CPU's step; the system stops and reports it
    (docs/undocumented-behavior.md, "The bus and memory map", item 2).
    """


class Board:
    """Memory maps and devices for one :class:`Machine`."""

    def __init__(self, machine: Machine, cartridge: Cartridge | None) -> None:
        self.machine = machine
        self.cartridge = cartridge if cartridge is not None else Cartridge(b"")
        self.ram = bytearray(0x10000)
        self.z80_ram = bytearray(0x2000)
        self.io = IOChip(machine.version_register)
        self.vdp = VDP(machine, self)
        #: The two sound chips.  Both are lazy: they catch up to the master clock of
        #: an access before it, and to the board's own clock when a caller drains
        #: their samples (:meth:`sound_sync`).  The FM shares /RESET with the Z80,
        #: which is held at power-on ("For as long as the Z80 is reset, the YM2612
        #: cannot be used", CMD ``gen-hw.txt`` 2.2).
        self.fm = YM2612(machine.fm)
        self.fm.reset_line(True)
        #: The PSG lives inside the VDP and is written at $C00011 or $7F11; reading
        #: its port locks up a real console (CMD).  It is not held by the Z80's reset.
        self.psg = PSG()
        self.psg_mclk = 0
        #: 68000 address bits 23-15 of the Z80's bank window; power-on 0 (unknown on
        #: hardware; MAME 0.285 ``machine_reset`` clears it).
        self.bank = 0
        #: "On power-up, the Z80 will be reset and will have the bus" (CMD 2.2).
        self.z80_reset = True
        self.z80_bus_requested = False
        #: TMSS: the VDP is locked until "SEGA" is written to $A14000 (Manual p. 94).
        self.tmss_register = 0
        self.m68k: M68000CPU | None = None  # set by the system; read for open bus
        #: The master clock at which the current CPU step started, set by the system.
        self.step_start = 0
        #: Master clocks the 68000 has been held during the current step (stalls).
        self.stall = 0
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
        self.ram_refresh_due = RAM_REFRESH_AFTER_MCLK
        self.refresh_enabled = True
        #: The arbiter's delays (milestone 5, :mod:`megadrive_python.arbiter`): the Z80's
        #: bank accesses holding both CPUs, and the bus grant waiting for the Z80's
        #: machine cycle to end.  MAME models neither, so the milestone-1 lockstep turns
        #: this off along with ``refresh_enabled``, which it also needs for the 68000's
        #: access grid.
        self.arbiter_enabled = True
        #: The master clock of each 68000 bus cycle started in the step under way, in
        #: order: the arbiter reads it to find how long the 68000 still needs the bus.
        self.m68k_accesses: list[int] = []
        #: Master clocks the Z80 has been held during its current step, and master
        #: clocks the 68000 owes for Z80 bank accesses made in its wake (they are added
        #: to its next step: the Z80 runs behind it, so the instruction they really fell
        #: inside has already been executed -- ``System.step``).
        self.z80_stall = 0
        self.m68k_pending_stall = 0
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

    # -- time ----------------------------------------------------------------

    def m68k_access_clock(self) -> int:
        """The master clock at which the 68000's current bus access starts.

        m68000-python counts an instruction's clocks as it goes, adding 4 for each
        bus access *before* calling the host (``_cycles`` in its ``_core.py``), so
        inside a bus callback ``_cycles - 4`` is the clock the access began at,
        counted from the start of the step. That attribute is private: reading it
        here is the labelled workaround for the accessor proposed to m68000-python as
        its PR #1, ``M68000CPU.step_clocks`` (docs/worklog.md, "Proposed core
        change"). This is the only place the board reads it; when the PR lands,
        ``cpu._cycles`` below becomes ``cpu.step_clocks``.
        """
        cpu = self.m68k
        if cpu is None:
            return self.step_start + self.stall
        return self.step_start + self.stall + (cpu._cycles - 4) * M68K_DIVIDER

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
            self.stall += self.busack_at - t
        return True

    def _bus_cycle(self, address: int) -> None:
        """One 68000 bus cycle about to start at ``address``: its refresh stalls, and
        its place in the grid the arbiter reads (:meth:`_m68k_release`)."""
        t = self.m68k_access_clock()
        if self.arbiter_enabled:
            self.m68k_accesses.append(t)
        if t >= self.cart_refresh_due:
            self.cart_refresh_due += (
                (t - self.cart_refresh_due) // CART_REFRESH_MCLK + 1
            ) * CART_REFRESH_MCLK
            if address < 0x800000:
                self.stall += CART_REFRESH_STALL
                t += CART_REFRESH_STALL
        if address >= 0xE00000 and t >= self.ram_refresh_due:
            self.stall += RAM_REFRESH_STALL
            self.ram_refresh_due = t + RAM_REFRESH_AFTER_MCLK

    # -- the arbiter ---------------------------------------------------------

    def _m68k_release(self, t: int) -> int:
        """Kabuto's ``a``: the 68000 cycles until the 68000 has finished the bus cycle
        it is on, for a Z80 request at master clock ``t``.

        "If the next 68k access 2 cycles ahead or less or already ongoing the 68k will
        finish it first ... Thus there is an initial delay of 1 to 6 cycles" (Kabuto,
        footnote 1). The board knows where each of the step's accesses began
        (:meth:`_bus_cycle`); each takes four 68000 clocks, m68000-python modelling no
        wait states of its own.
        """
        release = 1
        span = BUS_CYCLE * M68K_DIVIDER
        for start in self.m68k_accesses:
            if start + span <= t:
                continue
            if start - t > LOOKAHEAD * M68K_DIVIDER:
                break
            left = -(-(start + span - t) // M68K_DIVIDER)
            if left > release:
                release = left
        return min(release, MAX_RELEASE)

    def _bank_delay(self) -> None:
        """What one Z80 access through the bank window costs both CPUs (Kabuto's
        footnote 1, :func:`~megadrive_python.arbiter.bank_access_delay`).

        z80-python reports only an instruction's total, so the access is placed at the
        Z80 step's start plus whatever it has already been held: where inside the
        instruction it really falls is not known (docs/polish.md). The 68000's share is
        owed rather than charged, because the Z80 runs in its wake (``m68k_pending_stall``).
        """
        t = self.step_start + self.z80_stall
        # "b (2 if the z80->ROM access is prolonged by a refresh delay, 0 if not)": the
        # cartridge refresh grid (``CART_REFRESH_MCLK``) ticking inside the access.
        # The tick is read, not consumed: the 68000's own accesses walk the same grid
        # in ``_bus_cycle`` and, running ahead of the Z80, would otherwise always have
        # taken it first.
        since = (t - self.cart_refresh_due) % CART_REFRESH_MCLK
        z80_cycles, m68k_cycles = bank_access_delay(
            -t % M68K_DIVIDER, self._m68k_release(t), since < CART_REFRESH_STALL
        )
        self.z80_stall += z80_cycles * Z80_DIVIDER
        self.m68k_pending_stall += m68k_cycles * M68K_DIVIDER
        self.bank_accesses += 1
        self.bank_z80_cycles += z80_cycles
        self.bank_m68k_cycles += m68k_cycles

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
            self.stall += vdp.hold
            return value
        if top == 0xA0:
            value = self._z80_area_read(address)
            return value | (value << 8) if value is not None else self.open_bus_msb()
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
            self.stall += vdp.hold
            return value
        if top == 0xA0:
            value = self._z80_area_read(address)
            if value is not None:
                return value
            return (self.open_bus() >> 8) if not address & 1 else 0
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
            self.stall += self._vdp_access(address).write_word(
                address & 0x1F, value, self.m68k_access_clock()
            )
        elif top == 0xA0:
            # "When doing word-wide writes to Z80 RAM, only the MSB is written"
            # (CMD 1.2): the Z80's bus is eight bits wide and takes D15-D8.
            self._z80_area_write(address & ~1, value >> 8)
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
            self.stall += self._vdp_access(address).write_byte(
                address & 0x1F, value, self.m68k_access_clock()
            )
        elif top == 0xA0:
            self._z80_area_write(address, value)
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
        it gives (not claimed)."""
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
            return self._fm_read(offset & 3)
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
            self.sound_sync(self.m68k_access_clock())
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
            value = self.io.read(address | 1)
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

    def _system_write(self, address: int, high: int, low: int, *, word: bool) -> None:
        """A write to $A1xxxx: ``high`` is what D15-D8 carry, ``low`` D7-D0."""
        offset = address & 0xFFFF
        if offset < 0x20:
            self.io.write(address, low)
        elif offset & 0xFF00 == 0x1100:
            request = bool(high & 1)
            t = self.m68k_access_clock()
            if request and self.z80_running and self.arbiter_enabled:
                # BUSACK: the Z80 samples BUSREQ at the end of the machine cycle it is
                # in and lets go after it (Zilog UM0080; :mod:`megadrive_python.arbiter`).
                # The system probes the Z80 for that clock and runs it up to it.
                self.busack_at = self.z80_busack(t)
            else:
                self.busack_at = t
                self.catch_up_z80(t, stopping=request and self.z80_running)
            self.z80_bus_requested = request
        elif offset & 0xFF00 == 0x1200:
            held = not high & 1
            self.catch_up_z80(self.m68k_access_clock(), stopping=held and self.z80_running)
            if held != self.z80_reset:
                self.z80_reset = held
                self.sound_sync(self.m68k_access_clock())
                self.fm.reset_line(held)
                self.on_z80_reset(held)
        elif offset & 0xFF00 == 0x3000:
            self.cartridge.write_time(address | 1, low)
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
        if address < 0x4000:
            return self.z80_ram[address & 0x1FFF]
        if address >= 0x8000:
            if self.arbiter_enabled:
                self._bank_delay()  # the arbiter holds both CPUs for this access
            return self._z80_bank_read(self.bank | (address & 0x7FFF))
        if address < 0x6000:
            return self._fm_read(address & 3, self.step_start)
        if address >= 0x7F00:
            if address >= 0x7F20:
                raise Lockup(f"Z80 access to ${address:04X} (beyond the VDP's $7F00-$7F1F)")
            return self.vdp.z80_read(address & 0x1F, self.step_start)
        return 0xFF  # the bank register and $6100-$7EFF (CMD 2.1)

    def z80_write(self, address: int, value: int) -> None:
        if address < 0x4000:
            self.z80_ram[address & 0x1FFF] = value
        elif address >= 0x8000:
            if self.arbiter_enabled:
                self._bank_delay()
            self._z80_bank_write(self.bank | (address & 0x7FFF), value)
        elif address < 0x6000:
            self.sound_sync(self.step_start)
            self.fm.write(address & 3, value)
        elif address < 0x6100:
            self.write_bank(value)
        elif address >= 0x7F00:
            if address >= 0x7F20:
                raise Lockup(f"Z80 access to ${address:04X} (beyond the VDP's $7F00-$7F1F)")
            self.vdp.z80_write(address & 0x1F, value, self.step_start)

    def _z80_bank_read(self, address: int) -> int:
        """A byte of the 68000's space through the Z80's window.

        Reads of 68000 RAM fail ("writes always worked and reads always failed",
        MoD's ``gen_test_z80_bus`` on several machines, forum t=3021); CMD "Read
        banked 68000 RAM. (returns FFh)". The Z80's own space locks up (CMD 2.3, MoD);
        the VDP's unused addresses read $FF and its status's top six bits 1 (CMD 1.1).
        Undriven bits read 1 (CMD 1.1 note 1).
        """
        top = address >> 16
        if top < 0x40:
            return self.cartridge.read_byte(address)
        if top >= 0xE0:
            return 0xFF
        if top >= 0xC0:
            if address & 0xE700E0 != 0xC00000:
                raise Lockup(f"Z80 bank access to ${address:06X}: outside the VDP's pattern")
            return self.vdp.z80_bank_read(address & 0x1F, self.step_start)
        if top == 0xA0:
            raise Lockup(f"Z80 bank access to ${address:06X}: the Z80's own space")
        if top == 0xA1 and address & 0xFFFF < 0x20:
            return self.io.read(address)
        if 0x80 <= top < 0xA0:
            raise Lockup(f"Z80 bank access to ${address:06X} (32X area) without a 32X")
        return 0xFF

    def _z80_bank_write(self, address: int, value: int) -> None:
        top = address >> 16
        if top >= 0xE0:
            self.ram[address & 0xFFFF] = value
        elif top < 0x40:
            self.cartridge.write_byte(address, value)
        elif top >= 0xC0:
            if address & 0xE700E0 != 0xC00000:
                raise Lockup(f"Z80 bank access to ${address:06X}: outside the VDP's pattern")
            self.vdp.write_byte(address & 0x1F, value, self.step_start)
        elif top == 0xA0:
            raise Lockup(f"Z80 bank access to ${address:06X}: the Z80's own space")
        elif top == 0xA1:
            self._system_write(address, value, value, word=False)
        elif 0x80 <= top < 0xA0:
            raise Lockup(f"Z80 bank access to ${address:06X} (32X area) without a 32X")
