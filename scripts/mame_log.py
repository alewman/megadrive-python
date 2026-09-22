"""Read the two logs ``scripts/mame_trace.py`` writes (docs/mame-oracle.md).

``m68k.log``: ``M pc sr d0-d7 a0-a6 usp ssp totalcycles beamy beamx`` before each
68000 instruction, then that instruction's watched accesses, ``VR|VW|AR|AW|IR|IW|BR|BW
address size data totalcycles`` (hex, hex, hex, decimal; size is $8 or $10 bits).

``z80.log``: ``Z pc af bc de hl ix iy sp af' bc' de' hl' r totalcycles`` before each Z80
instruction, then ``YR|YW|KR|KW address data totalcycles``.

Every access line carries the cycle count of the instruction that made it, not of
the bus cycle (docs/mame-oracle.md, trap 6). The Z80's ``totalcycles`` counts only
the cycles the Z80 executed: it stands still while the Z80 is held in reset or has
given up its bus (found 2026-09-22: Sonic's first Z80 instruction is at totalcycles 0
although the 68000 released the Z80 at its cycle 984; docs/worklog.md). So the two
logs cannot be merged by ``totalcycles`` alone; ``scripts/lockstep_mame.py``
anchors the Z80's count to the 68000's clock each time the board lets the Z80 run.

``z80.log`` also holds markers, ``MR|MW address data totalcycles``: the 68000's
accesses to the Z80's space and its bus request and reset registers, logged in the
Z80's run with the Z80's count, which place them in the Z80's stream as MAME
interleaved the two CPUs. They are attached to the Z80 record they fell in.

Lines of any other shape (MAME's own messages) are skipped.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class Access:
    tag: str  # "VR", "AW", "KR", ...
    address: int
    size: int  # 8 or 16; the Z80's lines say 8
    value: int

    def __str__(self) -> str:
        return f"{self.tag} {self.address:06X}.{self.size}={self.value:X}"


@dataclass(slots=True)
class Record:
    """One instruction: the registers before it, its cycle count, its accesses.

    ``markers`` (Z80 records only): the 68000's accesses to the Z80's side that MAME
    made while this instruction was the Z80's latest, each with the number of this
    record's own accesses logged before it and the Z80's count at that moment.
    """

    registers: list[int]
    cycles: int
    accesses: list[Access]
    markers: list[tuple[int, Access, int]] = field(default_factory=list)


M68K_TAGS = {b"VR", b"VW", b"AR", b"AW", b"IR", b"IW", b"BR", b"BW"}
Z80_TAGS = {b"YR", b"YW", b"KR", b"KW"}
MARKER_TAGS = {b"MR", b"MW"}


def m68k_records(path: Path) -> Iterator[Record]:
    """68000 records: registers are pc, sr, d0-d7, a0-a6, usp, ssp (19 values)."""
    current: Record | None = None
    with path.open("rb") as log:
        for line in log:
            fields = line.split()
            if not fields:
                continue
            head = fields[0]
            if head == b"M" and len(fields) == 23:
                try:
                    registers = [int(f, 16) for f in fields[1:20]]
                    cycles = int(fields[20])
                except ValueError:
                    continue
                if current is not None:
                    yield current
                current = Record(registers, cycles, [])
            elif head in M68K_TAGS and len(fields) == 5 and current is not None:
                try:
                    current.accesses.append(
                        Access(
                            head.decode(),
                            int(fields[1], 16),
                            int(fields[2], 16),
                            int(fields[3], 16),
                        )
                    )
                except ValueError:
                    continue
    if current is not None:
        yield current


def z80_records(path: Path) -> Iterator[Record]:
    """Z80 records: registers are pc, af, bc, de, hl, ix, iy, sp, af', bc', de', hl', r.

    Markers logged before the first Z80 instruction come out on a record with no
    registers (``registers == []``).
    """
    current = Record([], 0, [])
    with path.open("rb") as log:
        for line in log:
            fields = line.split()
            if not fields:
                continue
            head = fields[0]
            if head == b"Z" and len(fields) == 15:
                try:
                    registers = [int(f, 16) for f in fields[1:14]]
                    cycles = int(fields[14])
                except ValueError:
                    continue
                if current.registers or current.markers:
                    yield current
                current = Record(registers, cycles, [])
            elif len(fields) == 4 and (head in Z80_TAGS or head in MARKER_TAGS):
                try:
                    access = Access(head.decode(), int(fields[1], 16), 8, int(fields[2], 16))
                    cycles = int(fields[3])
                except ValueError:
                    continue
                if head in MARKER_TAGS:
                    current.markers.append((len(current.accesses), access, cycles))
                else:
                    current.accesses.append(access)
    if current.registers or current.markers:
        yield current
