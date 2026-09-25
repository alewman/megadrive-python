"""The full-screen vertical scroll latch: once a line, at the end of the line before.

The judge is Nuked-MD (die-derived; nothing hardware-captured covers the question):
``scripts/run_vscroll_latch.py`` measures the latch points on it and checks the board
against its pictures. These tests hold the board to the rule it measured, without the
referee.
"""

from __future__ import annotations

import sys

from conftest import ROOT

sys.path.insert(0, str(ROOT / "scripts"))


def _vdp():
    from megadrive_python.board import Board
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES
    from megadrive_python.vdp import H40

    vdp = Board(MACHINES["genesis"], Cartridge(b"")).vdp
    vdp.hmode = H40
    vdp.line_start = vdp.mclk = 0
    return vdp


def _at(vdp, h: int) -> None:
    vdp.mclk = vdp.line_start + vdp.hmode.offset_of[h]


def test_a_write_before_the_latch_reaches_the_next_line() -> None:
    vdp = _vdp()
    _at(vdp, 0x142)  # the last active slot before both latches
    vdp._vsram_store(0, 0x55)
    vdp._vsram_store(1, 0x66)
    vdp._next_line()
    assert vdp.vscroll_latch == [0x55, 0x66]


def test_a_write_in_the_blanking_before_a_line_waits_for_the_line_after() -> None:
    """The board's line runs from the V increment ($14A) round to $149, so a write in
    the horizontal blank at its start (an H interrupt handler's) lands after the latch
    that line is drawn from: the line keeps the old value and the next one gets the
    new. Before the latch, the board drew such a write into the same line."""
    vdp = _vdp()
    vdp.vsram[0] = 0x11
    vdp._next_line()
    assert vdp.vscroll_latch[0] == 0x11
    _at(vdp, 0x1CE)  # the horizontal blank, before this line's display
    vdp._vsram_store(0, 0x22)
    assert vdp.vscroll_latch[0] == 0x11  # what this line is drawn from
    vdp._next_line()
    assert vdp.vscroll_latch[0] == 0x22


def test_plane_b_latches_six_pixels_before_plane_a() -> None:
    """$143 for B and $149 for A in H40: a write at $146 (an idle line's slot) reaches
    A's latch and misses B's."""
    vdp = _vdp()
    _at(vdp, 0x146)
    vdp._vsram_store(0, 0x31)
    vdp._vsram_store(1, 0x32)
    vdp._next_line()
    assert vdp.vscroll_latch == [0x31, 0]


def test_no_line_changes_plane_row_part_way_along() -> None:
    """The measuring cartridge sweeps its VSRAM write across the whole line; before the
    latch, the board drew the start of a line from one value and the rest from the
    next."""
    from run_vscroll_latch import cartridge

    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES
    from megadrive_python.system import System

    system = System(MACHINES["genesis"], Cartridge(cartridge("A", True)))
    system.board.fm.enabled = False
    for _ in range(9):
        system.run_frame()
    rows = system.vdp.renderer.frame_rgb()[:224]
    colours = [{p for p in row if p in (0xFF0000, 0x00FF00)} for row in rows]
    assert all(len(seen) == 1 for seen in colours)
    assert {0xFF0000} in colours and {0x00FF00} in colours


def _two_cell():
    vdp = _vdp()
    vdp.registers[11] = 0x04  # 2-cell column vertical scroll
    return vdp


def test_a_column_write_before_its_latch_reaches_the_same_line() -> None:
    """Plane A's column 10 is latched at the board's H $09E in H40 (the die's 161)."""
    vdp = _two_cell()
    _at(vdp, 0x092)  # a slot before column 10's latch
    vdp._vsram_store(20, 0x40)
    assert vdp.column_scroll(20) == 0x40


def test_a_column_write_after_its_latch_waits_a_line() -> None:
    vdp = _two_cell()
    vdp.vsram[20] = 0x11
    _at(vdp, 0x0A2)  # the slot after column 10's latch
    vdp._vsram_store(20, 0x22)
    assert vdp.column_scroll(20) == 0x11  # what this line's column 10 is drawn from
    vdp._next_line()
    assert vdp.column_scroll(20) == 0x22


def test_the_line_is_not_drawn_into_a_column_still_to_be_latched() -> None:
    """Plane B's column k is latched six pixels into it (H40), after the board has
    begun drawing it: a write landing before the latch holds the drawing at the
    column's first pixel, so the whole column takes the new value, as on the die."""
    from megadrive_python.vdp import WRITE_VSRAM

    vdp = _two_cell()
    _at(vdp, 0x0A2)  # column 10 begins at pixel 160; plane B latches it at $0A6
    assert vdp._column_limit(WRITE_VSRAM, 2 * 21) == 160
    _at(vdp, 0x0B2)
    assert vdp._column_limit(WRITE_VSRAM, 2 * 21) is None
    vdp.registers[11] = 0x00
    _at(vdp, 0x0A2)
    assert vdp._column_limit(WRITE_VSRAM, 2 * 21) is None  # full-screen: no columns
