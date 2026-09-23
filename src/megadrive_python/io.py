"""The I/O chip: the version register, three ports with their registers, and the pads.

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

A device on a port sees the level on TH and drives the six input pins D5-D0. The
3-button pad (:class:`Pad3`) multiplexes its buttons on TH; the 6-button pad
(:class:`Pad6`) is the same pad with a counter that the *edges* of TH advance, so
every access that can change TH tells the port at what master clock it did
(:meth:`Port.set_th`), and every read says when it read.
"""

from __future__ import annotations

# Button bits of the pad's own word (active high here; the pins are active low).
UP, DOWN, LEFT, RIGHT, B, C, A, START = (1 << i for i in range(8))
#: The 6-button pad's four extra buttons (Terakawa's table, below).
Z, Y, X, MODE = (1 << i for i in range(8, 12))

#: The buttons by name, for tools that take them as text.
BUTTONS = {"up": UP, "down": DOWN, "left": LEFT, "right": RIGHT,
           "b": B, "c": C, "a": A, "start": START,
           "z": Z, "y": Y, "x": X, "mode": MODE}  # fmt: skip


class Pad3:
    """A 3-button control pad (CMD ``gen-hw.txt`` 3.2).

    :meth:`pins` returns the six input pins D5-D0 (TR, TL, RIGHT, LEFT, DOWN, UP),
    low for a pressed button, given the level on TH. Terakawa's table says the same
    thing pin by pin (``reference/web/terakawa-6button.html``, "In Case of
    3-Button-Controller").
    """

    #: What the pad is called in a report.
    kind = "3-button"

    def __init__(self) -> None:
        self.pressed = 0

    def set_th(self, th: int, mclk: int) -> None:
        """TH changed. A 3-button pad is combinational and has nothing to remember."""

    def pins(self, th: int, mclk: int = 0) -> int:
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


#: The three times Ein Terakawa measured on his own 6-button pads
#: (``reference/web/terakawa-6button.html``), in milliseconds, and his warning about
#: them: "It seems that the time is measured by discharging of a capacitor in a
#: 6-Button-Controller. It may mean parameters differ one by one controller. Please
#: choose values you think proper for parameters." So these are **his pads'**
#: figures, not the hardware's.
TERAKAWA_TRIGGER_MS = 1.1  # "If there is two up-edge in 1.1 milli seconds"
TERAKAWA_VALID_MS = 1.6  # "Only Datas read in 1.6 milli seconds ... are reliable"
TERAKAWA_RESTART_MS = 1.8  # "sequence can't be started for 1.8 milli seconds"

#: CMD's own figure for the same capacitor, from the other end: "If TH isn't modified
#: in about 8192 (probably less than that) 68000 CPU cycles, a 'time-out' will occur
#: and the sequence to read 6-button values can be done again" (``gen-hw.txt`` 3.2).
#: 8192 68000 clocks are 57,344 master clocks, 1.07 ms -- the same order as Terakawa's
#: three, and his own words say the number is not one number. This is the board's
#: default because it is the only source that says *from what* the pad times out (the
#: last change of TH), and because it is the shortest of the four, which is the safe
#: end: a game that means to read three buttons and is slow gets three buttons.
CMD_TIMEOUT_M68K_CYCLES = 8192

#: Master clocks a millisecond, to turn Terakawa's figures into the board's own unit.
#: The NTSC master clock; PAL's is 0.9 % slower, which is far inside the spread the
#: sources show, so one number is used for both and said so here.
MCLK_PER_MS = 53_693.175
#: Master clocks a 68000 clock (:mod:`megadrive_python.machine`, kept here so that this
#: module has no import of its own).
MCLK_PER_M68K = 7


class Pad6:
    """A 6-button control pad: a 3-button pad with a counter on TH.

    Two sources describe it and they agree on every value it returns.

    **Ein Terakawa** (``reference/web/terakawa-6button.html``; hardware-corrected, he
    timed real pads) gives the sequence as rows of Sel (TH) and the six pins, "LO"
    a pin held low, "HI" one held high:

        Sel  D0 D1 D2 D3 D4 D5
        Low  UP DW LO LO A  ST
        High UP DW LF RG B  C
        Low  UP DW LO LO A  ST
        High UP DW LF RG B  C   # if there is two up-edge in 1.1 ms,
        Low  LO LO LO LO A  ST  # D0-D3 go Low as Sel goes Low.
        High Z  Y  X  MD HI HI  # make Sel High and read D0-D3.
        Low  HI HI HI HI A  ST  # check that D0-D3 go High as Sel goes Low.
        High UP DW LF RG B  C   # once this sequence take place
        Low  UP DW LO LO A  ST  # sequence can't be started for 1.8 ms.

    **CMD** (``gen-hw.txt`` 3.2, "checked on the real thing") gives the same rows in
    his own notation, and two things Terakawa's table does not say:

        TH = 1 : ?1CBRLDU    3-button pad return value
        TH = 0 : ?0SA00DU    3-button pad return value
        TH = 1 : ?1CBRLDU    3-button pad return value
        TH = 0 : ?0SA0000    D3-0 are forced to '0'
        TH = 1 : ?1CBMXYZ    Extra buttons returned in D3-0
        TH = 0 : ?0SA1111    D3-0 are forced to '1'

    -- that **D4 and D5 still carry B and C in the extra-buttons phase** (Terakawa's
    "HI HI" is those two buttons released), and that "from this point on, the standard
    3-button pad values will be returned if any further TH transitions are done", so
    the counter stops rather than starting round again.

    So: the counter advances on each rising edge of TH; after the second the low phase
    forces D0-D3 low, after the third the high phase carries Mode, X, Y and Z and the
    low phase forces D0-D3 high, and from the fourth on the pad is a 3-button pad
    again until it times out.

    **What is deliberately not claimed: the timeout.** It is a capacitor discharging,
    and Terakawa says in as many words that it "may mean parameters differ one by one
    controller". The four published figures do not even measure the same thing: CMD
    times out 8192 68000 cycles (1.07 ms) after TH was last *modified*; Terakawa gives
    1.1 ms for the second up-edge to arrive, 1.6 ms for the data to stay readable and
    1.8 ms before the sequence may start again, all from the *first* up-edge; MAME
    0.285 uses 1.5 ms from the last rising edge (``bus/sms_ctrl/md6bt.cpp``). The board
    takes CMD's shape and figure as a field, :attr:`timeout_mclk`, and nothing turns on
    the value in ordinary use: a poll takes a few microseconds and a game polls once a
    frame, so every one of the four resets the pad between frames and none of them
    resets it inside one.
    """

    kind = "6-button"

    def __init__(self, *, timeout_mclk: int = CMD_TIMEOUT_M68K_CYCLES * MCLK_PER_M68K,
                 mode_switch: bool = True) -> None:  # fmt: skip
        self.pressed = 0
        #: How long TH may stay still before the counter goes back to the start.
        self.timeout_mclk = int(timeout_mclk)
        #: The pad's own MODE switch: "the MODE button can be held down when powering-up
        #: the console, and the 6-button gamepad will respond like a 3-button one" (CMD
        #: 3.2). False here is that pad. MAME models the same switch (``md6bt.cpp``,
        #: ``m_mode``) and, as it happens, defaults it to the 3-button side.
        self.mode_switch = mode_switch
        #: Rising edges of TH since the sequence began (0 before the first).
        self.edges = 0
        #: When TH last changed, in master clocks.
        self.last_change = 0
        self.th_level = 1  # TH is pulled up until a port drives it

    def _timeout(self, mclk: int) -> None:
        if self.edges and mclk - self.last_change >= self.timeout_mclk:
            self.edges = 0

    def set_th(self, th: int, mclk: int) -> None:
        """TH went to ``th`` at master clock ``mclk``: count its rising edges."""
        th = 1 if th else 0
        self._timeout(mclk)
        if th != self.th_level:
            if th and self.mode_switch and self.edges < 4:
                self.edges += 1
            self.last_change = mclk
        self.th_level = th

    def pins(self, th: int, mclk: int = 0) -> int:
        self._timeout(mclk)
        held = self.pressed
        edges = self.edges
        if th:
            if edges == 3:
                # ?1CBMXYZ: Mode, X, Y and Z on D3-D0, B and C still on D4 and D5.
                value = (
                    (0x01 if held & Z else 0)
                    | (0x02 if held & Y else 0)
                    | (0x04 if held & X else 0)
                    | (0x08 if held & MODE else 0)
                    | (0x10 if held & B else 0)
                    | (0x20 if held & C else 0)
                )
                return ~value & 0x3F
            # ?1CBRLDU, as the 3-button pad.
            value = (
                (held & (UP | DOWN | LEFT | RIGHT))
                | (0x10 if held & B else 0)
                | (0x20 if held & C else 0)
            )
        else:
            # ?0SA00DU, with D0-D3 forced low after the second rising edge and high
            # after the third: the two phases a program uses to recognise the pad.
            directions = held & (UP | DOWN)
            if edges == 2:
                directions, low = 0, 0x0F  # ?0SA0000
            elif edges == 3:
                directions, low = 0, 0x00  # ?0SA1111
            else:
                low = 0x0C
            value = directions | low | (0x10 if held & A else 0) | (0x20 if held & START else 0)
        return ~value & 0x3F


#: Anything that can sit on a control port.
Device = Pad3 | Pad6 | None

#: The pads a caller can ask for by name (``python -m megadrive_python --pad``).
PAD_TYPES = {"3button": Pad3, "6button": Pad6, "none": lambda: None}


class Port:
    """One port's data, control and serial registers, and the device on it."""

    def __init__(self, device: Device = None) -> None:
        self.device = device
        self.data = 0x00  # the value last written (bit 7 is a latch: CMD 3.0)
        self.data_written = False
        self.control = 0x00  # bits 6-0: 1 = output
        self.tx_data = 0xFF
        self.s_control = 0x00

    def th(self) -> int:
        """The level on TH: driven by the port when it is an output, else pulled up."""
        return (self.data >> 6) & 1 if self.control & 0x40 else 1

    def set_th(self, mclk: int) -> None:
        """Tell the device where TH is now, after a write that could have moved it."""
        if self.device is not None:
            self.device.set_th(self.th(), mclk)

    def read_data(self, mclk: int = 0) -> int:
        outputs = self.control & 0x7F
        pins = 0x7F  # undriven inputs read 1
        if self.device is not None:
            pins = (self.th() << 6) | self.device.pins(self.th(), mclk)
        return (self.data & (outputs | 0x80)) | (pins & ~outputs & 0x7F)


class IOChip:
    """The version register and ports A, B and C (EXT)."""

    def __init__(self, version: int,
                 pads: tuple[Device, Device] | None = None) -> None:  # fmt: skip
        self.version = version
        if pads is None:
            pads = (Pad3(), Pad3())
        self.ports = [Port(pads[0]), Port(pads[1]), Port(None)]

    def read(self, address: int, mclk: int = 0) -> int:
        """The byte register at ``address`` ($A10000-$A1001F; even reads as odd)."""
        register = (address >> 1) & 0x0F
        if register == 0:
            return self.version
        if register <= 3:
            return self.ports[register - 1].read_data(mclk)
        if register <= 6:
            return self.ports[register - 4].control
        port = self.ports[(register - 7) // 3]
        kind = (register - 7) % 3
        if kind == 0:
            return port.tx_data
        if kind == 1:
            return 0x00  # RxData
        return port.s_control & 0xF0

    def write(self, address: int, value: int, mclk: int = 0) -> None:
        register = (address >> 1) & 0x0F
        value &= 0xFF
        if register == 0:
            return  # the version register is read-only
        if register <= 3:
            port = self.ports[register - 1]
            port.data = value
            port.data_written = True
            port.set_th(mclk)
        elif register <= 6:
            # A control-register write can make TH an output or let it float back up,
            # which is a TH edge as much as a data write is.
            port = self.ports[register - 4]
            port.control = value
            port.set_th(mclk)
        else:
            port = self.ports[(register - 7) // 3]
            kind = (register - 7) % 3
            if kind == 0:
                port.tx_data = value
            elif kind == 2:
                port.s_control = value & 0xF0
