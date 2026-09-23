"""Milestone 7's acceptance: cartridges and peripherals.

What is checked here needs nothing fetched and no commercial image: the two pads
against CMD's published table, the serial EEPROM against Eke-Eke's description of the
protocol, the Sega mapper's registers against ``ssf2tnc.txt``, the battery's mapping
and write-protect registers against CMD's 4.3, TMSS's bank switch and VDP lock against
the boot ROM's own disassembly, and the version register against the Manual. The runs
that need a game or the boot ROM -- ``scripts/run_battery.py``, ``run_pads.py
--games``, ``run_mapper.py``, ``run_tmss.py``, ``run_region.py`` and
``run_r57shell_tests.py`` -- are run by hand and recorded in docs/worklog.md.
"""

from __future__ import annotations

import pytest
from conftest import ROOT  # noqa: F401  (puts scripts/ on the path)

from megadrive_python.board import Lockup
from megadrive_python.cartridge import EEPROM_BOARDS, Cartridge
from megadrive_python.eeprom import MODE1, MODE2, MODE3, SerialEEPROM
from megadrive_python.io import (
    LEFT,
    MODE,
    START,
    UP,
    A,
    B,
    C,
    IOChip,
    Pad3,
    Pad6,
    X,
    Y,
    Z,
)
from megadrive_python.machine import MACHINES
from megadrive_python.system import System


def _bare_rom(body: bytes, header: bytes = b"SEGA MEGA DRIVE ") -> bytes:
    rom = bytearray(0x400)
    rom[0:4] = (0x00FFFE00).to_bytes(4, "big")
    rom[4:8] = (0x00000200).to_bytes(4, "big")
    rom[0x100 : 0x100 + len(header)] = header
    rom[0x200 : 0x200 + len(body)] = body
    return bytes(rom)


# -- the pads --------------------------------------------------------------------------


def _walk(pad: Pad3 | Pad6, phases: int = 8) -> list[int]:
    """Drive TH high, low, high, ... through an I/O chip and collect what it reads."""
    chip = IOChip(0xA0, (pad, None))
    chip.write(0xA10009, 0x40, 0)  # TH is an output
    out = []
    clock = 0
    for index in range(phases):
        clock += 1000  # a poll takes a few microseconds; nothing here times out
        chip.write(0xA10003, 0x40 if index % 2 == 0 else 0x00, clock)
        out.append(chip.read(0xA10003, clock))
    return out


def test_three_button_pad_is_cmds_two_rows() -> None:
    """CMD ``gen-hw.txt`` 3.2: "TH = 0 : ?0SA00DU", "TH = 1 : ?1CBRLDU", for ever."""
    pad = Pad3()
    pad.pressed = UP | START | C
    read = _walk(pad, 6)
    # TH = 1: C pressed, up pressed -> D5 and D0 low; bit 6 is TH, bit 7 the latch.
    assert read[0::2] == [0x40 | (~(0x20 | 0x01) & 0x3F)] * 3
    # TH = 0: Start pressed, up pressed, D3 and D2 read 0.
    assert read[1::2] == [(~(0x20 | 0x0C | 0x01) & 0x3F)] * 3


def test_six_button_pad_walks_cmds_sequence() -> None:
    """CMD's six rows, with every extra button pressed so that each one shows."""
    pad = Pad6()
    pad.pressed = Z | Y | X | MODE
    read = _walk(pad, 6)
    assert read[0] == 0x40 | 0x3F  # ?1CBRLDU, nothing of the four pressed
    assert read[1] == 0x33  # ?0SA00DU
    assert read[2] == 0x40 | 0x3F  # ?1CBRLDU
    assert read[3] == 0x30  # ?0SA0000: D3-D0 forced to 0
    assert read[4] == 0x40 | 0x30  # ?1CBMXYZ: all four extra buttons pressed
    assert read[5] == 0x3F  # ?0SA1111: D3-D0 forced to 1


def test_six_button_pad_returns_to_three_buttons_and_times_out() -> None:
    """ "From this point on, the standard 3-button pad values will be returned if any
    further TH transitions are done" (CMD), and the counter starts again once TH has
    been left alone (his "about 8192 ... 68000 CPU cycles")."""
    pad = Pad6()
    read = _walk(pad, 10)
    assert read[6] == 0x40 | 0x3F and read[7] == 0x33  # back to the 3-button rows
    assert read[8] == 0x40 | 0x3F and read[9] == 0x33
    assert pad.edges == 4
    pad.set_th(1, 10_000 + pad.timeout_mclk)
    assert pad.edges == 1


def test_six_button_pad_keeps_b_and_c_in_the_extra_phase() -> None:
    """CMD's "?1CBMXYZ" keeps B and C on D4 and D5 where Terakawa's table shows them
    released ("HI HI"); the two agree once the buttons are pressed."""
    pad = Pad6()
    pad.pressed = B | C
    read = _walk(pad, 6)
    assert read[4] == 0x40 | (~0x30 & 0x3F)


def test_the_two_pads_agree_until_the_sequence_bites() -> None:
    """The first three reads are the same on both pads -- which is what lets a
    3-button game read a 6-button pad -- and the fourth is where CMD's "?0SA0000"
    replaces "?0SA00DU"."""
    for held in (0, A, START, UP | LEFT, B | C, A | B | C | START):
        pad3, pad6 = Pad3(), Pad6()
        pad3.pressed = pad6.pressed = held
        three, six = _walk(pad3, 4), _walk(pad6, 4)
        assert three[:3] == six[:3]
        assert six[3] & 0x0F == 0  # ?0SA0000: D3-D0 forced low
        assert six[3] & 0x30 == three[3] & 0x30  # Start and A are where they were


# -- the serial EEPROM -------------------------------------------------------------------


class _Master:
    """The 68000 side of the two wires, driving one byte at a time as Eke describes."""

    def __init__(self, chip: SerialEEPROM) -> None:
        self.chip = chip
        self.scl = self.sda = 1

    def _set(self, scl: int, sda: int) -> None:
        self.scl, self.sda = scl, sda
        self.chip.update(scl, sda)

    def start(self) -> None:
        self._set(1, 1)
        self._set(1, 0)  # /SDA high to low while /SCL is high
        self._set(0, 0)

    def stop(self) -> None:
        self._set(0, 0)
        self._set(1, 0)
        self._set(1, 1)  # /SDA low to high while /SCL is high

    def send(self, value: int) -> bool:
        for index in range(8):
            bit = (value >> (7 - index)) & 1
            self._set(0, bit)
            self._set(1, bit)
        self._set(0, 1)  # let go of /SDA for the chip's acknowledge
        self._set(1, 1)
        acknowledged = self.chip.read_sda() == 0
        self._set(0, 1)
        return acknowledged

    def receive(self, acknowledge: bool = True) -> int:
        value = 0
        for _index in range(8):
            self._set(1, 1)
            value = (value << 1) | self.chip.read_sda()
            self._set(0, 1)
        self._set(0, 0 if acknowledge else 1)
        self._set(1, 0 if acknowledge else 1)
        self._set(0, 1)
        return value


def test_mode1_eeprom_writes_and_reads_back() -> None:
    """Mode 1 (the X24C01): "The 7 first bits set the memory address to be read/write
    and the 8th bit set the operation type" (Eke)."""
    chip = SerialEEPROM(MODE1, 0x7F, 0x03)
    master = _Master(chip)
    master.start()
    assert master.send(0x20 << 1)  # address $20, write
    assert master.send(0xA5)
    master.stop()
    assert chip.memory[0x20] == 0xA5
    master.start()
    assert master.send((0x20 << 1) | 1)  # address $20, read
    assert master.receive(acknowledge=False) == 0xA5
    master.stop()


def test_mode2_eeprom_takes_a_device_word_and_an_address_word() -> None:
    chip = SerialEEPROM(MODE2, 0xFF, 0x03)
    master = _Master(chip)
    master.start()
    assert master.send(0xA0)  # device, write
    assert master.send(0x10)  # address
    assert master.send(0x11)
    assert master.send(0x22)
    master.stop()
    assert bytes(chip.memory[0x10:0x12]) == b"\x11\x22"
    master.start()
    assert master.send(0xA0)
    assert master.send(0x10)
    master.start()
    assert master.send(0xA1)  # device, read: the address counter is where it was
    assert master.receive() == 0x11
    assert master.receive(acknowledge=False) == 0x22
    master.stop()


def test_mode3_eeprom_takes_two_address_words() -> None:
    chip = SerialEEPROM(MODE3, 0x1FFF, 0x07)
    master = _Master(chip)
    master.start()
    assert master.send(0xA0)
    assert master.send(0x12)  # the high address word
    assert master.send(0x34)  # the low one
    assert master.send(0x5A)
    master.stop()
    assert chip.memory[0x1234] == 0x5A


def test_a_written_address_wraps_inside_its_page() -> None:
    """ "The address will roll up to base memory address when maximal write page size
    has been reached" (Eke): a 4-byte page keeps the write inside $10-$13."""
    chip = SerialEEPROM(MODE2, 0xFF, 0x03)
    master = _Master(chip)
    master.start()
    master.send(0xA0)
    master.send(0x10)
    for value in (1, 2, 3, 4, 5):
        master.send(value)
    master.stop()
    assert bytes(chip.memory[0x10:0x14]) == b"\x05\x02\x03\x04"  # the fifth wrapped


def test_every_eeprom_board_has_its_lines_where_the_sources_put_them() -> None:
    """Spot-checks of :data:`EEPROM_BOARDS` against Eke's own table."""
    sega = EEPROM_BOARDS["rom_eeprom_mode1"]
    assert (sega.sda_in, sega.sda_out, sega.scl) == ((0x200001, 0), (0x200001, 0), (0x200001, 1))
    assert (sega.mode, sega.size_mask, sega.page_mask) == (MODE1, 0x7F, 0x03)
    ea = EEPROM_BOARDS["rom_nhlpa"]
    assert (ea.sda_in, ea.sda_out, ea.scl) == ((0x200001, 7), (0x200001, 7), (0x200001, 6))
    acclaim = EEPROM_BOARDS["rom_cslam"]
    assert (acclaim.scl, acclaim.mode, acclaim.size_mask) == ((0x200000, 0), MODE3, 0x1FFF)
    codemasters = EEPROM_BOARDS["rom_codemast"]
    assert (codemasters.sda_in, codemasters.sda_out) == ((0x380001, 7), (0x300000, 0))


def test_an_eeprom_cartridge_drives_the_chip_from_the_68000s_bus() -> None:
    """A word write to $200000 carries both lines, which is how the EA and Acclaim
    games drive the chip ("these games use 16-bits write & read", Eke)."""
    cartridge = Cartridge(_bare_rom(b""), slot="rom_eeprom_mode1")
    assert cartridge.eeprom is not None
    cartridge.write_word(0x200000, 0x0003)  # /SCL and /SDA both high
    cartridge.write_word(0x200000, 0x0002)  # /SDA low with /SCL high: START
    assert cartridge.eeprom.state != "idle"
    assert cartridge.read_byte(0x200001) in (0, 1)


# -- the Sega (SSF2) mapper ---------------------------------------------------------------


def test_the_mapper_pages_the_seven_windows_ssf2tnc_names() -> None:
    """``ssf2tnc.txt``: $A130F3 -> $080000 ... $A130FF -> $380000, and "The first area,
    0x000000-0x07FFFF is fixed and cannot be remapped"."""
    image = bytes(range(256)) * (10 * 0x80000 // 256)  # ten 512 KB pages
    cartridge = Cartridge(image, slot="rom_ssf2")
    assert cartridge.banks == list(range(8))
    for index, register in enumerate((0xF3, 0xF5, 0xF7, 0xF9, 0xFB, 0xFD, 0xFF)):
        cartridge.write_time(0xA13000 | register, 9)
        assert cartridge.banks[index + 1] == 9
    cartridge.write_time(0xA130F1, 9)  # not a bank register
    assert cartridge.banks[0] == 0
    cartridge.write_time(0xA130FF, 0xC1)  # "bits 7 and 6 are always 0"
    assert cartridge.banks[7] == 1


def test_the_mapper_shows_the_paged_bytes() -> None:
    image = bytearray(10 * 0x80000)
    for page in range(10):
        image[page * 0x80000 : page * 0x80000 + 2] = bytes([page, page])
    cartridge = Cartridge(bytes(image), slot="rom_ssf2")
    assert cartridge.read_word(0x380000) == 0x0707
    cartridge.write_time(0xA130FF, 9)
    assert cartridge.read_word(0x380000) == 0x0909
    assert cartridge.read_word(0x000000) == 0x0000  # the fixed first window


# -- the battery --------------------------------------------------------------------------


def test_a130f1_maps_and_protects_the_battery() -> None:
    """Phantasy Star 4 "will always write $01 to $A130F1 before accessing the RAM, and
    then write $00 when done" (CMD 4.3); bit 1 write-protects (MAME ``write_a13``)."""
    cartridge = Cartridge(_bare_rom(b""), slot="rom_sram", sram_size=0x2000)
    assert cartridge.sram_mapped  # the ROM ends below $200000
    cartridge.write_byte(0x200001, 0x5A)
    assert cartridge.read_byte(0x200001) == 0x5A
    assert cartridge.read_byte(0x200000) == 0xFF  # eight bits on the odd byte only
    cartridge.write_time(0xA130F1, 0x03)  # mapped and protected
    cartridge.write_byte(0x200001, 0x00)
    assert cartridge.read_byte(0x200001) == 0x5A
    cartridge.write_time(0xA130F1, 0x00)  # unmapped: the ROM shows through
    assert cartridge.read_byte(0x200001) != 0x5A
    cartridge.write_time(0xA130F1, 0x01)
    assert cartridge.save_bytes()[0] == 0x5A
    cartridge.load_bytes(b"\x99")
    assert cartridge.read_byte(0x200001) == 0x99


# -- TMSS ---------------------------------------------------------------------------------


def test_tmss_locks_the_vdp_until_sega_is_written() -> None:
    """ "Games must write the text 'SEGA' to A14000h if the lower four bits of the
    version register return 01h" (CMD 4.4; the Manual's page 94)."""
    system = System(MACHINES["genesis-tmss"], Cartridge(_bare_rom(b"")))
    with pytest.raises(Lockup):
        system.board.read_word(0xC00004)
    system.board.write_word(0xA14000, 0x5345)
    system.board.write_word(0xA14002, 0x4741)
    system.board.read_word(0xC00004)  # unlocked


def test_a_machine_without_tmss_never_locks_the_vdp() -> None:
    system = System(MACHINES["genesis"], Cartridge(_bare_rom(b"")))
    system.board.read_word(0xC00004)


def test_a14101_swaps_the_boot_rom_for_the_cartridge() -> None:
    """The boot ROM's own ``RAM_Code`` does ``bset #0,(a3)`` with a3 = $A14101 to look
    at the cartridge and ``bclr #0,(a3)`` to come back; CMD 4.4: "Writing 01h to
    A14101h disables the OS ROM and swaps in the cart ROM"."""
    cartridge = Cartridge(_bare_rom(b"", header=b"SEGA IS HERE    "))
    boot = bytearray(0x800)
    boot[0x100:0x110] = b"BOOT ROM IS HERE"
    system = System(MACHINES["genesis-tmss"], cartridge, tmss_rom=bytes(boot))
    assert not system.board.tmss_cart_selected
    assert system.board.read_word(0x100) == int.from_bytes(b"BO", "big")
    system.board.write_byte(0xA14101, 0x01)
    assert system.board.tmss_cart_selected
    assert system.board.read_word(0x100) == int.from_bytes(b"SE", "big")
    system.board.write_byte(0xA14101, 0x00)
    assert system.board.read_word(0x100) == int.from_bytes(b"BO", "big")
    assert system.board.game_cartridge is cartridge


def test_without_a_boot_rom_the_cartridge_is_on_the_bus_from_power_on() -> None:
    """Every run before milestone 7 put the cartridge straight on the bus, and a board
    with no boot ROM fitted still does, so nothing earlier moves."""
    cartridge = Cartridge(_bare_rom(b"", header=b"SEGA IS HERE    "))
    system = System(MACHINES["genesis-tmss"], cartridge)
    assert system.board.tmss_cart_selected
    assert system.board.game_cartridge is cartridge
    system.board.write_byte(0xA14101, 0x00)  # nothing to swap in
    assert system.board.read_word(0x100) == int.from_bytes(b"SE", "big")


# -- the region and version register --------------------------------------------------------


@pytest.mark.parametrize(
    ("machine", "value"),
    [
        ("genesis", 0xA0),  # overseas, NTSC, no FDD, version 0
        ("genesis-tmss", 0xA1),  # ... version 1, which is what TMSS means
        ("megadrive", 0xE0),  # overseas, PAL
        ("megadrive-japan", 0x20),  # domestic, NTSC
        ("megadrive2-japan", 0x21),
    ],
)
def test_version_register_is_the_manuals_fields(machine: str, value: int) -> None:
    """The Manual's page 72 (PDF 84): MODE, VMOD, DISK, RSV, VER3-0."""
    assert MACHINES[machine].version_register == value


def test_the_version_register_reads_the_same_at_both_addresses() -> None:
    """ "Each one can be read at an even address ... as well" (CMD 3.0), and a word
    read has the byte in both halves."""
    board = System(MACHINES["genesis"], Cartridge(_bare_rom(b""))).board
    assert board.read_byte(0xA10001) == board.read_byte(0xA10000) == 0xA0
    assert board.read_word(0xA10000) == 0xA0A0
