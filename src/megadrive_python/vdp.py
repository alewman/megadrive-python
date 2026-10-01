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
from .render import Renderer

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
        self.no_read_fill: frozenset[int] = frozenset()
        self.slots_idle: list[int] = []
        #: Where display pixel 0 falls in ``hvals``, so that ``position(offset) -
        #: display_at`` is the screen X a master clock lands on.  The active display is
        #: external H $00-$9F in H40 and $00-$7F in H32 -- internal H $000 onwards,
        #: 320 or 256 pixels (CMD ``genvdp.txt``; the V counter increments after it, at
        #: external $A5/$85, and Kabuto's access-slot groups fall in place around it:
        #: his 15 "mid-tiles" slots are the ones inside $000-$13F, his 2
        #: "between-tiles-and-sprites" ones are $140 and $142 just past it, and his
        #: single "mid-sprite" one is $1CE, in the blanking).  What is *not* modelled
        #: is his statement that the second mid-tiles slot lands "at the pixel with
        #: X=-3 (display area coordinates)", which would put the output about 21 pixels
        #: behind the H counter: a mid-line write here lands at the pixel its access
        #: slot's own H counter names (docs/undocumented-behavior.md).
        self.display_at = 0

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
#: Active-line slots that fill no data-port read (:data:`READ_RELEASE_MCLK`).
_NO_READ_FILL = {"H40": (0x142,)}
#: The refresh slots of an idle line, by H counter, from Exodus's own tables
#: ("Refresh cycle slots (H32)"/"(H40)", Timing.cpp 1463-1520): five in H32 and six in
#: H40, "one more than there is during an active line", which is where the fifth (H32)
#: and sixth (H40) fall -- in the horizontal blank.
_REFRESH = {"H32": (0x035, 0x075, 0x0B5, 0x0F5, 0x1F5),
            "H40": (0x032, 0x072, 0x0B2, 0x0F2, 0x132, 0x1F2)}  # fmt: skip


def _idle_slots(mode: HMode) -> list[int]:
    """Slots on an idle line: every odd (H32) or even (H40) H counter value except the
    refresh slots -- "When display is disabled the VDP still does those refresh cycles
    and uses all other cycles as access slots" (Kabuto, "Memory access pattern").

    So an idle line has 171 - 5 = 166 slots in H32 and 210 - 6 = 204 in H40. **Exodus
    skips two slots per refresh** (its ``GetPixelClockTicksUntilNextAccessSlot`` jumps
    from a refresh block's start to six pixels past it), which would give 161 and 198;
    two hardware sources say otherwise and the board follows them (milestone 5,
    docs/undocumented-behavior.md): Kabuto's access pattern has one ``r`` per group,
    four in H32 and five in H40 per active line, and the Manual's DMA transfer capacity
    table (PDF page 44) gives 167 and 205 bytes a line in blanking, which is one slot
    lost per refresh. The board's 166 and 204 are those figures less the extra
    blanking refresh Exodus measured, which Sega's table does not account for.
    """
    parity = 1 if mode.name == "H32" else 0
    refresh = set(_REFRESH[mode.name])
    return sorted(mode.offset_of[h] for h in mode.hvals if h % 2 == parity and h not in refresh)


#: Where in a line the full-screen vertical scroll is latched, by internal H counter,
#: planes A and B: the last pixels before the V counter increments (Nuked-MD's
#: ``l184``/``l185``, measured with a test cartridge on the referee; docs/worklog.md,
#: "The vertical scroll latch"). Die-derived, the tier that judges where nothing
#: hardware-captured covers the question.
VSCROLL_LATCH_H = {"H40": (0x149, 0x143), "H32": (0x109, 0x103)}

#: Registers that say where or how the planes' tiles are fetched (the plane, window
#: and table bases, the scroll modes, the plane size, the window's position, and
#: register 12's cell width): a write to one freezes every tile whose name has been
#: fetched, as a name-table write does (``Renderer.catch_up``).
PLANE_REGISTERS = frozenset({2, 3, 4, 11, 12, 16, 17, 18})

#: The command address is 17 bits, not 16: the second control word's bits 2-0 are
#: address bits 16-14 in mode 5, and the auto-increment wraps at 128 KB (Nuked-MD,
#: ``reg_addr`` and its ``& 0x1ffff`` increment). With one 64 KB VRAM chip fitted a
#: write lands at the address modulo 64 KB, but the sprite cache compares all 17 bits
#: "even in 64 KB mode, so when setting bit 16 ... for the Write/DMA target address
#: ... the sprite table in memory will get updated but the cache won't" (Kabuto,
#: "Precise details of sprite rendering"). Overdrive 2's rain is drawn that way
#: (docs/worklog.md, "The rain").
ADDRESS_MASK = 0x1FFFF

#: Where in its line each 2-cell column's vertical scroll is latched, as (the board's
#: internal H for column 0, the step per column), planes A and B: column k's word at
#: first + 16k. Nuked-MD latches plane A's column k at its H counter 16k + 1 and plane
#: B's at 16k + 9, in both modes, and display line v shows what counter line v latched
#: (224 of 224 lines, columns 0, 10 and 19 in H40, 0, 8 and 15 in H32); its H40
#: counter runs three above the board's (docs/undocumented-behavior.md VDP item 20),
#: so H40's column 0 of plane A is the board's $1FE, in the blank before the display.
#: docs/worklog.md, "The 2-cell column latch".
VSCROLL_COLUMN_H = {"H40": ((0x1FE, 16), (0x006, 16)), "H32": ((0x001, 16), (0x009, 16))}

#: Internal H of display pixel 0 (``HMode.display_at``).
_DISPLAY_START = {"H32": 0x000, "H40": 0x000}

for _mode in (H32, H40, H40_VSYNC, H40_LONG, H40_SHORT):
    _mode.slots_active = sorted(_mode.offset_of[h] for h in _ACTIVE_SLOTS[_mode.name])
    _mode.slots_idle = _idle_slots(_mode)
    _mode.no_read_fill = frozenset(_mode.offset_of[h] for h in _NO_READ_FILL.get(_mode.name, ()))
    _mode.display_at = _mode.hvals.index(_DISPLAY_START[_mode.name])


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

#: Whether VINT fires on a frame whose vertical border never started (the V28/V30
#: trick, :attr:`VDP.border`). Kabuto says only that "vblank still works normally";
#: BlastEm raises VINT from its border state alone, so not on such a frame. Not
#: measured: an experiment switch while it is decided.
VINT_WITHOUT_BORDER = False

#: The border tested against the live V28/V30 mode (:attr:`VDP.border`); False puts
#: back the board's earlier rule, the border from the latched mode's blanking line,
#: for the compatibility sweep's triage (``scripts/compat_sweep.py``).
LIVE_BORDER = True

#: Write targets (code bits 3-0) and read targets (bits 4-0).
WRITE_VRAM, WRITE_CRAM, WRITE_VSRAM = 0x01, 0x03, 0x05
READ_VRAM, READ_VSRAM, READ_CRAM, READ_VRAM8 = 0x00, 0x04, 0x08, 0x0C

#: MCLK a DMA transfer takes to read one word from the 68000's bus (Exodus
#: ``dmaTransferReadTimeInMclkCycles``, ``S315_5313.h`` line 919).
DMA_READ_MCLK = 16

#: The data port's read-ahead, as the die model times it. A read that waits for the
#: buffer is let go :data:`READ_RELEASE_MCLK` after the slot that fills it, not at the
#: slot; a slot fills the buffer only :data:`READ_FILL_LEAD_MCLK` or more after the read
#: that emptied it began; and in an active H40 line the slot at H $142 fills no read.
#: Nuked-MD, a cartridge sweeping bursts and pairs of CRAM, VSRAM and VRAM reads across
#: the line (``scripts/run_read_slots.py``): the die ends a waiting read 42-48 MCLK
#: after the board did at every slot, catches $140 from a read begun 22 MCLK before it
#: and not from one 15 before, and from 15 before misses $142 too. Overdrive 2's boot
#: polls vblank once per pass of a CRAM readback, so the rules decide which pass it
#: leaves on: without them the board reaches the Z80's start 4,182 MCLK late against its
#: own vblank, with them 57 MCLK early (docs/worklog.md, Session 14). Die-derived:
#: nothing hardware-captured times the read-ahead. VDPFIFOTesting's "FIFO Wait States"
#: fails with them every time; it passes on the die about once per 800 iterations, so
#: it cannot judge them (``scripts/run_vdpfifotesting.py``, ``INTERMITTENT``).
READ_RELEASE_MCLK = 45
READ_FILL_LEAD_MCLK = 21
#: A 68000 bus cycle without wait states: 4 clocks.
WRITE_CYCLE_MCLK = 28
#: A 68000-memory transfer's fixed start-up, before its first word is read.
#:
#: Kabuto ("DMA speed", hardware-measured research) states the cost of a fast transfer
#: as ``words * 2.4 + 5.6`` 68000 cycles: the board reproduced his slope within 2 % and
#: had no mechanism at all for his **+5.6**, so a one-word transfer cost it about one
#: cycle where his formula says eight (milestone 5, open item 15).  Nuked-MD
#: (die-derived) then measured the same transfer on the die: 11.0 68000 cycles for one
#: word and +8.0 fitted over 1 to 512 (milestone 6), which corroborates that the
#: start-up is real and puts it on Kabuto's side of the question rather than the
#: board's.  The two disagree in size, so the **hardware source is the judge**: the
#: board charges Kabuto's own 5.6 cycles, 39 MCLK (5.57 cycles, the rounding to whole
#: master clocks), and the referee's remaining 2.4 cycles are named rather than fitted
#: away (docs/worklog.md, the timing session).
DMA_STARTUP_MCLK = 39
#: The 68000's write that starts a 68000-to-VDP transfer is itself held 3 clocks
#: longer: on Nuked-MD every such write holds /AS 39 MCLK where an ordinary one holds 18
#: (all 119 of them in the Overdrive 2 boot and logo traces; docs/worklog.md "The DMA
#: trigger"). The transfer begins once that longer write is over. Die-derived, and on
#: top of Kabuto's start-up above, which a hardware-measured total of his would already
#: contain: Aubrey's option A, measured before the rest of the gap is questioned. 0
#: leaves it out.
DMA_TRIGGER_HOLD_MCLK = 21
#: The transfer's own bus cycles take the cartridge refresh ticks that fall due while it
#: runs, so the 68000's first cycle after it is not stretched for them: on Nuked-MD it
#: is not, 114 times of 118 (the boot and logo traces), where the board stretched it
#: every time. Die-derived. False leaves the ticks to the 68000.
DMA_TAKES_REFRESH = True
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
        #: Planes A and B's full-screen vertical scroll as the line being drawn took it.
        #: The VDP latches each once a line, at the end of the line before (internal H
        #: $149 for plane A and $143 for plane B in H40, $109 and $103 in H32: Nuked-MD,
        #: whose VSRAM-to-scroll latches ``l184`` and ``l185`` change there and nowhere
        #: else, and whose picture never changes plane row part way along a line --
        #: docs/worklog.md, "The vertical scroll latch"). A VSRAM write made during a
        #: line therefore reaches the picture on the next one, never on its own.
        self.vscroll_latch = [0, 0]
        #: What a plane's word held before a write landed after that plane's latch point
        #: in the current line, which is what the latch took; None when no write has.
        self._vscroll_before = [None, None]
        #: The same for 2-cell column scroll, word by word: each column's word is
        #: latched once a line, during that line, as the column is fetched
        #: (``VSCROLL_COLUMN_H``), so a write after that point in the line reaches the
        #: column on the next line. None where no such write has landed.
        self._column_before: list[int | None] = [None] * 40
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
        #: When the buffer's word can be handed over, and the earliest a slot may fill
        #: it again (:data:`READ_RELEASE_MCLK`, :data:`READ_FILL_LEAD_MCLK`).
        self.read_ready_at = 0
        self.read_fill_after = 0
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
        #: The debug register $C0001C (Kabuto, "The mighty debug register"): only its
        #: plane-forcing bits 8-6 are modelled (:mod:`megadrive_python.render`).
        self.debug_register = 0
        #: The picture (milestone 3). Rendering can be turned off for speed where only
        #: the ports and counters matter; the status register's sprite flags then stay
        #: clear, so a harness that reads them must leave it on.
        self.renderer = Renderer(self)
        self.rendering = True
        #: MCLK the last 68000 port access held the 68000.
        self.hold = 0
        # Timing: the latched modes, the current line and how far the VDP has run.
        self.hmode = H32
        self.vmode = V28_PAL if self.pal else V28_NTSC
        self.interlace = False
        self.interlace_double = False
        self.v = 0x1FF
        self.previous_v = 0x1FE
        #: The vertical border: on from the line where the V counter meets the blanking
        #: line of the V28/V30 mode in force *at that moment* ($E0 or $F0) to the end of
        #: the frame. "The VDP tests it constantly, so having V28 active for a single VDP
        #: cycle during line 224 or V30 for line 240 will start the border", and
        #: switching to V30 before line 224 and back to V28 before line 240 means it
        #: never starts: the display stays active all frame and "HIRQ runs continuously
        #: and is not reset before the start of each frame" (Kabuto, "V28/V30 tricks").
        #: Overdrive 2's arcade scene runs on that (docs/worklog.md, "The arcade").
        self.border = False
        self._border_started = False  # the border began on the line under way
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
        # A register write reaches the VDP at once, not through the FIFO, so it too
        # lands part way along the line being drawn.
        if self.renderer.pending:
            # Tiles already fetched keep the old state; the output, behind the H counter
            # (``render.OUTPUT_DELAY``), takes the new one from where it has got to.
            self.renderer.catch_up(self.mclk - self.line_start, names=number in PLANE_REGISTERS)
        old = self.registers[number]
        self.registers[number] = value
        if number == 0 and (value ^ old) & 0x02 and value & 0x02:
            # 0 -> 1 on the HV latch bit latches the counter (Exodus Ports.cpp 1858-1871).
            self.hv_latched = self._hv_now()
        elif number == 1 and (value ^ old) & 0x48:
            if (
                LIVE_BORDER
                and (value ^ old) & 0x08
                and not self.border
                and self.v == self._blank_line()
            ):
                # The mode that blanks from this line came in part way along it.
                self.border = self._border_started = True
            self._choose_slots()

    # -- time ------------------------------------------------------------------------------

    def _choose_slots(self) -> None:
        """The slot list for the current line, and where in it the VDP is."""
        active = self.display_enabled and (not self.border or self.v == 0x1FF)
        self.slots = self.hmode.slots_active if active else self.hmode.slots_idle
        self.slot_index = bisect_right(self.slots, self.mclk - self.line_start - 1)

    def _line_events(self) -> None:
        mode = self.hmode
        start = self.line_start
        self.line_end = start + mode.length
        self.hblank_at = start + mode.offset_of[mode.hblank_set]
        vint_line = self.v == self.vmode.vblank_set if VINT_WITHOUT_BORDER else self._border_started
        self.vint_at = start + mode.offset_of[0x001] if vint_line else -1
        self.odd_at = start + mode.offset_of[0x002] if vint_line else -1
        # The picture of a line is drawn where its active display begins (the HBlank
        # flag clearing), so that an H interrupt handler's register writes -- made in
        # the horizontal blank that precedes the same line -- are already in place.
        self.render_at = (
            start + mode.offset_of[mode.hblank_clear]
            if self.rendering and not self.border and self.v < 0x0F0
            else -1
        )
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
            kind = 0  # 0 line, 1 hblank, 2 vint, 3 odd, 4 slot, 5 render
            if self.mclk <= self.hblank_at < next_event:
                next_event, kind = self.hblank_at, 1
            if not self._vint_done and self.mclk <= self.vint_at < next_event:
                next_event, kind = self.vint_at, 2
            if not self._odd_done and self.mclk <= self.odd_at < next_event:
                next_event, kind = self.odd_at, 3
            if self.mclk <= self.render_at < next_event:
                next_event, kind = self.render_at, 5
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
            elif kind == 5:
                self.render_at = -1
                self.renderer.begin_line(self.v)
            else:
                self._odd_done = True
                if self.interlace:
                    self.odd = not self.odd

    def _next_line(self) -> None:
        # The line is over: whatever is left of its picture is drawn from the state it
        # ends with, before the next line can change anything.
        if self.renderer.pending:
            self.renderer.finish_line()
        # The vertical scroll latches fell in the last few pixels of this line: they
        # took each word as it stood then, which is its value now unless a write landed
        # after the latch point (``_vsram_store``).
        for plane in (0, 1):
            before = self._vscroll_before[plane]
            self.vscroll_latch[plane] = self.vsram[plane] if before is None else before
            self._vscroll_before[plane] = None
        # Every 2-cell column is latched afresh in the line now starting.
        column_before = self._column_before
        for index in range(40):
            column_before[index] = None
        vmode = self.vmode
        new_v = vmode.next(self.v, self.interlace, self.odd)
        self.previous_v = self.v
        if new_v == vmode.vblank_set:
            self._latch_vmode()
            vmode = self.vmode
        was_border = self.border
        if new_v == 0:
            self.border = False
            was_border = False  # line 0 counts the H interrupt down, as every line did
        self._border_started = False
        if not LIVE_BORDER:
            self.border = new_v >= vmode.vblank_set
            self._border_started = new_v == vmode.vblank_set
            was_border = new_v > vmode.vblank_set
        elif not self.border and new_v == self._blank_line():
            self.border = self._border_started = True
        self.v = new_v
        if new_v == 0:
            self.frame += 1
            self.renderer.start_frame()
        if self.hmode.name == "H40":
            self.hmode = vmode.h40_line(new_v, self.interlace, self.odd)
        # The HINT counter at the V increment point (Exodus General.cpp 2268-2289).
        if was_border:
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

    def _blank_line(self) -> int:
        """The line the vertical border starts on for the V28/V30 mode register 1 holds
        now ($E0 or $F0): the border is tested against the live mode (:attr:`border`),
        where the V counter's sequence, VSYNC and interlace keep the mode latched at
        the blanking line (:meth:`_latch_vmode`) -- which Nemesis's HV captures hold the
        board to."""
        return 0x0F0 if self.registers[1] & 0x08 else 0x0E0

    def _latch_vmode(self) -> None:
        """V28/V30 and interlace changes latch at VBlank (Exodus General.cpp 2203-2215),
        for the V counter's sequence and VSYNC; the border does not wait for it."""
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
        """The 10-bit status register (the Manual; Exodus's status bits).

        Bit 6 is set when a line has more sprites than it has slots or runs out of
        sprite tiles, bit 5 when two sprite pixels overlap (CMD ``genvdp.txt`` 6, which
        says only that both are "most likely cleared at the end of the frame"); this
        board clears them when the status is read, as Genesis Plus GX does
        (``vdp_ctrl.c``, emulator-derived: no hardware source pinned here settles it).
        """
        h = self.hcounter()
        v = self.v
        vblank = (self.border and v != 0x1FF) or not self.display_enabled
        hblank = h >= self.hmode.hblank_set or h < self.hmode.hblank_clear
        renderer = self.renderer
        return (
            (0 if self.fifo[self.fifo_read][4] else 0x200)
            | (0x100 if self._fifo_full() else 0)
            | (0x80 if self.f_flag else 0)
            | (0x40 if renderer.overflow else 0)
            | (0x20 if renderer.collision else 0)
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
        """One access slot (Exodus ``UpdateInternalState``, General.cpp 1773-1878).

        Anything written here lands part way along the line being drawn, so the
        renderer is brought up to this slot's pixel first (milestone 5).
        """
        fifo = self.fifo
        if self.renderer.pending:
            # Tiles whose fetch time has come are fetched with VRAM as it stands; the
            # output is mixed up to where it has got to, behind the H counter
            # (``render.FETCH_AT``, ``render.OUTPUT_DELAY``).
            entry = fifo[self.fifo_read]
            code = entry[0] if entry[4] else self.code
            address = entry[1] if entry[4] else self.address
            self.renderer.catch_up(
                self.mclk - self.line_start,
                names=(code & 0x0F) == WRITE_VRAM and self._in_name_table(address),
                limit=self._column_limit(code, address),
            )
        dmd = self.registers[23] >> 6
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
        if (
            not fifo[self.fifo_read][4] and not self.read_available and (self.code & 0x13) == 0
            and self.mclk >= self.read_fill_after and not self._no_read_fill()
        ):  # fmt: skip
            self._read_cache()
            did_read = True
            if self.read_available:
                self.read_ready_at = self.mclk + READ_RELEASE_MCLK
        if fifo[self.fifo_read][4] and not did_read:
            self._fifo_write()
        if self.code & 0x20 and dmd < 2 and self.transfer_cached and not self._fifo_full():
            self._transfer_push()
            self._dma_step()

    def _no_read_fill(self) -> bool:
        """This slot fills no data-port read: H40's $142 in an active line
        (:data:`READ_RELEASE_MCLK`)."""
        return (
            self.slots is self.hmode.slots_active
            and self.mclk - self.line_start in self.hmode.no_read_fill
        )

    def _vram_write_byte(self, address: int, value: int) -> None:
        # VRAM holds each word byte-swapped, so a byte access reaches the other byte of
        # the word (Exodus M5WriteVRAM8Bit, General.cpp 3104-3140). Stored here
        # un-swapped: the XOR puts the byte where a word read expects it.
        self.vram[(address ^ 1) & 0xFFFF] = value & 0xFF
        # The sprite cache is written through by every VRAM write that lands in the
        # sprite table, over the whole 128 KB address range (Kabuto). It is checked
        # against the address as the program gives it, which is this one un-swapped.
        self.renderer.sprite_cache_write((address ^ 1) & 0x1FFFF, value & 0xFF)

    def _vram_write_128k(self, address: int, data: int) -> None:
        """One VRAM write in 128 KB mode (register 1 bit 7), on a console with the
        usual single 64 KB chip.

        "each VRAM chip just stores one byte from each 16-bit word", so with only one
        chip fitted "just the lower byte of each word written to VRAM will get stored,
        the upper byte will get sent to the non-existing 2nd VRAM chip and get lost";
        the address bits are rearranged, "Writes to address a in 128K mode end up in the
        68k VRAM address (((a & 2) >> 1) ^ 1) | ((a & $400) >> 9) | a & $3FC |
        ((a & $1F800) >> 1)"; "writing to odd addresses does not seem to have an effect
        (as if the lowmost address bit was just ignored)"; and the write takes one slot
        rather than two, which is why "write/DMA speed to VRAM increases to that of CRAM
        and VSRAM" (Kabuto, "128 KB mode abuse"; hardware-measured research, but no test
        ROM pinned here exercises it -- Overdrive 2 in milestone 5 is the judge).

        What 128 KB mode does to the *picture* is not modelled: the renderer keeps
        reading VRAM as in 64 KB mode, where the hardware would show "garbled display
        contents" (Kabuto).
        """
        if address & 1:
            return
        target = (
            (((address & 2) >> 1) ^ 1)
            | ((address & 0x400) >> 9)
            | (address & 0x3FC)
            | ((address & 0x1F800) >> 1)
        )
        self.vram[target & 0xFFFF] = data & 0xFF
        self.renderer.sprite_cache_write(target & 0x1FFFF, data & 0xFF)

    def _fifo_write(self) -> None:
        """Commit the oldest FIFO entry, a byte at a time for VRAM (Exodus
        ``PerformFIFOWriteOperation``, General.cpp 2479-2627)."""
        entry = self.fifo[self.fifo_read]
        code, address, data, half = entry[0], entry[1], entry[2], entry[3]
        target = code & 0x0F
        if target == WRITE_VRAM:
            if self.registers[1] & 0x80:
                self._vram_write_128k(address, data)
                entry[4] = False
            else:
                # The low byte first, then the high: Nuked-MD's own VRAM writes
                # (``NMD_VRAMSIG``, with each byte's bank and data) commit a name's tile
                # index a slot before its attributes, as this does.
                byte = (data >> 8) if ((address & 1) ^ half) else data & 0xFF
                self._vram_write_byte((address & (ADDRESS_MASK - 1)) | half, byte)
                entry[3] = not half
                entry[4] = entry[3]
        elif target == WRITE_CRAM:
            self.cram[(address & 0x7E) >> 1] = data & 0x0EEE
            self.renderer.cram_dirty = True
            self.renderer.cram_dot(data & 0x0EEE)
            entry[4] = False
        elif target == WRITE_VSRAM:
            index = (address & 0x7E) >> 1
            if index < self.vsram_words:
                self._vsram_store(index, data & 0x07FF)
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

    def _vsram_store(self, index: int, value: int) -> None:
        """Store one VSRAM word, keeping what the vertical scroll latches took if the
        write lands after the latch point of its plane (see :attr:`vscroll_latch`) or,
        in 2-cell column mode, of its column (:meth:`column_scroll`)."""
        offset = self.mclk - self.line_start
        mode = self.hmode
        if index < 2 and self._vscroll_before[index] is None:
            latch_h = VSCROLL_LATCH_H[mode.name][index]
            if offset > mode.offset_of[latch_h]:
                self._vscroll_before[index] = self.vsram[index]
        if index < 40 and self._column_before[index] is None:
            first, per_column = VSCROLL_COLUMN_H[mode.name][index & 1]
            latch_h = (first + (index >> 1) * per_column) & 0x1FF
            at = mode.offset_of.get(latch_h)
            if at is not None and offset > at:
                self._column_before[index] = self.vsram[index]
        self.vsram[index] = value

    def _in_name_table(self, address: int) -> bool:
        """Whether a VRAM byte address lies in plane A's, plane B's or the window's
        name table, whose entries are fetched ahead of the patterns they name."""
        registers = self.registers
        size = registers[16]
        cells = (32, 64, 32, 128)[size & 3] * (32, 64, 32, 128)[(size >> 4) & 3]
        address &= 0xFFFF
        tables = [((registers[2] << 10) & 0xE000, cells * 2),
                  ((registers[4] << 13) & 0xE000, cells * 2)]  # fmt: skip
        # The window's table only where the line under way shows the window: with no
        # window its base register is often left at 0, which would make the patterns
        # at the bottom of VRAM look like names.
        if self.renderer.shows_window():
            tables.append(((registers[3] << 10) & 0xF800, 64 * 32 * 2))
        return any(base <= address < base + length for base, length in tables)

    def _column_limit(self, code: int, address: int) -> int | None:
        """For a 2-cell VSRAM write that lands before its column's latch: that column's
        first pixel, which the line may not be drawn past yet.

        The board draws a column from its first pixel on, but the die outputs it about
        21 pixels later (``render.OUTPUT_DELAY``), after its latch -- which for plane B
        falls 6 (H40) or 9 (H32) pixels into the column. A write landing between the two
        reaches the whole column on the die; drawing only up to the column keeps the
        board from splitting it."""
        if (code & 0x0F) != WRITE_VSRAM or not self.registers[11] & 0x04:
            return None
        index = (address & 0x7E) >> 1
        if index >= 40:
            return None
        mode = self.hmode
        first, per_column = VSCROLL_COLUMN_H[mode.name][index & 1]
        at = mode.offset_of.get((first + (index >> 1) * per_column) & 0x1FF)
        if at is None or self.mclk - self.line_start > at:
            return None
        return (index >> 1) * 16

    def column_scroll(self, index: int) -> int:
        """The 2-cell column scroll word ``index`` as the line being drawn latched it."""
        before = self._column_before[index] if index < 40 else None
        return self.vsram[index] if before is None else before

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
            value = self.vsram[index] if index < self.vsram_words else self._vsram_last_render()
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
        self.address = (self.address + self.registers[15]) & ADDRESS_MASK

    def _vsram_last_render(self) -> int:
        """What a read of VSRAM past its end gives: the last word the renderer read from
        VSRAM, which follows the H counter (the column's entry; in full-screen vertical
        scroll mode the first two entries), or where that index is past the end too,
        entries $26 AND $27 (Exodus ``UpdateInternalState``, General.cpp 2114-2129;
        VDPFIFOTesting's DMA Fill to VSRAM tests, which read back $50-$7F)."""
        index = (self.hcounter() >> 2) & 0x7E
        if not self.registers[11] & 0x04:
            index &= 0x03
        if index < 0x50:
            return self.vsram[index >> 1]
        return self.vsram[0x26] & self.vsram[0x27]

    def _fill(self) -> None:
        """One DMA fill write (Exodus ``PerformDMAFillOperation``, General.cpp 2680-2825):
        VRAM takes the upper byte of the triggering write; CRAM and VSRAM take the word
        in the next FIFO entry."""
        fifo = self.fifo
        last = fifo[(self.fifo_read + 3) & 3]
        last[1] = (last[1] + self.registers[15]) & ADDRESS_MASK
        target = self.code & 0x0F
        if target == WRITE_VRAM:
            self._vram_write_byte(last[1], last[2] >> 8)
        elif target == WRITE_CRAM:
            self.cram[(last[1] & 0x7E) >> 1] = fifo[self.fifo_read][2] & 0x0EEE
            self.renderer.cram_dirty = True
            self.renderer.cram_dot(fifo[self.fifo_read][2] & 0x0EEE)
        elif target == WRITE_VSRAM:
            index = (last[1] & 0x7E) >> 1
            if index < self.vsram_words:
                self._vsram_store(index, fifo[self.fifo_read][2] & 0x07FF)
        self.address = (self.address + self.registers[15]) & ADDRESS_MASK

    def _copy(self) -> None:
        """One DMA copy byte, VRAM to VRAM (Exodus ``PerformDMACopyOperation``,
        General.cpp 2630-2677)."""
        source = self.registers[21] | (self.registers[22] << 8)
        self._vram_write_byte(self.address, self.vram[(source ^ 1) & 0xFFFF])
        self.address = (self.address + self.registers[15]) & ADDRESS_MASK

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
            entry[1] = (previous[1] + self.registers[15]) & ADDRESS_MASK
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
            self.hold = max(0, self.read_ready_at - mclk)
            self.read_fill_after = mclk + self.hold + READ_FILL_LEAD_MCLK
            self.read_available = False
            self.read_half = False
            return self.read_buffer
        if port == 1:
            self.pending = False
            # The upper six lines float: the next opcode word (CMD 1.1 note 1).
            self.sample_point(mclk)
            value = (self.board.open_bus() & 0xFC00) | self.status()
            self.renderer.overflow = False
            self.renderer.collision = False
            return value
        return self.read_hv(mclk)

    def read_byte(self, offset: int, mclk: int) -> int:
        word = self.read_word(offset & 0x1E, mclk)
        return word & 0xFF if offset & 1 else word >> 8

    def write_word(self, offset: int, value: int, mclk: int) -> int:
        """A word write; returns (and leaves in :attr:`hold`) the MCLK the 68000 is held."""
        self.hold = 0
        port = (offset >> 2) & 7
        if port == 7:
            # $C0001C, the debug register (Kabuto, "The mighty debug register"; CMD
            # ``newreg.txt``). Only the plane-forcing bits are modelled, plus the
            # PSG's three test bits, which Nuked-PSG takes at the same place.
            self.debug_register = value & 0xFFFF
            self.board.psg.write_test(value & 0xFFFF)
            return 0
        if port in (4, 5):
            # $C00011-$C00017: the PSG.  "If you want to write to the PSG via
            # word-wide writes, the data must be in the LSB" (CMD 1 note 3).
            self.board.sound_sync(mclk)
            self.board.psg.write(value & 0xFF)
            return 0
        if port >= 4:
            return 0  # the unused addresses and the test register
        self.sync(mclk)
        if port == 0:
            self.hold = self._data_write(value, mclk)
        elif port == 1:
            self.hold = self._control_write(value, mclk)
        return self.hold

    def write_byte(self, offset: int, value: int, mclk: int) -> int:
        if offset >= 0x10:
            port = (offset >> 2) & 7
            if port == 7:  # the debug register takes a byte as a word
                return self.write_word(offset & 0x1E, (value << 8) | value, mclk)
            self.hold = 0
            if port in (4, 5) and offset & 1:
                # "Doing byte-wide writes to even PSG addresses has no effect"
                # (CMD 1 note 3), so only the odd half reaches the chip.
                self.board.sound_sync(mclk)
                self.board.psg.write(value)
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
        transfer = False
        if not self.pending:
            self.pending = True
            self.code = (self.code & 0x3C) | (value >> 14)
            self.original_address = (self.original_address & 0x1C000) | (value & 0x3FFF)
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
            # Bits 2-0 are address bits 16-14 in mode 5 (Nuked-MD: ``reg_addr`` takes
            # ``io_data & 7`` there), so a program can name the upper 64 KB.
            upper = 7 if self.mode5 else 3
            self.original_address = (self.original_address & 0x3FFF) | ((value & upper) << 14)
            transfer = bool(self.code & 0x20) and self.dmd < 2
        self.address = self.original_address
        self.modified = True
        self.read_available = False
        self.read_half = False
        # A 68000-to-VDP transfer starts once the whole command word is in place.
        return self._run_transfer(mclk) if transfer else 0

    def _run_transfer(self, mclk: int) -> int:
        """A 68000-to-VDP transfer: the VDP takes the bus once the write that started it
        is over and holds it until the last word is read (Nemesis's
        TestDMATransferBusLock); returns how long the 68000's next bus cycle waits."""
        self.transfer_active = True
        self.transfer_cached = False
        # The first word is fetched once the write that started the transfer is over,
        # not from where it began ("the VDP takes the bus once the write ... is over"),
        # and after the fixed start-up Kabuto measured (:data:`DMA_STARTUP_MCLK`).
        written = mclk + WRITE_CYCLE_MCLK + DMA_TRIGGER_HOLD_MCLK  # the write is over
        self.transfer_next_read = written + DMA_STARTUP_MCLK - DMA_READ_MCLK
        self._advance(mclk, lambda: not self.transfer_active)
        board = self.board
        if DMA_TAKES_REFRESH and board.cart_refresh_due <= self.mclk:
            from .board import CART_REFRESH_MCLK as period  # board imports this module

            board.cart_refresh_due += ((self.mclk - board.cart_refresh_due) // period + 1) * period
        return DMA_TRIGGER_HOLD_MCLK + max(0, self.mclk - written)

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
