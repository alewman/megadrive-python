"""The expansion sockets (docs/expansion-sockets.md): every socket reaches a test-only
device (tests/fake_devices.py), and with nothing plugged in the board is what it was.

The fakes model no add-on. What is checked is the board's side of the contract: which
accesses reach a device, from which bus master, at which master clock, and the events
and records it is handed.
"""

from __future__ import annotations

import itertools

import pytest
from fake_devices import (
    FakeAudio,
    FakeDevice,
    FakePicture,
    FakePortDevice,
    FakeSlotDevice,
    FakeVideo,
)

from megadrive_python.board import Lockup, _LowDecode
from megadrive_python.cartridge import BootROM, Cartridge
from megadrive_python.machine import M68K_DIVIDER, MACHINES
from megadrive_python.render import LEVELS
from megadrive_python.sockets import M68K, Z80, Reset
from megadrive_python.system import System

STACK = 0x00FFFE00
CODE = 0x200


def _image(code: str, *, at: int = CODE) -> bytes:
    """A ROM: the two reset vectors, a header, and ``code`` (hex) at ``at``."""
    rom = bytearray(0x10000)
    rom[0:4] = STACK.to_bytes(4, "big")
    rom[4:8] = at.to_bytes(4, "big")
    rom[0x100:0x110] = b"SEGA GENESIS    "
    program = bytes.fromhex(code)
    rom[at : at + len(program)] = program
    return bytes(rom)


def _cartridge(code: str) -> Cartridge:
    return Cartridge(_image(code))


def _run(system: System, instructions: int) -> None:
    for _ in range(instructions):
        system.step()


def _ram(system: System, address: int) -> int:
    return (system.board.ram[address & 0xFFFF] << 8) | system.board.ram[(address + 1) & 0xFFFF]


LOOP = "60fe"  # bra.s *


# -- nothing plugged in ---------------------------------------------------------------


def test_nothing_attached_is_the_board_as_it_was() -> None:
    system = System(MACHINES["genesis"], _cartridge(LOOP))
    board = system.board
    assert board.slot is None and board.port is None and board.devices == ()
    assert board._pages is None  # the paths that look at claims see None at once
    assert board.cartridge is board.game_cartridge is board.slot_cartridge
    assert board.cartridge_present
    assert system.clocked == [] and system.audio_sources == []
    assert system.vdp.video_timing is None and system.vdp.renderer.picture is None
    assert board.io.version & 0x20  # no expansion unit
    with pytest.raises(Lockup):
        board.read_word(0x840000)
    with pytest.raises(Lockup):
        board._z80_bank_read(0x840000)


def test_an_empty_slot_is_a_state_and_still_reads_ff() -> None:
    system = System(MACHINES["genesis"], None)
    board = system.board
    assert board.slot_cartridge is None and not board.cartridge_present
    assert board.cartridge.read_word(0x1000) == 0xFFFF  # as the board always read it


def test_claims_are_whole_pages_on_a_sockets_bus() -> None:
    for ranges in ([(0xA15180, 0xA15200)], [(0xA11100, 0xA11200)], [(0xE00000, 0xE00100)],
                   [(0xA10000, 0xA10100)], [(0xA14000, 0xA14100)]):  # fmt: skip
        with pytest.raises(ValueError):
            System(MACHINES["genesis"], None, port=FakeDevice(ranges))  # type: ignore[arg-type]


# -- socket 1: the slot, and socket 3: one decode ---------------------------------------


SLOT_PROGRAM = (
    "303900a15100"  # move.w $A15100,d0     -- the slot device's register
    "33c000ff0000"  # move.w d0,$FF0000
    "33fc123400a15102"  # move.w #$1234,$A15102
    "123900a15103"  # move.b $A15103,d1     -- the byte just written, back
    "13c100ff0002"  # move.b d1,$FF0002
    "343900840000"  # move.w $840000,d2     -- the 32X area, claimed
    "33c200ff0004"  # move.w d2,$FF0004
    "303900000200"  # move.w $000200,d0     -- the held cartridge, unclaimed
    "33c000ff0006"  # move.w d0,$FF0006
    "4e70"  # reset
    "60fe"  # bra.s *
)


def _slot_system(code: str = SLOT_PROGRAM, **kwargs) -> tuple[System, FakeSlotDevice]:
    device = FakeSlotDevice(_cartridge(code))
    system = System(MACHINES["genesis"], None, slot=device, **kwargs)  # type: ignore[arg-type]
    system.board.fm.enabled = False
    return system, device


def test_a_slot_device_answers_its_claims_and_its_cartridge_the_rest() -> None:
    system, device = _slot_system()
    board = system.board
    assert board.slot is device and board.slot_cartridge is device.cartridge
    assert board.cartridge is device.cartridge  # nothing claimed at $000000: no decode
    assert board.cartridge_present and board.io.version & 0x20
    _run(system, 10)
    assert _ram(system, 0xFF0000) == device.value(0xA15100)
    assert device.written[0xA15102] == 0x1234
    assert system.board.ram[2] == 0x34  # the byte read back from the device
    assert _ram(system, 0xFF0004) == device.value(0x840000)
    assert _ram(system, 0xFF0006) == 0x3039  # the cartridge's own first word
    kinds = [(kind, address, master) for kind, address, _t, master, _s, _c in device.log]
    assert kinds == [("rw", 0xA15100, M68K), ("ww", 0xA15102, M68K),
                     ("rb", 0xA15103, M68K), ("rw", 0x840000, M68K)]  # fmt: skip


def test_an_access_carries_its_clock_and_catches_the_device_up_inside_it() -> None:
    """Socket 4: the clock handed over is the access's own, past the instruction's start
    and the device has run to it before answering. Socket 5: after each instruction it
    is run to the 68000's."""
    system, device = _slot_system()
    system.step()  # move.w $A15100,d0
    _kind, _address, mclk, _master, step_start, clock = device.log[0]
    assert mclk > step_start and clock == mclk
    # Two bus cycles of 4 clocks before it (m68000-python's prefetch: the opcode and the
    # first extension word were already in), then the read.
    assert mclk == step_start + 8 * M68K_DIVIDER
    assert device.catch_ups[-1] == system.mclk and device.clock == system.mclk


def test_a_device_holds_the_68000_on_its_clock_edge() -> None:
    elapsed = []
    for hold in (0, 10):
        system, device = _slot_system()
        device.hold_mclk = hold
        elapsed.append(system.step())
    assert elapsed[1] - elapsed[0] == 2 * M68K_DIVIDER  # 10 MCLK, rounded up to 14


def test_the_z80_reaches_a_claim_through_its_bank_window() -> None:
    system, device = _slot_system()
    board = system.board
    board.arbiter_enabled = False
    board.bank = 0x840000
    assert board.z80_read(0x8000) == device.value(0x840000) >> 8
    board.z80_write(0x8003, 0x42)
    assert device.log[0][:4:3] == ("rb", Z80) and device.log[0][1] == 0x840000
    assert device.log[1][0] == "wb" and device.log[1][1] == 0x840003
    assert device.log[1][3] == Z80 and device.written[0x840002] == 0x42
    assert board.z80_peek(0x8000) == 0x5A and device.peeks == [0x840000]


def test_the_schedulers_probe_peeks_through_the_same_decode() -> None:
    system, device = _slot_system()
    system.board.bank = 0x800000
    system.z80.pc = 0
    system.board.z80_ram[0:3] = bytes.fromhex("3a0080")  # ld a,($8000)
    reads = len(device.log)
    system.z80_probe()
    assert device.peeks == [0x800000] and len(device.log) == reads  # no side effects


DMA_PROGRAM = (
    "33fc811400c00004"  # move.w #$8114,$C00004 -- DMA on, mode 5
    "33fc8f0200c00004"  # move.w #$8F02,$C00004 -- increment 2
    "33fc930200c00004"  # move.w #$9302,$C00004 -- two words
    "33fc940000c00004"  # move.w #$9400,$C00004
    "33fc950000c00004"  # move.w #$9500,$C00004 -- from $840000
    "33fc960000c00004"  # move.w #$9600,$C00004
    "33fc974200c00004"  # move.w #$9742,$C00004
    "23fc4000008000c00004"  # move.l #$40000080,$C00004 -- to VRAM 0, DMA
    "60fe"  # bra.s *
)


def test_vdp_dma_reaches_a_claim_with_a_start_notice() -> None:
    system, device = _slot_system(DMA_PROGRAM)
    system.run_frame()
    assert len(device.dma_starts) == 1
    started, bus = device.dma_starts[0]
    assert bus == 0x60FE  # the 68000's prefetch: the loop it runs next
    assert [address for address, _t in device.dma_reads] == [0x840000, 0x840002]
    assert all(t > started for _a, t in device.dma_reads)
    assert device.dma_reads[0][1] < device.dma_reads[1][1]
    assert not device.log  # DMA has its own entry


# -- socket 2: the expansion port -------------------------------------------------------


PORT_BIOS = _image(
    "33fc000100a12000"  # move.w #1,$A12000      -- the port's gate array
    "103900a10001"  # move.b $A10001,d0       -- the version register
    "13c000ff0000"  # move.b d0,$FF0000
    "60fe"  # bra.s *
)


def test_a_port_device_boots_with_no_cartridge() -> None:
    device = FakePortDevice(PORT_BIOS)
    system = System(MACHINES["genesis"], None, port=device)  # type: ignore[arg-type]
    board = system.board
    assert not board.cartridge_present and device.base == 0
    assert isinstance(board.cartridge, _LowDecode)  # its claims at $000000
    _run(system, 4)
    assert board.ram[0] & 0x20 == 0  # /DISK: an expansion unit is there
    assert device.written[0xA12000] == 1
    fetched = [address for kind, address, *_ in device.log if kind == "rw" and address < 8]
    assert fetched == [0, 2, 4, 6]  # the 68000's reset vectors came from the port


def test_a_port_device_moves_to_400000_with_a_cartridge() -> None:
    device = FakePortDevice(PORT_BIOS)
    # move.w $400000,d0; move.w d0,$FF0000; bra.s *
    cartridge = _cartridge("303900400000" + "33c000ff0000" + LOOP)
    system = System(MACHINES["genesis"], cartridge, port=device)  # type: ignore[arg-type]
    board = system.board
    assert board.cartridge_present and device.base == 0x400000
    assert board.cartridge is cartridge and board.io.version & 0x20 == 0
    _run(system, 3)
    assert _ram(system, 0xFF0000) == 0x00FF  # the BIOS's first word, at $400000


def test_the_tmss_rom_sits_above_the_port() -> None:
    device = FakePortDevice(PORT_BIOS)
    machine = MACHINES["genesis-tmss"]
    system = System(machine, None, port=device, tmss_rom=_image(LOOP))  # type: ignore[arg-type]
    board = system.board
    assert isinstance(board.cartridge, BootROM)
    assert not any(address < 8 for _k, address, *_ in device.log)  # vectors: the boot ROM's
    board._tmss_select(True)
    assert isinstance(board.cartridge, _LowDecode)
    board._tmss_select(False)
    assert isinstance(board.cartridge, BootROM)


def test_the_port_wins_a_page_both_claim() -> None:
    port = FakeDevice([(0xA15100, 0xA15200)])
    system, slot = _slot_system(port=port)
    assert system.board._pages[0xA151] is port and system.board._pages[0xA152] is slot


# -- socket 9: the three resets -----------------------------------------------------------


def test_the_three_resets_reach_the_devices() -> None:
    system, device = _slot_system()
    assert device.resets == [(Reset.POWER_ON, 0)]
    _run(system, 10)  # ... ends with RESET
    kind, at = device.resets[1]
    assert kind is Reset.INSTRUCTION and 0 < at <= system.mclk
    system.press_reset()
    assert device.resets[2] == (Reset.BUTTON, system.reset_at)


# -- socket 5: clock domains ------------------------------------------------------------


def test_clocked_devices_follow_the_68000() -> None:
    system, device = _slot_system(LOOP)
    assert system.clocked == [device]
    for _ in range(50):
        system.step()
        assert device.clock == system.mclk


# -- socket 8: sound ------------------------------------------------------------------------


def _audio(source: FakeAudio | None) -> list[tuple[int, int]]:
    system = System(MACHINES["genesis"], _cartridge(LOOP))
    if source is not None:
        system.audio_sources.append(source)
    out = []
    for _ in range(3):
        system.run_frame()
        out += system.take_audio()
    return out


def test_a_further_source_is_mixed_at_its_own_rate() -> None:
    plain = _audio(None)
    assert _audio(FakeAudio(32_552.08, 0.0, (1000.0, -1000.0))) == plain  # no gain: nothing
    source = FakeAudio(32_552.08, 1.0, (1000.0, -1000.0))
    mixed = _audio(source)
    assert len(mixed) == len(plain) and mixed != plain
    assert len(source.calls) == 3
    late = [left - p for (left, _r), (p, _q) in zip(mixed[-200:], plain[-200:], strict=True)]
    assert all(value > 0 for value in late)  # the level comes through, left positive
    source.rate = 22_050.0  # a rate may change between calls
    assert _audio(source)


# -- socket 6: video timing -----------------------------------------------------------------


def test_every_line_is_published_with_its_edges() -> None:
    system = System(MACHINES["genesis"], _cartridge(LOOP))
    video = FakeVideo()
    system.attach_video_timing(video)
    system.run_frame()
    first = len(video.lines)
    system.run_frame()
    frame = video.lines[first:]
    assert len(frame) == 262  # NTSC V28, no mode change
    for before, after in itertools.pairwise(frame):
        assert after.start == before.start + before.length
    for line in frame:
        assert line.start <= line.hblank_set < line.start + line.length
        assert line.start <= line.hblank_clear < line.start + line.length
        assert line.hmode == "H32" and not line.pal and len(line.pixels) == 342
    assert sum(line.vsync for line in frame) == 1
    assert any(line.vblank for line in frame) and not frame[0].vblank


# -- socket 7: the picture ------------------------------------------------------------------


def _picture_system(picture: FakePicture) -> System:
    system = System(MACHINES["genesis"], _cartridge(LOOP))
    system.board.fm.enabled = False
    system.run_frame()
    vdp = system.vdp
    vdp.registers[1] = 0x44  # display on, mode 5
    vdp.registers[2] = 0x30  # plane A at $C000
    vdp.registers[4] = 0x07  # plane B at $E000
    # Plane A's first cell: tile $101 (the name word reads the same either byte order),
    # every pixel colour 1, which is white.
    vdp.vram[0xC000] = vdp.vram[0xC001] = 0x01
    vdp.vram[0x2020:0x2040] = b"\x11" * 32
    vdp.cram[1] = 0x0EEE
    vdp.renderer.cram_dirty = True
    system.attach_picture(picture)
    system.run_frame()
    system.run_frame()
    return system


def test_each_finished_line_is_handed_over_with_ys_and_shade() -> None:
    picture = FakePicture()
    system = _picture_system(picture)
    lines = [line for line in picture.lines if line.row == 0]
    line = lines[-1]
    assert line.hmode == "H32" and line.width == 256 and line.pixel_mclk == 10
    assert line.output_delay == 24 and line.display_at > line.start
    white = (LEVELS[0][7] << 16) | (LEVELS[0][7] << 8) | LEVELS[0][7]
    assert line.rgb[:8] == [white] * 8 and line.rgb[8] == 0
    assert line.ys[:8] == bytes(8) and line.ys[8:] == b"\x01" * 248
    assert line.shade == bytes(256)
    assert line.rgb == system.vdp.renderer.frame_rgb()[0]  # what leaves without a change
    assert len({line.row for line in picture.lines}) == 224


def test_the_line_the_device_returns_leaves_the_console() -> None:
    picture = FakePicture({0: 0x123456})
    system = _picture_system(picture)
    rows = system.vdp.renderer.frame_rgb()
    assert rows[0] == [0x123456] * 320  # wider than the H32 line: the device's own
    assert len(rows[1]) == 256
