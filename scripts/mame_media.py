"""How to hand a Mega Drive cartridge image to MAME so that it uses the right board.

Adapted from sms-python's ``scripts/mame_media.py`` (same author, MIT; the handoff
brief says to copy that board's design rather than depend on it). The Mega Drive's
problem is the same one and a little worse.

Given a *file* (``-cart game.md``), MAME 0.285 guesses the board from the image
(``bus/megadrive/md_slot.cpp``, ``get_cart_type``: a table of code signatures and
header strings by exact file length, then the header's ``RA`` backup-RAM field) and
falls back to ``rom_sramsafe`` -- 64 KB of battery RAM at $200000 -- for *every*
cartridge whose header does not declare any ("Unfortunately, there are ROMs without
correct info in the header, hence, when loading from fullpath we do the SRAM mapping
anyway", the same file). The board mirrors its ROM there instead, which is Eke's
mirroring rule and what a plain cartridge does.

Loaded by its **software-list name**, MAME uses the board the list gives -- which is
the board this repository's :mod:`megadrive_python.cartridge` also reads from that
list. So an image the list names is offered under that name, from a scratch rompath
holding just that image under the file name the list expects; an unlisted image is
given as a file and :func:`mame_guess` says which board MAME then puts it on.
"""

from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path
from xml.etree import ElementTree

MAME_HASH = Path("/usr/share/games/mame/hash")
LIST = "megadriv"

# ``get_cart_type``'s signatures, by the exact file length it tests them under
# (md_slot.cpp lines 674-880 of MAME 0.285, ``reference/src/mame`` @ f379cd0).
# Each entry is (offset, bytes, slot option). They are tried in order and a later
# match wins, as the C code's sequence of ``if``s does. The slot names are what
# ``md_get_slot`` returns for the type (its ``slot_list``, lines 215-277).
_SMOUSE = bytes((0x4D, 0xF9, 0x00, 0x40, 0x00, 0x02))
_MJLOVER = bytes((0x13, 0xF9, 0x00, 0x40, 0x00, 0x00))
_SQUIR = bytes((0x26, 0x79, 0x00, 0xFF, 0x00, 0xFA))
_BUGSL = bytes((0x20, 0x12, 0x13, 0xC0, 0x00, 0xFF))
_SBUB = bytes((0x0C, 0x39, 0x00, 0x55, 0x00, 0x40))
_LK3 = bytes((0x0C, 0x01, 0x00, 0x30, 0x66, 0xE4))
_SDK = bytes((0x48, 0xE7, 0xFF, 0xFE, 0x52, 0x79))
_REDCLIFF = bytes((0x10, 0x39, 0x00, 0x40, 0x00, 0x04))
_REDCL_EN = bytes((0x50, 0x79, 0x40, 0x00, 0x40, 0x44))
_SMB = bytes((0x20, 0x4D, 0x41, 0x52, 0x49, 0x4F))
_SMB2 = bytes((0x4E, 0xB9, 0x00, 0x0F, 0x25, 0x84))
_KAIJU = bytes((0x19, 0x7C, 0x00, 0x01, 0x00, 0x00))
_CHIFI3 = bytes((0xB6, 0x16, 0x66, 0x00, 0x00, 0x4A))
_LIONK2 = bytes((0x26, 0x79, 0x00, 0xFF, 0x00, 0xF4))
_RX3 = bytes((0x66, 0x00, 0x00, 0x0E, 0x30, 0x3C))
_KOF98 = bytes((0x9B, 0xFC, 0x00, 0x00, 0x4A, 0x00))
_S15IN1 = bytes((0x22, 0x3C, 0x00, 0xA1, 0x30, 0x00))
_KOF99 = bytes((0x20, 0x3C, 0x30, 0x00, 0x00, 0xA1))
_SOULB = bytes((0x33, 0xFC, 0x00, 0x0C, 0x00, 0xFF))
_S19IN1 = bytes((0x13, 0xC0, 0x00, 0xA1, 0x30, 0x38))

SIGNATURES: dict[int, list[tuple[int, bytes, str]]] = {
    0x80000: [
        (0x08C8, _SMOUSE, "rom_smouse"),
        (0x7E30E, b"SEGA", "rom_realtec"),
        (0x7E100, b"SEGA", "rom_realtec"),
        (0x7E1E6, b"SEGA", "rom_realtec"),
        (0x0180, b"GM T-50396", "rom_nhlpa"),
        (0x0180, b"GM MK-1215", "rom_eeprom"),
    ],
    0xC0000: [(0x0180, b"GM G-4060", "rom_eeprom")],
    0x100000: [
        (0x01B24, _MJLOVER, "rom_mjlov"),
        (0x03B4, _SQUIR, "rom_squir"),
        (0xEE0D0, _BUGSL, "rom_bugs"),
        (0x0172, b"GAME : ELF WOR", "rom_elfwor"),
        (0x123E4, _SBUB, "rom_sbubl"),
        (0x0180, b"GM T-50176", "rom_nhlpa"),
        (0x0180, b"MK 00001211-00", "rom_eeprom"),
        (0x0180, b"GM T-120096-", "rom_codemast"),
        (0x0180, b"GM T-120146-", "rom_blara"),
        (0x0190, b"OJKRPTBVFCA     ", "rom_codemast"),
    ],
    0x200000: [
        (0x18C6, _LK3, "rom_lion3"),
        (0x0220, _SDK, "rom_lion3"),
        (0xCE560, _REDCLIFF, "rom_redcl"),
        (0xC8CB0, _SMB, "rom_smb"),
        (0xF24D6, _SMB2, "rom_smb2"),
        (0x674E, _KAIJU, "rom_pokestad"),
        (0x1780, _CHIFI3, "rom_chinf3"),
        (0x03C2, _LIONK2, "rom_lion2"),
        (0xC8B90, _RX3, "rom_rx3"),
        (0x56AE2, _KOF98, "rom_kof98"),
        (0x17BB2, _S15IN1, "rom_mcpir"),
        (0x0180, b"GM T-081326 ", "rom_nbajam"),
        (0x0180, b"GM MK-1228", "rom_eeprom"),
        (0x0180, b"GM T-12046", "rom_eeprom"),
        (0x0150, b"Virtua Racing", "rom_svp"),
        (0x0180, b"SF-004", "rom_sf004"),
    ],
    0x200005: [(0xCE564, _REDCL_EN, "rom_redcl")],
    0x220000: [(0x0180, b"SF-002", "rom_sf002")],
    0x300000: [
        (0x0220, _SDK, "rom_lion3"),
        (0x1FD0D2, _KOF99, "rom_kof99"),
        (0x0180, b"GM T-81406", "rom_nbajamte"),
        (0x0180, b"GM T-081276 ", "rom_nbajamte"),
    ],
    0x400000: [
        (0x028460, _SOULB, "rom_soulblad"),
        (0x1E700, _S19IN1, "rom_mcpir"),
        (0x0180, b"GM T-081586-", "rom_nflqb96"),
        (0x0180, b"GM T-081576 ", "rom_cslam"),
        (0x0180, b"GM T-81476", "rom_cslam"),
        (0x0180, b"SF-001", "rom_sf001"),
    ],
    0x500000: [(0x0120, b"SUPER STREET FIGHTER2 ", "rom_ssf2")],
    0x800000: [(0x0180, b"GM T-574023-", "rom_stm95")],
}


@cache
def _entries() -> dict[str, tuple[str, str, str]]:
    """SHA-1 of the ROM -> (set name, ROM file name, slot) for the single-ROM entries
    of MAME's ``megadriv`` software list (2,656 of its 2,739 parts)."""
    entries: dict[str, tuple[str, str, str]] = {}
    path = MAME_HASH / f"{LIST}.xml"
    if not path.exists():
        return entries
    for software in ElementTree.parse(path).getroot().iter("software"):
        for part in software.iter("part"):
            slot = "rom"
            for feature in part.iter("feature"):
                if feature.get("name") == "slot":
                    slot = feature.get("value", "rom")
            roms = [r for r in part.iter("rom") if r.get("sha1")]
            if len(roms) == 1:
                entries[roms[0].get("sha1", "")] = (
                    software.get("name", ""),
                    roms[0].get("name", ""),
                    slot,
                )
    return entries


def listed(image: bytes) -> tuple[str, str, str] | None:
    """(set name, ROM file name, slot) if MAME's list has this exact image."""
    return _entries().get(hashlib.sha1(image).hexdigest())


def mame_guess(image: bytes) -> str:
    """The board MAME 0.285 puts an image given as a *file* on.

    ``md_slot.cpp`` ``get_cart_type`` then ``md_get_slot``: the signature table
    above, else the header's backup-RAM field ($1B0 ``RA``; a range of fewer than 2
    bytes means a serial EEPROM), else ``rom_sramsafe`` -- the fallback that gives
    every other cartridge 64 KB of battery RAM at $200000.
    """
    slot = ""
    # Rock Man / Mega Man - The Wily Wars is the one entry with two signatures
    # ("GM T-12053" and ``rockman_sig`` at $18E, md_slot.cpp line 800); it sits
    # among the 2 MB entries, where nothing else can match the same image.
    if (
        len(image) == 0x200000
        and image[0x180:0x18A] == b"GM T-12053"
        and image[0x18E:0x190] == bytes((0xEA, 0x80))
    ):
        slot = "rom_eeprom"
    for offset, text, board in SIGNATURES.get(len(image), ()):
        if image[offset : offset + len(text)] == text:
            slot = board
    if slot:
        return slot
    if len(image) > 0x1BC and image[0x1B0:0x1B2] == b"RA":
        start = int.from_bytes(image[0x1B4:0x1B8], "big")
        end = int.from_bytes(image[0x1B8:0x1BC], "big")
        # 32-bit arithmetic, as MAME's: an end below the start wraps and is not < 2.
        return "rom_eeprom" if ((end - start) & 0xFFFFFFFF) < 2 else "rom_sram"
    return "rom_sramsafe"


def mame_board(image: bytes) -> str:
    """The board MAME runs this image on with :func:`cart_arguments`: the software
    list's if the list has it, else :func:`mame_guess`."""
    entry = listed(image)
    return entry[2] if entry is not None else mame_guess(image)


def cart_arguments(image: bytes, work: Path, image_path: Path, rompath: str) -> list[str]:
    """MAME's ``-cart``, ``-hashpath`` and ``-rompath`` arguments for this image.

    A software-list name when the list has the image (a scratch rompath under
    ``work`` then holds it, as loose files named as the list expects), else
    ``image_path``, which this writes.
    """
    entry = listed(image)
    if entry is None:
        image_path.write_bytes(image)
        return ["-cart", str(image_path), "-rompath", rompath]
    setname, romname, _ = entry
    softlist = work / "softlist" / LIST / setname
    softlist.mkdir(parents=True, exist_ok=True)
    (softlist / Path(romname).name).write_bytes(image)
    # -noreadconfig drops MAME's configured hash path, so name it.
    return [
        "-cart", setname,
        "-hashpath", str(MAME_HASH),
        "-rompath", f"{work / 'softlist'};{rompath}",
    ]  # fmt: skip
