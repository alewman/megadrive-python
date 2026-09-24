"""Cartridges: ROM images, their boards, and the registers at $A130F1-$A130FF.

A cartridge answers the 68000's accesses to $000000-$3FFFFF and the /TIME range
$A13000-$A130FF. The board it is on comes from MAME's software list
(``/usr/share/games/mame/hash/megadriv.xml``, read at runtime by SHA-1, as
sms-python reads its lists): the ``slot`` feature names the board and a ``sram``
data area gives the size of battery RAM. An unlisted image falls back to its header
(``RA`` at $1B0, the Manual's backup-RAM field; CMD ``gen-hw.txt`` 4.1).

Boards modelled:

- ``rom``: ROM only. Sizes that are not a power of two are padded with $FF to the
  next power of two and the whole is mirrored through $000000-$3FFFFF, as MAME 0.285
  loads them (``md_slot.cpp``, header comment; a detector: no hardware source here
  says what a given board decodes).
- ``rom_sram``, ``rom_fram``, ``rom_hardbl95``: battery RAM (SRAM or FRAM) at $200000
  on odd addresses. $A130F1 bit 0 switches it over the ROM there and bit 1
  write-protects it (Phantasy Star IV "will always write $01 to $A130F1 before
  accessing the RAM", CMD ``gen-hw.txt`` 4.3, confirmed on hardware by Jeff Quinn;
  the write-protect bit after MAME ``rom.cpp`` ``write_a13``). On a ROM of 2 MB or
  less the RAM is mapped from power-on (MAME ``md_slot.cpp``: active when the ROM
  ends at or below it).
- ``rom_ssf2``: the Sega mapper Super Street Fighter II is on. Eight 512 KB pages
  are visible at once; the registers $A130F3-$A130FF page the second to the eighth
  (Bart Trzynadlowski, ``ssf2tnc.txt`` in ``reference/docs/md_tech.zip``, second
  edition 2000, which lists the register for each range and the game's own
  bank-switching code; Plutiedev, "Going beyond 4MB"). The first 512 KB "is fixed
  and cannot be remapped because that is where the vector table resides", so there
  is no register for it. A page number is six bits, "bits 7 and 6 are always 0".
- ``rom_titan``: Titan's Overdrive 2 board, the same mapper. MAME 0.285 gives it a
  device of its own whose comment says so ("This is same as SSF2 mapper, merge at
  cart slot rewrite", ``bus/megadrive/titan.cpp`` lines 8-9) and whose registers are
  the same words of $A130F0 (``write_a13``, lines 53-66); the demo's own readme
  calls it "SSF2-style bank switching", and it prints that sentence and stops when
  the banks do not move.
- the **serial EEPROM** boards (:mod:`megadrive_python.eeprom`): ``rom_eeprom``,
  ``rom_eeprom_mode1``, ``rom_nbajam_alt``, ``rom_nbajamte``, ``rom_nflqb96``,
  ``rom_cslam``, ``rom_nhlpa``, ``rom_codemast``, ``rom_mm96``, ``rom_blara95``,
  ``rom_blara96``. Each decodes the chip's two lines out of one or two words of the
  68000's address space; :data:`EEPROM_BOARDS` has the table and its sources.
- the **J-Cart** boards (``rom_jcart``, ``rom_jcart_sampras``, and the Codemasters
  EEPROM ones, which are J-Carts too): two more control ports on the cartridge,
  read and written at $38FFFE.

Other boards (the unlicensed mappers, the SVP, the Game Genie) are not modelled; an
image on one of them loads as plain ROM with :attr:`Cartridge.unsupported` naming the
board.
"""

from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from .eeprom import MODE1, MODE2, MODE3, SerialEEPROM

MAME_HASH = Path("/usr/share/games/mame/hash/megadriv.xml")
IMAGE_SUFFIXES = (".md", ".bin", ".gen", ".smd")
ROM_WINDOW = 0x400000

#: Boards that are ROM plus battery RAM.
SRAM_SLOTS = ("rom_sram", "rom_fram", "rom_hardbl95")
#: Boards on Krikzz's Extended SSF, with and without battery RAM. MAME's names for
#: them (``bus/megadrive/cart/options.cpp``); it marks its own entry ``supported="no"``.
SSF_SLOTS = ("ssf", "ssf_sram")
#: Boards on a Sega-mapper-shaped board: the SSF2 mapper, Titan's Overdrive 2 board
#: (MAME's own comment says it is the same mapper), and Krikzz's Extended SSF.
MAPPER_SLOTS = ("rom_ssf2", "rom_titan", *SSF_SLOTS)

#: The bank number a board's page registers take, as a mask.
#:
#: Six bits on the Sega mapper -- "A page is specified with 6 bits (bits 7 and 6 are
#: always 0)" (Trzynadlowski, ``ssf2tnc.txt``) -- and **five** on Krikzz's Extended
#: SSF, whose own document writes the field ``...RRRRR``
#: (``reference/docs/krikzz/extended_ssf-v2.txt``). Five is not a formality: Demons of
#: Asteborg was observed writing page $1C, which MAME's *legacy* ``rom_ssf2`` device
#: would mask to $0C with its ``data & 0xf`` (docs/undocumented-behavior.md item 20).
BANK_MASK = dict.fromkeys(SSF_SLOTS, 0x1F)
DEFAULT_BANK_MASK = 0x3F

#: What a cartridge on Krikzz's Extended SSF says at $100 instead of "SEGA GENESIS".
#: krikzz: "Mapper will be activated in case if ROM header contains "SEGA SSF" string".
#: ``SEGA DOA`` is Demons of Asteborg's own variant of the same announcement, which
#: five of its releases use and four do not; both behave identically (item 20).
SSF_HEADERS = (b"SEGA SSF", b"SEGA DOA")


@dataclass(frozen=True)
class EEPROMBoard:
    """Where one board puts the EEPROM's two lines, and which chip it carries.

    ``sda_in`` is the byte address and bit the 68000 *reads* the chip's /SDA at;
    ``sda_out`` and ``scl`` the byte address and bit it *writes* them at. ``mode``,
    ``size_mask`` and ``page_mask`` are Eke's fields for the chip itself
    (:mod:`megadrive_python.eeprom`).
    """

    sda_in: tuple[int, int]
    sda_out: tuple[int, int]
    scl: tuple[int, int]
    mode: int
    size_mask: int
    page_mask: int
    chip: str
    source: str


#: The EEPROM boards, by the name MAME's software list gives the slot.
#:
#: The numbers are Eke-Eke's ``gen_eeprom.pdf`` (``reference/docs/md_tech.zip``),
#: which is the research document for this milestone; MAME 0.285 repeats the same
#: table in ``bus/megadrive/eeprom.cpp`` and ``jcart.cpp`` and credits him for it, and
#: its code says which of the two addresses is read and which written, which his
#: table's ``SDA_IN``/``SDA_OUT`` naming leaves ambiguous for the Codemasters and
#: Brian Lara boards. Where the two disagree the disagreement is in the ``source``
#: field, and [undocumented-behavior.md] carries it.
# fmt: off
EEPROM_BOARDS: dict[str, EEPROMBoard] = {
    # Sega and Capcom, mode 1: Evander Holyfield's Boxing, Greatest Heavyweights,
    # Wonder Boy in Monster World / Monster World III, Sports Talk Baseball, Dodge
    # Danpei, Ninja Burai Densetsu, Mega Man - The Wily Wars / Rockman Mega World.
    # CMD's gen-eeprom.txt found a Xicor X24C01 on the Monster World 3 and Wily Wars
    # boards, which is the chip Eke's mode 1 is.
    "rom_eeprom_mode1": EEPROMBoard(
        (0x200001, 0), (0x200001, 0), (0x200001, 1), MODE1, 0x7F, 0x03,
        "X24C01", "Eke gen_eeprom.pdf, SEGA and CAPCOM; CMD gen-eeprom.txt",
    ),
    # Ninja Burai Densetsu's prototype. Eke lists the game under SEGA's mode 1;
    # MAME gives this slot a 24C01 in mode 2 instead. The research document is
    # followed, the difference recorded.
    "rom_eeprom": EEPROMBoard(
        (0x200001, 0), (0x200001, 0), (0x200001, 1), MODE1, 0x7F, 0x03,
        "X24C01", "Eke gen_eeprom.pdf, SEGA (MAME uses a mode-2 24C01 here)",
    ),
    # Acclaim type 1: NBA Jam. "Also software use 16-bits write & read" (Eke).
    # His table gives SDA_IN bit 0, SDA_OUT bit 1 and SCL bit 1, which puts the data
    # out and the clock on one bit and cannot be right; MAME's NBA Jam device -- the
    # one its comment says was written until the game worked -- reads /SDA at bit 1
    # and writes /SDA at bit 0 and /SCL at bit 1 (``eeprom.cpp`` lines 606-628). That
    # is what is followed, because the game running is the judge here.
    "rom_nbajam_alt": EEPROMBoard(
        (0x200001, 1), (0x200001, 0), (0x200001, 1), MODE2, 0xFF, 0x03,
        "24C02", "Eke gen_eeprom.pdf, ACCLAIM type #1, with the bits as MAME "
        "eeprom.cpp lines 606-628 has them (his are self-contradictory)",
    ),
    # Acclaim type 2: NBA Jam TE, NFL Quarterback Club.
    "rom_nbajamte": EEPROMBoard(
        (0x200001, 0), (0x200001, 0), (0x200000, 0), MODE2, 0xFF, 0x03,
        "24C02", "Eke gen_eeprom.pdf, ACCLAIM type #2",
    ),
    "rom_nflqb96": EEPROMBoard(
        (0x200001, 0), (0x200001, 0), (0x200000, 0), MODE2, 0x7FF, 0x07,
        "24C16", "Eke gen_eeprom.pdf, ACCLAIM type #2",
    ),
    "rom_cslam": EEPROMBoard(
        (0x200001, 0), (0x200001, 0), (0x200000, 0), MODE3, 0x1FFF, 0x07,
        "24C64", "Eke gen_eeprom.pdf, ACCLAIM type #2",
    ),
    # Electronic Arts: NHLPA Hockey 93, John Madden 93, Bill Walsh, Rings of Power.
    # "These games use 16-bits write & read to access EEPROM" (Eke).
    "rom_nhlpa": EEPROMBoard(
        (0x200001, 7), (0x200001, 7), (0x200001, 6), MODE1, 0x7F, 0x03,
        "X24C01", "Eke gen_eeprom.pdf, ELECTRONIC ARTS",
    ),
    # Codemasters, on a J-Cart: Micro Machines 2, Micro Machines Military.
    "rom_codemast": EEPROMBoard(
        (0x380001, 7), (0x300000, 0), (0x300000, 1), MODE2, 0x3FF, 0x0F,
        "24C08", "Eke gen_eeprom.pdf, CODEMASTERS (which of $380001 and $300000 is "
        "read and which written from MAME jcart.cpp lines 186-210)",
    ),
    "rom_mm96": EEPROMBoard(
        (0x380001, 7), (0x300000, 0), (0x300000, 1), MODE2, 0x7FF, 0x0F,
        "24C16", "Eke gen_eeprom.pdf, CODEMASTERS",
    ),
    # Brian Lara Cricket and Cricket 96 / Shane Warne. Eke gives the Codemasters
    # addresses; MAME puts all three lines on $380000-$380001 for these two boards
    # (eeprom.cpp lines 344-366), which is what is followed, and it gives Brian Lara
    # Cricket a 24C65 where Eke gives it an X24C01. Neither is settled here.
    "rom_blara95": EEPROMBoard(
        (0x380001, 7), (0x380000, 0), (0x380000, 1), MODE1, 0x7F, 0x03,
        "X24C01", "Eke gen_eeprom.pdf, CODEMASTERS mode 1; addresses from MAME "
        "eeprom.cpp lines 344-366 (MAME fits a 24C65 instead)",
    ),
    "rom_blara96": EEPROMBoard(
        (0x380001, 7), (0x380000, 0), (0x380000, 1), MODE3, 0x1FFF, 0x07,
        "24C64", "Eke gen_eeprom.pdf, CODEMASTERS mode 3 (his PAGE_MASK is '?'; "
        "0x07 is MAME's 24C65); addresses from MAME eeprom.cpp lines 344-366",
    ),
}
# fmt: on

#: Boards with the two extra control ports Codemasters put on the cartridge, read and
#: written at $38FFFE (MAME ``jcart.cpp`` lines 150-172: port 3 in bits 5-0 and port 4
#: in bits 13-8, bit 7 and bit 15 set, and bit 0 of a write drives TH on both).
JCART_SLOTS = (
    "rom_jcart", "rom_jcart_sampras", "rom_codemast", "rom_mm96",
    "rom_blara95", "rom_blara96",
)  # fmt: skip
JCART_PORT = 0x38FFFE


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


def _pick_image(archive: zipfile.ZipFile, stem: str) -> str:
    """Which member of a zip is the cartridge.

    Almost every romset entry holds one image and the question does not arise. Where
    it does, the first member is the wrong answer: ``Sonic & Knuckles (World).zip``
    holds the 256 KB ``(Lock-on).bin`` adapter ROM *before* the 2 MB ``.md`` game, so
    taking the first gave a 256 KB image whose reset vector is an odd address, and
    the board raised out of ``System.__init__`` on a flagship retail cartridge.

    The member whose name matches the archive's own is the cartridge; failing that,
    the largest image is. Both rules pick the game for Sonic & Knuckles, and there is
    exactly one such archive in the local romset's 3,514 entries.
    """
    names = [n for n in archive.namelist() if n.lower().endswith(IMAGE_SUFFIXES)]
    if not names:
        names = [n for n in archive.namelist() if not n.endswith("/")]
    if not names:
        raise ValueError("no image inside")
    if len(names) == 1:
        return names[0]
    for name in names:
        if PurePosixPath(name).stem == stem:
            return name
    return max(names, key=lambda n: archive.getinfo(n).file_size)


def load_image(path: str | Path) -> bytes:
    """Read a cartridge image, from a zip if need be (see :func:`_pick_image`)."""
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            try:
                name = _pick_image(archive, path.stem)
            except ValueError as exc:
                raise ValueError(f"{path}: {exc}") from None
            data = archive.read(name)
            smd = name.lower().endswith(".smd")
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
        self.mapper = slot in MAPPER_SLOTS
        self.bank_mask = BANK_MASK.get(slot, DEFAULT_BANK_MASK)
        if self.mapper:
            self.full = image + b"\xff" * (-len(image) % 0x80000)
        #: The serial EEPROM, its board, and its two line levels, or None.
        self.eeprom: SerialEEPROM | None = None
        self.eeprom_board: EEPROMBoard | None = None
        self._scl = 1
        self._sda = 1
        board = EEPROM_BOARDS.get(slot)
        if board is not None:
            self.eeprom_board = board
            self.eeprom = SerialEEPROM(board.mode, board.size_mask, board.page_mask)
        #: The J-Cart's two extra control ports, if the board has them. Each is a
        #: device with the same interface as a pad on the console's own ports, or
        #: None for an empty socket (all pins pulled up, so nothing pressed).
        self.jcart = slot in JCART_SLOTS
        self.jcart_devices: list[object | None] = [None, None]
        self.jcart_th = 1
        #: Anything the cartridge decodes besides plain ROM: an EEPROM's two lines or
        #: a J-Cart's ports.
        self.decoded = self.eeprom is not None or self.jcart
        #: True when *anything* has to be looked at before the ROM -- battery RAM or
        #: :attr:`decoded`. The read and write paths test this one attribute, so a
        #: plain cartridge pays exactly what it paid before either existed; a 68000
        #: fetch is the hottest thing in the emulator.
        self.special = bool(self.sram) or self.decoded
        if slot not in ("rom", *SRAM_SLOTS, *MAPPER_SLOTS) and not self.decoded:
            self.unsupported = slot
        elif len(image) > ROM_WINDOW and slot not in MAPPER_SLOTS:
            # Larger than the 68000's cartridge window with no mapper to page it in.
            # ``_rom_offset`` is the identity without a mapper and the board masks an
            # access to $000000-$3FFFFF, so only the first 4 MB is ever reachable and
            # the rest is unreachable rather than mirrored: the cartridge would run
            # as a truncated copy of itself. Say so instead, so that it is counted
            # apart rather than losing a comparison it cannot win (milestone 8: nine
            # ``Earthion`` images scored ``differs`` for exactly this reason).
            #
            # These boards are real and documented -- most are Krikzz's Extended SSF
            # (``reference/docs/krikzz/extended_ssf-v2.txt``), which the header at
            # $100 announces as "SEGA SSF" -- but implementing that mapper is its own
            # piece of work, and guessing one from the image's size is the very thing
            # MAME's ``SEGA_SRAM_FALLBACK`` does and that this repository counts
            # against it. See docs/polish.md.
            self.unsupported = f"oversize: {len(image) / (1 << 20):.2f} MB, no mapper"

    @classmethod
    def from_file(cls, path: str | Path) -> Cartridge:
        return cls.from_image(load_image(path))

    @classmethod
    def from_image(cls, image: bytes) -> Cartridge:
        """The board from MAME's software list, else from the header."""
        listed = identify(image)
        if listed is not None:
            slot = str(listed["slot"])
            # MAME's ``sram`` data area is the size of the *address range* the RAM
            # answers, not of the RAM: ``m_nvram_end = m_nvram_start + length - 1``
            # and the array it allocates is that many bytes read as 16-bit words
            # (``md_slot.cpp`` lines 618-623 and 101). The chip is eight bits wide on
            # the odd byte (CMD ``gen-hw.txt`` 4.3: Phantasy Star 4's "16k of battery
            # backed RAM mapped to $200001-$203FFF. (odd addresses used)", which is
            # 8 KB of chip in 16 KB of address space), so the board holds half.
            sram = int(listed["sram"]) // 2
            if slot in SRAM_SLOTS and not sram:
                sram = 0x8000  # MAME's default range, $200000 + $FFFF
            return cls(image, slot=slot, sram_size=sram, name=str(listed["name"]))
        if image[0x100:0x108] in SSF_HEADERS:
            # Krikzz's Extended SSF announces itself in the console field, which is
            # the activation rule his own document gives. These cartridges are
            # aftermarket and almost none is in MAME's software list, so the header
            # is the only thing that names the board -- and it is a statement by the
            # cartridge rather than a guess from its size, which is the difference
            # between this and MAME's ``SEGA_SRAM_FALLBACK``.
            #
            # The backup-RAM header then says which of the two boards it is. Twenty
            # of the romset's twenty-two announce ``RA`` over $200000-$20FFFF, which
            # is exactly the range MAME's ``ssf_sram`` device answers with a 32 KB
            # chip on the odd byte; Overdrive 2 and the Joyzz test program announce
            # none and are the plain board.
            backup = header_backup_ram(image)
            if backup is None:
                return cls(image, slot="ssf")
            start, end = backup
            cart = cls(image, slot="ssf_sram", sram_size=min((end - start) // 2 + 1, 0x8000))
            cart.sram_start = start & 0x3FFFFE
            cart.sram_end = cart.sram_start + 2 * len(cart.sram)
            return cart
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

    # -- the battery ------------------------------------------------------------

    @property
    def battery(self) -> str:
        """What this board saves with: "sram", "eeprom" or "" (nothing)."""
        if self.eeprom is not None:
            return "eeprom"
        return "sram" if self.sram else ""

    def save_bytes(self) -> bytes:
        """The battery's contents, to write to a file."""
        if self.eeprom is not None:
            return bytes(self.eeprom.memory)
        return bytes(self.sram)

    def load_bytes(self, data: bytes) -> None:
        """Put a saved file's contents back, as plugging the cartridge in does."""
        if self.eeprom is not None:
            memory = self.eeprom.memory
            memory[: len(data)] = data[: len(memory)]
        elif self.sram:
            self.sram[: len(data)] = data[: len(self.sram)]

    # -- the 68000's accesses to $000000-$3FFFFF -------------------------------

    def _rom_offset(self, address: int) -> int:
        if self.mapper:
            return (self.banks[address >> 19] << 19) | (address & 0x7FFFF)
        return address

    def _decode_read(self, address: int) -> int | None:
        """The EEPROM's /SDA or a J-Cart port, as a byte, or None for ROM.

        The value carries the line at its own bit and zero elsewhere. Nothing pinned
        says what the rest of the byte reads; MAME's own handlers differ among
        themselves (most return the bit alone, ``md_std_eeprom_device::read`` returns
        it in both halves of the word), and every game seen here masks it. Only the
        byte Eke's table names is decoded, so a read of the *other* byte of the same
        word falls through to the ROM.
        """
        board = self.eeprom_board
        if board is not None and address == board.sda_in[0]:
            assert self.eeprom is not None
            return (self.eeprom.read_sda() & 1) << board.sda_in[1]
        if self.jcart and address & ~1 == JCART_PORT:
            return self._jcart_byte(address)
        return None

    def _jcart_byte(self, address: int) -> int:
        """One byte of the J-Cart's port word: port 3 low, port 4 high, bit 7 set."""
        index = 0 if address & 1 else 1
        device = self.jcart_devices[index]
        pins = 0x3F if device is None else device.pins(self.jcart_th)  # type: ignore[attr-defined]
        return (pins & 0x3F) | (self.jcart_th << 6) | 0x80

    def _decode_write(self, address: int, value: int) -> bool:
        """A byte written where the cartridge decodes something; True if it did."""
        board = self.eeprom_board
        touched = False
        if board is not None:
            if address == board.scl[0]:
                self._scl = (value >> board.scl[1]) & 1
                touched = True
            if address == board.sda_out[0]:
                self._sda = (value >> board.sda_out[1]) & 1
                touched = True
            if touched:
                assert self.eeprom is not None
                self.eeprom.update(self._scl, self._sda)
        if self.jcart and address & ~1 == JCART_PORT:
            # "assume TH only actively driven low" (MAME): bit 0 of the write is TH.
            self.jcart_th = value & 1
            touched = True
        return touched

    def read_word(self, address: int) -> int:
        if self.special:
            if self.sram_mapped and self.sram_start <= address < self.sram_end:
                # 8-bit RAM on the odd byte: the even byte is undriven. Reads $FF
                # there as MAME does; no hardware source here says what floats on
                # D15-D8.
                return 0xFF00 | self.sram[(address - self.sram_start) >> 1]
            if self.decoded:
                high = self._decode_read(address)
                low = self._decode_read(address | 1)
                if high is not None or low is not None:
                    return ((high or 0) << 8) | (low or 0)
        if self.mapper:
            offset = self._rom_offset(address)
            data = self.full
            if offset + 1 < len(data):
                return (data[offset] << 8) | data[offset + 1]
            return 0xFFFF
        rom = self.rom
        return (rom[address] << 8) | rom[address + 1]

    def read_byte(self, address: int) -> int:
        if self.special:
            if self.sram_mapped and self.sram_start <= address < self.sram_end:
                return self.sram[(address - self.sram_start) >> 1] if address & 1 else 0xFF
            if self.decoded:
                value = self._decode_read(address)
                if value is not None:
                    return value
        if self.mapper:
            offset = self._rom_offset(address)
            return self.full[offset] if offset < len(self.full) else 0xFF
        return self.rom[address]

    def write_byte(self, address: int, value: int) -> None:
        if not self.special:
            return
        if self.decoded and self._decode_write(address, value):
            return
        if (
            self.sram_mapped
            and not self.sram_protected
            and address & 1
            and self.sram_start <= address < self.sram_end
        ):
            self.sram[(address - self.sram_start) >> 1] = value & 0xFF

    def write_word(self, address: int, value: int) -> None:
        if not self.special:
            return
        if self.decoded:
            # A word write drives both bytes, and several of these boards put the
            # clock on one and the data on the other ("these games use 16-bits write
            # & read to access EEPROM", Eke, of the EA and Acclaim type 1 boards), so
            # both halves are presented before the chip is clocked.
            self._decode_write_word(address, value)
            return
        self.write_byte(address | 1, value & 0xFF)

    def _decode_write_word(self, address: int, value: int) -> None:
        board = self.eeprom_board
        touched = False
        if board is not None:
            for line, setter in (("scl", "_scl"), ("sda_out", "_sda")):
                where, bit = getattr(board, line)
                if where == (address & ~1):
                    setattr(self, setter, (value >> (8 + bit)) & 1)
                    touched = True
                elif where == (address | 1):
                    setattr(self, setter, (value >> bit) & 1)
                    touched = True
            if touched:
                assert self.eeprom is not None
                self.eeprom.update(self._scl, self._sda)
        if self.jcart and address & ~1 == JCART_PORT:
            self.jcart_th = value & 1
            touched = True
        if not touched:
            self.write_byte(address | 1, value & 0xFF)

    # -- the /TIME range $A13000-$A130FF ---------------------------------------

    def write_time(self, address: int, value: int) -> None:
        """A byte written to $A130xx (the board decides what it means).

        $A130F1 maps and protects the battery RAM; $A130F3-$A130FF page the Sega
        mapper's second to eighth 512 KB windows, in that order
        (``ssf2tnc.txt``: $A130F3 -> $080000, ... , $A130FF -> $380000).
        """
        register = address & 0xFF
        if register == 0xF1 and self.sram:
            self.sram_mapped = bool(value & 1)
            self.sram_protected = bool(value & 2)
        elif self.mapper and register & 1 and register >= 0xF3:
            # The page's width is the board's (:data:`BANK_MASK`): six bits on the
            # Sega mapper, five on Krikzz's Extended SSF.
            self.banks[(register - 0xF1) >> 1] = value & self.bank_mask

    def read_time(self, address: int) -> int | None:
        """A byte read from $A130xx, or None when no cartridge chip drives the bus."""
        return None


class BootROM:
    """The TMSS boot ROM in the cartridge's place, while $A14101 bit 0 is clear.

    A TMSS console powers up with its own 16 KB ROM where the cartridge would be, and
    the boot program switches to the cartridge and back with bit 0 of $A14101 while
    running from work RAM (``reference/src/TMSS-Disassembly``, ``RAM_Code``). This is
    a stand-in with a :class:`Cartridge`'s read and write interface so that the board
    swaps one object for the other rather than testing a flag on every access.

    The No-Intro dump is the 2,048 bytes the chip holds; MAME's is the same 2,048
    bytes followed by 14 KB of zeros, which the disassembly's README calls "hardware
    padding". Above the 16 KB the board reads $FFFF, as MAME does (``mdconsole.cpp``
    ``tmss_r``); nothing hardware says what a TMSS board decodes there.
    """

    unsupported = None
    name = "TMSS boot ROM"
    slot = "tmss"
    sram = b""
    eeprom = None
    battery = ""

    def __init__(self, image: bytes) -> None:
        self.image = image
        self.rom = image + b"\x00" * max(0, 0x4000 - len(image))
        self.rom_size = len(self.rom)

    def read_word(self, address: int) -> int:
        if address + 1 < self.rom_size:
            return (self.rom[address] << 8) | self.rom[address + 1]
        return 0xFFFF

    def read_byte(self, address: int) -> int:
        return self.rom[address] if address < self.rom_size else 0xFF

    def write_byte(self, address: int, value: int) -> None:
        """The boot ROM is a ROM."""

    def write_word(self, address: int, value: int) -> None:
        """The boot ROM is a ROM."""

    def write_time(self, address: int, value: int) -> None:
        """No cartridge is decoded while the boot ROM is switched in."""

    def read_time(self, address: int) -> int | None:
        return None

    def save_bytes(self) -> bytes:
        return b""

    def load_bytes(self, data: bytes) -> None:
        """Nothing to load."""
