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

from .cartridge import Cartridge
from .io import IOChip
from .machine import M68K_DIVIDER, Machine
from .vdp import VDP
from .ym2612 import YM2612Stub

if TYPE_CHECKING:
    from m68000_python import M68000CPU


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
        self.fm = YM2612Stub()
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
        #: Called when the Z80's reset line is released or asserted (True = held).
        self.on_z80_reset: Callable[[bool], None] = lambda held: None

    # -- time ----------------------------------------------------------------

    def m68k_access_clock(self) -> int:
        """The master clock at which the 68000's current bus access starts.

        m68000-python counts an instruction's clocks as it goes, adding 4 for each
        bus access *before* calling the host (``_cycles`` in its ``_core.py``), so
        inside a bus callback ``_cycles - 4`` is the clock the access began at,
        counted from the start of the step. That attribute is private: reading it
        here is the labelled workaround for the accessor proposed to m68000-python
        (docs/worklog.md, "Proposed core change"). It is the only place the board
        reads it.
        """
        cpu = self.m68k
        if cpu is None:
            return self.step_start + self.stall
        return self.step_start + self.stall + (cpu._cycles - 4) * M68K_DIVIDER

    @property
    def z80_running(self) -> bool:
        """The Z80 runs unless it is held in reset or has granted its bus."""
        return not self.z80_reset and not self.z80_bus_requested

    @property
    def m68k_has_z80_bus(self) -> bool:
        """ "The Z80 bus can only be accessed by the 68000 when the Z80 is running
        and the 68000 has the bus" (CMD 2.2): requested and out of reset."""
        return self.z80_bus_requested and not self.z80_reset

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
        top = address >> 16
        if top < 0x40:
            return self.cartridge.read_word(address)
        if top >= 0xE0:
            offset = address & 0xFFFF
            return (self.ram[offset] << 8) | self.ram[offset + 1]
        if top >= 0xC0:
            return self._vdp_access(address).read_word(address & 0x1F, self.m68k_access_clock())
        if top == 0xA0:
            value = self._z80_area_read(address)
            return value | (value << 8) if value is not None else self.open_bus_msb()
        if top == 0xA1:
            return self._system_read_word(address)
        if 0x80 <= top < 0xA0:
            raise Lockup(f"68000 read of ${address:06X} (32X area) without a 32X")
        return self.open_bus_msb()

    def read_byte(self, address: int) -> int:
        top = address >> 16
        if top < 0x40:
            return self.cartridge.read_byte(address)
        if top >= 0xE0:
            return self.ram[address & 0xFFFF]
        if top >= 0xC0:
            return self._vdp_access(address).read_byte(address & 0x1F, self.m68k_access_clock())
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
        if not self.m68k_has_z80_bus:
            return None
        self.catch_up_z80(self.m68k_access_clock())
        offset = address & 0x7FFF  # "A08000-A0FFFFh mirror A00000-A07FFFh" (CMD 1)
        if offset < 0x4000:
            # A word read "has the LSB of the data duplicated in the MSB"; CMD's
            # example reads $AAAA from AA BB, the byte at the even address, which
            # the word paths above duplicate.
            return self.z80_ram[offset & 0x1FFF]
        if offset < 0x6000:
            return self.fm.read(offset & 3)
        if offset >= 0x7F00:
            raise Lockup(f"68000 access to ${address:06X}: the VDP through the Z80's space")
        return 0xFF  # the bank register and $6100-$7EFF read $FF (CMD 2.1)

    def _z80_area_write(self, address: int, value: int) -> None:
        if not self.m68k_has_z80_bus:
            return
        self.catch_up_z80(self.m68k_access_clock())
        offset = address & 0x7FFF
        if offset < 0x4000:
            self.z80_ram[offset & 0x1FFF] = value
        elif offset < 0x6000:
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
            self.catch_up_z80(self.m68k_access_clock())
            busy = 0 if self.m68k_has_z80_bus else 0x100
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
            self.catch_up_z80(self.m68k_access_clock(), stopping=request and self.z80_running)
            self.z80_bus_requested = request
        elif offset & 0xFF00 == 0x1200:
            held = not high & 1
            self.catch_up_z80(self.m68k_access_clock(), stopping=held and self.z80_running)
            if held != self.z80_reset:
                self.z80_reset = held
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
            return self._z80_bank_read(self.bank | (address & 0x7FFF))
        if address < 0x6000:
            return self.fm.read(address & 3)
        if address >= 0x7F00:
            if address >= 0x7F20:
                raise Lockup(f"Z80 access to ${address:04X} (beyond the VDP's $7F00-$7F1F)")
            return self.vdp.z80_read(address & 0x1F, self.step_start)
        return 0xFF  # the bank register and $6100-$7EFF (CMD 2.1)

    def z80_write(self, address: int, value: int) -> None:
        if address < 0x4000:
            self.z80_ram[address & 0x1FFF] = value
        elif address >= 0x8000:
            self._z80_bank_write(self.bank | (address & 0x7FFF), value)
        elif address < 0x6000:
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
