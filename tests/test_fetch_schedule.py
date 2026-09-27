"""The planes' fetch schedule: a change reaches only the tiles not yet fetched.

The judge is Nuked-MD's own VRAM accesses (``NMD_VRAMSIG``; docs/worklog.md, "Where
tiles are fetched"): in die H, 2-cell group g's plane A name at 16g + 3 and patterns at
16g + 7 and 9, plane B's name at 16g + 11 and patterns at 16g + 15 and 17; the die's
H40 counter runs three above the board's. These tests hold the renderer to it without
the referee.
"""

from __future__ import annotations


def _renderer(h40: bool = True):
    from megadrive_python.board import Board
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES
    from megadrive_python.vdp import H32, H40

    vdp = Board(MACHINES["genesis"], Cartridge(b"")).vdp
    vdp.registers[1] = 0x44  # display on, mode 5
    vdp.registers[12] = 0x81 if h40 else 0x00
    vdp.hmode = H40 if h40 else H32
    vdp.renderer.begin_line(10)
    return vdp.renderer


def test_a_name_change_freezes_every_group_whose_name_is_fetched() -> None:
    renderer = _renderer()
    renderer._fetch_until(130, names=True, limit=None)
    # plane A: group 8's name at 128 is fetched, group 9's at 144 not
    assert renderer._fetched[0] == 144
    # plane B: its names come eight later -- group 7's at 120, group 8's at 136
    assert renderer._fetched[1] == 128


def test_a_pattern_change_freezes_only_the_cells_whose_pattern_is_fetched() -> None:
    renderer = _renderer()
    renderer._fetch_until(130, names=False, limit=None)
    # cell 15's pattern at 118 is fetched, cell 16's at 132 not
    assert renderer._fetched[0] == 128
    # plane B: cell 14's at 124 (group 7 + 12), cell 15's at 126, cell 16's at 140
    assert renderer._fetched[1] == 128


def test_h32_fetches_three_pixels_later_in_the_boards_counter() -> None:
    """The die's schedule is the same in both modes; only H40's counter is offset, so
    in the board's H32 counter group 8's name is fetched at 131, not 128."""
    renderer = _renderer(h40=False)
    renderer._fetch_until(130, names=True, limit=None)
    assert renderer._fetched[0] == 128
    renderer = _renderer(h40=False)
    renderer._fetch_until(131, names=True, limit=None)
    assert renderer._fetched[0] == 144


def test_fine_scroll_moves_the_groups_on_the_screen() -> None:
    renderer = _renderer()
    renderer._fine = (5, 0)
    renderer._fetch_until(130, names=True, limit=None)
    # the same fetches, each group five pixels further right
    assert renderer._fetched[0] == 144 + 5
