"""The bus arbiter (YM6045): what the Z80 and the 68000 cost each other.

Three things share the 68000's bus -- the 68000, the VDP's DMA, and the Z80 through
its $8000-$FFFF bank window -- and one arbiter hands it out. Milestone 5's subject is
what that costs, in master clocks, on each side.

What is modelled here, and from where (docs/validation.md ranks the sources):

- **A Z80 access over the 68000's bus** (the bank window, or the VDP) costs the Z80 2
  to 5 of its own cycles and the 68000 about 11 of its own (Kabuto, "The bus system";
  hardware-measured research).  The board modelled it with his footnote 1's arithmetic
  until the die model was measured access by access (:func:`z80_access_wait`, and its
  constants' comment); his words stay here because they still say what happens:

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

  The timing session had already changed the ``a`` his arithmetic was given: the 68000
  holds the bus for the /AS-low part of its cycle (:data:`AS_LOW_MCLK`), not for all
  four of its clocks, and an access it has not started yet holds nothing. Kabuto's "If
  the next 68k access 2 cycles ahead or less ... the 68k will finish it first" was
  modelled as a look-ahead and made ``a`` too large: in his own ``jmp (a0)`` case it
  produced 5s and 6s that neither Nuked-MD nor MoD sees, a mean of 3.56 wait states
  against the referee's 2.74 and MoD's 2.93. With the /AS-low span and no look-ahead
  the board reproduces the referee's whole distribution (docs/worklog.md, the timing
  session). Kabuto's own sentence is not contradicted: a 68000 cycle is four clocks,
  and the next one does begin within two -- the bus is simply free for the 10 MCLK in
  between.

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
  tier), so the Z80 lets go at the end of the machine cycle in progress.  No hardware
  measurement of the Mega Drive's own delay is published: MoD's ``gen_test_z80_bus``
  counts it twice but his numbers are not in the pinned material, and his t=1661
  measurement is of the other direction.  Milestone 5 therefore modelled the
  specification's rule and nothing else, and milestone 6 measured what that leaves
  out: the arbiter's own propagation, which Nuked-MD puts at about 7.15 68000 cycles.
  :func:`busack_grant` is the two together -- the specification says *when* the Z80
  lets go, the referee says how long the board takes to say so
  (:data:`BUSACK_PROPAGATION_MCLK`; docs/undocumented-behavior.md, open item 8).

None of this is modelled by MAME, which grants the bus at once and adds one 68000
clock to each $A00000-$A0FFFF access instead, so :attr:`Board.arbiter_enabled` turns
the whole of it off for the milestone-1 lockstep, exactly as ``refresh_enabled``
already does for the refresh steal.
"""

from __future__ import annotations

#: How long the 68000 actually holds the bus for one bus cycle, in master clocks.
#:
#: A 68000 bus cycle is four clocks (28 MCLK), but the bus belongs to the 68000 only
#: while /AS is asserted: a requester takes it once /AS and /DTACK are negated (68000
#: UM, "Bus Arbitration").  The board charged the whole 28 MCLK until the timing
#: session, which made the 68000 look continuously on the bus in a tight loop and
#: Kabuto's ``a`` come out systematically too large.  Nuked-MD's bus log measures the
#: real figure directly: in a ``jmp (a0)`` loop 1,706 of its 1,766 68000 cycles hold
#: /AS low for exactly **18 MCLK** with a 10-MCLK gap to the next, and the other 60
#: hold it 32 -- 18 plus the cartridge refresh's 14, which is Kabuto's "up to 6 cycles
#: if the last access is delayed by the refresh logic" (docs/worklog.md, the timing
#: session).
AS_LOW_MCLK = 18
#: The arbiter's own propagation, in master clocks, from the Z80 letting go of the bus
#: to /BUSAK being seen on the 68000's side.  Zilog UM0080 says *when* the Z80 lets go
#: and nothing about the board around it; nothing hardware in the pinned material
#: measures this direction.  The tier rule therefore lets the die-derived model answer,
#: and Nuked-MD does: over 122 grants it takes 68-118 MCLK (9.71-16.86 68000 cycles,
#: mean 12.34) from the 68000's write to $A11100 to /BUSAK, where the machine-cycle
#: rule alone takes 10-37 MCLK (mean 4.15).  This constant is the difference of the two
#: means, 8.19 68000 cycles, rounded to 57 MCLK; **die-derived and fitted**, not
#: measured on hardware (docs/worklog.md, the timing session).
BUSACK_PROPAGATION_MCLK = 57


#: The arbitration of one Z80 access over the 68000's bus, as the die model times it
#: (Nuked-MD, all 24,343 bank-window and VDP accesses of Overdrive 2's first 80
#: frames; docs/worklog.md, "The arbiter, measured"), from the Z80's /MREQ at ``r``: a
#: 68000 cycle that begins by ``r`` + :data:`CUTOFF_MCLK` still goes first; the Z80's
#: own cycle on the 68000's bus begins :data:`GRANT_GAP_MCLK` after the last of them
#: ends and no sooner than :data:`IDLE_GRANT_MCLK` after ``r``, on the next of the
#: 68000's clock edges (the 7- and 14-MCLK "late" grants are that edge); it ends on
#: the first Z80 clock edge ``r`` + :data:`RELEASE_MCLK` + 15w (w >= 2 wait states) at
#: least :data:`OWN_CYCLE_MCLK` after it began; and the 68000's next cycle waits for
#: the first of its clock edges :data:`RESUME_MCLK` after that. It predicts the die's
#: wait exactly for 94.3 % of the accesses, Kabuto's arithmetic for 76.8 %; in nearly
#: all the rest (97.8 % together) the grant is right and the Z80's own cycle is a ROM
#: read that took the cartridge refresh, which the board's refresh grid supplies at
#: run time. Die-derived. MoD's logic-analyser figures (hardware-captured) judge it: it
#: gives his "2-3" with the 68000 stopped and his 2-4, mean just under 3, with it in
#: ``jmp (a0)``; his ``pop`` case, the second read "at least 4", it does not (mean 2.2,
#: as Kabuto's arithmetic before it; ``scripts/run_nuked_md.py arbiter``).
CUTOFF_MCLK = 21
GRANT_GAP_MCLK = 17
IDLE_GRANT_MCLK = 29
OWN_CYCLE_MCLK = 30
RELEASE_MCLK = 37
RESUME_MCLK = 25
MIN_WAIT = 2
REFRESH_MCLK = 14
#: Where the 68000's clock edges fall, mod 7 MCLK: where its cycles begin
#: (``system.M68K_POWER_ON_PHASE``, the die's relation in the board's time).
M68K_EDGE_PHASE = 4


def z80_access_wait(request: int, m68k_cycles: list[list[int]],
                    refresh: int | None = None) -> tuple[int, int, int]:  # fmt: skip
    """For a Z80 access over the 68000's bus with /MREQ at ``request`` (MCLK), given
    the 68000's bus cycles as ``[start, span]`` and the cartridge refresh still owed
    (``refresh``, the clock it fell due, or None): the Z80's wait states, the master
    clock from which the 68000 may start its next cycle, and when the Z80's own cycle
    began. A refresh owed by then is taken by the Z80's own cycle, which takes
    :data:`REFRESH_MCLK` longer -- Kabuto's ``b``, "The Z80 bus access itself can be
    delayed as well by what seems to be refresh logic (up to 2 more cycles)"; on the
    die with the 68000 stopped a quarter of the Z80's reads, one per refresh period."""
    start = request + IDLE_GRANT_MCLK
    for begin, span in m68k_cycles:
        if begin > request + CUTOFF_MCLK:
            break
        if begin + span > request:
            start = max(start, begin + span + GRANT_GAP_MCLK)
    start += (M68K_EDGE_PHASE - start) % 7  # on one of the 68000's clock edges
    length = OWN_CYCLE_MCLK
    if refresh is not None and refresh <= start:
        length += REFRESH_MCLK
    wait = MIN_WAIT
    while request + RELEASE_MCLK + 15 * wait < start + length:
        wait += 1
    end = request + RELEASE_MCLK + 15 * wait
    return wait, end + RESUME_MCLK, start


def busack_grant(request: int, machine_cycle_ends: list[int]) -> int:
    """The master clock /BUSAK reaches the 68000, given the request's clock and the
    ends of the Z80 step's machine cycles.

    Two things in one: Zilog UM0080's rule for *when* the Z80 lets go (the end of the
    machine cycle in progress; specification tier) and the arbiter's own propagation
    for how long the board takes to say so (:data:`BUSACK_PROPAGATION_MCLK`; fitted to
    Nuked-MD, die-derived).
    """
    for end in machine_cycle_ends:
        if end >= request:
            return end + BUSACK_PROPAGATION_MCLK
    return request + BUSACK_PROPAGATION_MCLK
