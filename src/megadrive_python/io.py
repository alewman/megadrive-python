"""The I/O chip: the version register and three ports with their registers.

Registers at $A10000-$A1001F, one byte each at the odd address, the even address
reading the same ("Each one can be read at an even address (e.g. A1000Dh ==
A1000Ch) as well", CMD ``gen-hw.txt`` 3.0, checked on hardware):

    $A10001 version   $A10003/5/7 data A/B/C   $A10009/B/D control A/B/C
    $A1000F/15/1B TxData   $A10011/17/1D RxData   $A10013/19/1F S-Ctrl

CMD 3.0 gives the power-on values and the read rules, which this module follows:
data $7F, control $00, TxData $FF, RxData $00, S-Ctrl $00; "Bit 7 of the Data
registers can be read or written. Any bit that is set as an input will return '1'
[with nothing driving it]. Any bit that is set as an output will return the value last
written."; "The RxData register will always return zero"; "Bits 7-4 of the S-Ctrl
registers can be read or written." The serial mode itself is not modelled.

A device on a port sees the output pins and drives the input pins. The 3-button pad
(:class:`Pad3`) multiplexes its buttons on TH (CMD 3.2: ``TH = 0 : ?0SA00DU``,
``TH = 1 : ?1CBRLDU``). The 6-button pad and the timing Terakawa measured are
milestone 7.
"""

from __future__ import annotations

# Button bits of the pad's own word (active high here; the pins are active low).
UP, DOWN, LEFT, RIGHT, B, C, A, START = (1 << i for i in range(8))


class Pad3:
    """A 3-button control pad (CMD ``gen-hw.txt`` 3.2).

    :meth:`pins` returns the six input pins D5-D0 (TR, TL, RIGHT, LEFT, DOWN, UP),
    low for a pressed button, given the level on TH.
    """

    def __init__(self) -> None:
        self.pressed = 0

    def pins(self, th: int) -> int:
        held = self.pressed
        if th:
            # ?1CBRLDU: TR = C, TL = B.
            value = (
                (held & (UP | DOWN | LEFT | RIGHT))
                | (0x10 if held & B else 0)
                | (0x20 if held & C else 0)
            )
        else:
            # ?0SA00DU: TR = A, TL = Start, LEFT and RIGHT read as 0.
            value = (
                (held & (UP | DOWN))
                | 0x0C
                | (0x10 if held & A else 0)
                | (0x20 if held & START else 0)
            )
        return ~value & 0x3F


class Port:
    """One port's data, control and serial registers, and the device on it."""

    def __init__(self, device: Pad3 | None = None) -> None:
        self.device = device
        self.data = 0x00  # the value last written (bit 7 is a latch: CMD 3.0)
        self.data_written = False
        self.control = 0x00  # bits 6-0: 1 = output
        self.tx_data = 0xFF
        self.s_control = 0x00

    def th(self) -> int:
        """The level on TH: driven by the port when it is an output, else pulled up."""
        return (self.data >> 6) & 1 if self.control & 0x40 else 1

    def read_data(self) -> int:
        outputs = self.control & 0x7F
        pins = 0x7F  # undriven inputs read 1
        if self.device is not None:
            pins = (self.th() << 6) | self.device.pins(self.th())
        return (self.data & (outputs | 0x80)) | (pins & ~outputs & 0x7F)


class IOChip:
    """The version register and ports A, B and C (EXT)."""

    def __init__(self, version: int, pads: tuple[Pad3 | None, Pad3 | None] | None = None) -> None:
        self.version = version
        if pads is None:
            pads = (Pad3(), Pad3())
        self.ports = [Port(pads[0]), Port(pads[1]), Port(None)]

    def read(self, address: int) -> int:
        """The byte register at ``address`` ($A10000-$A1001F; even reads as odd)."""
        register = (address >> 1) & 0x0F
        if register == 0:
            return self.version
        if register <= 3:
            return self.ports[register - 1].read_data()
        if register <= 6:
            return self.ports[register - 4].control
        port = self.ports[(register - 7) // 3]
        kind = (register - 7) % 3
        if kind == 0:
            return port.tx_data
        if kind == 1:
            return 0x00  # RxData
        return port.s_control & 0xF0

    def write(self, address: int, value: int) -> None:
        register = (address >> 1) & 0x0F
        value &= 0xFF
        if register == 0:
            return  # the version register is read-only
        if register <= 3:
            port = self.ports[register - 1]
            port.data = value
            port.data_written = True
        elif register <= 6:
            self.ports[register - 4].control = value
        else:
            port = self.ports[(register - 7) // 3]
            kind = (register - 7) % 3
            if kind == 0:
                port.tx_data = value
            elif kind == 2:
                port.s_control = value & 0xF0
