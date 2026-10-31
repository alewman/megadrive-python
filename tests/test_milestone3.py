"""Milestone 3's acceptance: the picture.

The judges here are Nemesis's photographs of his MD1600 running his sprite masking ROM
(hardware-captured) and MoD's ``gen_test_z80_bus``, whose expected values are the
hardware's (forum t=3021). The rest of milestone 3's evidence -- CMD's window and
colour tests, r57shell's VDP tests and the lockstep games against MAME's frames -- is
detector work that needs MAME and is run by hand
(``scripts/compare_mame_frames.py``; docs/worklog.md has the runs).

Everything fetched is ignored, so each test skips when its ROM or capture is missing
(``python scripts/fetch_references.py``; the MD1600 captures are fetched by
``scripts/run_sprite_masking.py --fetch``).
"""

from __future__ import annotations

import pytest
from conftest import NEMESIS, ROOT

SPRITE_MASKING = NEMESIS / "SpriteMaskingTestRom.bin"
CAPTURES = ROOT / "reference" / "images"
Z80_BUS = ROOT / "tests" / "test_roms" / "mask-of-destiny" / "gen_test_z80_bus.md"


@pytest.mark.skipif(
    not SPRITE_MASKING.exists() or not (CAPTURES / "MD1600SpriteMaskH32.png").exists(),
    reason="the sprite masking ROM or Nemesis's MD1600 captures are not fetched",
)
@pytest.mark.parametrize("mode", ["h32", "h40"])
def test_sprite_masking_matches_the_md1600(mode: str) -> None:
    """Every one of the nine tests' two result cells reads the same on the board as in
    Nemesis's photograph of his MD1600 (forum t=541 start=30)."""
    import run_sprite_masking as harness

    frame = harness.board_frame("genesis", mode, None)
    width = len(frame[0])
    assert width == (256 if mode == "h32" else 320)
    capture, left, top, cell_w, cell_h = harness.capture_block(mode)
    for index in range(9):
        for cell in range(2):
            board = harness.classify(
                frame,
                harness.RESULT_LEFT[width] + cell * harness.CELL_WIDTH,
                harness.RESULT_TOP + index * harness.CELL_HEIGHT,
                harness.CELL_WIDTH,
                harness.CELL_HEIGHT,
            )
            hardware = harness.classify(
                capture, left + cell * cell_w, top + index * cell_h, cell_w, cell_h
            )
            assert board == hardware, f"test {index + 1}, cell {cell + 1}"


@pytest.mark.skipif(not Z80_BUS.exists(), reason="gen_test_z80_bus is not fetched")
def test_gen_test_z80_bus() -> None:
    """MoD's test ROM: every check it makes passes, the Z80's writes to the 68000's
    work RAM land and its reads of it fail, as he reports from several machines
    (forum t=3021)."""
    import run_z80_bus_test as harness

    lines = harness.run_rom("genesis")
    results = dict(harness.results(lines))
    assert results, lines
    assert all(verdict != "FAIL" for verdict, _ in results.values()), results
    assert results["Z80 writes to work RAM"][1] == "4243"
    assert results["Z80 reads to work RAM"][1] == "FFFF"


def test_the_colour_ramp_is_the_measured_one() -> None:
    """TmEE's oscilloscope levels (forum t=2188), which MAME also uses."""
    from megadrive_python.render import LEVELS

    assert LEVELS[0] == (0, 52, 87, 116, 144, 172, 206, 255)
    assert LEVELS[1] == (0, 29, 52, 70, 87, 101, 116, 130)
    assert LEVELS[2] == (130, 144, 158, 172, 187, 206, 228, 255)


def test_window_bug_is_the_partial_column() -> None:
    """CMD's window bug: with the window on the left and a fine horizontal scroll, the
    two cells before plane A's first whole 2-cell column are fetched from two cells
    further on, so they repeat the column that follows them (``genvdp.txt``)."""
    from megadrive_python.board import Board
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES

    vdp = Board(MACHINES["genesis"], Cartridge(b"")).vdp
    vdp.registers[1] = 0x64  # display on, mode 5
    vdp.registers[2] = 0x30  # plane A at $C000
    vdp.registers[3] = 0x20  # window at $8000
    vdp.registers[4] = 0x07  # plane B at $E000
    vdp.registers[11] = 0x00  # full-screen scroll
    vdp.registers[13] = 0x3F  # scroll table at $FC00
    vdp.registers[16] = 0x01  # 64 x 32 cells
    vdp.registers[17] = 0x04  # window: the left eight cells
    vdp.registers[18] = 0x00
    for cell in range(64):  # plane A: cell N names pattern N
        vdp.vram[0xC000 + cell * 2] = 0
        vdp.vram[0xC000 + cell * 2 + 1] = cell
    fetched: list[tuple[int, int]] = []
    line = [0] * 320
    original = vdp.renderer._plane_columns

    def record(line_out, base, row, size, scroll, im2, window_bug, first, last):
        before = len(vdp.renderer._tiles)
        marks: list[int] = []
        inner = vdp.renderer._tile_row

        def note(address: int, attribute: int, flip: bool):
            marks.append(address // 32)
            return inner(address, attribute, flip)

        vdp.renderer._tile_row = note  # type: ignore[method-assign]
        try:
            original(line_out, base, row, size, scroll, im2, window_bug, first, last)
        finally:
            vdp.renderer._tile_row = inner  # type: ignore[method-assign]
        if base == 0xC000:  # plane A only
            x = first - ((first - scroll) & 7)
            fetched.extend((x + 8 * i, cell) for i, cell in enumerate(marks))
        assert len(vdp.renderer._tiles) >= before

    vdp.renderer._plane_columns = record  # type: ignore[method-assign]
    edge = 8 * 8  # the window covers cells 0-7, so plane A starts at pixel 64
    for scroll, repeats in ((0x0000, False), (0x0006, True)):
        vdp.vram[0xFC00], vdp.vram[0xFC01] = scroll >> 8, scroll & 0xFF
        fetched.clear()
        vdp.renderer._line_a = line
        vdp.renderer._line_b = [0] * 320
        vdp.renderer._spans = vdp.renderer._plane_setup(320, 0, 0, False)
        vdp.renderer._plane_span(0, 320)
        around = [cell for x, cell in fetched if edge - 16 <= x < edge + 16]
        assert len(around) == 4, (scroll, fetched[:8])
        if repeats:
            # The partial column repeats the two cells of the column after it.
            assert around[:2] == around[2:], around
        else:
            assert around == [around[2] - 2, around[3] - 2, around[2], around[3]], around


def test_window_bug_does_not_touch_plane_b() -> None:
    """CMD's window bug is plane A's alone ("the VDP is drawing the 2-cell column
    from plane A"): plane B has no window over it and must fetch its own columns
    undisturbed, scroll or not."""
    from megadrive_python.board import Board
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES

    vdp = Board(MACHINES["genesis"], Cartridge(b"")).vdp
    vdp.registers[1] = 0x64  # display on, mode 5
    vdp.registers[2] = 0x30  # plane A at $C000
    vdp.registers[3] = 0x20  # window at $8000
    vdp.registers[4] = 0x07  # plane B at $E000
    vdp.registers[11] = 0x00  # full-screen scroll
    vdp.registers[13] = 0x3F  # scroll table at $FC00
    vdp.registers[16] = 0x01  # 64 x 32 cells
    vdp.registers[17] = 0x04  # window: the left eight cells
    vdp.registers[18] = 0x00
    for cell in range(64):  # plane B: cell N names pattern N (no window over it)
        vdp.vram[0xE000 + cell * 2] = 0
        vdp.vram[0xE000 + cell * 2 + 1] = cell
    vdp.vram[0xFC00], vdp.vram[0xFC01] = 0x00, 0x06  # plane A: fine scroll 6 (nonzero
    # low bits, the condition the window bug needs) so it is active for this line
    fetched: list[tuple[int, int]] = []
    inner = vdp.renderer._tile_row

    def note(address: int, attribute: int, flip: bool):
        fetched.append((len(fetched), address // 32))
        return inner(address, attribute, flip)

    vdp.renderer._tile_row = note  # type: ignore[method-assign]
    try:
        vdp.renderer._line_a = [0] * 320
        vdp.renderer._line_b = [0] * 320
        vdp.renderer._spans = vdp.renderer._plane_setup(320, 0, 0, False)
        vdp.renderer._plane_span(0, 320, planes=(1,))  # plane B only
    finally:
        vdp.renderer._tile_row = inner  # type: ignore[method-assign]
    cells = [cell for _, cell in fetched]
    assert cells == list(range(40)), cells


def test_invalid_width_stays_on_the_first_name_row_at_any_scroll() -> None:
    """HSZ = 10b: "the first row of the name table is shown for every line of the
    display" (CMD), past row $200 of the invalid height's wrap as well."""
    from megadrive_python.board import Board
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES

    vdp = Board(MACHINES["genesis"], Cartridge(b"")).vdp
    for row, name in ((0, 1), (1, 2)):
        vdp.vram[0xC000 + row * 0x40 + 1] = name
    fetched: list[int] = []
    renderer = vdp.renderer
    inner = renderer._tile_row

    def note(address: int, attribute: int, flip: bool):
        fetched.append(address // 32)
        return inner(address, attribute, flip)

    renderer._tile_row = note  # type: ignore[method-assign]
    renderer._plane_columns([0] * 320, 0xC000, 0x22B, 0x22, 0, False, -1, 0, 8)
    assert fetched == [1]
