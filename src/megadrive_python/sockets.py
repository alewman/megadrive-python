"""The expansion sockets: what a 32X or a Sega CD board plugs into (docs/expansion-sockets.md).

The add-ons are boards of their own, in their own repositories (``sega32x-python``,
``segacd-python``), which pin this one by git revision. This module is the whole of
the contract between them and this board: nine narrow :class:`typing.Protocol`\\ s,
each of which transcribes to a Rust trait for ``megadrive-rust`` without redesign, and
the plain records two of them hand over. Nothing here does any work; the board
(:mod:`megadrive_python.board`), the system (:mod:`megadrive_python.system`), the VDP,
the renderer and the audio path call these at the points the design names.

**With nothing attached the board is byte-for-byte what it was before the sockets**
(the design's gate: the MAME lockstep, VDPFIFOTesting, the HV captures, the sound
referees, the compatibility sweep and the speed, all rerun). Every hook is either
reached only on a path that had nothing to do before -- open bus, the 32X area's
lock-up, the $A1 page's unused registers -- or guarded by one ``None`` test or one
empty-list test; the 68000's hot paths (cartridge ROM, work RAM, the VDP's ports) are
untouched, because a claim on $000000-$3FFFFF swaps the object that answers there
rather than testing anything per access (``Board._remap``).

The sockets, by the design's numbers:

1. **Cartridge slot** -- :class:`CartridgeSlot` (what :class:`~megadrive_python.cartridge.
   Cartridge` already is), and a :class:`SlotDevice` that *holds* one (the 32X). An
   empty slot is a real state: ``Board.slot_cartridge`` is None and /CART reads high
   (``Board.cartridge_present``).
2. **Expansion port** -- a :class:`PortDevice` (the Sega CD), which reads /CART from the
   board when it is attached and claims $000000-$3FFFFF or $400000-$7FFFFF accordingly,
   plus $A12000-$A120FF. It clears $A10001 bit 5 (/DISK), and the TMSS boot ROM, when
   switched in, sits above it.
3. **One decode for every bus master** -- the 68000, the Z80's bank window (and the
   side-effect-free peek the scheduler's probe uses) and the VDP's 68000-to-VDP DMA all
   reach a device through the same claims (:meth:`BusDevice.claims`). DMA has its own
   entry (:meth:`BusDevice.dma_read_word`) and a start notice with the bus's value then
   (:meth:`BusDevice.dma_start`), for word RAM's one-word-late rule.
4. **Bus timing** -- every device access carries its master clock, and the device may
   hold the master that made it (``Board.hold``). The device can run itself up to that
   clock *inside* the access, before it answers (:meth:`Clocked.catch_up` from its own
   access method): the board never asks it to wait for anything.
5. **Clock domains** -- ``System.clocked``: each is run up to the 68000's clock after
   every 68000 instruction, after the Z80 (:meth:`Clocked.catch_up`).
6. **Video timing** -- a :class:`VideoTiming` gets one :class:`VideoLine` per line as
   the VDP starts it (and again when an H32/H40 change re-anchors it), every edge in
   master clocks.
7. **Picture** -- a :class:`Picture` gets each finished line as a :class:`PictureLine`
   (finished RGB, YS, shadow/highlight) and returns the line that leaves the console.
8. **Sound** -- ``System.audio_sources``: further :class:`AudioSource`\\ s, each at its
   own and changeable rate with its own gain, mixed with the FM and the PSG.
9. **Reset** -- three events, :class:`Reset`: power-on, the reset button, and the
   68000's RESET instruction (:meth:`BusDevice.reset`).

What the design calls for and this does not do is listed in docs/expansion-sockets.md,
"What is built".
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import IntEnum
from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
    from .board import Board

#: Which bus master made a byte access (:meth:`BusDevice.read_byte`). A word access is
#: always the 68000's: the Z80's bus is eight bits wide, and the VDP's DMA has its own
#: entry, :meth:`BusDevice.dma_read_word`.
M68K = 0
Z80 = 1

#: The granularity of a claim: 256 bytes, so that $A12000-$A120FF (the Sega CD's gate
#: array) and $A15100-$A153FF (the 32X's registers) are claims of their own without
#: taking the I/O chip's page, the Z80's bus and reset registers or TMSS's with them.
CLAIM_PAGE = 0x100

#: What a device may claim, as [start, end) 68000 addresses: the cartridge and
#: expansion ranges, the 32X area, and the $A1 page -- less the pages the board's own
#: registers decode (``Board._system_read_word``): the I/O chip ($A10000-$A1001F), the
#: Z80's bus request and reset ($A111xx, $A112xx) and TMSS ($A140xx, $A141xx). Work RAM,
#: the VDP and the Z80's space are not on either socket's bus.
CLAIMABLE = ((0x000000, 0xA00000), (0xA10100, 0xA11100), (0xA11300, 0xA14000),
             (0xA14200, 0xA20000))  # fmt: skip


class Reset(IntEnum):
    """The three reset events (socket 9), which the add-ons treat differently: the Sega
    CD is reset only at power-on (Genesis Plus GX: "FRES is only asserted on Power ON")
    and the reset button interrupts the 32X's SH-2s (VRES; testpico
    ``t_32x_reset_btn``). Whether the 68000's RESET instruction reaches either socket's
    reset line is not established by any source here: it is delivered, and the device
    decides."""

    POWER_ON = 0
    BUTTON = 1
    INSTRUCTION = 2


class CartridgeSlot(Protocol):
    """Socket 1, the cartridge itself: what :class:`~megadrive_python.cartridge.Cartridge`
    and the TMSS boot ROM already are. No master clock: a ROM board needs none, and a
    board that does is a :class:`SlotDevice`.

    ``grounds_cart``: whether the cartridge grounds /CART, which tells the Sega CD the
    slot is in use. A ROM cartridge does; the Sega CD's RAM cartridge does not, and so
    sits at $400000-$7FFFFF with the CD booting (segacd-python ``docs/sockets.md`` 1).
    """

    grounds_cart: bool

    def read_word(self, address: int) -> int: ...

    def read_byte(self, address: int) -> int: ...

    def write_word(self, address: int, value: int) -> None: ...

    def write_byte(self, address: int, value: int) -> None: ...

    def read_time(self, address: int) -> int | None: ...

    def write_time(self, address: int, value: int) -> None: ...


class Clocked(Protocol):
    """Socket 5: a device with its own clock, run up to ``mclk`` (master clocks from
    power-on) after every 68000 instruction. A device in the 68000's clock domain (the
    32X: SH-2 = MCLK x 3/7) converts exactly; one with its own crystal (the Sega CD's
    50 MHz) keeps its own ratio and remainder. It may also be called from inside a bus
    access (socket 4), by the device itself, at the access's clock; when nothing is due
    it should cost a comparison. Overshoot is the device's: it may run whole
    instructions past ``mclk`` (the Sega CD's sub-CPU: up to a DIVS, about 680 MCLK)."""

    def catch_up(self, mclk: int) -> None: ...


class BusDevice(Clocked, Protocol):
    """What both sockets' devices answer (sockets 1-5 and 9).

    ``attach``: the board, once, when the device is plugged in; the device keeps it for
    the open-bus value (``Board.open_bus``), /CART (``Board.cartridge_present``), the
    cartridge it may pass accesses through to (``Board.game_cartridge``), its own wait
    states (``Board.hold``) and for re-reading its claims when they change
    (``Board.update_claims``: the 32X's ADEN, the Sega CD's mode).

    ``claims``: [start, end) ranges of 68000 addresses the device answers, each a
    multiple of :data:`CLAIM_PAGE` and inside :data:`CLAIMABLE`. Read at attach and at
    every ``update_claims``. A range nobody claims behaves as it does without a device:
    the cartridge at $000000-$3FFFFF, open bus at $400000-$7FFFFF, a lock-up at
    $800000-$9FFFFF. Where both sockets claim a page the port wins (TMSS > port >
    slot, the order segacd-python's ``docs/sockets.md`` 2 gives for $000000; for the
    Sega CD with a 32X it is provisional, a question for that milestone).

    The accesses: ``mclk`` is the master clock the access starts at -- the 68000's
    own clock inside its instruction (``Board.m68k_access_clock``), or the Z80's
    (``Board._z80_access_time``) -- so the device can catch itself up to it before it
    answers (socket 4). The cartridge refresh has already stretched the 68000's cycle
    (``Board._bus_cycle``) when the device is called. ``peek_byte`` must have no side
    effects: the scheduler's probe and the Z80's code peeks use it.

    ``dma_start``: a 68000-to-VDP DMA has started at ``mclk``, with ``bus`` the value
    on the 68000's data bus then (its prefetch, ``Board.open_bus``). BlastEm and Genesis
    Plus GX both delay word RAM's words by one read and differ on the first word
    (segacd-python ``docs/sockets.md`` 9); the device latches as it decides. Every
    device is told, whether or not the transfer's source is its own.
    ``dma_read_word``: a word of such a transfer from a claimed page, at the VDP's
    clock for that read.

    ``reset``: one of the three :class:`Reset` events, at master clock ``mclk``.
    """

    def attach(self, board: Board) -> None: ...

    def claims(self) -> Sequence[tuple[int, int]]: ...

    def read_word(self, address: int, mclk: int) -> int: ...

    def write_word(self, address: int, value: int, mclk: int) -> None: ...

    def read_byte(self, address: int, mclk: int, master: int) -> int: ...

    def write_byte(self, address: int, value: int, mclk: int, master: int) -> None: ...

    def peek_byte(self, address: int) -> int: ...

    def dma_start(self, mclk: int, bus: int) -> None: ...

    def dma_read_word(self, address: int, mclk: int) -> int: ...

    def reset(self, kind: Reset, mclk: int) -> None: ...


class SlotDevice(BusDevice, Protocol):
    """Socket 1's device: a board in the cartridge slot that holds a cartridge (the 32X).

    ``cartridge``: the cartridge in *its* slot, or None. The board puts it on the bus
    wherever the device does not claim -- the 32X with ADEN = 0 claims nothing at
    $000000 and the held cartridge answers as if it were in the console's own slot --
    and /CART is its: the 32X answers for its own slot (sega32x-python's handoff brief,
    finding 2).
    """

    cartridge: CartridgeSlot | None


class PortDevice(BusDevice, Protocol):
    """Socket 2's device: a board on the expansion port (the Sega CD). It reads /CART from
    the board at attach (``Board.cartridge_present``) and claims by it: $000000-$3FFFFF
    with no cartridge (its BIOS, PRG RAM window and word RAM), else $400000-$7FFFFF, and
    $A12000-$A120FF either way. A cartridge that does not ground /CART answering at
    $400000-$7FFFFF (the RAM cartridge) is the device's to route: it claims the range
    and passes the access to ``Board.game_cartridge``, so the Sega CD's swap lives in
    its own repository. Hot-plugging is not modelled."""


class VideoLine(NamedTuple):
    """One line of the Mega Drive VDP's timing, every time in master clocks from power-on
    (socket 6). What the 32X takes off the slot -- HSYNC, VSYNC, EDCLK, NTSC/PAL -- and
    needs at master-clock precision for its HBLK/VBLK/PEN bits, its HINT and VINT and its
    register latch (sega32x-python's handoff brief, finding 6c).

    ``start``/``length``: the line, from the V counter's increment point (the board's
    line boundary, ``VDP.line_start``). ``v``: the V counter on it. ``hmode``: "H32" or
    "H40". ``hblank_set``/``hblank_clear``: when the HBlank flag sets ($166 in H40,
    $126 in H32) and clears ($00B, $00A) in this line. ``vblank``: the vertical border
    is on for the line. ``vsync``: the line VSYNC is asserted on. ``pixels``: each
    pixel's start from ``start``, in order of the H counter from the increment point
    (``HMode.starts``): the dot clock, EDCLK's slow pixels around HSYNC included.
    ``published``: when this record was made -- ``start``, or later for a mode change
    that re-anchors the line at its HBlank (``VDP._latch_hmode``).
    """

    start: int
    length: int
    v: int
    hmode: str
    hblank_set: int
    hblank_clear: int
    vblank: bool
    vsync: bool
    interlace: bool
    odd: bool
    pal: bool
    pixels: Sequence[int]
    published: int


class VideoTiming(Protocol):
    """Socket 6: told of each line as the VDP starts it. The VDP is lazy (it runs when
    read, written, or at the start of a 68000 step), so a line is published when the VDP
    reaches it, which may be after the device's own clock; a device that needs every
    line up to a clock first runs the VDP there (``board.vdp.sync(mclk)``)."""

    def video_line(self, line: VideoLine) -> None: ...


class PictureLine(NamedTuple):
    """One finished display line of the Mega Drive's picture, handed to the device before
    it leaves the console (socket 7; sega32x-python's handoff brief, findings 6a-6b).

    ``row``: the picture row (with double interlace, both fields' rows). ``start``: the
    line's start in master clocks (``VDP.line_start``). ``hmode``, ``width`` (256 or
    320) and ``pixel_mclk`` (10 or 8: the active display's dot clock) describe the
    line; ``display_at`` is the master clock the H counter names pixel 0 at, and the
    output runs ``output_delay`` pixels behind it (``render.OUTPUT_DELAY``). ``rgb``:
    the finished colour of each pixel, RGB888 -- the DAC's levels, after
    shadow/highlight, CRAM dots and register 0's blanking, as ``Renderer.frame_rgb``
    gives it. ``ys``: 1 where no plane or sprite pixel shows (the backdrop: YS, per
    TmEE), 0 elsewhere. ``shade``: 0 normal, 1 shadow, 2 highlight.
    """

    row: int
    start: int
    hmode: str
    width: int
    pixel_mclk: int
    display_at: int
    output_delay: int
    rgb: list[int]
    ys: bytes
    shade: bytes


class Picture(Protocol):
    """Socket 7: each finished line in, the line that leaves the console out -- RGB888 of
    any width (the 32X draws its own 320 even in H32), or None for the Mega Drive's
    own."""

    def picture_line(self, line: PictureLine) -> list[int] | None: ...


class AudioSource(Protocol):
    """Socket 8: a further sound source beside the FM and the PSG.

    ``rate``: its sample rate in Hz, which may change between calls (the 32X's PWM
    follows its cycle register). ``gain``: what each sample is multiplied by, in the
    FM's summed-DAC units (full scale about +/-6,100) -- a labelled assumption for
    either add-on, since no recording of either mix exists. ``take_samples``: run up to
    master clock ``mclk`` and return the stereo samples finished since the last call,
    the device's own filters already applied.
    """

    rate: float
    gain: float

    def take_samples(self, mclk: int) -> list[tuple[float, float]]: ...
