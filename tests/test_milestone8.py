"""Milestone 8: the compatibility sweep's own tools, on things that need nothing fetched.

The sweep itself needs MAME and the local romset, so it is not a test; what is
testable here is the machinery it rests on -- how a picture is compared, and which
board MAME would put an image on, which is the difference between comparing two
machines and comparing two different machines (docs/worklog.md, milestone 8).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import compat_sweep  # noqa: E402
import mame_media  # noqa: E402


def picture(width: int = 4, height: int = 3, colour: int = 0x102030) -> list[list[int]]:
    return [[colour] * width for _ in range(height)]


def test_compare_counts_every_differing_pixel():
    ours, theirs = picture(), picture()
    assert compat_sweep.compare(ours, theirs) == (0, 12)
    ours[1][2] = 0
    ours[0][0] = 1
    assert compat_sweep.compare(ours, theirs) == (2, 12)


def test_compare_calls_a_different_size_wholly_different():
    # An H32 picture against MAME's H40 snapshot must not be scored on the columns
    # they happen to share: the sweep reports it as ``size``.
    wrong, total = compat_sweep.compare(picture(4, 3), picture(6, 3))
    assert (wrong, total) == (18, 18)


def _image(length: int, pieces: dict[int, bytes] | None = None) -> bytes:
    image = bytearray(b"\x00" * length)
    for offset, text in (pieces or {}).items():
        image[offset : offset + len(text)] = text
    return bytes(image)


def test_mame_guess_falls_back_to_sram_for_a_plain_cartridge():
    # MAME's fullpath loader gives every cartridge without a backup-RAM header
    # 64 KB of battery RAM at $200000 ("in case this is a broken header",
    # md_slot.cpp); the board mirrors its ROM there. That is why a listed image is
    # handed to MAME by its software-list name instead.
    assert mame_media.mame_guess(_image(0x80000)) == "rom_sramsafe"


def test_mame_guess_reads_the_backup_ram_header():
    ram = _image(
        0x80000, {0x1B0: b"RA\xf8\x20" + bytes.fromhex("00200000") + bytes.fromhex("0020ffff")}
    )
    assert mame_media.mame_guess(ram) == "rom_sram"
    # "For some games using serial EEPROM, difference between SRAM end to start is
    # 0 or 1" (md_slot.cpp).
    eeprom = _image(
        0x80000, {0x1B0: b"RA\xf8\x20" + bytes.fromhex("00200000") + bytes.fromhex("00200001")}
    )
    assert mame_media.mame_guess(eeprom) == "rom_eeprom"


def test_mame_guess_takes_a_signature_only_at_its_own_length():
    ssf2 = {0x120: b"SUPER STREET FIGHTER2 "}
    assert mame_media.mame_guess(_image(0x500000, ssf2)) == "rom_ssf2"
    assert mame_media.mame_guess(_image(0x400000, ssf2)) == "rom_sramsafe"


def test_mame_guess_lets_a_later_signature_win():
    # ``get_cart_type`` is a sequence of ifs, so the last match decides; Virtua
    # Racing's string is tested after the EEPROM ones at 2 MB.
    both = {0x180: b"GM MK-1228", 0x150: b"Virtua Racing"}
    assert mame_media.mame_guess(_image(0x200000, both)) == "rom_svp"


@pytest.mark.skipif(
    not (mame_media.MAME_HASH / f"{mame_media.LIST}.xml").exists(),
    reason="MAME's software list is not installed",
)
def test_the_software_list_is_read_by_sha1():
    entries = mame_media._entries()
    assert len(entries) > 2000
    for sha1, (setname, romname, slot) in entries.items():
        assert len(sha1) == 40 and setname and romname and slot
        break


def test_an_image_larger_than_the_window_with_no_mapper_is_unsupported():
    # Without a mapper ``_rom_offset`` is the identity and the board masks an access
    # to $000000-$3FFFFF, so only the first 4 MB of a larger image is reachable: the
    # cartridge would run as a truncated copy of itself and lose a comparison it
    # could not win. Milestone 8 found 29 such images in the local romset, nine of
    # them ``Earthion`` scoring ``differs`` for this reason alone.
    from megadrive_python.cartridge import Cartridge

    big = Cartridge(_image(0x800000))
    assert big.unsupported is not None
    assert big.unsupported.startswith("oversize")
    assert "8.00 MB" in big.unsupported

    # Exactly the window is fine, and so is anything under it.
    assert Cartridge(_image(0x400000)).unsupported is None
    assert Cartridge(_image(0x80000)).unsupported is None


def test_a_mapper_board_may_be_larger_than_the_window():
    # The whole point of the Sega mapper: Super Street Fighter II is 5 MB and
    # Overdrive 2 is 8 MB, and both page 512 KB windows in. Neither is oversize.
    from megadrive_python.cartridge import MAPPER_SLOTS, Cartridge

    for slot in MAPPER_SLOTS:
        cart = Cartridge(_image(0x800000), slot=slot)
        assert cart.unsupported is None, slot


def test_an_unmodelled_board_keeps_its_own_reason_when_it_is_also_oversize():
    # Pier Solar is 8 MB on ``rom_stm95``, which the board does not model at all.
    # The more specific reason wins; "oversize" would say less.
    from megadrive_python.cartridge import Cartridge

    assert Cartridge(_image(0x800000), slot="rom_stm95").unsupported == "rom_stm95"


def test_a_zip_with_two_images_yields_the_cartridge_not_the_lock_on(tmp_path):
    # ``Sonic & Knuckles (World).zip`` holds the 256 KB ``(Lock-on).bin`` adapter ROM
    # before the 2 MB game, and taking the zip's first image member gave the adapter:
    # a 256 KB image whose initial PC is odd, which raised ``GroupZero`` out of
    # ``System.__init__`` on a flagship retail cartridge (milestone 8's sweep found
    # it as one of two remaining ``error:`` rows). The member named after the archive
    # is the cartridge.
    import zipfile

    from megadrive_python.cartridge import load_image

    path = tmp_path / "Sonic & Knuckles (World).zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Sonic & Knuckles (World) (Lock-on).bin", _image(0x40000, {0: b"LOCK"}))
        archive.writestr("Sonic & Knuckles (World).md", _image(0x200000, {0: b"GAME"}))
    image = load_image(path)
    assert len(image) == 0x200000
    assert image[:4] == b"GAME"


def test_a_zip_with_two_images_and_no_name_match_takes_the_largest(tmp_path):
    import zipfile

    from megadrive_python.cartridge import load_image

    path = tmp_path / "whatever.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("small.bin", _image(0x40000, {0: b"SMAL"}))
        archive.writestr("big.md", _image(0x200000, {0: b"BIGG"}))
    assert load_image(path)[:4] == b"BIGG"


def test_a_zip_with_one_image_is_unchanged(tmp_path):
    import zipfile

    from megadrive_python.cartridge import load_image

    path = tmp_path / "one.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("only.md", _image(0x80000, {0: b"ONLY"}))
    assert load_image(path)[:4] == b"ONLY"


def _ssf_image(length: int, header: bytes, backup: bool) -> bytes:
    pieces = {0x100: header}
    if backup:
        # The Manual's backup-RAM field: "RA", then the odd-byte flags, then the
        # range. $200000-$20FFFF is what all twenty of the romset's battery-backed
        # Extended SSF cartridges declare.
        pieces[0x1B0] = b"RA\xf8\x20\x00\x20\x00\x00\x00\x20\xff\xff"
    return _image(length, pieces)


def test_a_sega_ssf_header_selects_krikzz_extended_ssf():
    # krikzz: "Mapper will be activated in case if ROM header contains "SEGA SSF"
    # string". The header is a statement by the cartridge, not a guess from its size,
    # which is what separates this from MAME's SEGA_SRAM_FALLBACK.
    from megadrive_python.cartridge import Cartridge

    plain = Cartridge.from_image(_ssf_image(0x600000, b"SEGA SSF        ", False))
    assert plain.slot == "ssf"
    assert plain.mapper and plain.unsupported is None
    assert not plain.sram

    battery = Cartridge.from_image(_ssf_image(0x600000, b"SEGA SSF        ", True))
    assert battery.slot == "ssf_sram"
    assert len(battery.sram) == 0x8000  # 32 KB, as MAME's ssf_sram device allocates
    assert battery.sram_start == 0x200000


def test_sega_doa_is_the_same_board():
    # Five Demons of Asteborg releases announce SEGA DOA and four announce SEGA SSF;
    # both were observed writing the same registers (undocumented-behavior.md item 20).
    from megadrive_python.cartridge import Cartridge

    assert Cartridge.from_image(_ssf_image(0xE00000, b"SEGA DOA        ", True)).slot == "ssf_sram"


def test_the_extended_ssf_bank_field_is_five_bits():
    # Demons of Asteborg was observed writing page $1C. MAME's legacy rom_ssf2 device
    # masks data & 0xf and would fetch $0C for it; krikzz's field is ...RRRRR.
    from megadrive_python.cartridge import Cartridge

    cart = Cartridge.from_image(_ssf_image(0xE00000, b"SEGA SSF        ", True))
    assert cart.bank_mask == 0x1F
    cart.write_time(0xA130FF, 0x1C)
    assert cart.banks[7] == 0x1C
    cart.write_time(0xA130FF, 0xFF)  # bits 7-5 are not part of the field
    assert cart.banks[7] == 0x1F


def test_the_sega_mapper_keeps_its_six_bit_field():
    # Trzynadlowski: "A page is specified with 6 bits (bits 7 and 6 are always 0)".
    from megadrive_python.cartridge import Cartridge

    cart = Cartridge(_image(0x500000), slot="rom_ssf2")
    assert cart.bank_mask == 0x3F
    cart.write_time(0xA130FF, 0xFF)
    assert cart.banks[7] == 0x3F


def test_an_extended_ssf_cartridge_maps_its_ram_with_a130f1():
    # $A130F1 stays the Sega mapper's SRAM control on these boards, which is what the
    # games want and what MAME implements, against krikzz's own document
    # (undocumented-behavior.md item 20). Demons of Asteborg writes $01 and $03.
    from megadrive_python.cartridge import Cartridge

    cart = Cartridge.from_image(_ssf_image(0xE00000, b"SEGA DOA        ", True))
    assert not cart.sram_mapped  # ROM covers $200000 until the game asks for the RAM
    cart.write_time(0xA130F1, 0x01)
    assert cart.sram_mapped and not cart.sram_protected
    cart.write_time(0xA130F1, 0x03)
    assert cart.sram_mapped and cart.sram_protected
