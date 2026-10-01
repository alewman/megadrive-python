"""Milestone 5's acceptance: bus timing.

What is checked here without fetched material: Kabuto's footnote-1 arithmetic against
the cases MoD's logic analyser reports (forum t=1661), the arbiter's delays measured on
the board with his two experiments, the DMA's cost per raster line against the Manual's
"DMA transfer capacity" table, the bus grant waiting for the end of the Z80's machine
cycle, and the per-slot renderer's two effects -- a write landing inside the line being
drawn, and a CRAM dot. VDPFIFOTesting's timing tests are in ``test_milestone2.py``
(they are part of its 122); Overdrive 2 and MoD's ``gen_test_z80_bus`` need fetched or
commercial images and are run by hand (docs/worklog.md).
"""

from __future__ import annotations

import pytest
from conftest import ROOT  # noqa: F401  (puts scripts/ on the path)

from megadrive_python.arbiter import (
    AS_LOW_MCLK,
    BUSACK_PROPAGATION_MCLK,
    busack_grant,
    z80_access_wait,
)
from megadrive_python.cartridge import Cartridge
from megadrive_python.machine import M68K_DIVIDER, MACHINES, MCLK_PER_LINE
from megadrive_python.system import System
from megadrive_python.vdp import VDP


def test_a_z80_access_waits_as_the_die_times_it() -> None:
    """On an idle bus the Z80 waits 2 (MoD: "2-3"); a 68000 cycle that begins up to 21
    MCLK after the Z80's request still goes first, and the Z80's cycle begins 17 MCLK
    after it ends; one that begins later does not; an owed refresh lengthens the
    Z80's own cycle by 14 (Nuked-MD, docs/worklog.md "The arbiter, measured")."""
    assert z80_access_wait(1000, [])[0] == 2
    assert z80_access_wait(1000, [], refresh=1000)[0] == 3
    assert z80_access_wait(1000, [[1021, AS_LOW_MCLK]])[0] == 4  # granted at 1,056
    assert z80_access_wait(1000, [[1022, AS_LOW_MCLK]])[0] == 2
    # Ending 8 MCLK in, the 68000's cycle leaves the grant at its floor, 29 MCLK after
    # the request, on the next 68000 clock edge (6 past a multiple of 7).
    wait, resume, own = z80_access_wait(1000, [[990, AS_LOW_MCLK]])
    assert (own, wait, resume) == (1035, 2, 1000 + 37 + 30 + 25)


def test_bus_grant_waits_for_the_end_of_the_machine_cycle() -> None:
    """Two rules in one, as the timing session settled it.

    Zilog UM0080: BUSREQ is sampled "with the rising edge of the last clock period of
    any machine cycle", so the Z80 lets go at the first machine-cycle end at or after
    the request (and at once if the request is past the last one). The arbiter then
    takes ``BUSACK_PROPAGATION_MCLK`` to report it -- fitted to Nuked-MD, which
    measures the whole $A11100-write-to-/BUSAK delay 7.15 68000 cycles longer than the
    specification's rule alone gives (docs/worklog.md, the timing session).
    """
    ends = [60, 105, 150]
    p = BUSACK_PROPAGATION_MCLK
    assert busack_grant(0, ends) == 60 + p
    assert busack_grant(60, ends) == 60 + p
    assert busack_grant(61, ends) == 105 + p
    assert busack_grant(200, ends) == 200 + p


def test_the_arbiter_charges_mods_measured_delays() -> None:
    """MoD's two experiments run on the board (``scripts/run_bus_arbiter.py``): with
    the 68000 in ``stop #$2700`` the Z80 waits 2 to 3 cycles, and with it in his ``jmp
    (a0)`` loop the range widens and the mean rises, staying inside Kabuto's 2 to 5
    with a 68000 delay near his 11."""
    import run_bus_arbiter as harness

    stopped = harness.run(harness.Z80_READ, harness.M68K_STOP, 300)
    assert len(stopped) == 300
    assert {z80 for z80, _ in stopped} == {2, 3}
    spinning = harness.run(harness.Z80_READ, harness.M68K_SPIN, 300)
    z80 = [value for value, _ in spinning]
    m68k = [value for _, value in spinning]
    assert min(z80) >= 2 and max(z80) <= 5
    assert sum(z80) / len(z80) > sum(value for value, _ in stopped) / len(stopped)
    assert 9 <= sum(m68k) / len(m68k) <= 13


def test_a_z80_access_costs_a_busy_68000_only_what_it_overlaps() -> None:
    """A 68000 inside a multiply is not on the bus, so a Z80 bank read granted then
    costs it only what runs past the multiply: ``mulu.w d0,d0`` / ``bra.s`` loses about
    3 of its cycles to each read where MoD's always-on-the-bus ``jmp (a0)`` loses about
    11 (Nuked-MD in Overdrive 2's 3D scene; docs/worklog.md "The ninja's arm colours")."""
    import run_bus_arbiter as harness

    multiplying = [
        m68k for _, m68k in harness.run(harness.Z80_READ, bytes.fromhex("c0c060fc"), 300)
    ]
    spinning = [m68k for _, m68k in harness.run(harness.Z80_READ, harness.M68K_SPIN, 300)]
    assert min(multiplying) == 0
    assert sum(multiplying) / len(multiplying) < 5 < 9 < sum(spinning) / len(spinning)


@pytest.mark.parametrize(
    ("register12", "display", "expected"),
    [(0x00, True, 16), (0x00, False, 167), (0x81, True, 18), (0x81, False, 205)],
)
def test_dma_moves_the_manuals_bytes_per_line(register12: int, display: bool,
                                              expected: int) -> None:  # fmt: skip
    """The Manual's "DMA transfer capacity" table (PDF page 44) for a 68000-memory to
    VRAM transfer: 16 and 167 bytes a line in H32 with the display on and off, 18 and
    205 in H40. A VRAM word takes two access slots, so these are twice the slots a
    line has for the CPU."""
    import run_dma_cost as harness

    system = System(MACHINES["genesis"], Cartridge(bytes(0x10000)))
    system.vdp.rendering = False
    words = 512
    held = harness.transfer_cost(system, 0x21, words, 0x00, display, register12)
    bytes_per_line = 2 * words * MCLK_PER_LINE / held
    assert abs(bytes_per_line - expected) <= 1


def test_kabutos_dma_slopes() -> None:
    """Kabuto's "DMA speed": a fast transfer (CRAM, VSRAM, VRAM in 128 KB mode) costs
    words x 2.4 68000 cycles and a slow one (VRAM in 64 KB mode) words x 4.7, both on
    top of the instructions' own cost. The board's slopes are within 5 % of his."""
    import run_dma_cost as harness

    for code, mode1, slope in ((0x23, 0x00, 2.4), (0x25, 0x00, 2.4),
                               (0x21, 0x80, 2.4), (0x21, 0x00, 4.7)):  # fmt: skip
        system = System(MACHINES["genesis"], Cartridge(bytes(0x10000)))
        system.vdp.rendering = False
        costs = []
        for words in (64, 128, 256, 512):
            held = harness.transfer_cost(system, code, words, mode1, False, 0x81)
            costs.append((words, held / M68K_DIVIDER))
        measured, _intercept = harness.fit(costs)
        assert abs(measured - slope) / slope < 0.05, (code, mode1, measured)


def test_a_dma_transfer_pays_kabutos_start_up() -> None:
    """Kabuto's fast-transfer formula is ``words * 2.4 + 5.6`` 68000 cycles (hardware-
    measured research); Nuked-MD fits 2.47 and **+8.0** to the same transfers. The
    board charges his own 5.6-cycle start-up (:data:`~megadrive_python.vdp.
    DMA_STARTUP_MCLK`, 39 MCLK) and, since option A, the die's 3-clock hold on the
    write that starts it (:data:`~megadrive_python.vdp.DMA_TRIGGER_HOLD_MCLK`).
    Fitted over 4 to 128 words as his formula is a fit, the board's slope is his within
    2 % and its start-up lies between his and the referee's (docs/worklog.md, "The DMA
    trigger"). One word is not checked alone: one and two words fall on the same slot
    and cost the board 12.0 cycles, above both."""
    import run_dma_cost as harness

    from megadrive_python.vdp import DMA_STARTUP_MCLK

    assert round(5.6 * M68K_DIVIDER) == DMA_STARTUP_MCLK  # Kabuto's own constant
    costs = {}
    for words in (4, 128):
        system = System(MACHINES["genesis"], Cartridge(bytes(0x10000)))
        system.vdp.rendering = False
        costs[words] = harness.transfer_cost(system, 0x23, words, 0x00, False, 0x81) / M68K_DIVIDER
    slope = (costs[128] - costs[4]) / 124
    start_up = costs[4] - 4 * slope
    assert abs(slope - 2.4) <= 0.02 * 2.4
    assert 5.6 <= start_up <= 8.0


def _display_machine() -> System:
    """A board with the VDP in mode 5, H40, the display on and a plain palette."""
    system = System(MACHINES["genesis"], Cartridge(bytes(0x10000)))
    vdp = system.vdp
    mclk = 0
    for number, value in ((0, 0x04), (1, 0x64), (2, 0x30), (4, 0x07), (11, 0x00),
                          (12, 0x81), (13, 0x3F), (15, 0x02), (16, 0x01)):  # fmt: skip
        vdp.write_word(0x04, 0x8000 | (number << 8) | value, mclk)
        mclk += 28
    vdp.sync(mclk + 4 * MCLK_PER_LINE)
    return system


def _line_offset(vdp: VDP, x: int) -> int:
    """The master clock into the current line at which display pixel ``x`` begins."""
    mode = vdp.hmode
    return mode.starts[mode.display_at + x]


def _to_active_line(vdp: VDP) -> int:
    """Advance to just after the next line begins being drawn; return its row."""
    mode = vdp.hmode
    vdp.sync(vdp.line_start + mode.length + mode.offset_of[mode.hblank_clear] + 1)
    assert vdp.renderer.pending
    return vdp.renderer._row


def test_a_cram_write_in_the_display_leaves_a_dot() -> None:
    """A CRAM write during the active display shows the colour written for one pixel
    (docs/undocumented-behavior.md, "The VDP" item 5) -- 21 pixels left of the pixel its
    access slot's H counter names in H40, where the colour output runs behind the
    counter (item 20, measured on Nuked-MD) -- and one made in the horizontal blank
    does not."""
    system = _display_machine()
    vdp = system.vdp
    renderer = vdp.renderer
    row = _to_active_line(vdp)
    at = vdp.line_start + _line_offset(vdp, 120)
    vdp.write_word(0x04, 0xC000, at)  # CRAM address 0, write
    vdp.write_word(0x04, 0x0000, at + 28)
    vdp.write_word(0x00, 0x0EEE, at + 56)  # white
    vdp.sync(vdp.line_start + vdp.hmode.length)
    dots = renderer._dots[row]
    assert len(dots) == 1, dots
    x, colour = dots[0]
    assert 97 <= x <= 114, x  # the next access slot after pixel 120, less 21
    assert colour == 0xFFFFFF

    # The same write during the horizontal blank leaves nothing.
    row = _to_active_line(vdp)
    renderer.finish_line()
    blank = vdp.line_start + vdp.hmode.offset_of[vdp.hmode.hblank_set] + 40
    vdp.write_word(0x04, 0xC000, blank)
    vdp.write_word(0x04, 0x0000, blank + 28)
    vdp.write_word(0x00, 0x0AAA, blank + 56)
    vdp.sync(blank + 200)
    assert renderer._dots[row] == []


def test_a_vram_write_lands_inside_the_line_being_drawn() -> None:
    """The per-slot renderer's point: a name-table word written part way along a line
    changes the pixels after that point and leaves the ones before it alone."""
    system = _display_machine()
    vdp = system.vdp
    renderer = vdp.renderer
    for cell in range(64):  # plane A: every cell names pattern 1
        vdp.vram[0xC000 + cell * 2], vdp.vram[0xC000 + cell * 2 + 1] = 0, 1
    for byte in range(32):  # pattern 1 solid colour 1, pattern 2 solid colour 2
        vdp.vram[32 + byte] = 0x11
        vdp.vram[64 + byte] = 0x22
    vdp.cram[1], vdp.cram[2] = 0x00E, 0x0E0
    renderer.cram_dirty = True
    row = _to_active_line(vdp)
    at = vdp.line_start + _line_offset(vdp, 160)
    # Rewrite the last cell's name to pattern 2, through the data port, half way along
    # the line: cell 39 covers pixels 312-319, which have not been drawn yet.
    vdp.write_word(0x04, 0x404E, at)  # VRAM write at $C04E
    vdp.write_word(0x04, 0x0003, at + 28)
    vdp.write_word(0x00, 0x0002, at + 56)
    vdp.sync(vdp.line_start + vdp.hmode.length)
    pixels = renderer.pixels[row]
    assert pixels[0] == 1, pixels[:8]
    assert pixels[319] == 2, pixels[312:]


def test_data_port_reads_end_where_the_die_lets_them_go() -> None:
    """A burst of CRAM reads in an active H40 line waits for the read-ahead at each
    slot; Nuked-MD lets each go within these windows of the line (MCLK, the board's
    line start; ``scripts/run_read_slots.py``), and never at the end-of-line slot $140
    or $142 the board used to catch (``vdp.READ_RELEASE_MCLK``; docs/worklog.md
    Session 14). Active lines only: in blanking every slot is open."""
    import run_read_slots

    die = [(365, 371), (828, 834), (956, 962), (1084, 1090), (1340, 1346), (1468, 1474),
           (1596, 1602), (1852, 1858), (1980, 1986), (2108, 2114), (2364, 2370),
           (2492, 2498), (2620, 2626), (2876, 2882), (3004, 3010), (3132, 3138),
           (3372, 3378)]  # fmt: skip
    rom = run_read_slots.cartridge(True, True)
    ends = [end for end, stall, v in run_read_slots.board(rom) if stall and 2 <= v < 222]
    assert ends
    outside = [e for e in ends if not any(lo - 8 <= e <= hi + 8 for lo, hi in die)]
    assert len(outside) <= len(ends) // 50, sorted(set(outside))[:10]
