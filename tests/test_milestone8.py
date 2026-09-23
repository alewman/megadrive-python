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
