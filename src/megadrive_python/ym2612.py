"""The YM2612 as a register stub (milestones 1 and 2; the chip itself is milestone 4).

It keeps what a program writes: the two address latches (part I at port 0, part II
at port 2) and both register banks. Reads return the status byte, which here is
always 0: not busy, no timer overflow. A sound driver that waits for BUSY to clear
therefore never waits, and one that paces itself on the timers' overflow flags runs
at the wrong speed until milestone 4 models them; the MAME lockstep replays the
status reads from MAME's log instead (scripts/lockstep_mame.py).

The /RESET line is shared with the Z80 ("For as long as the Z80 is reset, the YM2612
cannot be used", CMD ``gen-hw.txt`` 2.2).
"""

from __future__ import annotations


class YM2612Stub:
    def __init__(self) -> None:
        self.registers = [bytearray(0x100), bytearray(0x100)]
        self.address = [0, 0]
        self.in_reset = True
        self.writes = 0

    def reset_line(self, held: bool) -> None:
        self.in_reset = held
        if held:
            for bank in self.registers:
                bank[:] = bytes(0x100)
            self.address = [0, 0]

    def read(self, port: int) -> int:
        """The status byte (bit 7 BUSY, bits 1-0 timer overflows): 0 in the stub."""
        return 0

    def write(self, port: int, value: int) -> None:
        if self.in_reset:
            return
        part = (port >> 1) & 1
        if port & 1:
            self.registers[part][self.address[part]] = value
            self.writes += 1
        else:
            self.address[part] = value
