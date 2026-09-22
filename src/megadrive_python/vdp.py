"""The VDP (315-5313 / YM7101): milestone 1's register stub.

It decodes the ports, keeps the registers and the three memories, and counts lines
so that a program waiting for the vertical interrupt runs. Timing, the FIFO and DMA
are milestone 2 (docs/handoff-brief.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .machine import MCLK_PER_LINE, Machine

if TYPE_CHECKING:
    from .board import Board


class VDP:
    def __init__(self, machine: Machine, board: Board) -> None:
        self.machine = machine
        self.board = board
        self.registers = bytearray(0x18)
        self.vram = bytearray(0x10000)
        self.cram = [0] * 64
        self.vsram = [0] * (machine.vdp.vsram_bytes // 2)
        self.code = 0
        self.address = 0
        self.pending = False
        self.lines_per_frame = machine.lines_per_frame
        self.line = 0
        self.line_start = 0  # MCLK at which self.line began
        self.vint_pending = False
        self.hint_pending = False
        self.hint_counter = 0
        self.z80_irq_start = -1
        self.z80_irq_end = -1
        self.frame = 0

    # -- time -------------------------------------------------------------------

    def sync(self, mclk: int) -> None:
        while mclk >= self.line_start + MCLK_PER_LINE:
            self.line_start += MCLK_PER_LINE
            self.line = (self.line + 1) % self.lines_per_frame
            self._line_started()

    def _line_started(self) -> None:
        height = 240 if self.registers[1] & 0x08 else 224
        if self.line <= height:
            if self.hint_counter == 0:
                self.hint_counter = self.registers[10]
                self.hint_pending = True
            else:
                self.hint_counter -= 1
        else:
            self.hint_counter = self.registers[10]
        if self.line == height:
            self.vint_pending = True
            self.z80_irq_start = self.line_start
            self.z80_irq_end = self.line_start + MCLK_PER_LINE
        if self.line == 0:
            self.frame += 1

    def z80_irq_at(self, mclk: int) -> bool:
        return self.z80_irq_start <= mclk < self.z80_irq_end

    @property
    def ipl(self) -> int:
        if self.vint_pending and self.registers[1] & 0x20:
            return 6
        if self.hint_pending and self.registers[0] & 0x10:
            return 4
        return 0

    def acknowledge(self, level: int) -> None:
        if level == 6:
            self.vint_pending = False
        elif level == 4:
            self.hint_pending = False

    # -- ports --------------------------------------------------------------------

    def read_word(self, offset: int, mclk: int) -> int:
        self.sync(mclk)
        if offset < 4:
            return self._read_data()
        if offset < 8:
            self.pending = False
            status = 0x3400 | (0x80 if self.vint_pending else 0)
            if self.line >= 224:
                status |= 0x08
            return status | (self.board.open_bus() & 0xFC00)
        if offset < 0x10:
            return ((self.line & 0xFF) << 8) | (((mclk - self.line_start) // 20) & 0xFF)
        return self.board.open_bus()

    def read_byte(self, offset: int, mclk: int) -> int:
        word = self.read_word(offset & 0x1E, mclk)
        return word & 0xFF if offset & 1 else word >> 8

    def write_word(self, offset: int, value: int, mclk: int) -> int:
        self.sync(mclk)
        if offset < 4:
            self.pending = False
            self._write_data(value)
        elif offset < 8:
            self._write_control(value)
        return 0

    def write_byte(self, offset: int, value: int, mclk: int) -> int:
        if offset >= 0x10:
            return 0  # the PSG (a later milestone)
        return self.write_word(offset & 0x1E, (value << 8) | value, mclk)

    def z80_read(self, offset: int, mclk: int) -> int:
        return self.read_byte(offset, mclk)

    def z80_write(self, offset: int, value: int, mclk: int) -> None:
        self.write_byte(offset, value, mclk)

    def z80_bank_read(self, offset: int, mclk: int) -> int:
        return self.read_byte(offset, mclk)

    # -- internals -------------------------------------------------------------------

    def _write_control(self, value: int) -> None:
        if not self.pending:
            if value & 0xC000 == 0x8000:
                register = (value >> 8) & 0x1F
                if register < 0x18:
                    self.registers[register] = value & 0xFF
                return
            self.code = (self.code & 0x3C) | (value >> 14)
            self.address = (self.address & 0xC000) | (value & 0x3FFF)
            self.pending = True
        else:
            self.pending = False
            self.code = (self.code & 0x03) | ((value >> 2) & 0x3C)
            self.address = (self.address & 0x3FFF) | ((value & 3) << 14)
            if self.code & 0x20 and self.registers[1] & 0x10:
                self._dma()

    def _dma(self) -> None:
        length = (self.registers[20] << 8) | self.registers[19] or 0x10000
        mode = self.registers[23] >> 6
        if mode < 2:
            source = (
                ((self.registers[23] & 0x7F) << 17)
                | (self.registers[22] << 9)
                | (self.registers[21] << 1)
            )
            for _ in range(length):
                word = self.board.read_word(source & 0xFFFFFE)
                self._write_data(word)
                source = (source & 0xFE0000) | ((source + 2) & 0x1FFFE)
        self.code &= ~0x20

    def _write_data(self, value: int) -> None:
        target = self.code & 0x0F
        address = self.address
        if target == 1:
            if address & 1:
                value = ((value << 8) | (value >> 8)) & 0xFFFF
            self.vram[address & 0xFFFE] = value >> 8
            self.vram[(address & 0xFFFE) | 1] = value & 0xFF
        elif target == 3:
            self.cram[(address >> 1) & 0x3F] = value & 0x0EEE
        elif target == 5:
            index = (address >> 1) & 0x3F
            if index < len(self.vsram):
                self.vsram[index] = value & 0x07FF
        self.address = (address + self.registers[15]) & 0xFFFF

    def _read_data(self) -> int:
        self.pending = False
        address = self.address
        target = self.code & 0x0F
        if target == 0:
            value = (self.vram[address & 0xFFFE] << 8) | self.vram[(address & 0xFFFE) | 1]
        elif target == 8:
            value = self.cram[(address >> 1) & 0x3F]
        elif target == 4:
            index = (address >> 1) & 0x3F
            value = self.vsram[index] if index < len(self.vsram) else 0
        else:
            value = 0
        self.address = (address + self.registers[15]) & 0xFFFF
        return value
