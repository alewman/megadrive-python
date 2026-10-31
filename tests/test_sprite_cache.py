"""The sprite cache sees the 17-bit command address, VRAM only its low 16 bits.

Kabuto ("Precise details of sprite rendering", hardware-measured research): the cache
check "is sensitive to the entire 128 KB address range even in 64 KB mode, so when
setting bit 16 for the sprite table address but not for the Write/DMA target address
(or the other way round) the sprite table in memory will get updated but the cache
won't". Nuked-MD (die-derived) agrees on the address: ``reg_addr`` takes bits 16-14
from the second control word's bits 2-0 in mode 5 and increments ``& 0x1ffff``.
Overdrive 2's rain scene depends on it (docs/worklog.md, "The rain"): it zeroes Y and
link in VRAM through $10000 every frame and the cache has to keep them.
"""

from __future__ import annotations

CONTROL, DATA = 4, 0


def _vdp():
    from megadrive_python.board import Board
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES

    vdp = Board(MACHINES["genesis"], Cartridge(b"")).vdp
    clock = [0]

    def write(port: int, value: int) -> None:
        clock[0] += 1_000
        vdp.write_word(port, value, clock[0])

    def settle() -> None:
        clock[0] += 50_000
        vdp.sync(clock[0])

    for register in (0x8104, 0x8C81, 0x8500, 0x8F02):  # mode 5, H40, table at 0, +2
        write(CONTROL, register)
    return vdp, write, settle


def _vram_write(write, address: int, *words: int) -> None:
    write(CONTROL, 0x4000 | (address & 0x3FFF))
    write(CONTROL, (address >> 14) & 7)
    for word in words:
        write(DATA, word)


def test_bit_16_writes_vram_but_not_the_cache() -> None:
    vdp, write, settle = _vdp()
    cache = vdp.renderer.sprite_cache
    _vram_write(write, 0x00000, 0x0080, 0x0F01)  # sprite 0: Y 128, 4x4, link 1
    settle()
    assert bytes(vdp.vram[0:4]) == bytes(cache[0:4]) == b"\x00\x80\x0f\x01"

    _vram_write(write, 0x10000, 0x0000, 0x0000)  # the same four bytes, through $10000
    settle()
    assert bytes(vdp.vram[0:4]) == b"\x00\x00\x00\x00"  # one 64 KB chip: VRAM $0000
    assert bytes(cache[0:4]) == b"\x00\x80\x0f\x01"  # the cache compared all 17 bits


def test_the_increment_carries_into_bit_16_and_wraps_at_128_kb() -> None:
    """$0FFFE + 2 is $10000, which the cache ignores; $1FFFE + 2 is $00000, which it
    takes."""
    vdp, write, settle = _vdp()
    cache = vdp.renderer.sprite_cache
    _vram_write(write, 0x0FFFE, 0x1234, 0x0099)
    settle()
    assert bytes(vdp.vram[0xFFFE:0x10000]) == b"\x12\x34"
    assert bytes(vdp.vram[0:2]) == b"\x00\x99"
    assert bytes(cache[0:2]) == b"\x00\x00"

    _vram_write(write, 0x1FFFE, 0x5678, 0x00AA)
    settle()
    assert bytes(vdp.vram[0xFFFE:0x10000]) == b"\x56\x78"
    assert bytes(vdp.vram[0:2]) == bytes(cache[0:2]) == b"\x00\xaa"


def test_phase_1_keeps_the_cache_from_the_line_change() -> None:
    """A cache write after the V counter steps misses that line's phase 1 (Kabuto: "2
    H-borders before the raster line to be rendered"): Overdrive 2's rotating boxes
    rewrite Y from 796 MCLK into a line, and one sprite dropped out of a row when the
    scan saw only the first of the writes."""
    from megadrive_python.board import Board
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES

    renderer = Board(MACHINES["genesis"], Cartridge(b"")).vdp.renderer
    renderer.sprite_cache[0:4] = bytes([0x00, 0x90, 0x05, 0x00])  # 16 rows from row 16
    renderer.latch_scan_cache()
    renderer.sprite_cache[0:2] = bytes([0x00, 0xB0])  # moved away, after the V step
    assert len(renderer._sprite_scan(320, 20, False)) == 1
    renderer.latch_scan_cache()
    assert renderer._sprite_scan(320, 20, False) == []
