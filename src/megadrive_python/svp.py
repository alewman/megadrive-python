"""Virtua Racing's cartridge: the SVP (``rom_svp``), an SSP1601 DSP and its memory controller.

The DSP is :mod:`ssp1601_python`, an optional dependency (``pip install ssp1601-python``):
without it the image loads as plain ROM with :attr:`Cartridge.unsupported` set, as before.
The memory controller -- the DSP's external registers, the 128 KB of DRAM the 68000 and the
DSP share, the 2 KB of instruction RAM and the two status words at $A15000 -- is this
module, a transcription of MAME 0.285's ``bus/megadrive/svp.cpp`` (Grazvydas Ignotas, from
Tasco Deluxe's reverse engineering; notaz's ``svpdoc.txt`` is the description). Nothing
here is hardware-measured: Virtua Racing is the only software, and MAME runs it.

What it does not do, which MAME does not either: the chip's internal ROM (``svp.bin``, 2 KB
at program words $FC00-$FFFF, holding the reset vector and the boot process that checks the
header's security string) -- the DSP starts at the game's entry point, word $400 -- and the
halt at $A15006.
"""

from __future__ import annotations

from typing import ClassVar

from .mappers import Board, board

try:
    from ssp1601_python import SSP1601
except ImportError:  # the optional dependency is not installed
    SSP1601 = None

#: How many DSP cycles run between looks for an idle loop, and how many looks are kept.
IDLE_CHUNK = 256
IDLE_WINDOW = 64

#: The DSP's clock is the master clock * 3/7 (MAME: MASTER_CLOCK_NTSC / 7 * 3).
SSP_CLOCK_NUM, SSP_CLOCK_DEN = 3, 7

HAVE_ADDR = 1  # an address has gone into PMC and it waits for the mode
PMC_SET = 2  # PMC holds both and the next PMx access programs that register


def _increment(mode: int) -> int:
    """The auto-increment a PM mode word asks for: 0, 1, 2, 4, 8, 16, 32 or 128 words."""
    bits = (mode >> 11) & 7
    if not bits:
        return 0
    step = 128 if bits == 7 else 1 << (bits - 1)
    return -step if mode & 0x8000 else step


def _overwrite(old: int, new: int) -> int:
    """Overwrite mode: only the nibbles of ``new`` that are not zero are written."""
    for shift in (12, 8, 4, 0):
        nibble = new & (0xF << shift)
        if nibble:
            old = (old & ~(0xF << shift)) | nibble
    return old


class SVP(Board):
    """Virtua Racing: 2 MB of ROM, 128 KB of DRAM at $300000 and the DSP behind them."""

    DRAM_WORDS: ClassVar[int] = 0x10000

    def start(self) -> None:
        words = self.padded
        self.rom_words = [(words[i] << 8) | words[i + 1] for i in range(0, len(words) - 1, 2)]
        self.dram = [0] * self.DRAM_WORDS
        self.iram = [0] * 0x400
        self.pmac_read = [0] * 5
        self.pmac_write = [0] * 5
        self.pmc = 0  # 32 bits: the address word below, the mode word above
        self.status = 0
        self.xst = 0  # the word both sides read and write
        self.xst2 = 0  # bit 0: the DSP wrote XST, bit 1: the 68000 did
        self.dsp = SSP1601(self.program_read, self.ext_read, self.ext_write)
        self.dsp_cycles = 0
        #: Bumped by anything that changes what the DSP could read from outside: the
        #: 68000's writes and status reads, and a DSP write that changes a word.
        self.version = 0
        self.idle_period = 0  # cycles of the idle loop the DSP is in, or 0
        self.idle_base = 0  # the cycle count at the loop's head
        self.target = 0  # where the DSP is owed to be (the cycles an idle DSP has not run)
        self.next_look = IDLE_CHUNK
        self.seen: dict[tuple, tuple[int, int]] = {}
        #: (write, mode) of every programmable-memory access whose mode the controller
        #: does not decode (MAME logs "ssp FIXME: PMx unhandled ... mode"), for diagnosis.
        self.unmatched: set[tuple[bool, int]] = set()

    # -- the clock ----------------------------------------------------------------

    def catch_up(self, mclk: int) -> None:
        """Run the DSP up to ``mclk`` master clocks from power-on (socket 5).

        **Idle loops are not executed, exactly.** Virtua Racing's DSP spends nearly all of
        its time polling for the 68000 (loops at $400, $425 and $2789), and the DSP can
        observe nothing but its own state and what the 68000 changes -- never the time.
        When the DSP's whole state, the memory controller's, and :attr:`version` come
        round to what they were some cycles ago, it is in a loop that changes nothing and
        stays in it until the 68000 touches the chip. From then on the cycles are only
        counted, and when the 68000 does touch the chip :meth:`wake` runs the DSP from
        the loop's head for the cycles over a whole number of turns, which leaves it where
        it would have been, instruction for instruction."""
        due = mclk * SSP_CLOCK_NUM // SSP_CLOCK_DEN
        if self.idle_period:
            self.target = due
            return
        if self.dsp_cycles < due:
            self.dsp_cycles += self.dsp.run(due - self.dsp_cycles)
            if self.dsp_cycles >= self.next_look:
                self.look()
        self.target = self.dsp_cycles if self.dsp_cycles > due else due

    def look(self) -> None:
        """Has the DSP come back to a state it was in, with nothing outside changed?"""
        self.next_look = self.dsp_cycles + IDLE_CHUNK
        signature = self.signature()
        before = self.seen.get(signature)
        if before is not None and before[1] == self.version:
            self.idle_period = self.dsp_cycles - before[0]
            self.idle_base = self.dsp_cycles
            return
        if len(self.seen) >= IDLE_WINDOW:
            self.seen.clear()
        self.seen[signature] = (self.dsp_cycles, self.version)

    def wake(self) -> None:
        """The 68000 is about to change what the DSP can see: bring an idle DSP up to date."""
        if self.idle_period:
            turns, rest = divmod(self.target - self.idle_base, self.idle_period)
            self.dsp_cycles = self.idle_base + turns * self.idle_period
            self.idle_period = 0
            self.dsp_cycles += self.dsp.run(rest)
            self.seen.clear()
            self.next_look = self.dsp_cycles + IDLE_CHUNK
        self.version += 1

    def signature(self) -> tuple:
        """Everything that decides what the DSP and the memory controller do next."""
        d = self.dsp
        return (
            d.pc, d.x, d.y, d.a, d.st, d.sp, tuple(d.stack), tuple(d.r), tuple(d.ram),
            self.status, self.pmc, self.xst, self.xst2,
            tuple(self.pmac_read), tuple(self.pmac_write),
        )  # fmt: skip

    # -- the 68000's side -----------------------------------------------------------

    def rb(self, address: int) -> int:
        word = self.read_word(address & ~1)
        return word & 0xFF if address & 1 else word >> 8

    def read_word(self, address: int) -> int:
        if address < 0x200000:
            return self.rom_words[address >> 1] if address >> 1 < len(self.rom_words) else 0xFFFF
        if 0x300000 <= address < 0x320000:
            return self.dram[(address - 0x300000) >> 1]
        if 0x390000 <= address < 0x3A0000:
            # "rewritten 68k test code": the cell arrangement the SVP's test reads
            a = (address - 0x390000) >> 1
            a = (a & 0x7001) | ((a & 0x3E) << 6) | ((a & 0xFC0) >> 5)
            return self.dram[a]
        if 0x3A0000 <= address < 0x3B0000:
            a = (address - 0x3A0000) >> 1
            a = (a & 0x7801) | ((a & 0x1E) << 6) | ((a & 0x7E0) >> 4)
            return self.dram[a]
        return 0xFFFF

    def read_byte(self, address: int) -> int:
        return self.rb(address)

    def write_word(self, address: int, value: int) -> None:
        if 0x300000 <= address < 0x320000:
            index = (address - 0x300000) >> 1
            if self.dram[index] != value & 0xFFFF:
                self.wake()
                self.dram[index] = value & 0xFFFF

    def write_byte(self, address: int, value: int) -> None:
        if 0x300000 <= address < 0x320000:
            index = (address - 0x300000) >> 1
            old = self.dram[index]
            lane = (value & 0xFF) if address & 1 else (value & 0xFF) << 8
            new = (old & (0xFF00 if address & 1 else 0x00FF)) | lane
            if new != old:
                self.wake()
                self.dram[index] = new

    def wb(self, address: int, value: int) -> None:
        self.write_byte(address, value)

    def read_a15_word(self, address: int) -> int:
        """$A15000/$A15002: the DSP's XST; $A15004: the status, bit 0 cleared by the read."""
        offset = (address & 0xFF) >> 1
        if offset < 2:
            return self.xst
        if offset == 2:
            value = self.xst2
            if value & 1:
                self.wake()
                self.xst2 &= ~1
            return value
        return 0

    def write_a15(self, address: int, high: int, low: int, word: bool) -> None:
        offset = (address & 0xFF) >> 1
        if offset < 2:
            self.wake()
            if word:
                self.xst = (high << 8) | low
            elif address & 1:
                self.xst = (self.xst & 0xFF00) | low
            else:
                self.xst = (self.xst & 0x00FF) | (high << 8)
            self.xst2 |= 2

    # -- the DSP's program memory -----------------------------------------------------

    def program_read(self, address: int) -> int:
        if address < 0x400:
            return self.iram[address]
        index = address
        return self.rom_words[index] if index < len(self.rom_words) else 0xFFFF

    # -- the DSP's external registers (svp.cpp ``md_svp_ext_map``) -----------------------

    def ext_read(self, n: int) -> int:
        if n == 0:
            result = self.pm_io(0, False, 0)
            if result is not None:
                return result
            value = self.xst2
            self.xst2 &= ~2
            return value
        if n in (1, 2):
            result = self.pm_io(n, False, 0)
            return 0 if result is None else result
        if n == 3:
            result = self.pm_io(3, False, 0)
            return self.xst if result is None else result
        if n == 4:
            return self.pm_io(4, False, 0) or 0
        if n == 6:
            return self.read_pmc()
        if n == 7:
            self.status &= ~(PMC_SET | HAVE_ADDR)
            return 0
        return 0

    def ext_write(self, n: int, value: int) -> None:
        if n == 0:
            if self.pm_io(0, True, value) is None:
                self.xst2 = value
        elif n in (1, 2):
            self.pm_io(n, True, value)
        elif n == 3:
            if self.pm_io(3, True, value) is None:
                self.xst2 |= 1
                self.xst = value
        elif n == 4:
            self.pm_io(4, True, value)
        elif n == 6:
            self.write_pmc(value)

    def store_dram(self, address: int, data: int, mode: int) -> None:
        new = _overwrite(self.dram[address], data) if mode & 0x0400 else data & 0xFFFF
        if new != self.dram[address]:
            self.dram[address] = new
            self.version += 1

    def read_pmc(self) -> int:
        low = self.pmc & 0xFFFF
        if self.status & HAVE_ADDR:
            self.status = (self.status | PMC_SET) & ~HAVE_ADDR
            return ((low << 4) & 0xFFF0) | ((low >> 4) & 0xF)
        self.status |= HAVE_ADDR
        return low

    def write_pmc(self, value: int) -> None:
        if self.status & HAVE_ADDR:
            self.status = (self.status | PMC_SET) & ~HAVE_ADDR
            self.pmc = (self.pmc & 0xFFFF) | (value << 16)
        else:
            self.status |= HAVE_ADDR
            self.pmc = (self.pmc & 0xFFFF0000) | value

    def pm_io(self, reg: int, write: bool, data: int) -> int | None:
        """A programmable-memory access; None when the register is not in that mode.

        Registers 0-3 are programmable only while ST5 or ST6 is set (else they are the
        status words); register 4 always is. The first access after PMC was set
        *programs* the register with it, whatever the access is, and moves no data."""
        if self.status & PMC_SET:
            if write:
                self.pmac_write[reg] = self.pmc
            else:
                self.pmac_read[reg] = self.pmc
            self.status &= ~PMC_SET
            return 0
        self.status &= ~HAVE_ADDR  # "just in case"
        if reg != 4 and not self.dsp.st & 0x60:
            return None
        table = self.pmac_write if write else self.pmac_read
        mode = table[reg] >> 16
        address = table[reg] & 0xFFFF
        if write:
            if (mode & 0x43FF) == 0x0018:  # DRAM
                self.store_dram(address, data, mode)
                table[reg] = (table[reg] + _increment(mode)) & 0xFFFFFFFF
            elif (mode & 0xFBFF) == 0x4018:  # DRAM, "cell" increment: 1, or 31 from an odd word
                self.store_dram(address, data, mode)
                table[reg] = (table[reg] + (31 if address & 1 else 1)) & 0xFFFFFFFF
            elif (mode & 0x47FF) == 0x001C:  # IRAM
                if self.iram[address & 0x3FF] != data & 0xFFFF:
                    self.iram[address & 0x3FF] = data & 0xFFFF
                    self.version += 1
                table[reg] = (table[reg] + _increment(mode)) & 0xFFFFFFFF
            else:
                self.unmatched.add((True, mode))
            data = 0
        elif (mode & 0xFFF0) == 0x0800:  # ROM, one word on
            table[reg] = (table[reg] + 1) & 0xFFFFFFFF
            index = address | ((mode & 0xF) << 16)
            data = self.rom_words[index] if index < len(self.rom_words) else 0xFFFF
        elif (mode & 0x47FF) == 0x0018:  # DRAM
            data = self.dram[address]
            table[reg] = (table[reg] + _increment(mode)) & 0xFFFFFFFF
        else:
            self.unmatched.add((False, mode))
            data = 0
        self.pmc = table[reg]  # "PMC corresponds to the last PMx accessed"
        return data


if SSP1601 is not None:
    board("rom_svp")(SVP)
