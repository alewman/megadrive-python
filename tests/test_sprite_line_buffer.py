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

from itertools import pairwise

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
    vdp.renderer.latch_scan_cache()


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
    renderer.begin_line(9)  # phase 1 of line 10
    renderer.latch_scan_cache()  # the V step: phase 2, from table A
    vdp.registers[5] = TABLE_B >> 9
    renderer.begin_line(10)
    assert renderer._line_s[16:24] == [0x01] * 8

    # With nothing scanned ahead the line reads the table as it stands, as before.
    renderer._scanned = None
    renderer.begin_line(10)
    assert renderer._line_s[16:24] == [0] * 8


def test_a_transparent_high_priority_tile_stops_the_shadow() -> None:
    """ "If both A+B are low prio, the visible color becomes shadowed" (Kabuto, "S/H"):
    the tile's priority, whether or not its pixel is transparent. Overdrive 2's scene
    at frames 9,750-10,125 has plane B showing through high-priority transparent tiles
    of plane A, drawn normal on the hardware and shadowed on the board before."""
    from megadrive_python.render import SHADE_SHIFT

    vdp = _vdp()
    vdp.registers[2] = 0xC000 >> 10  # plane A names at $C000
    vdp.registers[4] = 0xE000 >> 13  # plane B names at $E000
    vdp.registers[12] = 0x89  # H40, shadow/highlight on
    for row in range(8):  # tile 1 opaque, colour 5; tile 0 left transparent
        vdp.vram[32 + row * 4 : 32 + row * 4 + 4] = b"\x55\x55\x55\x55"
    for cell in range(64):
        vdp.vram[0xC000 + cell * 2] = 0x80  # plane A: tile 0, high priority
        vdp.vram[0xE000 + cell * 2 + 1] = 0x01  # plane B: tile 1, low priority
    renderer = vdp.renderer
    renderer.begin_line(0)
    renderer.finish_line()
    pixel = renderer.pixels[0][100]
    assert pixel & 0x3F == 0x05  # plane B's colour shows through
    assert pixel >> SHADE_SHIFT == 0  # normal, not shadowed


def _run_lines(vdp, count: int, before=None) -> list[tuple[int, bool, int]]:
    """Advance ``count`` lines; ``before(v)`` may write registers as each line starts.
    Returns (V, status VBlank bit, H interrupt counter) per line."""
    seen = []
    for _ in range(count):
        vdp.sync(vdp.line_end)
        if before:
            before(vdp.v)
        seen.append((vdp.v, bool(vdp.status() & 0x08), vdp.hint_counter))
    return seen


def test_v30_before_line_224_and_v28_before_240_keeps_the_border_open() -> None:
    """Kabuto, "V28/V30 tricks": the border is tested against the mode in force as the
    V counter passes 224 and 240, so V30 at 224 and V28 at 240 never start it -- the
    display stays active and the H interrupt counter is never reloaded. Overdrive 2's
    arcade scene (frames 12,154-12,960) runs its raster chain on that."""
    vdp = _vdp()
    vdp.registers[10] = 0xFF

    def trick(v: int) -> None:
        if v == 0xD0:
            vdp._write_register(1, 0x4C)  # V30, display on
        elif v == 0xE8:
            vdp._write_register(1, 0x44)  # V28

    lines = _run_lines(vdp, 300, trick)  # the first frame, from the line before line 0
    after = [(v, vb, hc) for v, vb, hc in lines if 0xE0 <= v <= 0x100]
    assert after and not any(vb for _, vb, _ in after)  # no VBlank: no border
    counts = [hc for _, _, hc in after]
    # Counting down line by line, reloaded only when it reaches 0 and fires -- never
    # reloaded as a border line would.
    assert all(b == a - 1 or (a == 0 and b == 0xFF) for a, b in pairwise(counts))


def test_v28_starts_the_border_at_line_224() -> None:
    vdp = _vdp()
    lines = _run_lines(vdp, 400)
    assert [vb for v, vb, _ in lines if v == 0xDF][-1] is False
    assert [vb for v, vb, _ in lines if v == 0xE0][-1] is True


def test_the_fm_slot_counter_runs_on_under_reset() -> None:
    """Nuked-MD's FM stops for 12 FM clocks after /IC is asserted and then runs on while
    /IC stays low, so the chip's sample phase after a reset follows when the reset began
    (docs/worklog.md, "The Z80 from the reset"). Whether its slot counter restarts at 0
    or only pauses depends on where in the slot the assertion falls: 1-3 FM clocks in,
    it restarts; 0, 4 or 5, it keeps its count and takes up the next slot at the restart
    (Nuked-MD's own ``fm.c`` clocked through every position, and the Overdrive 2 boot's
    assertions; "The one-frame slip", "The FM's restart"). Overdrive 2's Z80 driver waits on timer A
    straight after its reset."""
    from ym2612_python import YM2612
    from ym2612_python import YM2612_DISCRETE as VARIANT
    from ym2612_python.chip import IC_FSM_HOLD, IC_PAUSE_CARRY, MCLK_PER_CYCLE

    fm = YM2612(VARIANT)
    fm.run(1000 * MCLK_PER_CYCLE)
    start = fm.mclk + 17  # 2 FM clocks into the die's slot: a reset
    fm.reset_line(True, start)
    assert fm.mclk == start  # the grid restarts from the assertion
    fm.run(start + 30 * MCLK_PER_CYCLE)
    assert fm.cycles == (30 - IC_FSM_HOLD) % 24  # counting on, after the brief hold
    fm.reset_line(False, fm.mclk)
    fm.run(fm.mclk + 3 * MCLK_PER_CYCLE)
    assert fm.cycles == (30 - IC_FSM_HOLD + 3) % 24  # release resets nothing

    fm = YM2612(VARIANT)
    fm.run(1000 * MCLK_PER_CYCLE)
    before = fm.cycles
    start = fm.mclk + 5  # at a slot's very start: a pause
    fm.reset_line(True, start)
    fm.run(start + 30 * MCLK_PER_CYCLE)
    assert fm.cycles == (before + IC_PAUSE_CARRY + 30 - IC_FSM_HOLD) % 24  # the count goes on


def test_phase_2_takes_y_written_through_the_line_before() -> None:
    """Overdrive 2's rotating boxes change sprite 0's link at the end of a line, into a
    group whose Y they write through the next line, and that line's scan needs them
    all: phase 2 reads at the V step, not as the line began."""
    vdp = _vdp()
    vdp.registers[5] = TABLE_A >> 9
    for row in range(8):
        vdp.vram[32 + row * 4 : 32 + row * 4 + 4] = b"\x11\x11\x11\x11"
    _sprite(vdp, TABLE_A, 0, y=10, x=16, name=0x0001, link=0)
    renderer = vdp.renderer
    renderer.begin_line(9)
    renderer.sprite_cache[0:2] = bytes([0x00, 0x88])  # Y 8, after phase 1, on the line
    renderer.latch_scan_cache()
    ((_, _, _, _, _, _, cell_row, pattern_line),) = renderer._scanned[1]
    assert (cell_row, pattern_line) == (0, 2)  # row 10 of a sprite at Y 8
