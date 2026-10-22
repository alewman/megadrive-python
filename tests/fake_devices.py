"""Test-only devices for the expansion sockets (docs/expansion-sockets.md).

Nothing here models a 32X or a Sega CD: each fake does the least that shows a socket
reaching it -- it answers with values a test can recognise and logs every call, with
the master clock and the bus master the board gave it.
"""

from __future__ import annotations

from megadrive_python.board import Board
from megadrive_python.cartridge import Cartridge
from megadrive_python.sockets import M68K, Reset


class FakeDevice:
    """The calls every socket device answers (``sockets.BusDevice``), logged.

    Its clock runs whenever it is asked to catch up, and every access catches it up to
    the access's own clock before answering, as the add-ons must (socket 4); each log
    entry records the board's step start then, so a test can see the access fell
    *inside* the instruction. ``hold_mclk``: wait states it puts on each access.
    """

    def __init__(self, ranges: list[tuple[int, int]]) -> None:
        self.ranges = ranges
        self.board: Board | None = None
        self.clock = 0
        self.catch_ups: list[int] = []
        self.log: list[tuple] = []
        self.peeks: list[int] = []
        self.dma_starts: list[tuple[int, int]] = []
        self.dma_reads: list[tuple[int, int]] = []
        self.resets: list[tuple[Reset, int]] = []
        self.written: dict[int, int] = {}
        self.hold_mclk = 0

    def attach(self, board: Board) -> None:
        self.board = board

    def claims(self) -> list[tuple[int, int]]:
        return self.ranges

    def catch_up(self, mclk: int) -> None:
        self.catch_ups.append(mclk)
        self.clock = max(self.clock, mclk)

    def _access(self, kind: str, address: int, mclk: int, master: int) -> None:
        self.catch_up(mclk)
        assert self.board is not None
        self.log.append((kind, address, mclk, master, self.board.step_start, self.clock))
        if self.hold_mclk:
            self.board.hold(master, self.hold_mclk)

    def value(self, address: int) -> int:
        """What a read of ``address`` gives: what was written there, or a recognisable
        word made from the address."""
        return self.written.get(address & ~1, (0xA500 | (address >> 1)) & 0xFFFF)

    def read_word(self, address: int, mclk: int) -> int:
        self._access("rw", address, mclk, M68K)
        return self.value(address)

    def write_word(self, address: int, value: int, mclk: int) -> None:
        self._access("ww", address, mclk, M68K)
        self.written[address & ~1] = value

    def read_byte(self, address: int, mclk: int, master: int) -> int:
        self._access("rb", address, mclk, master)
        word = self.value(address)
        return word & 0xFF if address & 1 else word >> 8

    def write_byte(self, address: int, value: int, mclk: int, master: int) -> None:
        self._access("wb", address, mclk, master)
        self.written[address & ~1] = value

    def peek_byte(self, address: int) -> int:
        self.peeks.append(address)
        return 0x5A

    def dma_start(self, mclk: int, bus: int) -> None:
        self.dma_starts.append((mclk, bus))

    def dma_read_word(self, address: int, mclk: int) -> int:
        self.dma_reads.append((address, mclk))
        return self.value(address)

    def reset(self, kind: Reset, mclk: int) -> None:
        self.resets.append((kind, mclk))


class FakeSlotDevice(FakeDevice):
    """In the cartridge slot, holding a cartridge, claiming what the 32X claims that is
    not the cartridge's: its registers at $A15100-$A153FF and $800000-$9FFFFF."""

    def __init__(self, cartridge: Cartridge | None) -> None:
        super().__init__([(0xA15100, 0xA15400), (0x800000, 0xA00000)])
        self.cartridge = cartridge


class FakePortDevice(FakeDevice):
    """On the expansion port, mapped by /CART as the Sega CD is: its "BIOS" at $000000
    with no cartridge, at $400000 with one, and $A12000-$A120FF either way. The BIOS is
    ``bios``, a ROM image, read at the range's own offset."""

    def __init__(self, bios: bytes) -> None:
        super().__init__([])
        self.bios = bios
        self.base = 0

    def attach(self, board: Board) -> None:
        super().attach(board)
        self.base = 0x400000 if board.cartridge_present else 0x000000
        self.ranges = [(self.base, self.base + 0x400000), (0xA12000, 0xA12100)]

    def value(self, address: int) -> int:
        if self.base <= address < self.base + 0x400000 and address & ~1 not in self.written:
            offset = (address - self.base) & ~1
            if offset + 1 < len(self.bios):
                return (self.bios[offset] << 8) | self.bios[offset + 1]
            return 0xFFFF
        return super().value(address)


class FakeAudio:
    """A further sound source at a rate of its own: a constant level, so many samples a
    call, whatever the clock (``sockets.AudioSource``)."""

    def __init__(self, rate: float, gain: float, level: tuple[float, float]) -> None:
        self.rate = rate
        self.gain = gain
        self.level = level
        self.calls: list[int] = []

    def take_samples(self, mclk: int) -> list[tuple[float, float]]:
        self.calls.append(mclk)
        return [self.level] * 600


class FakeVideo:
    """Keeps every line the VDP publishes (``sockets.VideoTiming``)."""

    def __init__(self) -> None:
        self.lines: list = []

    def video_line(self, line) -> None:
        self.lines.append(line)


class FakePicture:
    """Keeps every line handed over and replaces the ones in ``replace`` with a line of
    its own colour, 320 wide (``sockets.Picture``)."""

    def __init__(self, replace: dict[int, int] | None = None) -> None:
        self.lines: list = []
        self.replace = replace or {}

    def picture_line(self, line) -> list[int] | None:
        self.lines.append(line)
        colour = self.replace.get(line.row)
        return None if colour is None else [colour] * 320
