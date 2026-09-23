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
    bank_access_delay,
    busack_grant,
)
from megadrive_python.cartridge import Cartridge
from megadrive_python.machine import M68K_DIVIDER, MACHINES, MCLK_PER_LINE
from megadrive_python.system import System
from megadrive_python.vdp import VDP


def test_kabutos_formula_gives_mods_measured_wait_states() -> None:
    """MoD's two logic-analyser cases fall out of Kabuto's footnote 1.

    With the 68000 off the bus (``a`` = 1, no refresh delay) the Z80 waits "2-3"
    cycles, depending only on how the clocks line up; when the request lands inside a
    68000 bus cycle so that the grant takes a cycle longer, "there will be 4 wait
    states"; and the 68000's own delay is "around 11 68k cycles" (Kabuto).
    """
    idle = {bank_access_delay(subcycle, 1, False)[0] for subcycle in range(7)}
    assert idle == {2, 3}
    assert {bank_access_delay(subcycle, 4, False)[0] for subcycle in range(7)} == {4}
    delays = [bank_access_delay(s, a, False)[1] for s in range(7) for a in range(1, 5)]
    assert 10 <= sum(delays) / len(delays) <= 12


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


def test_the_68000_holds_the_bus_only_while_as_is_low() -> None:
    """A 68000 bus cycle is four clocks but the bus is the 68000's only while /AS is
    asserted (68000 UM, "Bus Arbitration"), which Nuked-MD measures as 18 of the 28
    MCLK.  So Kabuto's ``a`` is 3 at the start of a cycle, 1 at its end, and 1 again in
    the 10 MCLK before the next one begins -- never the 4, 5 and 6 the board charged
    while it held the whole cycle and looked ahead to the next
    (docs/worklog.md, the timing session)."""
    system = System(MACHINES["genesis"], Cartridge(bytes(0x10000)))
    board = system.board
    board.m68k_accesses = [[0, AS_LOW_MCLK], [28, AS_LOW_MCLK]]
    assert board._m68k_release(0) == 3  # the whole of the first cycle is left
    assert board._m68k_release(14) == 1  # 4 MCLK of it left
    assert board._m68k_release(18) == 1  # /AS has risen: the bus is free
    assert board._m68k_release(24) == 1  # still free, 4 MCLK before the next cycle
    assert board._m68k_release(28) == 3  # the second cycle has begun
    # A refresh stretch lengthens the cycle, which is Kabuto's "up to 6 cycles if the
    # last access is delayed by the refresh logic".
    board.m68k_accesses = [[0, AS_LOW_MCLK + 21]]
    assert board._m68k_release(0) == 6


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
    """Kabuto's fast-transfer formula is ``words * 2.4 + 5.6`` 68000 cycles, so a
    one-word transfer costs eight.  The board had no mechanism for his +5.6 and charged
    about one cycle (milestone 5, open item 15); Nuked-MD then measured 11.0 for the
    same transfer and fitted +8.0, confirming that the start-up is real
    (milestone 6).  The hardware source is the judge, so the board charges his own
    5.6 cycles (:data:`~megadrive_python.vdp.DMA_STARTUP_MCLK`, 39 MCLK)."""
    import run_dma_cost as harness

    from megadrive_python.vdp import DMA_STARTUP_MCLK

    assert round(5.6 * M68K_DIVIDER) == DMA_STARTUP_MCLK  # Kabuto's own constant
    system = System(MACHINES["genesis"], Cartridge(bytes(0x10000)))
    system.vdp.rendering = False
    one_word = harness.transfer_cost(system, 0x23, 1, 0x00, False, 0x81) / M68K_DIVIDER
    assert 5.6 <= one_word <= 11.0  # Kabuto's 8.0 and the referee's 11.0 bracket it


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
    """A CRAM write during the active display shows the colour written for the pixel of
    its access slot (docs/undocumented-behavior.md, "The VDP" item 5), and one made in
    the horizontal blank does not."""
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
    assert 118 <= x <= 135, x  # the next access slot after pixel 120
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
