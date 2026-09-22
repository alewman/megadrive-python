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
