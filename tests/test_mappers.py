"""The boards of :mod:`megadrive_python.mappers`: what each one decodes.

Every expectation is MAME's (the device file named in each board's docstring), because
that is the only source there is; these tests pin the transcription, not the hardware.
"""

from __future__ import annotations

import pytest

from megadrive_python.cartridge import BOARDS, Cartridge


def image(size: int, unit: int = 0x1000) -> bytes:
    """Every ``unit`` bytes carry their own index, so a bank shows which page it is."""
    return b"".join(bytes([i & 0xFF]) * unit for i in range(size // unit))


def cart(slot: str, size: int = 0x100000, unit: int = 0x1000) -> Cartridge:
    return BOARDS[slot](image(size, unit), slot=slot)


def test_every_registered_board_is_supported_and_names_its_slot():
    for slot, cls in BOARDS.items():
        c = cls(image(0x100000), slot=slot)
        assert c.unsupported is None, slot
        assert c.slot == slot


@pytest.mark.parametrize(
    ("slot", "reads"),
    [
        ("rom_16mj2", {0x400002: 0x98, 0x400004: 0xC9, 0x400006: 0x18}),
        ("rom_elfwor", {0x400000: 0x55, 0x400002: 0x0F, 0x400004: 0xC9, 0x400006: 0x18}),
        ("rom_smouse", {0x400000: 0x55, 0x400002: 0x0F, 0x400004: 0xAA, 0x400006: 0xF0}),
        ("rom_soulb", {0x400000: 0x63, 0x400006: 0xF0}),
        ("rom_sbubl", {0x400000: 0x55, 0x400002: 0x0F}),
        ("rom_cjmjclub", {0x400000: 0x90, 0x400002: 0xD3}),
        ("rom_mjlov", {0x400000: 0x90, 0x401000: 0xD3}),
        ("rom_rx3", {0x400004: 0xC9, 0x400006: 0xF0}),
        ("rom_kof98", {0x480000: 0xAA, 0x4BFFFE: 0xAA, 0x4C0000: 0xF0}),
        ("rom_sram_arg96", {0x4C6201: 0x0A, 0x4C6601: 0x09, 0x4C6A01: 0x07}),
    ],
)
def test_protection_bytes_read_back_and_the_board_claims_the_upper_window(slot, reads):
    c = cart(slot)
    assert c.upper
    for address, value in reads.items():
        assert c.read_byte(address) == value
    # An address the chip does not decode reads $FF (nothing drives the bus).
    assert c.read_byte(0x7FFFFE) == 0xFF
    # A word read of a register at an even address has the byte high.
    even = next((a for a in reads if not a & 1), None)
    if even is not None:
        assert c.read_word(even) >> 8 == reads[even]


def test_the_rom_is_not_disturbed_by_the_protection_window():
    c = cart("rom_sbubl")
    assert c.read_byte(0x2000) == 2
    assert c.read_word(0x2000) == 0x0202


@pytest.mark.parametrize(
    ("slot", "words"),
    [
        ("rom_smb", {0x00: 0x1C}),
        ("rom_smb2", {0x00: 0x0A}),
        ("rom_rx3", {0x00: 0x0C}),
        ("rom_bugs", {0x00: 0x28, 0x02: 0x01, 0x3E: 0x1F}),
        ("rom_pokea", {0x00: 0x14, 0x02: 0x01, 0x3E: 0x1F}),
        ("rom_kof99", {0x00: 0x00, 0x02: 0x01, 0x3E: 0x1F}),
    ],
)
def test_time_range_words(slot, words):
    c = cart(slot)
    for offset, value in words.items():
        assert c.read_time_word(0xA13000 + offset) == value
    assert c.read_time_word(0xA13004) is None


def test_squirrel_king_latch_is_shared_by_the_four_even_addresses():
    c = cart("rom_squir")
    assert c.read_byte(0x400000) == 0xFF
    c.write_byte(0x400004, 0x5A)
    assert [c.read_byte(a) for a in (0x400000, 0x400002, 0x400004, 0x400006)] == [0x5A] * 4
    assert c.read_byte(0x400008) == 0xFF  # outside


def test_lion_king_2_writes_one_address_and_reads_another():
    c = cart("rom_lion2")
    c.write_word(0x400000, 0x1100)
    c.write_word(0x400004, 0x2200)
    assert c.read_byte(0x400002) == 0x11
    assert c.read_byte(0x400006) == 0x22


def test_tekken_special_collects_strobes_and_reads_the_count_minus_one():
    c = cart("rom_tekkensp")
    c.write_byte(0x400000, 0xFF)  # reset
    assert c.read_byte(0x400002) == 0xFF  # 0 - 1
    for address in (0x400004, 0x400008):  # bits 0 and 2
        c.write_byte(address, 0x01)
    c.write_byte(0x400006, 0x00)  # D0 clear: nothing
    assert c.read_byte(0x400002) == 0b0101 - 1
    c.write_byte(0x400000, 0)
    assert c.read_byte(0x400002) == 0xFF


def test_tc2000_strobe_then_read():
    c = cart("rom_tc2000")
    assert c.read_byte(0x100000) == 0
    c.write_byte(0x100008, 0)
    assert c.read_byte(0x100000) == 0x50
    c.write_byte(0x10000C, 0)
    assert c.read_byte(0x180000) == 0xA0  # mirrored through the megabyte
    c.write_byte(0x100000, 0)
    assert c.read_byte(0x100000) == 0
    assert c.read_byte(0x2000) == 2  # the first megabyte is ROM
    assert c.read_byte(0x200000) == 0xFF and not c.upper


def test_chinese_fighter_3_bank_and_latches():
    c = cart("rom_chinf3", 0x200000, unit=0x10000)
    assert c.read_byte(0x30000) == 3
    c.write_word(0x600000, 0x0300)  # byte $03 in the high lane: page 3
    assert c.read_byte(0x0000) == 3
    assert c.read_byte(0x1F0000) == 3  # the 64 KB window repeats through 2 MB
    c.write_word(0x600000, 0x0000)
    assert c.read_byte(0x30000) == 3 and c.read_byte(0x0000) == 0
    c.write_byte(0x400004, 0x77)
    assert c.read_byte(0x400004) == 0x77 and c.read_byte(0x400014) == 0x77
    assert c.read_byte(0x400000) == 0xFF


def test_top_fighter_bank_and_bitswap():
    c = cart("rom_lion3", 0x200000, unit=0x8000)  # 64 pages of 32 KB
    assert c.read_byte(0x8000) == 1
    c.write_byte(0x700000, 0x08)  # non-zero: 32 KB pages, (k | 8), odd k | 8 | 1
    assert c.read_byte(0x0000) == 0 | 8
    assert c.read_byte(0x8000) == (0 | 8 | 1)
    assert c.read_byte(0x10000) == (2 | 8)
    assert c.read_byte(0x100000) == 0xFF
    c.write_byte(0x700000, 0)
    assert c.read_byte(0x8000) == 1
    c.write_byte(0x770077, 0x08)  # Pokemon Stadium writes its bank at an odd address
    assert c.read_byte(0x0000) == 8
    c.write_word(0x700000, 0x0000)  # a word write counts by its low byte
    assert c.read_byte(0x8000) == 1
    # the protection device: modes 0-3 on latch $B1 = 1011 0001
    c.write_byte(0x600001, 0xB1)
    expected = {0: 0x62, 1: 0x58, 2: 0x1B, 3: 0x8D}
    for mode, value in expected.items():
        c.write_byte(0x600003, mode)
        assert c.read_byte(0x600005) == value, mode
        assert c.read_byte(0x6FFFFD) == value  # mirrored every 8 bytes


def test_realtec_applies_its_registers_once():
    c = cart("rom_realtec", 0x80000, unit=0x2000)  # 64 pages of 8 KB
    assert c.read_byte(0x0000) == 0x3F  # every slot shows the last page at power-on
    assert c.read_byte(0x7E000 & 0x1FFF) == 0x3F
    c.write_byte(0x402000, 0x03)  # size
    c.write_byte(0x404000, 0x02)  # select
    c.write_byte(0x400000, 0x01)  # apply
    first = c.read_byte(0x0000)
    base = (0x02 << 4) & (0x03 << 4)
    assert first == (0 & ~(0x03 << 4) | base) & 0x3F
    c.write_byte(0x404000, 0x01)
    c.write_byte(0x400000, 0x01)  # locked: ignored
    assert c.read_byte(0x0000) == first


def test_red_cliff_decrypts_the_dump():
    raw = bytes(range(8)) + bytes(0x100)
    c = BOARDS["rom_redcl"](raw, slot="rom_redcl")
    assert c.read_byte(0) == (4 ^ 0x40)  # four bytes skipped, XOR $40
    assert c.read_byte(0x3FFFFF) == 0xBF
    assert c.read_byte(0x400000) == 0x55 and c.read_byte(0x400004) == 0xAA


def test_multicart_pirate_selects_four_consecutive_128k_pages():
    c = cart("rom_mcpir", 0x100000, unit=0x20000)  # 8 pages
    assert [c.read_byte(i * 0x20000) for i in range(4)] == [0, 1, 2, 3]
    c.write_time_lanes(0xA13006, 0x00, 0x00, True)  # even byte, offset 6 -> page 3
    assert [c.read_byte(i * 0x20000) for i in range(4)] == [3, 4, 5, 6]
    c.write_time_lanes(0xA1303E, 0, 0, True)  # 31 -> wraps modulo 8
    assert c.read_byte(0) == 31 % 8
    c.write_time_lanes(0xA13003, 0xFF, 0xFF, False)  # a byte write at an ODD address: no
    assert c.read_byte(0) == 31 % 8
    assert c.read_byte(0x400000) == 0xFF  # nothing above 4 MB; ROM mirrors inside it
    assert c.read_byte(0x80000) == c.read_byte(0x0)


def test_codemasters_2in1_switches_game_on_every_reset():
    c = cart("rom_cm2in1", 0x400000, unit=0x200000)
    assert c.read_byte(0) == 0 and c.read_byte(0x200000) == 0  # game 0, mirrored
    c.reset()
    assert c.read_byte(0) == 1 and c.read_byte(0x3FFFFF) == 1
    c.reset()
    assert c.read_byte(0) == 0


def test_super_mario_world_64_banks_and_arithmetic():
    c = cart("rom_smw64", 0x200000, unit=0x10000)
    assert c.read_byte(0x70000) == 7 and c.read_byte(0xF0000) == 7  # 512 KB mirrored
    assert c.read_byte(0x600000) == 8  # both windows start on page 8
    c.write_byte(0x600001, 0x07)  # mode 7: page for window 1
    c.write_byte(0x600003, 0x14)  # ((0x14 & 0x1C) >> 2) + 8 = 13
    assert c.read_byte(0x610000) == 13
    c.write_byte(0x600001, 0x01)  # mode 1: latch 1
    c.write_byte(0x600003, 0x35)
    assert c.read_byte(0x660005) == 0x34 and c.read_byte(0x660007) == 0x35
    # $670001 with the control bytes at their reset values gives zero
    assert c.read_byte(0x670001) == 0


def test_battery_boards_keep_the_low_lane_only():
    c = cart("rom_tplay96", 0x100000)
    assert c.read_word(0x200000) == 0xFFFF
    c.write_byte(0x200001, 0x42)
    assert c.read_word(0x200000) == 0xFF42
    c.write_byte(0x200000, 0x99)  # the even lane drives nothing
    assert c.read_byte(0x200000) == 0xFF and c.read_byte(0x200001) == 0x42
    assert c.battery == "sram" and bytes(c.sram[:1]) == b"\x42"
    # the chip repeats inside its address range: 32 KB in 64 KB
    assert c.read_byte(0x208001) == 0xFF  # a different chip byte, never written


def test_yasechuanshuo_and_sanguo5_ram_ranges():
    c = cart("rom_yasech")
    c.write_byte(0x200001, 0x11)
    assert c.read_byte(0x200001) == 0x11
    assert c.read_byte(0x400000) == 0x63  # and the protection bytes of the family
    s = cart("rom_sanguo5")
    s.write_byte(0x203FFF, 0x22)
    assert s.read_byte(0x203FFF) == 0x22
    assert s.read_byte(0x204001) == 4  # past the RAM's 16 KB the ROM shows again


def test_futbol_argentino_mirrors_two_megabytes():
    c = cart("rom_sram_arg96", 0x200000)
    assert c.read_byte(0x300000) == c.read_byte(0x100000)
    c.write_byte(0x200001, 0x7E)
    assert c.read_byte(0x200001) == 0x7E


def test_xin_qi_gai_wang_zi_has_ram_in_the_upper_window():
    c = cart("rom_xinqig", 0x100000)
    assert c.upper
    c.write_byte(0x400001, 0x33)
    assert c.read_byte(0x400001) == 0x33 and c.read_byte(0x404001) == 0xFF  # one byte per word


def test_beggar_prince_hides_its_ram_until_unlocked():
    c = cart("rom_sf001", 0x400000, unit=0x40000)  # 16 pages of 256 KB
    assert c.read_byte(0x0) == 0  # ordinary ROM at 0 to begin with
    assert c.read_byte(0x400001) == 0xFF
    c.write_byte(0x400001, 0x12)  # RAM off: lost
    c.write_byte(0xE01, 0x80)  # RAM on, page $0E over $000000
    assert c.read_byte(0x0) == 0x0E
    c.write_byte(0x400001, 0x12)
    assert c.read_byte(0x400001) == 0x12
    c.write_byte(0xE01, 0x20)  # off again, and lock
    assert c.read_byte(0x0) == 0 and c.read_byte(0x400001) == 0xFF
    c.write_byte(0xE01, 0x80)  # the lock holds
    assert c.read_byte(0x0) == 0
    rev1 = cart("rom_sf001_beggarp1", 0x400000, unit=0x40000)
    rev1.write_byte(0xE01, 0x80)
    rev1.write_byte(0x3C0001, 0x44)
    assert rev1.read_byte(0x3C0001) == 0x44 and rev1.read_byte(0x3C8001) == 0x44


def test_legend_of_wukong_swaps_the_upper_two_megabytes():
    c = cart("rom_sf002", 0x400000, unit=0x200000)
    assert c.read_byte(0x0) == 0 and c.read_byte(0x200000) == 1
    c.write_byte(0xE01, 0x81)  # bit 7: first half again; bit 0: RAM
    assert c.read_byte(0x200000) == 0
    c.write_byte(0x3C0001, 0x5C)
    assert c.read_byte(0x3C0001) == 0x5C and c.read_byte(0x3C4001) == 0x5C  # 8 KB chip repeats
    c.write_byte(0xE01, 0x00)
    assert c.read_byte(0x200000) == 1 and c.read_byte(0x3C0001) != 0x5C


def test_star_odyssey_modes_and_base_register():
    c = cart("rom_sf004", 0x200000, unit=0x40000)  # 8 pages of 256 KB
    assert c.read_byte(0x40000) == 0 and c.read_byte(0x1C0000) == 0  # page 0 repeated
    c.write_byte(0xF01, 0x30)  # base page 3
    c.write_byte(0xE01, 0xC0)  # five windows, no lock
    assert [c.read_byte(i * 0x40000) for i in range(5)] == [3, 4, 5, 6, 7]
    assert c.read_byte(0x140000) == 0xFF
    assert c.read_time_word(0xA13001) & 0xFF == 0x30
    # the single-window mode follows the base too (Star Odyssey boots from it)
    c.write_byte(0xE01, 0x80)
    assert c.read_byte(0x0) == 3 and c.read_byte(0x1C0000) == 3
    c.write_byte(0xD01, 0x80)
    c.write_byte(0x200001, 0x66)
    assert c.read_byte(0x200001) == 0x66
    c.write_byte(0xE01, 0x00)  # bit 7 clear: lock
    c.write_byte(0xF01, 0x00)
    assert c.read_time_word(0xA13001) & 0xFF == 0x30


def test_the_console_bus_routes_the_upper_window_only_for_boards_that_claim_it():
    from megadrive_python.board import Board
    from megadrive_python.machine import MACHINES

    machine = next(iter(MACHINES.values()))
    with_chip = Board(machine, cart("rom_sbubl"))
    assert with_chip.read_byte(0x400000) == 0x55
    assert with_chip.read_word(0x400000) == 0x55FF
    plain = Board(machine, Cartridge(image(0x100000)))
    assert plain.read_byte(0x400000) != 0x55
    # and a word write to a latch goes through the lanes
    squirrel = Board(machine, cart("rom_squir"))
    squirrel.write_word(0x400000, 0x7700)
    assert squirrel.read_byte(0x400000) == 0x77
    # /TIME word reads come back whole, not duplicated into both bytes
    smb = Board(machine, cart("rom_smb"))
    assert smb.read_word(0xA13000) == 0x001C
    assert smb.read_byte(0xA13000) == 0x00 and smb.read_byte(0xA13001) == 0x1C


def spi(c, byte: int) -> int:
    """Clock one byte into the M95320 through $A13009 (/CS low) and read back what SO did."""
    out = 0
    for bit in range(7, -1, -1):
        si = (byte >> bit) & 1
        c.write_time_lanes(0xA13009, 0xFF, si, False)
        c.write_time_lanes(0xA13009, 0xFF, si | 2, False)
        out = (out << 1) | (c.read_time_word(0xA1300B) & 1)
    return out


def select(c, level: int) -> None:
    c.write_time_lanes(0xA13009, 0xFF, level << 3, False)


def test_pier_solar_banks_and_spi_eeprom():
    c = cart("rom_stm95", 0x800000, 0x80000)
    assert c.read_byte(0x000000) == 0 and c.read_byte(0x27FFFF) == 4
    assert c.read_byte(0x280000) == 5 and c.read_byte(0x380000) == 7
    c.write_time_lanes(0xA13003, 0xFF, 0x0C, False)
    c.write_time_lanes(0xA13007, 0xFF, 0x1F, False)  # four bits: page 15
    assert c.read_byte(0x280000) == 12 and c.read_byte(0x380000) == 15
    assert c.read_byte(0x300000) == 6
    # the byte-read mirror of the first 32 KB over $018100-$0181FF
    rom = image(0x800000, 0x80000)
    assert c.read_byte(0x18100) == rom[0x100]
    # WREN, WRITE $0123 <- $5A, READ it back
    select(c, 1)
    select(c, 0)
    spi(c, 0x06)
    select(c, 1)
    select(c, 0)
    for byte in (0x02, 0x01, 0x23, 0x5A):
        spi(c, byte)
    select(c, 1)
    assert c.sram[0x123] == 0x5A and c.battery == "sram"
    select(c, 0)
    for byte in (0x03, 0x01, 0x23):
        spi(c, byte)
    assert spi(c, 0) == 0x5A
    # without WREN a write does nothing
    select(c, 1)
    select(c, 0)
    for byte in (0x02, 0x00, 0x10, 0x77):
        spi(c, byte)
    assert c.sram[0x10] == 0xFF


def test_an_unlisted_watermelon_image_is_pier_solar():
    raw = bytearray(0x800000)
    raw[0x110:0x118] = b"(c)T574 "
    assert Cartridge.from_image(bytes(raw)).slot == "rom_stm95"
