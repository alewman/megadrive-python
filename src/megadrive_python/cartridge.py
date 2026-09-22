"""Cartridges: ROM images, their boards, and the registers at $A130F1-$A130FF.

A cartridge answers the 68000's accesses to $000000-$3FFFFF and the /TIME range
$A13000-$A130FF. The board it is on comes from MAME's software list
(``/usr/share/games/mame/hash/megadriv.xml``, read at runtime by SHA-1, as
sms-python reads its lists): the ``slot`` feature names the board and a ``sram``
data area gives the size of battery RAM. An unlisted image falls back to its header
(``RA`` at $1B0, the Manual's backup-RAM field; CMD ``gen-hw.txt`` 4.1).

Boards modelled so far:

- ``rom``: ROM only. Sizes that are not a power of two are padded with $FF to the
  next power of two and the whole is mirrored through $000000-$3FFFFF, as MAME 0.285
  loads them (``md_slot.cpp``, header comment; a detector: no hardware source here
  says what a given board decodes).
- ``rom_sram``, ``rom_fram``: battery RAM (SRAM or FRAM) at $200000 on odd
  addresses. $A130F1 bit 0 switches it over the ROM there and bit 1 write-protects it
  (Phantasy Star IV "will always write $01 to $A130F1 before accessing the RAM",
  CMD ``gen-hw.txt`` 4.3, confirmed on hardware by Jeff Quinn; the write-protect bit
  after MAME ``rom.cpp`` ``write_a13``). On a ROM of 2 MB or less the RAM is mapped
  from power-on (MAME ``md_slot.cpp``: active when the ROM ends at or below it).
- ``rom_ssf2``: the Sega mapper, eight 512 KB banks, registers at $A130F3-$A130FF
  for banks 1-7 (Plutiedev, "Going beyond 4MB"; research).

Other boards (EEPROMs, the unlicensed mappers) are milestone 7; an image on one of
them loads as plain ROM with :attr:`Cartridge.unsupported` naming the board.
"""

from __future__ import annotations

import hashlib
import zipfile
from functools import cache
from pathlib import Path
from xml.etree import ElementTree

MAME_HASH = Path("/usr/share/games/mame/hash/megadriv.xml")
IMAGE_SUFFIXES = (".md", ".bin", ".gen", ".smd")
ROM_WINDOW = 0x400000


def _deinterleave_smd(data: bytes) -> bytes:
    """A Super Magic Drive dump: a 512-byte header, then 16 KB blocks whose first
    half holds the odd bytes and second half the even ones (a copier format)."""
    body = data[512:]
    out = bytearray(len(body))
    for block in range(0, len(body), 0x4000):
        chunk = body[block : block + 0x4000]
        half = len(chunk) // 2
        out[block + 1 : block + len(chunk) : 2] = chunk[:half]
        out[block : block + len(chunk) : 2] = chunk[half:]
    return bytes(out)


def load_image(path: str | Path) -> bytes:
    """Read a cartridge image, from a zip if need be (its first image member)."""
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(IMAGE_SUFFIXES)]
            if not names:
                names = [n for n in archive.namelist() if not n.endswith("/")]
            if not names:
                raise ValueError(f"{path}: no image inside")
            data = archive.read(names[0])
            smd = names[0].lower().endswith(".smd")
    else:
        data = path.read_bytes()
        smd = path.suffix.lower() == ".smd"
    if smd or (len(data) % 0x4000 == 512 and data[8:10] == b"\xaa\xbb"):
        data = _deinterleave_smd(data)
    return data


@cache
def _software_list() -> dict[str, dict[str, str | int]]:
    """SHA-1 of the ROM -> {"name", "slot", "sram"} from MAME's megadriv.xml."""
    table: dict[str, dict[str, str | int]] = {}
    if not MAME_HASH.exists():
        return table
    for software in ElementTree.parse(MAME_HASH).getroot().iter("software"):
        for part in software.iter("part"):
            slot = "rom"
            sram = 0
            for feature in part.iter("feature"):
                if feature.get("name") == "slot":
                    slot = feature.get("value", "rom")
            for area in part.iter("dataarea"):
                if area.get("name") == "sram":
                    sram = int(area.get("size", "0"), 0)
            roms = [r for r in part.iter("rom") if r.get("sha1")]
            if len(roms) == 1:
                table[roms[0].get("sha1", "")] = {
                    "name": software.get("name", ""),
                    "slot": slot,
                    "sram": sram,
                }
    return table


def identify(image: bytes) -> dict[str, str | int] | None:
    """MAME's software-list entry for this exact image, or None."""
    return _software_list().get(hashlib.sha1(image).hexdigest())


def header_backup_ram(image: bytes) -> tuple[int, int] | None:
    """The Manual's backup-RAM header field at $1B0: ``RA``, a type byte, $20,
    then the start and end addresses (CMD ``gen-hw.txt`` 4.1 shows one)."""
    if len(image) < 0x1BC or image[0x1B0:0x1B2] != b"RA":
        return None
    start = int.from_bytes(image[0x1B4:0x1B8], "big")
    end = int.from_bytes(image[0x1B8:0x1BC], "big")
    return (start, end) if end >= start else None


def _padded(image: bytes) -> bytes:
    size = 1
    while size < len(image):
        size <<= 1
    return image + b"\xff" * (size - len(image))


class Cartridge:
    """ROM on the 68000's bus, with optional battery RAM and bank registers."""

    #: A board this module does not model yet (the image runs as plain ROM).
    unsupported: str | None = None

    def __init__(self, image: bytes, *, slot: str = "rom", sram_size: int = 0,
                 name: str = "") -> None:  # fmt: skip
        self.image = image
        self.slot = slot
        self.name = name
        rom = _padded(image) if image else b"\xff\xff"
        # The 4 MB window as one bytes object: mirroring is then a plain index.
        repeats = max(1, ROM_WINDOW // len(rom))
        self.rom = bytes(rom * repeats)[: max(ROM_WINDOW, len(rom))]
        self.rom_size = len(image)
        # A battery RAM's contents before anything is saved are whatever it holds;
        # $FF is what MAME 0.285 starts a new one with, and the lockstep follows it.
        self.sram = bytearray(b"\xff" * sram_size)
        self.sram_start = 0x200000
        self.sram_end = self.sram_start + 2 * sram_size  # odd bytes only
        # $A130F1: bit 0 maps the RAM, bit 1 protects it.
        self.sram_mapped = bool(sram_size) and self.rom_size <= self.sram_start
        self.sram_protected = False
        # The Sega mapper: bank n shows page banks[n] (512 KB pages).
        self.banks = list(range(8))
        self.mapper = slot == "rom_ssf2"
        if self.mapper:
            self.full = image + b"\xff" * (-len(image) % 0x80000)
        if slot not in ("rom", "rom_sram", "rom_fram", "rom_ssf2"):
            self.unsupported = slot

    @classmethod
    def from_file(cls, path: str | Path) -> Cartridge:
        return cls.from_image(load_image(path))

    @classmethod
    def from_image(cls, image: bytes) -> Cartridge:
        """The board from MAME's software list, else from the header."""
        listed = identify(image)
        if listed is not None:
            slot = str(listed["slot"])
            sram = int(listed["sram"])
            if slot in ("rom_sram", "rom_fram") and not sram:
                sram = 0x10000
            return cls(image, slot=slot, sram_size=sram, name=str(listed["name"]))
        backup = header_backup_ram(image)
        if backup is not None:
            start, end = backup
            size = min((end - start) // 2 + 1, 0x8000)
            cart = cls(image, slot="rom_sram", sram_size=size)
            cart.sram_start = start & 0x3FFFFE
            cart.sram_end = cart.sram_start + 2 * size
            cart.sram_mapped = cart.rom_size <= cart.sram_start
            return cart
        return cls(image)

    # -- the 68000's accesses to $000000-$3FFFFF -------------------------------

    def _rom_offset(self, address: int) -> int:
        if self.mapper:
            return (self.banks[address >> 19] << 19) | (address & 0x7FFFF)
        return address

    def read_word(self, address: int) -> int:
        if self.sram_mapped and self.sram_start <= address < self.sram_end:
            # 8-bit RAM on the odd byte: the even byte is undriven. Reads $FF there
            # as MAME does; no hardware source here says what floats on D15-D8.
            return 0xFF00 | self.sram[(address - self.sram_start) >> 1]
        if self.mapper:
            offset = self._rom_offset(address)
            data = self.full
            if offset + 1 < len(data):
                return (data[offset] << 8) | data[offset + 1]
            return 0xFFFF
        rom = self.rom
        return (rom[address] << 8) | rom[address + 1]

    def read_byte(self, address: int) -> int:
        if self.sram_mapped and self.sram_start <= address < self.sram_end:
            return self.sram[(address - self.sram_start) >> 1] if address & 1 else 0xFF
        if self.mapper:
            offset = self._rom_offset(address)
            return self.full[offset] if offset < len(self.full) else 0xFF
        return self.rom[address]

    def write_byte(self, address: int, value: int) -> None:
        if (
            self.sram_mapped
            and not self.sram_protected
            and address & 1
            and self.sram_start <= address < self.sram_end
        ):
            self.sram[(address - self.sram_start) >> 1] = value & 0xFF

    def write_word(self, address: int, value: int) -> None:
        self.write_byte(address | 1, value & 0xFF)

    # -- the /TIME range $A13000-$A130FF ---------------------------------------

    def write_time(self, address: int, value: int) -> None:
        """A byte written to $A130xx (the board decides what it means)."""
        register = address & 0xFF
        if register == 0xF1 and self.sram:
            self.sram_mapped = bool(value & 1)
            self.sram_protected = bool(value & 2)
        elif self.mapper and register & 1 and register >= 0xF3:
            self.banks[(register - 0xF1) >> 1] = value & 0x3F

    def read_time(self, address: int) -> int | None:
        """A byte read from $A130xx, or None when no cartridge chip drives the bus."""
        return None
