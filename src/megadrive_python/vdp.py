"""The VDP (315-5313 / YM7101): ports, FIFO, DMA, counters, status and interrupts.

No pixels yet (milestone 3). What this module models, and where each rule comes from:

- **Time.** The VDP runs in master clocks (MCLK). A *line* here starts at the point
  where the V counter increments (internal H counter $10A in H32, $14A in H40), so a
  line has one V value throughout. Inside a line the internal 9-bit H counter steps
  once per pixel: every 10 MCLK in H32, every 8 in H40 except during horizontal sync,
  where the pixel clock alternates MCLK/5 and MCLK/4 (``_H40_SYNC``). Both lines total
  3,420 MCLK; on nine lines around VSYNC H40's second run of long pixels moves
  (``H40_VSYNC``, and in an interlaced odd field one 3,450-MCLK and one 3,390-MCLK
  line), which Nemesis's captures show and no table had. The pixel durations, the H
  and V counter sequences and the event positions are Nemesis's, from his
  logic-analyser work, as tabulated in Exodus (MIT;
  ``Devices/315-5313/S315-5313_Timing.cpp`` lines 1-120 for the horizontal tables,
  120-260 for the vertical ones, 2562-2640 ``GetMclkTicksForOnePixelClockTick``), and
  agree with Kabuto's table ("H/V counter values"). Tier: emulator tables written by
  the author of the hardware captures; the captures themselves
  (``scripts/run_hv_captures.py``) are the judge.
- **The HV counter** reads the internal H counter's upper 8 bits and the V counter
  mapped by interlace mode (Exodus ``S315-5313_Ports.cpp`` ``GetHVCounter``, lines
  1666-1730; CMD ``genvdp.txt``). A port read samples the counters (and the status
  flags) at the first pixel-clock edge at or after HV_SAMPLE_DELAY MCLK into its bus
  cycle; at the sampling phases where the read lands as a counter steps, what it sees
  is the measured console's (``HV_UNITS``). Fitted to Nemesis's captures, which judge
  it (``scripts/run_hv_captures.py``; docs/timing.md).
- **Access slots**, at which the VDP serves the FIFO, the read cache and DMA: 16 (H32)
  or 18 (H40) per active line; on idle lines (inactive, or display off) every second
  pixel except the refresh slots (Exodus ``GetPixelClockTicksUntilNextAccessSlot``,
  ``S315-5313_Timing.cpp`` lines 1431-1645; Kabuto, "Memory access pattern"; MoD,
  forum t=1291).
- **Ports, FIFO, read cache and DMA** follow the model Nemesis built beside
  VDPFIFOTesting (Exodus ``S315-5313_Ports.cpp`` ``ReadInterface``/``WriteInterface``,
  lines 955-1650; ``S315-5313_General.cpp`` ``UpdateInternalState`` and the operations
  after it, lines 1744-3200): a 4-entry FIFO whose entries keep their own code and
  address; a one-word read cache filled at a slot; CD4 set by a data port write and by
  the first half of a control word; CD5 writable only with DMA enabled; the DMA length
  and source registers counting live; fills taking the upper byte (VRAM) or the next
  FIFO entry's word (CRAM, VSRAM); copies byte-wise in VRAM; transfers through the
  FIFO with one read from the 68000's bus every 16 MCLK. VDPFIFOTesting
  (``scripts/run_vdpfifotesting.py``) is the judge.

The VDP catches up lazily: :meth:`sync` brings it to a master clock, doing the slot work
and the line events on the way. After a 68000 port access, :attr:`hold` is the MCLK
the access held the 68000 (a full FIFO, a read waiting for the cache, a DMA transfer).
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable
from typing import TYPE_CHECKING

from .machine import MCLK_PER_LINE, Machine

if TYPE_CHECKING:
    from .board import Board

# -- the horizontal scan -------------------------------------------------------------

#: MCLK per pixel in H40's sync region by internal H counter value (Exodus
#: ``mclkTicksForHCounterH40EDClkArray``); 8 everywhere else.
_H40_SYNC = {
    **dict.fromkeys(range(0x1CD, 0x1D4), 10), 0x1D4: 9, 0x1D5: 9,
    **dict.fromkeys(range(0x1D6, 0x1DD), 10), 0x1DD: 8,
    **dict.fromkeys(range(0x1DE, 0x1E5), 10), 0x1E5: 9, 0x1E6: 9,
    **dict.fromkeys(range(0x1E7, 0x1EE), 10),
}  # fmt: skip


def hv_word(v: int, h: int, interlace: bool, double: bool) -> int:
    """The HV counter as the port gives it, from the internal V and H counters: H's
    upper eight bits; V's lower eight, or in interlace its bits 7-1 and 8 (doubled:
    6-0 and 7) (Exodus ``S315-5313_Ports.cpp`` 1666-1730; CMD ``genvdp.txt``)."""
    v = v << 1 if double else v
    external = v & 0xFF
    if interlace:
        external = (external & 0xFE) | ((v >> 8) & 1)
    return (external << 8) | (h >> 1)


class HVUnit:
    """What one measured console's HV counter reads show at the two sampling phases
    where a read catches a counter changing (Nemesis's captures;
    ``scripts/run_hv_captures.py``; docs/timing.md, "HV counter sampling").

    The port samples the counters at the first pixel-clock edge at or after
    HV_SAMPLE_DELAY MCLK into the bus cycle. At the *edge* phase the pixel before that
    edge began exactly one MCLK before, so the read lands as the counter steps; at the
    *next* phase the sampled pixel began exactly at the sampling point, so the read
    lands as the counter starts its next step. What the read sees depends on the
    pixel clock, by *regime*: "H32" (10-MCLK pixels), "H40" (8-MCLK pixels) and
    "H40 long" (the 9- and 10-MCLK pixels of H40's sync). Rules: "new"; "old"; an
    integer n: old AND new when the increment changes bit n or above (it carries that
    far: the bits that clear have cleared and the one that sets has not), else new.
    ``jumps`` gives the value read at the edge phase as the H counter jumps between its
    runs. ``v_carry1`` is what the V counter shows at the edge phase when its increment
    carries into bit 1 (old, old OR new, or new). ``next_clear`` (per regime) is how
    many low bits of the H counter read clear at the next phase when the coming
    increment carries past them. ``late`` (per regime) is the edge rule one MCLK later
    (the pixel before the sampled edge began two MCLK before the sampling point),
    where a unit still shows the step under way. Where a reading varies between reads at the same
    phase the majority is taken; the rest are counted as such by the capture script.
    """

    def __init__(self, name: str, edge: dict[str, str | int], jumps: dict[str, dict],
                 v_carry1: dict[str, str], next_clear: dict[str, int],
                 late: dict[str, int] | None = None) -> None:  # fmt: skip
        self.name = name
        self.late = late or {}
        self.edge = edge
        self.jumps = jumps
        self.v_carry1 = v_carry1
        self.next_clear = next_clear

    def h_at_edge(self, regime: str, old: int, new: int) -> int:
        if new != old + 1:
            return self.jumps.get(regime, {}).get((old, new), new)
        rule = self.edge[regime]
        if rule == "old":
            return old
        if rule == "new" or not (old ^ new) >> rule:
            return new
        return old & new

    def h_late(self, regime: str, old: int, new: int) -> int:
        rule = self.late.get(regime, "new")
        if rule == "new" or new != old + 1 or not (old ^ new) >> rule:
            return new
        return old & new

    def v_at_edge(self, regime: str, old: int, new: int) -> int:
        if new == old + 1 and old & 3 == 1:
            rule = self.v_carry1.get(regime, "new")
            if rule == "old":
                return old
            if rule == "or":
                return old | new
        return new

    def h_at_next(self, regime: str, h: int, following: int) -> int:
        bits = self.next_clear.get(regime, 0)
        if bits and following == h + 1 and (h ^ following) >> bits:
            return h & ~((1 << bits) - 1)
        return h


HV_UNITS = {
    # PAL Model 1: H32 edge reads: 30,033 of 30,033 fit the rule (in the HV noint PAL
    # H32 captures); the jump reads $E1 54 times in 67 ($F1 otherwise); the V counter
    # carrying into bit 1 shows old 13 times in 22 (old OR new otherwise). H40: the
    # 8-MCLK pixels read cleanly; the long ones show the old value.
    "pal-model1": HVUnit(
        "Nemesis's PAL Model 1",
        edge={"H32": 2, "H40": "new", "H40 long": "old"},
        jumps={"H32": {(0x127, 0x1D2): 0x1C2}},
        v_carry1={"H32": "old"},
        next_clear={},
    ),
    # NTSC-J Model 1: every rule fits all but a few percent of its reads (the
    # capture script counts them).
    "ntscj-model1": HVUnit(
        "Nemesis's NTSC-J Model 1",
        edge={"H32": 3, "H40": 2, "H40 long": "old"},
        jumps={"H32": {(0x127, 0x1D2): 0x1C2}, "H40": {(0x16C, 0x1C9): 0x148}},
        v_carry1={"H32": "or"},
        next_clear={"H32": 3},
        late={"H40 long": 2},
    ),
}


class HMode:
    """One horizontal mode's line: the H counter sequence and when each value starts.

    ``hvals[i]`` is the internal H counter during pixel ``i`` of the line, the line
    starting at the V counter increment point; ``starts[i]`` is that pixel's first MCLK
    from the line start. ``slots_active``/``slots_idle`` are the access slots' offsets
    on an active line and on an idle one.
    """

    def __init__(self, name: str, last: int, first_high: int, vinc: int, hblank_set: int,
                 hblank_clear: int, pixel_mclk: Callable[[int], int]) -> None:  # fmt: skip
        self.name = name
        sequence = list(range(0, last + 1)) + list(range(first_high, 0x200))
        start = sequence.index(vinc)
        self.hvals = sequence[start:] + sequence[:start]
        self.starts: list[int] = []
        total = 0
        for h in self.hvals:
            self.starts.append(total)
            total += pixel_mclk(h)
        #: MCLK in the line: MCLK_PER_LINE but on some of H40's VSYNC lines in an
        #: interlaced odd field (H40_LONG, H40_SHORT).
        self.length = total
        self.offset_of = dict(zip(self.hvals, self.starts, strict=True))
        self.vinc = vinc
        self.hblank_set = hblank_set
        self.hblank_clear = hblank_clear
        self.last = last
        self.first_high = first_high
        self.slots_active: list[int] = []
        self.slots_idle: list[int] = []

    def position(self, offset: int) -> int:
        """The index of the pixel under way ``offset`` MCLK into the line."""
        return bisect_right(self.starts, offset) - 1

    def next_edge(self, offset: int) -> int:
        """The first pixel boundary at or after ``offset`` MCLK into the line (the line's
        end when there is none left in it)."""
        index = bisect_left(self.starts, offset)
        return self.starts[index] if index < len(self.starts) else self.length


H32 = HMode("H32", 0x127, 0x1D2, 0x10A, 0x126, 0x00A, lambda h: 10)
H40 = HMode("H40", 0x16C, 0x1C9, 0x14A, 0x166, 0x00B, lambda h: _H40_SYNC.get(h, 8))

#: Around VSYNC, H40's second run of long pixels ($1DE-$1ED) moves: on nine lines those
#: pixels last 8 MCLK and $09F-$0AE take their lengths instead (Nemesis's captures;
#: docs/timing.md, "H40 VSYNC lines"). Both sets of nine lines end on line ``jump`` + 5
#: (PAL V28 $100-$102 and $1CA-$1CF; PAL V30 $108-$10A, $1D2-$1D7; NTSC V28 $0E8-$0EA,
#: $1E5-$1EA), except that in an interlaced odd field the lines with the long
#: $09F-$0AE end one line earlier: the first of those lines is 30 MCLK long and the
#: last short one 30 MCLK short. The H sequence is the same; only when values start.
_H40_LONG_9F = {0x09F + i: _H40_SYNC[h] for i, h in enumerate(range(0x1DE, 0x1EE))}


def _h40_pixels(short_1de: bool, long_9f: bool) -> Callable[[int], int]:
    def pixel_mclk(h: int) -> int:
        if long_9f and h in _H40_LONG_9F:
            return _H40_LONG_9F[h]
        if short_1de and 0x1DE <= h <= 0x1ED:
            return 8
        return _H40_SYNC.get(h, 8)

    return pixel_mclk


def _h40(short_1de: bool, long_9f: bool) -> HMode:
    return HMode("H40", 0x16C, 0x1C9, 0x14A, 0x166, 0x00B, _h40_pixels(short_1de, long_9f))


H40_VSYNC = _h40(True, True)
H40_LONG = _h40(False, True)  # 3,450 MCLK
H40_SHORT = _h40(True, False)  # 3,390 MCLK
_H40_LINES = {(False, False): H40, (True, True): H40_VSYNC, (False, True): H40_LONG,
              (True, False): H40_SHORT}  # fmt: skip
VSYNC_LINES = 9

_ACTIVE_SLOTS = {
    "H32": (0x005, 0x015, 0x025, 0x045, 0x055, 0x065, 0x085, 0x095, 0x0A5, 0x0C5, 0x0D5,
            0x0E5, 0x103, 0x105, 0x121, 0x1E7),
    "H40": (0x002, 0x012, 0x022, 0x042, 0x052, 0x062, 0x082, 0x092, 0x0A2, 0x0C2, 0x0D2,
            0x0E2, 0x102, 0x112, 0x122, 0x140, 0x142, 0x1CE),
}  # fmt: skip
#: The refresh slots of an idle line: from the first H counter value of each block the
#: next slot is six pixels on (Exodus Timing.cpp 1463-1520).
_REFRESH = {"H32": (0x035, 0x075, 0x0B5, 0x0F5, 0x1F5),
            "H40": (0x030, 0x070, 0x0B0, 0x0F0, 0x130, 0x1F0)}  # fmt: skip


def _idle_slots(mode: HMode) -> list[int]:
    """Slots on an idle line: every odd (H32) or even (H40) H counter value, with the
    two slots after each refresh block's start skipped."""
    parity = 1 if mode.name == "H32" else 0
    skipped = set()
    for lo in _REFRESH[mode.name]:
        skipped.update(h for h in range(lo + 1, lo + 6) if h % 2 == parity)
    return sorted(mode.offset_of[h] for h in mode.hvals if h % 2 == parity and h not in skipped)


for _mode in (H32, H40, H40_VSYNC, H40_LONG, H40_SHORT):
    _mode.slots_active = sorted(_mode.offset_of[h] for h in _ACTIVE_SLOTS[_mode.name])
    _mode.slots_idle = _idle_slots(_mode)


# -- the vertical scan -----------------------------------------------------------------


class VMode:
    """The V counter sequence: 0..``last``, then ``jump``..$1FF; on an odd interlaced
    field one extra line ``jump - 1`` (Exodus vertical tables, ``S315-5313_Timing.cpp``
    lines 120-260). VBlank sets and VINT fires on line ``vblank_set``."""

    def __init__(self, last: int, jump: int, vblank_set: int, vsync: int | None,
                 interlaced_last: int | None = None) -> None:  # fmt: skip
        self.vsync = vsync  # the line VSYNC is asserted on (Exodus Timing.cpp 150-240)
        self._vsync_cache: dict = {}
        self.last = last
        self.jump = jump
        self.vblank_set = vblank_set
        self.active_last = vblank_set - 1
        self.interlaced_last = interlaced_last if interlaced_last is not None else last

    def vsync_lines(self, interlace: bool, odd: bool) -> tuple[frozenset, frozenset]:
        """The lines of H40's moved long pixels in a field: those whose $1DE-$1ED are
        short, and those whose $09F-$0AE are long (``_H40_LONG_9F``)."""
        key = (interlace, odd)
        if key not in self._vsync_cache:
            if self.vsync is None:
                self._vsync_cache[key] = (frozenset(), frozenset())
            else:
                sequence = []
                v = self.vblank_set
                while len(sequence) < 400 and v != self.jump + 6:
                    sequence.append(v)
                    v = self.next(v, interlace, odd)
                short = frozenset(sequence[-VSYNC_LINES:])
                long_end = -2 if interlace and odd else -1
                long = frozenset(
                    sequence[
                        len(sequence) + long_end + 1 - VSYNC_LINES : len(sequence) + long_end + 1
                    ]
                )
                self._vsync_cache[key] = (short, long)
        return self._vsync_cache[key]

    def h40_line(self, v: int, interlace: bool, odd: bool) -> HMode:
        """The H40 line table of line ``v`` in a field."""
        short, long = self.vsync_lines(interlace, odd)
        return _H40_LINES[(v in short, v in long)]

    def next(self, v: int, interlace: bool, odd: bool) -> int:
        if v == (self.interlaced_last if interlace else self.last):
            if self.jump > 0x1FF:
                return 0
            return self.jump - 1 if interlace and odd else self.jump
        return 0 if v == 0x1FF else v + 1


V28_NTSC = VMode(0x0EA, 0x1E5, 0x0E0, 0x0E8)
V30_NTSC = VMode(0x1FF, 0x200, 0x0F0, None)
V28_PAL = VMode(0x102, 0x1CA, 0x0E0, 0x100, interlaced_last=0x101)
V30_PAL = VMode(0x10A, 0x1D2, 0x0F0, 0x108, interlaced_last=0x109)

#: Write targets (code bits 3-0) and read targets (bits 4-0).
WRITE_VRAM, WRITE_CRAM, WRITE_VSRAM = 0x01, 0x03, 0x05
READ_VRAM, READ_VSRAM, READ_CRAM, READ_VRAM8 = 0x00, 0x04, 0x08, 0x0C

#: MCLK a DMA transfer takes to read one word from the 68000's bus (Exodus
#: ``dmaTransferReadTimeInMclkCycles``, ``S315_5313.h`` line 919).
DMA_READ_MCLK = 16
#: A read of the HV counter (or the status register) sees the counters as they stand at
#: the first pixel-clock edge at or after this many MCLK into the 68000's bus cycle.
#: Fitted to Nemesis's logic-analyser captures (docs/timing.md, "HV counter
#: sampling"): no document states it.
HV_SAMPLE_DELAY = 3


class VDP:
    def __init__(self, machine: Machine, board: Board) -> None:
        self.machine = machine
        self.board = board
        self.pal = machine.pal
        self.registers = bytearray(0x20)
        self.vram = bytearray(0x10000)  # byte-swapped within each word, as the chip
        self.cram = [0] * 64
        self.vsram_words = machine.vdp.vsram_bytes // 2
        self.hv_unit = HV_UNITS[machine.hv_reads]
        self.vsram = [0] * 64
        self.vsram_last_render = 0  # the renderer's last VSRAM read (for $50-$7F)
        # Command state (Exodus _commandCode, _commandAddress, _originalCommandAddress).
        self.code = 0
        self.address = 0
        self.original_address = 0
        self.pending = False  # the second control word is awaited
        self.modified = True  # code/address rewritten since the last FIFO entry
        # The FIFO: [code, address, data, half written, pending] x 4.
        self.fifo = [[0, 0, 0, False, False] for _ in range(4)]
        self.fifo_read = 0
        self.fifo_write = 0
        self.read_buffer = 0
        self.read_available = False
        self.read_half = False
        # DMA.
        self.fill_running = False
        self.transfer_active = False
        self.transfer_cached = False
        self.transfer_value = 0
        self.transfer_next_read = 0
        # Interrupts and flags.
        self.vint_pending = False
        self.hint_pending = False
        self.exint_pending = False
        self.f_flag = False
        self.odd = False
        self.hint_counter = 0
        self.hv_latched = 0
        self.frame = 0
        self.z80_irq_start = -1
        self.z80_irq_end = -1
        #: MCLK the last 68000 port access held the 68000.
        self.hold = 0
        # Timing: the latched modes, the current line and how far the VDP has run.
        self.hmode = H32
        self.vmode = V28_PAL if self.pal else V28_NTSC
        self.interlace = False
        self.interlace_double = False
        self.v = 0x1FF
        self.previous_v = 0x1FE
        self.line_start = 0
        self.mclk = 0
        self.slots = self.hmode.slots_idle
        self.slot_index = 0
        self._vint_done = False
        self._odd_done = False
        self._line_events()

    # -- registers ----------------------------------------------------------------------

    @property
    def display_enabled(self) -> bool:
        return bool(self.registers[1] & 0x40)

    @property
    def mode5(self) -> bool:
        return bool(self.registers[1] & 0x04)

    @property
    def dma_enabled(self) -> bool:
        return bool(self.registers[1] & 0x10)

    @property
    def dmd(self) -> int:
        return self.registers[23] >> 6

    def _write_register(self, number: int, value: int) -> None:
        old = self.registers[number]
        self.registers[number] = value
        if number == 0 and (value ^ old) & 0x02 and value & 0x02:
            # 0 -> 1 on the HV latch bit latches the counter (Exodus Ports.cpp 1858-1871).
            self.hv_latched = self._hv_now()
        elif number == 1 and (value ^ old) & 0x40:
            self._choose_slots()

    # -- time ------------------------------------------------------------------------------

    def _choose_slots(self) -> None:
        """The slot list for the current line, and where in it the VDP is."""
        active = self.display_enabled and (self.v <= self.vmode.active_last or self.v == 0x1FF)
        self.slots = self.hmode.slots_active if active else self.hmode.slots_idle
        self.slot_index = bisect_right(self.slots, self.mclk - self.line_start - 1)

    def _line_events(self) -> None:
        mode = self.hmode
        start = self.line_start
        self.line_end = start + mode.length
        self.hblank_at = start + mode.offset_of[mode.hblank_set]
        vint_line = self.v == self.vmode.vblank_set
        self.vint_at = start + mode.offset_of[0x001] if vint_line else -1
        self.odd_at = start + mode.offset_of[0x002] if vint_line else -1
        self._choose_slots()

    def _work_pending(self) -> bool:
        cd5 = self.code & 0x20
        dmd = self.registers[23] >> 6
        return bool(
            self.fifo[self.fifo_read][4]
            or (cd5 and (dmd < 2 or dmd == 3 or self.fill_running))
            or ((self.code & 0x13) == 0 and not self.read_available)
        )

    def sync(self, mclk: int) -> None:
        """Bring the VDP to ``mclk``: slot work, then line events on the way."""
        if mclk > self.mclk:
            self._advance(mclk, None)

    def _advance(self, target: int, until: Callable[[], bool] | None) -> None:
        """Run to ``target``; with ``until``, keep going past it slot by slot until the
        predicate holds (a port access held for the VDP)."""
        if until is not None and until():
            if self.mclk < target:
                self._advance(target, None)
            return
        while True:
            next_event = self.line_end
            kind = 0  # 0 line, 1 hblank, 2 vint, 3 odd, 4 slot
            if self.mclk <= self.hblank_at < next_event:
                next_event, kind = self.hblank_at, 1
            if not self._vint_done and self.mclk <= self.vint_at < next_event:
                next_event, kind = self.vint_at, 2
            if not self._odd_done and self.mclk <= self.odd_at < next_event:
                next_event, kind = self.odd_at, 3
            if self._work_pending() and self.slot_index < len(self.slots):
                slot_at = self.line_start + self.slots[self.slot_index]
                if slot_at < self.mclk:
                    self.slot_index = bisect_right(self.slots, self.mclk - self.line_start - 1)
                    continue
                if slot_at <= next_event:
                    next_event, kind = slot_at, 4
            if until is None and next_event > target:
                self.mclk = max(self.mclk, target)
                return
            self.mclk = next_event
            if kind == 4:
                self.slot_index += 1
                self._slot()
                if until is not None and until():
                    if self.mclk < target:
                        self._advance(target, None)
                    return
            elif kind == 0:
                self._next_line()
            elif kind == 1:
                self.hblank_at = -1
                self._latch_hmode()
            elif kind == 2:
                self._vint_done = True
                self.vint_pending = True
                self.f_flag = True
                self.z80_irq_start = self.mclk
                self.z80_irq_end = self.mclk + MCLK_PER_LINE
            else:
                self._odd_done = True
                if self.interlace:
                    self.odd = not self.odd

    def _next_line(self) -> None:
        vmode = self.vmode
        new_v = vmode.next(self.v, self.interlace, self.odd)
        self.previous_v = self.v
        if new_v == vmode.vblank_set:
            self._latch_vmode()
            vmode = self.vmode
        self.v = new_v
        if new_v == 0:
            self.frame += 1
        if self.hmode.name == "H40":
            self.hmode = vmode.h40_line(new_v, self.interlace, self.odd)
        # The HINT counter at the V increment point (Exodus General.cpp 2268-2289).
        if new_v > vmode.vblank_set:
            self.hint_counter = self.registers[10]
        elif self.hint_counter == 0:
            self.hint_counter = self.registers[10]
            self.hint_pending = True
        else:
            self.hint_counter -= 1
        self.line_start = self.line_end
        self._vint_done = False
        self._odd_done = False
        self._line_events()

    def _latch_hmode(self) -> None:
        """RS0/RS1 changes take effect at HBlank (Exodus General.cpp 1917-1935): the H
        counter keeps its value and counts on in the new mode's sequence."""
        new = H32
        if self.registers[12] & 0x81 == 0x81:
            new = self.vmode.h40_line(self.v, self.interlace, self.odd)
        if new.name == self.hmode.name:
            return
        h = self.hmode.hblank_set
        self.hmode = new
        self.line_start = self.mclk - new.offset_of.get(h, new.offset_of[new.hblank_set])
        self._line_events()
        self.hblank_at = -1

    def _latch_vmode(self) -> None:
        """V28/V30 and interlace changes latch at VBlank (Exodus General.cpp 2203-2215)."""
        v30 = bool(self.registers[1] & 0x08)
        if self.pal:
            self.vmode = V30_PAL if v30 else V28_PAL
        else:
            self.vmode = V30_NTSC if v30 else V28_NTSC
        lsm = (self.registers[12] >> 1) & 3
        self.interlace = bool(lsm & 1)
        self.interlace_double = lsm == 3

    # -- the counters and the status register ------------------------------------------------

    def hcounter(self) -> int:
        """The internal 9-bit H counter at the VDP's current time."""
        return self.hmode.hvals[self.hmode.position(self.mclk - self.line_start)]

    def _hv_now(self) -> int:
        return self._hv_word(self.v, self.hcounter())

    def _hv_word(self, v: int, h: int) -> int:
        return hv_word(v, h, self.interlace, self.interlace_double)

    def sample_point(self, mclk: int) -> str:
        """Bring the VDP to where a port read at ``mclk`` samples its counters: the first
        pixel-clock edge at or after ``mclk`` + HV_SAMPLE_DELAY. Returns the phase
        (``HVUnit``): "edge" when the pixel before that edge began one MCLK before
        ``mclk`` + HV_SAMPLE_DELAY, "late" when two, "next" when the sampled pixel
        begins exactly at it, else ""."""
        self.sync(mclk + HV_SAMPLE_DELAY)
        offset = self.mclk - self.line_start
        edge = self.hmode.next_edge(offset)
        self.sync(self.line_start + edge)
        if edge == offset:
            return "next"
        for back, phase in ((1, "edge"), (2, "late")):
            before = offset - back
            if before >= 0 and self.hmode.next_edge(before) == before:
                return phase
        return ""

    def _regime(self, index: int) -> str:
        """The pixel clock regime of pixel ``index`` of the line (``HVUnit``)."""
        mode = self.hmode
        if mode.name == "H32":
            return "H32"
        end = mode.starts[index + 1] if index + 1 < len(mode.starts) else mode.length
        return "H40" if end - mode.starts[index] == 8 else "H40 long"

    def read_hv(self, mclk: int) -> int:
        """The HV counter, as the machine's measured unit (``HV_UNITS``) reads it at the
        phase ``sample_point`` finds."""
        phase = self.sample_point(mclk)
        if self.registers[0] & 0x02:
            return self.hv_latched
        mode = self.hmode
        index = mode.position(self.mclk - self.line_start)
        h, v = mode.hvals[index], self.v
        if phase == "edge":
            regime = self._regime(index - 1 if index else len(mode.hvals) - 1)
            old_v = self.previous_v if index == 0 else v
            h = self.hv_unit.h_at_edge(regime, mode.hvals[index - 1], h)
            v = self.hv_unit.v_at_edge(regime, old_v, v)
        elif phase == "late":
            regime = self._regime(index - 1 if index else len(mode.hvals) - 1)
            h = self.hv_unit.h_late(regime, mode.hvals[index - 1], h)
        elif phase == "next":
            following = mode.hvals[(index + 1) % len(mode.hvals)]
            h = self.hv_unit.h_at_next(self._regime(index), h, following)
        return self._hv_word(v, h)

    def status(self) -> int:
        """The 10-bit status register (the Manual; Exodus's status bits)."""
        h = self.hcounter()
        v = self.v
        vblank = (self.vmode.vblank_set <= v < 0x1FF) or not self.display_enabled
        hblank = h >= self.hmode.hblank_set or h < self.hmode.hblank_clear
        return (
            (0 if self.fifo[self.fifo_read][4] else 0x200)
            | (0x100 if self._fifo_full() else 0)
            | (0x80 if self.f_flag else 0)
            | (0x10 if self.odd and self.interlace else 0)
            | (0x08 if vblank else 0)
            | (0x04 if hblank else 0)
            | (0x02 if self.code & 0x20 else 0)
            | (0x01 if self.pal else 0)
        )

    # -- interrupts ----------------------------------------------------------------------------

    @property
    def ipl(self) -> int:
        """VINT 6, HINT 4, EXINT 2: the highest enabled and pending (Exodus
        ``GetNewIPLLineState``, Ports.cpp 428-455)."""
        if self.vint_pending and self.registers[1] & 0x20:
            return 6
        if self.hint_pending and self.registers[0] & 0x10:
            return 4
        if self.exint_pending and self.registers[11] & 0x08:
            return 2
        return 0

    def acknowledge(self, level: int) -> None:
        """The acknowledge clears the request it answers, and the F flag with VINT's
        (Exodus Ports.cpp 118-133)."""
        if self.vint_pending and self.registers[1] & 0x20:
            self.vint_pending = False
            self.f_flag = False
        elif self.hint_pending and self.registers[0] & 0x10:
            self.hint_pending = False
        elif self.exint_pending and self.registers[11] & 0x08:
            self.exint_pending = False

    def z80_irq_at(self, mclk: int) -> bool:
        """The Z80's INT, from the VINT point for one line: CMD (gen-hw.txt 2.4) gives
        the line; the length is an open question (docs/undocumented-behavior.md)."""
        return self.z80_irq_start <= mclk < self.z80_irq_end

    # -- the FIFO and the work at a slot -----------------------------------------------------

    def _fifo_full(self) -> bool:
        fifo = self.fifo
        return fifo[self.fifo_read][4] and fifo[(self.fifo_read + 3) & 3][4]

    def _slot(self) -> None:
        """One access slot (Exodus ``UpdateInternalState``, General.cpp 1773-1878)."""
        dmd = self.registers[23] >> 6
        fifo = self.fifo
        while (
            self.code & 0x20 and dmd < 2 and self.transfer_active
            and (not self.transfer_cached or not self._fifo_full())
            and self.transfer_next_read + DMA_READ_MCLK <= self.mclk
        ):  # fmt: skip
            if not self.transfer_cached:
                self._transfer_read()
            self.transfer_next_read += DMA_READ_MCLK
            if not self._fifo_full():
                self._transfer_push()
                self._dma_step()
        if self.code & 0x20 and dmd == 2 and self.fill_running and not fifo[self.fifo_read][4]:
            self._fill()
            self._dma_step()
        if self.code & 0x20 and dmd == 3:
            self._copy()
            self._dma_step()
        did_read = False
        if not fifo[self.fifo_read][4] and not self.read_available and (self.code & 0x13) == 0:
            self._read_cache()
            did_read = True
        if fifo[self.fifo_read][4] and not did_read:
            self._fifo_write()
        if self.code & 0x20 and dmd < 2 and self.transfer_cached and not self._fifo_full():
            self._transfer_push()
            self._dma_step()

    def _vram_write_byte(self, address: int, value: int) -> None:
        # VRAM holds each word byte-swapped, so a byte access reaches the other byte of
        # the word (Exodus M5WriteVRAM8Bit, General.cpp 3104-3140). Stored here
        # un-swapped: the XOR puts the byte where a word read expects it.
        self.vram[(address ^ 1) & 0xFFFF] = value & 0xFF

    def _fifo_write(self) -> None:
        """Commit the oldest FIFO entry, a byte at a time for VRAM (Exodus
        ``PerformFIFOWriteOperation``, General.cpp 2479-2627)."""
        entry = self.fifo[self.fifo_read]
        code, address, data, half = entry[0], entry[1], entry[2], entry[3]
        target = code & 0x0F
        if target == WRITE_VRAM:
            byte = (data >> 8) if ((address & 1) ^ half) else data & 0xFF
            self._vram_write_byte((address & 0xFFFE) | half, byte)
            entry[3] = not half
            entry[4] = entry[3]
        elif target == WRITE_CRAM:
            self.cram[(address & 0x7E) >> 1] = data & 0x0EEE
            entry[4] = False
        elif target == WRITE_VSRAM:
            index = (address & 0x7E) >> 1
            if index < self.vsram_words:
                self.vsram[index] = data & 0x07FF
            entry[4] = False
        else:
            entry[4] = False
        if not entry[4]:
            self.fifo_read = (self.fifo_read + 1) & 3
            dmd = self.registers[23] >> 6
            if self.code & 0x20 and dmd == 2:
                self.fill_running = True
            if self.code & 0x20 and dmd < 2 and self.transfer_cached:
                self.transfer_next_read = self.mclk

    def _read_cache(self) -> None:
        """Fill the read buffer (Exodus ``PerformReadCacheOperation``, General.cpp
        2345-2476): bits CRAM and VSRAM do not drive keep the next FIFO entry's."""
        if not self.read_half:
            self.read_buffer = self.fifo[self.fifo_read][2]
        target = self.code & 0x1F
        address = self.address
        if target == READ_VRAM:
            word = address & 0xFFFE
            if not self.read_half:
                self.read_buffer = (self.read_buffer & 0xFF) | (self.vram[word] << 8)
                self.read_half = True
                return
            self.read_buffer = (self.read_buffer & 0xFF00) | self.vram[word | 1]
            self.read_half = False
            self.read_available = True
        elif target == READ_VSRAM:
            index = (address & 0x7E) >> 1
            value = self.vsram[index] if index < self.vsram_words else self.vsram_last_render
            self.read_buffer = (self.read_buffer & 0xF800) | (value & 0x07FF)
            self.read_available = True
        elif target == READ_CRAM:
            value = self.cram[(address & 0x7E) >> 1]
            self.read_buffer = (self.read_buffer & 0xF111) | (value & 0x0EEE)
            self.read_available = True
        elif target == READ_VRAM8:
            self.read_buffer = (self.read_buffer & 0xFF00) | self.vram[(address ^ 1) & 0xFFFF]
            self.read_available = True
        else:
            return
        self.address = (self.address + self.registers[15]) & 0xFFFF

    def _fill(self) -> None:
        """One DMA fill write (Exodus ``PerformDMAFillOperation``, General.cpp 2680-2825):
        VRAM takes the upper byte of the triggering write; CRAM and VSRAM take the word
        in the next FIFO entry."""
        fifo = self.fifo
        last = fifo[(self.fifo_read + 3) & 3]
        last[1] = (last[1] + self.registers[15]) & 0xFFFF
        target = self.code & 0x0F
        if target == WRITE_VRAM:
            self._vram_write_byte(last[1], last[2] >> 8)
        elif target == WRITE_CRAM:
            self.cram[(last[1] & 0x7E) >> 1] = fifo[self.fifo_read][2] & 0x0EEE
        elif target == WRITE_VSRAM:
            index = (last[1] & 0x7E) >> 1
            if index < self.vsram_words:
                self.vsram[index] = fifo[self.fifo_read][2] & 0x07FF
        self.address = (self.address + self.registers[15]) & 0xFFFF

    def _copy(self) -> None:
        """One DMA copy byte, VRAM to VRAM (Exodus ``PerformDMACopyOperation``,
        General.cpp 2630-2677)."""
        source = self.registers[21] | (self.registers[22] << 8)
        self._vram_write_byte(self.address, self.vram[(source ^ 1) & 0xFFFF])
        self.address = (self.address + self.registers[15]) & 0xFFFF

    def _transfer_read(self) -> None:
        registers = self.registers
        source = (registers[21] << 1) | (registers[22] << 9) | ((registers[23] & 0x7F) << 17)
        self.transfer_value = self.board.dma_read_word(source & 0xFFFFFE)
        self.transfer_cached = True

    def _transfer_push(self) -> None:
        self._push(self.transfer_value)
        self.transfer_cached = False

    def _push(self, data: int) -> None:
        """A new FIFO entry (Exodus WriteInterface, Ports.cpp 1273-1300): its code and
        address are the command registers' if rewritten since the last entry, else the
        last entry's with the address incremented."""
        entry = self.fifo[self.fifo_write]
        if self.modified:
            entry[0], entry[1] = self.code, self.address
            self.modified = False
        else:
            previous = self.fifo[(self.fifo_write + 3) & 3]
            entry[0] = previous[0]
            entry[1] = (previous[1] + self.registers[15]) & 0xFFFF
        entry[2] = data & 0xFFFF
        entry[3] = False
        entry[4] = True
        self.fifo_write = (self.fifo_write + 1) & 3

    def _dma_step(self) -> None:
        """Count down the length and up the source (Exodus ``AdvanceDMAState``,
        General.cpp 2890-2935): only registers 21 and 22 count, so a transfer wraps within
        128 KB; a length of 0 means $10000."""
        registers = self.registers
        length = (((registers[20] << 8) | registers[19]) - 1) & 0xFFFF
        registers[19], registers[20] = length & 0xFF, length >> 8
        source = (registers[21] | (registers[22] << 8)) + 1
        registers[21], registers[22] = source & 0xFF, (source >> 8) & 0xFF
        if length == 0:
            self.code &= ~0x20
            self.fill_running = False
            self.transfer_active = False

    # -- the 68000's ports ---------------------------------------------------------------

    def read_word(self, offset: int, mclk: int) -> int:
        self.hold = 0
        port = (offset >> 2) & 7
        if port >= 4:
            return self.board.open_bus()  # the PSG (reading locks up: CMD), unused, test
        self.sync(mclk)
        if port == 0:
            self.pending = False
            if (self.code & 0x13) != 0:
                return self.read_buffer  # an invalid read target locks up the hardware
            self._advance(mclk, lambda: self.read_available)
            self.hold = max(0, self.mclk - mclk)
            self.read_available = False
            self.read_half = False
            return self.read_buffer
        if port == 1:
            self.pending = False
            # The upper six lines float: the next opcode word (CMD 1.1 note 1).
            self.sample_point(mclk)
            return (self.board.open_bus() & 0xFC00) | self.status()
        return self.read_hv(mclk)

    def read_byte(self, offset: int, mclk: int) -> int:
        word = self.read_word(offset & 0x1E, mclk)
        return word & 0xFF if offset & 1 else word >> 8

    def write_word(self, offset: int, value: int, mclk: int) -> int:
        """A word write; returns (and leaves in :attr:`hold`) the MCLK the 68000 is held."""
        self.hold = 0
        port = (offset >> 2) & 7
        if port >= 4:
            return 0  # the PSG (milestone 4), the unused and test registers
        self.sync(mclk)
        if port == 0:
            self.hold = self._data_write(value, mclk)
        elif port == 1:
            self.hold = self._control_write(value, mclk)
        return self.hold

    def write_byte(self, offset: int, value: int, mclk: int) -> int:
        if offset >= 0x10:
            self.hold = 0
            return 0
        return self.write_word(offset & 0x1E, (value << 8) | value, mclk)

    def _data_write(self, value: int, mclk: int) -> int:
        self.pending = False
        self._advance(mclk, lambda: not self._fifo_full())
        held = max(0, self.mclk - mclk)
        self._push(value)
        self.code |= 0x10  # a data port write sets CD4 (Exodus Ports.cpp 1318)
        self.read_available = False
        self.read_half = False
        return held

    def _control_write(self, value: int, mclk: int) -> int:
        held = 0
        if not self.pending:
            self.pending = True
            self.code = (self.code & 0x3C) | (value >> 14)
            self.original_address = (self.original_address & 0xC000) | (value & 0x3FFF)
            self.code |= 0x10  # the first half sets CD4 (Exodus Ports.cpp 1385-1394)
            if self.code & 0x03 == 0x02:
                # A register write, taken from the address bits just written.
                number = (self.original_address >> 8) & 0x1F
                if not self.mode5:
                    # In mode 4 a register write clears the F flag: Nemesis's captures
                    # (the first run after power-on reads F clear until its first
                    # VBlank, every later run reads it set) and, for the mechanism,
                    # Nuked-MD probes (docs/worklog.md, "F flag").
                    self.f_flag = False
                if number < (0x18 if self.mode5 else 0x0B):
                    self._write_register(number, value & 0xFF)
                self.pending = False
        else:
            self.pending = False
            bits = 0x3C if self.dma_enabled else 0x1C  # CD5 only with DMA enabled
            self.code = (self.code & ~bits & 0x3F) | ((value >> 2) & bits)
            self.original_address = (self.original_address & 0x3FFF) | ((value & 3) << 14)
            if self.code & 0x20 and self.dmd < 2:
                held = self._run_transfer(mclk)
        self.address = self.original_address
        self.modified = True
        self.read_available = False
        self.read_half = False
        return held

    def _run_transfer(self, mclk: int) -> int:
        """A 68000-to-VDP transfer: the VDP takes the bus and holds it until the last word
        is read (Nemesis's TestDMATransferBusLock); returns the hold."""
        self.transfer_active = True
        self.transfer_cached = False
        self.transfer_next_read = mclk
        self._advance(mclk, lambda: not self.transfer_active)
        return max(0, self.mclk - mclk)

    # -- the Z80's side (byte accesses through $7F00 or its bank) ---------------------------

    def z80_read(self, offset: int, mclk: int) -> int:
        return self.read_byte(offset, max(mclk, self.mclk))

    def z80_write(self, offset: int, value: int, mclk: int) -> None:
        self.write_byte(offset, value, max(mclk, self.mclk))

    def z80_bank_read(self, offset: int, mclk: int) -> int:
        """Through the bank undriven bits read 1: the unused addresses $FF and the
        status's top six bits set (CMD 1.1 notes 1 and 4)."""
        port = (offset >> 2) & 7
        if port >= 4:
            return 0xFF
        value = self.read_byte(offset, max(mclk, self.mclk))
        if port == 1 and not offset & 1:
            value |= 0xFC
        return value
