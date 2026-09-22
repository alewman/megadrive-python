"""The bus arbiter (YM6045): what the Z80 and the 68000 cost each other.

Three things share the 68000's bus -- the 68000, the VDP's DMA, and the Z80 through
its $8000-$FFFF bank window -- and one arbiter hands it out. Milestone 5's subject is
what that costs, in master clocks, on each side.

What is modelled here, and from where (docs/validation.md ranks the sources):

- **A Z80 access through its bank window** costs the Z80 2 to 5 of its own cycles and
  the 68000 about 11 of its own (Kabuto, "The bus system"; hardware-measured
  research).  His footnote 1 gives the arithmetic behind those averages, measured on
  his Model 1, and :func:`bank_access_delay` is that arithmetic:

      "The 68k needs to release the bus first for the z80 to access it. If the next
      68k access 2 cycles ahead or less or already ongoing the 68k will finish it
      first. (TODO test other 68k instructions as well). Thus there is an initial
      delay of 1 to 6 cycles. (Typically 1 to 4; up to 6 cycles if the last access is
      delayed by the refresh logic.) After the last access finished there's a delay of
      3 cycles for the bus to be granted. The Z80 bus access itself can be delayed as
      well by what seems to be refresh logic (up to 2 more cycles). The access itself
      takes 2 cycles.  Using constants a (time until 68k finishes its last access
      before releasing the bus (1 to 6; 1 to 4 if there are no refresh delays)) and b
      (2 if the z80->ROM access is prolonged by a refresh delay, 0 if not) we can thus
      compute:  readdelay = a + 3 + b + 2.  Another ingredient is the "subcycle" (0 to
      6), indicating how much ahead of the starting 68k cycle the z80's request was to
      begin with. From this we can compute the delay the z80 will get:  Z80 delay =
      ((readdelay*7 + 4 - subcycle)/15.  This in turn can be used for computing the
      delay the 68k will effectively get before it can keep on doing its business
      again:  68k delay = ((Z80 delay) * 15 + subcycle + 46) / 7 - a."

  MoD's logic analyser on a Genesis 2 (forum t=1661; hardware-captured) measures the
  same thing from the other end and is what the model is judged against:
  "!BG will go low 2 68K cycles later ... The total number of wait states that results
  from the above appears to be 2-3" with the 68000 stopped, "sometimes 3 68K cycles
  and there were a total of 4 wait states" with it running a two-read loop, and "The
  range is 2-4 Z80 cycles with an average of a little less than 3" over 16 accesses.
  ``scripts/run_bus_arbiter.py`` runs both of his experiments on the board.

- **The bus grant to the 68000** (BUSREQ at $A11100 to BUSACK): the Z80 samples BUSREQ
  "with the rising edge of the last clock period of any machine cycle" and releases
  the bus after it (Zilog UM0080, "Bus Request/Acknowledge Cycle"; specification
  tier), so the grant falls at the end of the machine cycle in progress. No hardware
  measurement of the Mega Drive's own delay is published: MoD's ``gen_test_z80_bus``
  counts it twice but his numbers are not in the pinned material, and his t=1661
  measurement is of the other direction. :func:`busack_grant` is therefore the
  specification's rule and nothing more, and what it makes that test ROM print is
  recorded rather than claimed (docs/undocumented-behavior.md, open item 8).

None of this is modelled by MAME, which grants the bus at once and adds one 68000
clock to each $A00000-$A0FFFF access instead, so :attr:`Board.arbiter_enabled` turns
the whole of it off for the milestone-1 lockstep, exactly as ``refresh_enabled``
already does for the refresh steal.
"""

from __future__ import annotations

#: The longest the 68000 is given to finish the access it is on before it lets go
#: (Kabuto's ``a``, "1 to 6").
MAX_RELEASE = 6
#: An access the 68000 has not started yet still delays the grant if it begins within
#: this many 68000 cycles ("If the next 68k access 2 cycles ahead or less").
LOOKAHEAD = 2
#: A 68000 bus cycle without wait states, in 68000 clocks.
BUS_CYCLE = 4
#: "After the last access finished there's a delay of 3 cycles for the bus to be
#: granted", and "The access itself takes 2 cycles" (Kabuto, footnote 1).
GRANT = 3
ACCESS = 2
#: The refresh logic can prolong the Z80's own access by 2 cycles (Kabuto's ``b``).
REFRESH = 2


def bank_access_delay(subcycle: int, release: int, refreshed: bool) -> tuple[int, int]:
    """Kabuto's footnote 1, verbatim: the Z80's delay in Z80 cycles and the 68000's in
    68000 cycles, for one Z80 access through the bank window.

    ``subcycle`` is 0-6, how far ahead of the next 68000 clock edge the request was;
    ``release`` is his ``a``, the 68000 cycles until the 68000 has finished the access
    it is on (1-6); ``refreshed`` is his ``b`` != 0, the refresh logic prolonging the
    Z80's own access.
    """
    read_delay = release + GRANT + (REFRESH if refreshed else 0) + ACCESS
    z80_delay = (read_delay * 7 + 4 - subcycle) // 15
    m68k_delay = (z80_delay * 15 + subcycle + 46) // 7 - release
    return z80_delay, m68k_delay


def busack_grant(request: int, machine_cycle_ends: list[int]) -> int:
    """The master clock the Z80 lets go of its bus, given the request's clock and the
    ends of the Z80 step's machine cycles (Zilog UM0080: the end of the machine cycle
    in progress)."""
    for end in machine_cycle_ends:
        if end >= request:
            return end
    return request
