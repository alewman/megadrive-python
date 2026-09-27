"""The sprite line buffer and when a line's sprites are read.

Kabuto, "Precise details of sprite rendering" (hardware-measured research): phase 1
scans the cached list "2 H-borders before the raster line", phase 2 reads X and
pattern "1 raster line before", phase 3 renders into a buffer of "7 bits per pixel:
colour ..., palette, priority" where "only transparent pixels (i.e. the 4 bits that
come from tile data are 0; palette and priority are ignored for this check) will get
overwritten with the 7 bits from the current sprite". Overdrive 2's ring scene
(frames 6,540-7,060) shows both: its blue and magenta rings are palette entry 0 of
transparent sprite pixels under the debug register's S-masked mode, and it moves the
table before its last line (docs/worklog.md, "The rings").
"""

from __future__ import annotations

TABLE_A, TABLE_B = 0x1C00, 0x4000


def _vdp():
    from megadrive_python.board import Board
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES
    from megadrive_python.vdp import H40

    vdp = Board(MACHINES["genesis"], Cartridge(b"")).vdp
    vdp.hmode = H40
    vdp.registers[1] = 0x44  # display on, mode 5
    vdp.registers[12] = 0x81  # H40
    return vdp


def _sprite(vdp, table: int, index: int, y: int, x: int, name: int, link: int) -> None:
    """A 1x1 sprite at screen (x, y): in VRAM at ``table`` and in the cache."""
    entry = table + index * 8
    words = (y + 0x80, link, name, x + 0x80)  # size 0: one cell
    for k, word in enumerate(words):
        vdp.vram[entry + 2 * k] = word >> 8
        vdp.vram[entry + 2 * k + 1] = word & 0xFF
    vdp.renderer.sprite_cache[index * 4 : index * 4 + 4] = bytes(
        (words[0] >> 8, words[0] & 0xFF, words[1] >> 8, words[1] & 0xFF)
    )


def test_a_transparent_sprite_pixel_keeps_its_palette_and_the_last_one_wins() -> None:
    vdp = _vdp()
    vdp.registers[5] = TABLE_A >> 9
    # Tile 1: left half colour 3, right half transparent; tile 2: all transparent.
    for row in range(8):
        vdp.vram[32 + row * 4 : 32 + row * 4 + 4] = b"\x33\x33\x00\x00"
    _sprite(vdp, TABLE_A, 0, y=10, x=0, name=0x2001, link=1)  # palette 1, tile 1
    _sprite(vdp, TABLE_A, 1, y=10, x=0, name=0x4002, link=0)  # palette 2, tile 2
    line = [0] * 320
    vdp.renderer._sprites(line, 320, 10, False)
    assert line[:4] == [0x13] * 4  # the first sprite's colour 3 in palette 1
    assert line[4:8] == [0x20] * 4  # transparent twice: the later sprite's palette
    assert line[8] == 0


def test_a_line_takes_the_sprites_scanned_a_line_ahead() -> None:
    """Moving the table after the line before has scanned does not reach the line:
    Overdrive 2 points register 5 elsewhere in the blank before its last line."""
    vdp = _vdp()
    for row in range(8):
        vdp.vram[32 + row * 4 : 32 + row * 4 + 4] = b"\x11\x11\x11\x11"
    _sprite(vdp, TABLE_A, 0, y=10, x=16, name=0x0001, link=0)
    for k in range(8):  # table B's sprite 0: X = 0 and tile 0, which is blank
        vdp.vram[TABLE_B + k] = 0
    renderer = vdp.renderer
    vdp.registers[5] = TABLE_A >> 9
    renderer.begin_line(9)  # scans line 10 from table A
    vdp.registers[5] = TABLE_B >> 9
    renderer.begin_line(10)
    assert renderer._line_s[16:24] == [0x01] * 8

    # With nothing scanned ahead the line reads the table as it stands, as before.
    renderer._scanned = None
    renderer.begin_line(10)
    assert renderer._line_s[16:24] == [0] * 8
