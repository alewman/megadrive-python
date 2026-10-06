"""Quick checks of the board's tables and rules, each against its source."""

from __future__ import annotations

from megadrive_python.board import CART_REFRESH_MCLK, CART_REFRESH_STALL, RAM_REFRESH_STALL
from megadrive_python.machine import MACHINES, MCLK_PER_LINE
from megadrive_python.vdp import (
    H32,
    H40,
    H40_LONG,
    H40_SHORT,
    H40_VSYNC,
    HV_UNITS,
    V28_NTSC,
    V28_PAL,
    V30_NTSC,
    V30_PAL,
    hv_word,
)


def test_lines_are_3420_mclk():
    # Jorge Nuno's measurement (docs/timing.md, "The line and the frame").
    for mode in (H32, H40, H40_VSYNC):
        assert mode.length == MCLK_PER_LINE
    # The interlaced odd field's first and last VSYNC lines (Nemesis's captures).
    assert H40_LONG.length == MCLK_PER_LINE + 30
    assert H40_SHORT.length == MCLK_PER_LINE - 30


def test_h_counter_sequences():
    # Kabuto's table of Nemesis's captures (docs/timing.md): $00-$93, $E9-$FF in H32;
    # $00-$B6, $E4-$FF in H40, as the port shows them (H >> 1).
    assert sorted({h >> 1 for h in H32.hvals}) == [*range(0x00, 0x94), *range(0xE9, 0x100)]
    assert sorted({h >> 1 for h in H40.hvals}) == [*range(0x00, 0xB7), *range(0xE4, 0x100)]
    assert len(H32.hvals) == 342
    assert len(H40.hvals) == 420


def test_v_counter_sequences():
    def frame(vmode, interlace=False, odd=False):
        v, seen = 0, []
        while True:
            seen.append(v)
            v = vmode.next(v, interlace, odd)
            if v == 0:
                return seen

    assert len(frame(V28_NTSC)) == 262
    assert len(frame(V28_PAL)) == 313
    assert len(frame(V30_PAL)) == 313
    assert len(frame(V30_NTSC)) == 512  # $000-$1FF: the picture rolls
    # An odd interlaced field has one more line (Nemesis, forum t=768).
    assert len(frame(V28_NTSC, True, True)) == len(frame(V28_NTSC, True, False)) + 1


def test_vsync_lines():
    short, long = V28_PAL.vsync_lines(False, False)
    assert short == long == {0x100, 0x101, 0x102, *range(0x1CA, 0x1D0)}
    short, long = V28_PAL.vsync_lines(True, True)
    assert short - long == {0x1CF}
    assert long - short == {0x0FF}
    assert V30_NTSC.vsync_lines(False, False) == (frozenset(), frozenset())


def test_hv_word_interlace():
    # CMD genvdp.txt: "VC0 is replaced with VC8 when in interlace mode".
    assert hv_word(0x123, 0x10A, False, False) == 0x2385
    assert hv_word(0x123, 0x10A, True, False) == 0x2385 & 0xFEFF | 0x0100
    assert hv_word(0x0C0, 0x000, True, True) == 0x8100


def test_hv_units():
    pal, ntsc = HV_UNITS["pal-model1"], HV_UNITS["ntscj-model1"]
    # A step carrying past bit 1 reads with the clearing bits cleared, the setting one not.
    assert pal.h_at_edge("H32", 0x04B, 0x04C) == 0x048
    assert pal.h_at_edge("H32", 0x089, 0x08A) == 0x08A
    assert pal.h_at_edge("H40", 0x023, 0x024) == 0x024
    assert ntsc.h_at_edge("H40", 0x023, 0x024) == 0x020
    assert pal.h_at_edge("H40 long", 0x1E9, 0x1EA) == 0x1E9
    assert MACHINES["megadrive"].hv_reads == "pal-model1"
    assert MACHINES["genesis"].hv_reads == "ntscj-model1"


def test_refresh_constants():
    # Kabuto: every 128 68000 cycles the next access is slowed by 2 cycles.
    assert CART_REFRESH_MCLK == 128 * 7
    assert CART_REFRESH_STALL == 2 * 7
    assert RAM_REFRESH_STALL == 3 * 7


def test_a_68000_access_to_the_z80s_space_costs_one_extra_clock():
    """Nuked-MD holds every $A00000-$A0FFFF bus cycle 7 MCLK longer, and so does MAME
    (``before_delay(1)``); nothing hardware covers it, so the die-derived model judges
    (docs/worklog.md, the timing session).  The clock is charged after the access, so
    the access still lands at the clock its bus cycle began at, and it is counted apart
    for the lockstep, which has to compare it with MAME's own count."""
    from megadrive_python.board import Z80_SPACE_STALL
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.system import System

    assert Z80_SPACE_STALL == 7  # one 68000 clock
    system = System(MACHINES["genesis"], Cartridge(bytes(0x10000)))
    board = system.board
    board.z80_reset = False
    board.z80_bus_requested = True
    board.arbiter_enabled = False
    for kind, call in (
        ("read_byte", lambda: board.read_byte(0xA00000)),
        ("read_word", lambda: board.read_word(0xA00000)),
        ("write_byte", lambda: board.write_byte(0xA00000, 0)),
        ("write_word", lambda: board.write_word(0xA00000, 0)),
    ):
        board.stall = board.z80_space_stall = 0
        call()
        assert board.stall == Z80_SPACE_STALL, kind
        assert board.z80_space_stall == Z80_SPACE_STALL, kind
    # Work RAM, by contrast, is charged nothing beyond its refresh.
    board.stall = board.z80_space_stall = 0
    board.read_word(0xFF0000)
    assert board.z80_space_stall == 0


def test_a_refresh_on_the_z80s_space_costs_one_clock_unless_it_is_late():
    """The cartridge refresh falling on a 68000 access to $A00000-$A0FFFF holds it one
    68000 clock, not the cartridge's two -- and none if it fell due within the last
    clock before /AS, though it is used up either way: Nuked-MD's Overdrive 2 start-up,
    392 and 127 of its 519 refreshes on Z80-RAM writes (docs/worklog.md, "The one-frame
    slip")."""
    from megadrive_python.board import Z80_REFRESH_STALL, Z80_SPACE_STALL
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.system import System

    system = System(MACHINES["genesis"], Cartridge(bytes(0x10000)))
    board = system.board
    board.z80_reset = False
    board.z80_bus_requested = True
    board.arbiter_enabled = False
    for late, stretch in ((14, Z80_REFRESH_STALL), (7, Z80_REFRESH_STALL), (3, 0)):
        board.stall = 0
        t = board.m68k_access_clock()
        board.cart_refresh_due = t - late
        board.write_byte(0xA00000, 0)
        assert board.stall == Z80_SPACE_STALL + stretch, late
        assert board.cart_refresh_due > t, late  # used up either way


def test_capture_loop_rate():
    """The refresh rules give the capture loop its hardware rate, 143.588 MCLK per
    MOVE.L (A0),(A1)+ (docs/timing.md, "Bus sharing")."""
    from run_hv_captures import sample_times

    times = sample_times(0, 15360, 300, 400)
    per = (times[-2] - times[0]) / (15360 - 1)
    assert abs(per - 143.588) < 0.01


def test_genesis3_vsram():
    assert MACHINES["genesis3"].vdp.vsram_bytes == 0x80
    assert MACHINES["genesis"].vdp.vsram_bytes == 0x50


def test_the_z80_takes_its_bus_back_on_its_own_clock():
    """After the write that ends a bus request, the Z80's next access falls on the first
    edge of its clock (6 MCLK past a multiple of 105 here) at least 42 MCLK after the
    write began; after a reset release, 57 (Nuked-MD; docs/worklog.md, "The Z80 after a
    hold")."""
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import Z80_DIVIDER
    from megadrive_python.system import (
        Z80_CLOCK_PHASE,
        Z80_RESET_RESUME_MCLK,
        Z80_RESUME_MCLK,
        System,
    )

    for reset, wait in ((False, Z80_RESUME_MCLK), (True, Z80_RESET_RESUME_MCLK)):
        for t in range(1_000_000, 1_000_105):
            system = System(MACHINES["genesis"], Cartridge(bytes(0x10000)))
            system.z80_mclk = 0
            system._z80_resume(t, reset)
            fetch = system.z80_mclk + Z80_DIVIDER  # T1 of the M1 cycle, then its access
            assert fetch % Z80_DIVIDER == Z80_CLOCK_PHASE % Z80_DIVIDER, (reset, t)
            assert t + wait <= fetch < t + wait + Z80_DIVIDER, (reset, t)


def test_an_interrupted_z80_instruction_resumes_as_a_whole():
    """An instruction the 68000's bus request cut after one of its machine cycles moves
    as a whole: whatever follows the cut -- an access or an internal T-state -- comes at
    the Z80's resume edge, and the internal T-states after it stay in place (the die: a
    fetch, the release, then the instruction's internal T-states before its next read;
    docs/worklog.md "The bus holds")."""
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import Z80_DIVIDER
    from megadrive_python.system import Z80_CLOCK_PHASE, Z80_RESUME_MCLK, System

    system = System(MACHINES["genesis"], Cartridge(bytes(0x10000)))
    t = 1_000_000
    start, let_go = t - 200, t - 140  # cut at the end of its first machine cycle
    system._z80_partial = (start, let_go)
    system._z80_resume(t, False)
    edge = t + Z80_RESUME_MCLK + (Z80_CLOCK_PHASE - t - Z80_RESUME_MCLK) % Z80_DIVIDER
    assert system.board.z80_hold == (let_go, edge - Z80_DIVIDER - let_go)


def test_the_work_ram_refresh_is_hidden_when_the_ram_is_idle():
    """Nuked-MD (docs/worklog.md "The work-RAM refresh"): a work-RAM access within 98
    MCLK of the refresh falling due takes it and waits 21; otherwise it runs by itself
    112-133 MCLK after, an access then waiting for its end, and an idle RAM pays
    nothing. The next falls due 819 after whichever began."""
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.system import System

    board = System(MACHINES["genesis"], Cartridge(bytes(0x10000))).board
    board.arbiter_enabled = False
    board.cart_refresh_due = 1 << 62

    def stall_at(t: int, due: int, address: int = 0xFF0000) -> tuple[int, int]:
        board.ram_refresh_due, board.stall = due, 0
        board.m68k_access_clock = lambda: t
        board._bus_cycle(address)
        return board.stall, board.ram_refresh_due

    assert stall_at(999, 1000) == (0, 1000)  # not yet due
    assert stall_at(1000, 1000) == (21, 1819)  # takes it
    assert stall_at(1091, 1000) == (21, 1910)  # still takes it, 91 after
    assert stall_at(1098, 1000) == (35, 1931)  # too late: waits for the one at 1,112
    assert stall_at(1126, 1000) == (7, 1931)  # one under way: waits for its end
    assert stall_at(1133, 1000) == (0, 1931)  # it ran by itself, unseen
    assert stall_at(1931 + 931 * 3, 1000) == (21, 1931 + 931 * 3 + 819)
    assert stall_at(1000, 1000, address=0xC00000) == (0, 1000)  # not the RAM


def test_the_dot_clock_runs_slow_in_h40s_long_pixels():
    """Nuked-MD (scripts/run_refresh_tie.py, docs/worklog.md "The tie at 112"): the dot
    clock the VDP's logic runs on changes every 2 MCLK but in two windows of 30 edges in
    each run of long pixels, 3 and 2 MCLK apart -- 1,680 edges on an H40 line, the
    windows at 316, 399, 482 and 565 MCLK into it."""
    from megadrive_python.vdp import _dot_clock_edges

    edges = sorted(_dot_clock_edges(H40))
    assert len(edges) == 1680
    gaps = [b - a for a, b in zip(edges, edges[1:])]
    assert gaps.count(3) == 60 and set(gaps) == {2, 3}
    assert [a for a, b in zip(edges, edges[1:]) if b - a == 3][::15] == [316, 399, 482, 565]
    assert len(_dot_clock_edges(H40_VSYNC)) == 1680  # the second run moved, not changed


def test_a_refresh_due_a_clock_after_the_as_is_taken_on_a_dot_clock_edge():
    """The die's tie: a refresh due 7 MCLK after a work-RAM access's /AS is taken by it
    when the dot clock changes at the /AS (the nuked-md machine only)."""
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.system import System

    for name, expected in (("nuked-md", True), ("genesis", False)):
        board = System(MACHINES[name], Cartridge(bytes(0x10000))).board
        board.arbiter_enabled = False
        board.cart_refresh_due = 1 << 62
        vdp = board.vdp
        taken = {}
        for edge in (True, False):
            vdp.dot_clock_edge = lambda t, edge=edge: edge and vdp._dma_die
            board.ram_refresh_due, board.stall = 1007, 0
            board.m68k_access_clock = lambda: 1000
            board._bus_cycle(0xFF0000)
            taken[edge] = board.stall == 21
        assert taken == {True: expected, False: False}
