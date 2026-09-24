"""Read BlastEm's hardware event log (``blastem -b FRAMES -e FILE rom``).

BlastEm is emulator-derived, so under the tier rule it is a **detector**: this reads
its log to say *where to look*, never what is correct. It earns its place because its
author is Mask of Destiny, whose ``gen_test_z80_bus`` this repository already treats
as hardware tier, and because it runs cartridges MAME 0.285 cannot -- the Krikzz
Extended SSF ones, where MAME has no header rule, puts Earthion on ``rom_sram`` and
draws noise (docs/worklog.md, 2026-09-24).

The format is read from ``event_log.c`` and ``event_log.h`` at the pinned revision
(``reference/src/blastem`` @ 542164d) and implemented here from scratch. BlastEm is
GPL-3.0: it is read and cited, never copied.

Layout: ``BLSTEL``, a two-byte version, then an **uncompressed** start block (system
type, video standard, a length-prefixed game name) and a zlib stream of events.

An event is a header byte, optionally preceded by a MULTI byte that repeats the next
event's type and cycle delta:

===========================  ==================  ==========================
``header & 0xF0``            type                delta
===========================  ==================  ==========================
``< 0xE0``                   ``header >> 4``     ``(header & 0xF) + 16``
``== 0xE0``                  ``header & 0xF``    a 16-bit big-endian word
``== 0xF0``                  ``header & 0xF``    a sign-extended 24-bit value
===========================  ==================  ==========================

VRAM addresses are carried as deltas from the last one of their kind, so the reader
keeps ``last_word_address`` and ``last_byte_address`` exactly as BlastEm's own does.

**Two traps, both of which produced wrong readings here before they were found:**

- ``EVENT_FLUSH`` is **not** a frame marker. It is an output-buffer flush, and the
  count happens to land near the frame count (599 for a 600-frame run), which is what
  made it look like one. Its gaps cluster: over a 600-frame Earthion run the mean gap
  is one frame and the median is two. Use :func:`frame_of` instead.
- A cycle is **half a master clock** (about 26.85 MHz), not a 68000 clock.
"""

from __future__ import annotations

import argparse
import collections
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

FLUSH, ADJUST, PSG_REG, YM_REG, VDP_REG = 0, 1, 2, 3, 4
VRAM_BYTE, VRAM_BYTE_DELTA, VRAM_BYTE_ONE, VRAM_BYTE_AUTO = 5, 6, 7, 8
VRAM_WORD, VRAM_WORD_DELTA, VDP_INTRAM, STATE, MULTI = 9, 10, 11, 12, 13

NAMES = {
    FLUSH: "flush", ADJUST: "adjust", PSG_REG: "psg", YM_REG: "ym", VDP_REG: "vdp-reg",
    VRAM_BYTE: "vram-b", VRAM_BYTE_DELTA: "vram-bd", VRAM_BYTE_ONE: "vram-b1",
    VRAM_BYTE_AUTO: "vram-ba", VRAM_WORD: "vram-w", VRAM_WORD_DELTA: "vram-wd",
    VDP_INTRAM: "intram", STATE: "state", MULTI: "multi",
}  # fmt: skip

#: A frame in BlastEm's cycles (half master clocks), by video standard: 262 lines
#: NTSC and 313 PAL, 3,420 MCLK a line.
FRAME_CYCLES = {0: 262 * 3420 // 2, 1: 313 * 3420 // 2}


@dataclass
class Event:
    """One logged event: when, what, where and the value."""

    cycle: int
    kind: int
    address: int
    value: int

    @property
    def name(self) -> str:
        return NAMES.get(self.kind, f"?{self.kind}")


class Reader:
    """Every event of one log, in order, as :class:`Event`.

    Iterating to the end without raising *and* finishing on the last byte of the
    decompressed body is what says the decode is right: one wrong payload width
    desynchronises the stream and either raises or stops short.
    """

    def __init__(self, data: bytes) -> None:
        if data[:6] != b"BLSTEL":
            raise ValueError("not a BlastEm event log (no BLSTEL magic)")
        self.version = int.from_bytes(data[6:8], "big")
        self.system_type = data[8]
        self.video_std = data[9]
        length = data[10]
        self.name = data[11 : 11 + length].decode("latin1")
        self.body = zlib.decompress(data[11 + length :])
        self.pos = 0
        self.cycle = 0
        self.last_word_address = 0
        self.last_byte_address = 0
        #: The VDP's auto-increment, which ``EVENT_VRAM_BYTE_AUTO`` steps by. BlastEm
        #: carries it out of band; two is the value every log seen here uses.
        self.autoinc = 2

    @property
    def frame_cycles(self) -> int:
        return FRAME_CYCLES[self.video_std & 1]

    def frame_of(self, cycle: int) -> int:
        """Which frame a cycle falls in. The honest frame marker, unlike ``flush``."""
        return cycle // self.frame_cycles

    def _u8(self) -> int:
        value = self.body[self.pos]
        self.pos += 1
        return value

    def _u16(self) -> int:
        value = int.from_bytes(self.body[self.pos : self.pos + 2], "big")
        self.pos += 2
        return value

    def _payload(self, kind: int) -> tuple[int, int]:
        if kind == ADJUST:
            deduction = int.from_bytes(self.body[self.pos : self.pos + 4], "big")
            self.pos += 4
            self.cycle -= deduction
            return 0, deduction
        if kind == PSG_REG:
            return 0, self._u8()
        if kind == YM_REG:
            part, register, value = self._u8(), self._u8(), self._u8()
            return (part << 8) | register, value
        if kind == VDP_REG:
            return self._u8(), self._u8()
        if kind == VRAM_BYTE:
            self.last_byte_address = self._u16()
            return self.last_byte_address, self._u8()
        if kind == VRAM_BYTE_DELTA:
            self.last_byte_address = (self.last_byte_address + self._u8()) & 0xFFFF
            return self.last_byte_address, self._u8()
        if kind == VRAM_BYTE_ONE:
            self.last_byte_address = (self.last_byte_address + 1) & 0xFFFF
            return self.last_byte_address, self._u8()
        if kind == VRAM_BYTE_AUTO:
            self.last_byte_address = (self.last_byte_address + self.autoinc) & 0xFFFF
            return self.last_byte_address, self._u8()
        if kind == VRAM_WORD:
            self.last_word_address = (self._u8() << 16) | self._u16()
            return self.last_word_address, self._u16()
        if kind == VRAM_WORD_DELTA:
            self.last_word_address += self._u8()
            return self.last_word_address, self._u16()
        if kind == VDP_INTRAM:
            return self._u8(), self._u16()
        if kind == FLUSH:
            return 0, 0
        raise ValueError(f"event type {kind} has no payload this reader knows")

    def __iter__(self):
        while self.pos < len(self.body):
            repeats = 0
            header = self._u8()
            if header & 0xF0 == MULTI << 4:
                repeats = header & 0xF
                header = self._u8()
            if header & 0xF0 < 0xE0:
                kind, delta = header >> 4, (header & 0xF) + 16
            elif header & 0xF0 == 0xE0:
                kind, delta = header & 0xF, self._u16()
            else:
                kind = header & 0xF
                delta = self._u8() << 16
                if delta & 0x800000:  # sign-extended 24-bit, as BlastEm's reader does
                    delta -= 0x1000000
                delta |= self._u16()
            for _ in range(repeats + 1):
                self.cycle += delta
                address, value = self._payload(kind)
                yield Event(self.cycle, kind, address, value)


def register_writes(reader: Reader) -> dict[int, list[tuple[int, int]]]:
    """The VDP register writes of each frame, keyed by frame number."""
    frames: dict[int, list[tuple[int, int]]] = collections.defaultdict(list)
    for event in reader:
        if event.kind == VDP_REG:
            frames[reader.frame_of(event.cycle)].append((event.address, event.value))
    return dict(frames)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log", type=Path, help="a file written by blastem -e")
    parser.add_argument("--frames", type=int, metavar="N",
                        help="print the VDP register writes of the first N frames")  # fmt: skip
    args = parser.parse_args()

    reader = Reader(args.log.read_bytes())
    counts: collections.Counter[str] = collections.Counter()
    registers: collections.Counter[int] = collections.Counter()
    last = 0
    for event in reader:
        counts[event.name] += 1
        last = event.cycle
        if event.kind == VDP_REG:
            registers[event.address] += 1
    total = sum(counts.values())
    standard = "PAL" if reader.video_std & 1 else "NTSC"
    print(f"{args.log.name}: {reader.name!r}, {standard}, version {reader.version}")
    print(f"  {total:,} events over {reader.frame_of(last) + 1} frames "
          f"({last:,} cycles at MCLK/2); decoded to byte {reader.pos:,} of "
          f"{len(reader.body):,}")  # fmt: skip
    if reader.pos != len(reader.body):
        print("  WARNING: the stream did not end on a byte boundary: the decode desynced")
        return 1
    for name, count in counts.most_common():
        print(f"    {name:<9} {count:>9,}")
    if registers:
        print("  VDP registers written: "
              + ", ".join(f"{r}x{n}" for r, n in sorted(registers.items())))  # fmt: skip
    if args.frames:
        for frame, writes in sorted(register_writes(Reader(args.log.read_bytes())).items()):
            if frame >= args.frames:
                break
            print(f"  frame {frame:>4}: " + " ".join(f"{r}:{v:02X}" for r, v in writes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
