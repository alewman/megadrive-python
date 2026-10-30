"""The cartridge boards that are their own class: bank mappers and protection chips.

Every board here is in MAME's software list under a slot name (:data:`BOARDS`). None has
a datasheet. The sources are MAME's device files under
``reference/src/mame/src/devices/bus/megadrive/``: the newer ``cart/`` layout where the
board is in it (its author annotated the protection values with the program counters
that read them), else the 0.285 ``rom.cpp``. Where the two differ, the newer wins and
the difference is in docs/undocumented-behavior.md; this is a *detector*, not hardware.
Plutiedev and the SegaRetro pages cited in MAME's comments are the only other sources.

**Byte lanes.** A register on one of these boards is a byte at one address, and the
68000's 16-bit bus puts the byte at an even address on D15-D8 and at an odd address on
D7-D0. MAME maps them as 8-bit handlers and so does :class:`Board` here: a word access
is two byte accesses, and a word read of a protection register reads the value in the
high byte and ``$FF`` below it (nothing else drives the low lane).

**$400000-$7FFFFF.** The protection chips live there; :attr:`Board.upper` tells the
console's bus to ask the cartridge instead of reading open bus.
"""

from __future__ import annotations

from typing import ClassVar

from .cartridge import BOARDS, ROM_WINDOW, Cartridge, _padded


def board(*slots: str):
    """Register a class for these slot names."""

    def register(cls: type[Cartridge]) -> type[Cartridge]:
        for slot in slots:
            BOARDS[slot] = cls
        return cls

    return register


def _bitswap(value: int, *bits: int) -> int:
    """MAME's ``bitswap<N>(value, a, b, ...)``: the first index lands in the top bit."""
    out = 0
    for index in bits:
        out = (out << 1) | ((value >> index) & 1)
    return out


class Board(Cartridge):
    """A board as byte-wide reads and writes over $000000-$7FFFFF.

    Subclasses override :meth:`rb` and :meth:`wb` (and the /TIME hooks) and leave the
    word and byte paths of the 68000 to this class.
    """

    #: Fixed bytes read at an even address in the upper window, by address.
    CONSTANTS: ClassVar[dict[int, int]] = {}
    #: Fixed words read from $A130xx, by register offset in bytes.
    TIME_WORDS: ClassVar[dict[int, int]] = {}

    def __init__(self, image: bytes, *, slot: str = "rom", sram_size: int = 0,
                 name: str = "") -> None:  # fmt: skip
        super().__init__(image, slot=slot, sram_size=0, name=name)
        self.padded = _padded(image) if image else b"\xff\xff"
        self.special = True
        self.upper = bool(self.CONSTANTS) or type(self).upper
        self.start()

    def start(self) -> None:
        """Power-on state of whatever the board latches."""

    def reset(self) -> None:
        """The console's reset button reached the cartridge connector."""

    # -- the bus -------------------------------------------------------------

    def rb(self, address: int) -> int:
        """The byte the board drives at ``address`` (0 to $7FFFFF)."""
        if address < ROM_WINDOW:
            return self.rom[address]
        return self.CONSTANTS.get(address, 0xFF)

    def wb(self, address: int, value: int) -> None:
        """A byte written at ``address``."""

    def read_word(self, address: int) -> int:
        return (self.rb(address) << 8) | self.rb(address | 1)

    def read_byte(self, address: int) -> int:
        return self.rb(address)

    def write_word(self, address: int, value: int) -> None:
        self.wb(address & ~1, value >> 8)
        self.wb(address | 1, value & 0xFF)

    def write_byte(self, address: int, value: int) -> None:
        self.wb(address, value)

    # -- /TIME, $A13000-$A130FF ----------------------------------------------

    def read_time_word(self, address: int) -> int | None:
        return self.TIME_WORDS.get(address & 0xFE)

    def read_time(self, address: int) -> int | None:
        word = self.read_time_word(address & ~1)
        return None if word is None else word & 0xFF

    def write_time(self, address: int, value: int) -> None:
        """Every /TIME write reaches here as its low byte (the old interface)."""

    def write_time_lanes(self, address: int, high: int, low: int, word: bool) -> None:
        """A write to $A130xx with the byte lanes the 68000 drove: a byte write puts its
        byte on both and only the lane at ``address`` is the register's."""
        if word:
            self.time_byte(address & 0xFE, high)
            self.time_byte(address | 1, low)
        else:
            self.time_byte(address & 0xFF, low)

    def time_byte(self, offset: int, value: int) -> None:
        """A byte written at $A130xx + ``offset``."""


class _Battery(Board):
    """A board with a byte-wide battery RAM on the low lane (``0xff00 | byte`` on read,
    writes only when D7-D0 are driven, as MAME's ``nvram_r``/``nvram_w``)."""

    #: (first address, last address, chip size in bytes); the chip repeats inside.
    RAM = (0, -1, 0)

    def start(self) -> None:
        first, _, size = self.RAM
        self.sram = bytearray(b"\xff" * size)
        self.sram_start = first
        self.sram_end = first + 2 * size
        self.ram_on = True

    def ram_index(self, address: int) -> int | None:
        first, last, size = self.RAM
        if self.ram_on and first <= address <= last:
            return ((address - first) >> 1) & (size - 1)
        return None

    def rb(self, address: int) -> int:
        index = self.ram_index(address)
        if index is not None:
            return self.sram[index] if address & 1 else 0xFF
        return super().rb(address)

    def wb(self, address: int, value: int) -> None:
        index = self.ram_index(address)
        if index is not None and address & 1:
            self.sram[index] = value


# -- protection read-backs: a fixed byte at a few addresses ------------------------------


def _constants(*slots: str, **values: int):
    """A board whose only decoding is fixed bytes in the upper window."""
    table = {int(key[1:], 16): value for key, value in values.items()}

    class Fixed(Board):
        CONSTANTS = table

    Fixed.__name__ = Fixed.__qualname__ = "Fixed_" + "_".join(slots)
    return board(*slots)(Fixed)


# Creaton, Gamtec, Chuanpu and Never Ending Soft: "a fixed data (discrete?) chip at
# $400000 as protection device" (gamtec.cpp). The names after the PCs are MAME's.
_constants("rom_16mj2", a400002=0x98, a400004=0xC9, a400006=0x18)
_constants("rom_elfwor", a400000=0x55, a400002=0x0F, a400004=0xC9, a400006=0x18)
_constants("rom_smouse", a400000=0x55, a400002=0x0F, a400004=0xAA, a400006=0xF0)
# "unconfirmed values (carried over from GPGX)" for 777 Casino
_constants("rom_777casino", a400000=0x63, a400002=0x98, a400004=0xC9, a400006=0x18)
_constants("rom_soulb", a400000=0x63, a400002=0x98, a400004=0xC9, a400006=0xF0)
_constants("rom_sbubl", a400000=0x55, a400002=0x0F)
_constants("rom_cjmjclub", a400000=0x90, a400002=0xD3)
_constants("rom_mjlov", a400000=0x90, a401000=0xD3)


@board("rom_rockheaven")
class RockHeaven(Board):
    """RockHeaven: one word at $500008 (rockworld.cpp)."""

    upper = True
    WORDS: ClassVar[dict[int, int]] = {0x500008: 0x5082}

    def rb(self, address: int) -> int:
        word = self.WORDS.get(address & ~1)
        if word is not None:
            return (word >> 8) if not address & 1 else word & 0xFF
        return super().rb(address)


@board("rockworld")
class RockWorld(RockHeaven):
    WORDS: ClassVar[dict[int, int]] = {0x500008: 0x4000, 0x500208: 0xA000}


# The /TIME range as read-only words ("map(0x00, 0x01).lr16"): xboy.cpp and smb.cpp.
def _time_words(*slots: str, **values: int):
    table = {int(key[1:], 16): value for key, value in values.items()}

    class Words(Board):
        TIME_WORDS = table

    Words.__name__ = Words.__qualname__ = "TimeWords_" + "_".join(slots)
    return board(*slots)(Words)


_time_words("rom_smb", t00=0x1C)  # MAME 0.285's rom.cpp has $0C; cart/smb.cpp $1C
_time_words("rom_smb2", t00=0x0A)
_time_words("rom_bugs", t00=0x28, t02=0x01, t3e=0x1F)
_time_words("rom_pokea", t00=0x14, t02=0x01, t3e=0x1F)
_time_words("rom_kof99", t00=0x00, t02=0x01, t3e=0x1F)  # "Secondary memory access failure"


@board("rom_rx3")
class RockmanX3(Board):
    """Rockman X3: two protection bytes ($400004 "anded with 0x9c" at startup, $400006 at
    the start of a level) and one /TIME word."""

    CONSTANTS: ClassVar[dict[int, int]] = {0x400004: 0xC9, 0x400006: 0xF0}
    TIME_WORDS: ClassVar[dict[int, int]] = {0x00: 0x0C}


@board("rom_kof98")
class KOF98(Board):
    """King of Fighters '98: words in two ranges of the upper window.

    "everytime, trashes stack otherwise" ($480000-$4BFFFF); the pointer the menu
    reaches it through is $4C0000-$4FFFFF (xboy.cpp)."""

    upper = True

    def rb(self, address: int) -> int:
        if 0x480000 <= address < 0x4C0000:
            return 0xAA if not address & 1 else 0x00
        if 0x4C0000 <= address < 0x500000:
            return 0xF0 if not address & 1 else 0x00
        return super().rb(address)


# -- latches -----------------------------------------------------------------------------


@board("rom_squir")
class SquirrelKing(Board):
    """Squirrel King: $400000-$400006 is one read/write byte latch (gamtec.cpp)."""

    upper = True

    def start(self) -> None:
        self.latch = 0xFF

    def rb(self, address: int) -> int:
        if 0x400000 <= address < 0x400008 and not address & 1:
            return self.latch
        return super().rb(address)

    def wb(self, address: int, value: int) -> None:
        if 0x400000 <= address < 0x400008 and not address & 1:
            self.latch = value


@board("rom_lion2")
class LionKing2(Board):
    """The Lion King II: two latches, written at $400000 and $400004 and read back at
    $400002 and $400006 (``rom.cpp``; ``cart/gamtec.cpp`` indexes its two-entry array by
    the address bits 4 and 0, which reaches past its end)."""

    upper = True

    def start(self) -> None:
        self.latch = [0xFF, 0xFF]

    def rb(self, address: int) -> int:
        if address in (0x400002, 0x400006):
            return self.latch[(address >> 2) & 1]
        return super().rb(address)

    def wb(self, address: int, value: int) -> None:
        if address in (0x400000, 0x400004):
            self.latch[(address >> 2) & 1] = value


@board("rom_tekkensp")
class TekkenSpecial(Board):
    """Tekken Special: a four-bit shift of strobes read back minus one.

    A write at $400000 clears it; $400004, $400006, $400008 and $40000A set bits 0 to 3
    when D0 of the byte is set; $400002 reads the latch minus one (tekkensp.cpp)."""

    upper = True

    def start(self) -> None:
        self.latch = 0

    def rb(self, address: int) -> int:
        if address == 0x400002:
            return (self.latch - 1) & 0xFF
        return super().rb(address)

    def wb(self, address: int, value: int) -> None:
        if address == 0x400000:
            self.latch = 0
        elif address in (0x400004, 0x400006, 0x400008, 0x40000A) and value & 1:
            self.latch |= 1 << ((address - 0x400004) >> 1)


@board("rom_tc2000")
class TC2000(Board):
    """TC 2000 / Truco '96: the ROM is only 1 MB; $100000-$1FFFFF is a strobe-then-read
    protection byte (miky.cpp): a write at offset 0 of 16 gives $00, at 8 gives $50 and
    at 12 gives $A0, and every even address reads it back."""

    def start(self) -> None:
        self.latch = 0

    def rb(self, address: int) -> int:
        if address < 0x100000:
            return self.rom[address]
        if address < 0x200000:
            return self.latch if not address & 1 else 0xFF
        return 0xFF

    def wb(self, address: int, value: int) -> None:
        if 0x100000 <= address < 0x200000 and not address & 1:
            self.latch = {0x0: 0x00, 0x8: 0x50, 0xC: 0xA0}.get(address & 0xF, self.latch)


# -- banking -----------------------------------------------------------------------------


@board("rom_chinf3")
class ChineseFighter3(Board):
    """Chinese Fighter III: a 64 KB window at 0 shows page ``n`` repeated through the
    first 2 MB while $600000 holds a non-zero byte, and four protection latches sit at
    $400000, $400004, $400008, $40000C (gamtec.cpp)."""

    upper = True

    def start(self) -> None:
        self.page: int | None = None
        self.latch = [0xFF] * 4

    def rb(self, address: int) -> int:
        if address < 0x200000:
            if self.page is None:
                return self.rom[address]
            return self.padded[((self.page << 16) | (address & 0xFFFF)) % len(self.padded)]
        if 0x400000 <= address < 0x500000:
            return self.latch[(address >> 2) & 3]
        return super().rb(address)

    def wb(self, address: int, value: int) -> None:
        if 0x400000 <= address < 0x500000:
            self.latch[(address >> 2) & 3] = value
        elif 0x600000 <= address < 0x700000 and not address & 1:
            # cart/gamtec.cpp mirrors this byte register through both lanes; 0.285's
            # rom.cpp tests the *word* ($F100, $D700, $D300, $0000), so a word write's
            # zero low byte must not count: only the even lane (D15-D8) does.
            self.page = value & 0xF if value else None


@board("rom_lion3", "rom_pokestad")
class TopFighter(Board):
    """Top Fighter 2000 and the Lion King 3 family (xboy.cpp ``megadrive_unl_topf``).

    $700000-$7FFFFF takes a bank byte: zero shows the ROM straight; anything else maps
    the first MB as 32 KB pages, page ``k`` of the window showing image page
    ``(k | byte)`` (the odd half ``k | byte | 1``), modulo 64. A bit-swap device sits at
    $600000: a latch (odd byte 1), a mode (3) and the result (5), mirrored through the
    megabyte every 8 bytes. Pokemon Stadium is the one game that never reads it."""

    upper = True

    def start(self) -> None:
        self.bank = 0
        self.latch = 0
        self.mode = 0

    def rb(self, address: int) -> int:
        if address < 0x200000:
            if not self.bank:
                return self.rom[address]
            if address >= 0x100000:
                return 0xFF
            half = (address >> 15) & 0x1F
            page = (
                ((half | self.bank) & 0x3F)
                if not half & 1
                else ((half - 1) | (self.bank | 1)) & 0x3F
            )
            # window k (even) shows (k | data); odd k shows (k-1 | data | 1)
            return self.padded[((page << 15) | (address & 0x7FFF)) % len(self.padded)]
        if 0x600000 <= address < 0x700000 and address & 7 == 5:
            latch = self.latch
            return (
                (latch << 1) & 0xFF
                if self.mode == 0
                else latch >> 1
                if self.mode == 1
                else _bitswap(latch, 3, 2, 1, 0, 7, 6, 5, 4)
                if self.mode == 2
                else _bitswap(latch, 0, 1, 2, 3, 4, 5, 6, 7)
            )
        return super().rb(address)

    def wb(self, address: int, value: int) -> None:
        if 0x600000 <= address < 0x700000:
            if address & 7 == 1:
                self.latch = value
            elif address & 7 == 3:
                self.mode = value & 3
        elif address >= 0x700000:
            self.bank = value

    def write_word(self, address: int, value: int) -> None:
        # cart/xboy.cpp takes a byte on either lane; Pokemon Stadium writes it at an odd
        # address ($770077). 0.285's rom.cpp takes ``data & 0xff`` of a word write, so a
        # word write counts by its low byte, not its (zero) high one.
        if address >= 0x700000:
            self.bank = value & 0xFF
        else:
            super().write_word(address, value)


@board("rom_realtec")
class Realtec(Board):
    """Realtec / AV Artisan: the ROM in 8 KB pages, every one of 64 window slots showing
    the last page at power-on; $402000 and $404000 write the size and select, and a
    write of D0 to $400000 applies them once, then locks (avartisan.cpp: "practically
    only funnywld sets these to non-zero values")."""

    upper = True

    def start(self) -> None:
        self.size = 0
        self.select = 0
        self.locked = False
        self.slots = [0x3F] * 0x40
        self.pages = max(1, len(self.padded) >> 13)

    def rb(self, address: int) -> int:
        if address < 0x400000:
            slot = (address >> 13) & 0x3F
            return self.padded[(((self.slots[slot]) % self.pages) << 13) | (address & 0x1FFF)]
        return super().rb(address)

    def wb(self, address: int, value: int) -> None:
        if address == 0x402000:
            self.size = (value & 3) << 4
        elif address == 0x404000:
            self.select = (value & 3) << 4
        elif address == 0x400000 and not self.locked and value & 1:
            base = self.select & self.size
            self.slots = [((i & ~self.size) | base) & 0x3F for i in range(0x40)]
            self.locked = True


@board("rom_redcl")
class RedCliff(Board):
    """The Battle of Red Cliffs: the dump is XORed with $40 and four bytes too long; the
    decrypted image is the 4 MB window, $BF (=$FF ^ $40) beyond it (gamtec.cpp), and
    $400000 / $400004 read $55 / $AA."""

    CONSTANTS: ClassVar[dict[int, int]] = {0x400000: 0x55, 0x400004: 0xAA}

    def start(self) -> None:
        plain = bytearray(b"\xbf" * ROM_WINDOW)
        body = bytes(b ^ 0x40 for b in self.image[4 : 4 + ROM_WINDOW])
        plain[: len(body)] = body
        self.rom = bytes(plain)


@board("rom_mcpir")
class MultiCartPirate(Board):
    """The pirate "X in 1" multicarts of the S series: four 128 KB windows over the
    first 512 KB (mirrored through 4 MB), page ``(n + i) mod pages`` in window ``i``,
    where ``n`` is the register address' bits 5-1 of any write in $A130xx on the even
    byte (mcpirate.cpp)."""

    def start(self) -> None:
        self.pages = max(1, len(self.padded) >> 17)
        self.select = 0

    def rb(self, address: int) -> int:
        if address < ROM_WINDOW:
            window = (address >> 17) & 3
            page = (self.select + window) & (self.pages - 1)
            return self.padded[(page << 17) | (address & 0x1FFFF)]
        return 0xFF

    def time_byte(self, offset: int, value: int) -> None:
        if not offset & 1:
            self.select = (offset & 0x3E) >> 1


@board("rom_cm2in1")
class Codemasters2in1(Board):
    """Codemasters 2-in-1: two 2 MB games, the other one after every reset
    (multigame.cpp: "start in a predictable manner" with game 1, so the first power-on
    shows game 0)."""

    def start(self) -> None:
        self.game = 0

    def reset(self) -> None:
        self.game ^= 1

    def rb(self, address: int) -> int:
        if address < ROM_WINDOW:
            return self.padded[((self.game << 21) | (address & 0x1FFFFF)) % len(self.padded)]
        return 0xFF


@board("rom_smw64")
class SuperMarioWorld64(Board):
    """Super Mario World 64 (smw64.cpp): the lower 512 KB at 0 (mirrored to 1 MB), two
    64 KB windows of the upper ROM at $600000 and $610000 (mirrored every 512 KB), and
    a small arithmetic device behind the rest of $60xxxx-$67xxxx."""

    upper = True

    def start(self) -> None:
        self.page = [8, 8]
        self.latch = [0, 0, 0]
        self.ctrl = [0, 0]
        self.data = [0, 0]
        self.page_67 = 0
        self.page_61 = 0

    def _page_byte(self, which: int, address: int) -> int:
        return self.padded[((self.page[which] << 16) | (address & 0xFFFF)) % len(self.padded)]

    def rb(self, address: int) -> int:
        if address < 0x100000:
            return self.padded[address & 0x7FFFF]
        if not 0x600000 <= address < 0x700000:
            return super().rb(address)
        block = (address >> 16) & 7  # $60..$67 (mirrored through the megabyte)
        if block == 0:
            return self._page_byte(0, address)
        if block == 1:
            return self._page_byte(1, address)
        if block == 6:
            return self._latch_read(address)
        if block == 7:
            return self._control_read(address)
        return 0xFF

    def _latch_read(self, address: int) -> int:
        offset = address & 0xF
        if offset & 1 == 0:
            return 0xFF
        table = {
            1: self.latch[0],
            3: self.latch[0] + 1,
            5: self.latch[1],
            7: self.latch[1] + 1,
            9: self.latch[2],
            11: self.latch[2] + 1,
            13: self.latch[2] + 2,
            15: self.latch[2] + 3,
        }
        return table[offset] & 0xFF

    def _control_read(self, address: int) -> int:
        if not address & 1:
            return 0xFF
        result = 0
        if self.page_61 & 0x80:
            result = self.ctrl[1] & self.data[1] if self.page_67 & 0x40 else self.ctrl[1] ^ 0xFF
        if address & 2:
            return result & 0x7F
        if self.page_67 & 0x80:
            if self.page_67 & 0x20:
                self.latch[2] = (self.data[1] << 2) & 0xFC
            else:
                self.latch[0] = (self.data[0] ^ (self.ctrl[1] << 1)) & 0xFE
        return result

    def wb(self, address: int, value: int) -> None:
        if not 0x600000 <= address < 0x700000:
            return
        block = (address >> 16) & 7
        if block == 0:
            if address & 3 == 1:
                self.ctrl[0] = value
            elif address & 3 == 3:
                mode = self.ctrl[0] & 7
                if mode == 0:
                    self.latch[0] = ((self.latch[0] ^ self.data[0]) ^ value) & 0xFE
                elif mode == 1:
                    self.latch[1] = value & 0xFE
                elif mode == 7:
                    self.page[1] = 8 + ((value & 0x1C) >> 2)
                self.data[0] = value
        elif block == 1:
            if address & 3 == 3:
                self.page_61 = value
        elif block == 4:
            if address & 3 == 1:
                self.ctrl[1] = value
            elif address & 3 == 3:
                self.data[1] = value
        elif block == 7 and address & 3 == 1:
            self.page_67 = value
            if self.page_61 & 0x80:
                self.page[0] = 8 + ((value & 0x1C) >> 2)


# -- battery RAM on odd boards ------------------------------------------------------------


@board("rom_tplay96")
class TriplePlay96(_Battery):
    """Triple Play '96: 32 KB of 8-bit RAM at $200000-$20FFFF, no control register."""

    RAM = (0x200000, 0x20FFFF, 0x8000)


@board("rom_sanguo5")
class Sanguo5(_Battery):
    """San Guo Zhi V / Tun Shi Tian Di III: invalid header, 8 KB RAM at $200000-$203FFF."""

    RAM = (0x200000, 0x203FFF, 0x2000)


@board("rom_yasech")
class YaSeChuanShuo(_Battery):
    """Ya Se Chuan Shuo / Wu Kong Wai Zhuan: 4 KB RAM at $200000-$201FFF and the
    protection bytes of the same Gamtec family."""

    RAM = (0x200000, 0x201FFF, 0x1000)
    CONSTANTS: ClassVar[dict[int, int]] = {
        0x400000: 0x63,
        0x400002: 0x98,
        0x400004: 0xC9,
        0x400006: 0x18,
    }


@board("rom_sram_arg96")
class FutbolArgentino96(_Battery):
    """Futbol Argentino 96: 8 KB RAM at $200000-$203FFF, the 2 MB ROM mirrored through
    the rest, and three fixed odd-byte reads at $4C6201 / $4C6601 / $4C6A01 that the game
    only checks before DMA (miky.cpp: "protection not really understood")."""

    RAM = (0x200000, 0x203FFF, 0x2000)
    CONSTANTS: ClassVar[dict[int, int]] = {0x4C6201: 0x0A, 0x4C6601: 0x09, 0x4C6A01: 0x07}

    def rb(self, address: int) -> int:
        index = self.ram_index(address)
        if index is not None:
            return self.sram[index] if address & 1 else 0xFF
        if address < ROM_WINDOW:
            return self.padded[address & 0x1FFFFF]
        return self.CONSTANTS.get(address, 0xFF)


# -- Super Fighter Team ------------------------------------------------------------------


@board("rom_xinqig")
class XinQiGaiWangZi(_Battery):
    """Xin Qi Gai Wang Zi: 16 KB of RAM at $400000-$407FFF. The game checks that $100
    reads its own vectors and resets if not, "presumably against copiers" (sfteam.cpp)."""

    upper = True
    RAM = (0x400000, 0x407FFF, 0x4000)


class _SuperFighter(_Battery):
    """Common to Super Fighter Team's three: control bytes written at $E00 (low lane),
    a lock that once set "needs an hard reset to lift", and RAM that is off at power-on."""

    def start(self) -> None:
        super().start()
        self.ram_on = False
        self.locked = False


@board("rom_sf001")
class BeggarPrince(_SuperFighter):
    """Beggar Prince: Xin Qi Gai Wang Zi's mapper with a $E00 control byte. Bit 7 set
    shows the RAM and puts the 256 KB page at $380000 over $000000-$03FFFF (the program
    expects the original bank at 0 for its copier check, which MAME takes as the page);
    bit 5 locks the register."""

    upper = True
    RAM = (0x400000, 0x407FFF, 0x4000)
    PAGE = 0x0E

    def start(self) -> None:
        super().start()
        self.remap = False

    def rb(self, address: int) -> int:
        if self.remap and address < 0x40000:
            return self.padded[((self.PAGE << 18) | address) % len(self.padded)]
        return super().rb(address)

    def wb(self, address: int, value: int) -> None:
        if address == 0xE01 and not self.locked:
            self.ram_on = self.remap = bool(value & 0x80)
            self.locked = bool(value & 0x20)
        else:
            super().wb(address, value)


@board("rom_sf001_beggarp1")
class BeggarPrince1(BeggarPrince):
    """Beggar Prince rev 1: the RAM moves to $3C0000-$3C7FFF, mirrored through $3FFFFF."""

    upper = False
    RAM = (0x3C0000, 0x3FFFFF, 0x4000)


@board("rom_sf002")
class LegendOfWukong(_SuperFighter):
    """Legend of Wukong: the $E00 byte's bit 7 clear shows the image's second 2 MB at
    $200000 (set shows the first again), bit 0 shows 8 KB of RAM at $3C0000-$3FFFFF."""

    RAM = (0x3C0000, 0x3FFFFF, 0x2000)

    def start(self) -> None:
        super().start()
        self.half = 1

    def rb(self, address: int) -> int:
        if address >= 0x200000 and not (self.ram_on and address >= 0x3C0000):
            return self.padded[((self.half << 21) | (address & 0x1FFFFF)) % len(self.padded)]
        return super().rb(address)

    def wb(self, address: int, value: int) -> None:
        if address == 0xE01:
            self.half = 0 if value & 0x80 else 1
            self.ram_on = bool(value & 1)
        else:
            super().wb(address, value)


@board("rom_sf004")
class StarOdyssey(_SuperFighter):
    """Star Odyssey: 256 KB pages. ``$D01`` bit 7 shows the 32 KB of RAM at
    $200000-$2FFFFF; ``$E01`` bit 7 clear locks, bit 6 chooses the window mode (clear:
    page 0 repeated through 2 MB; set: five consecutive pages from the base at
    $000000-$13FFFF and nothing above); ``$F01`` bits 6-4 set the base page. /TIME odd
    bytes read the base back, shifted into bits 6-4 (sfteam.cpp)."""

    upper = False
    RAM = (0x200000, 0x2FFFFF, 0x8000)

    def start(self) -> None:
        super().start()
        self.windows = False
        self.base = 0

    def rb(self, address: int) -> int:
        if address < 0x200000:
            if not self.windows:
                # window 0's page follows the base register in this mode too
                return self.padded[((self.base << 18) | (address & 0x3FFFF)) % len(self.padded)]
            if address >= 0x140000:
                return 0xFF
            page = (self.base + (address >> 18)) & 7
            return self.padded[((page << 18) | (address & 0x3FFFF)) % len(self.padded)]
        return super().rb(address)

    def wb(self, address: int, value: int) -> None:
        if address == 0xD01 and not self.locked:
            self.ram_on = bool(value & 0x80)
        elif address == 0xE01 and not self.locked:
            self.locked = not value & 0x80
            self.windows = bool(value & 0x40)
        elif address == 0xF01 and not self.locked:
            self.base = (value >> 4) & 7
        else:
            super().wb(address, value)

    def read_time_word(self, address: int) -> int | None:
        return 0xFF00 | (self.base << 4)


# -- Pier Solar ----------------------------------------------------------------------------


class M95320:
    """ST M95320, the 4 KB SPI EEPROM on Pier Solar, after MAME's ``stm95.cpp``.

    Seven instructions, all of them eight bits clocked in on SCK's rising edge while
    /CS is low: WRSR ($01), WRITE ($02), READ ($03), WRDI ($04), RDSR ($05) and
    WREN ($06). A write is not delayed and block protection is ignored, as in MAME.
    """

    SIZE = 0x1000

    def __init__(self, memory: bytearray) -> None:
        self.memory = memory
        self.state = "idle"
        self.shift = 0
        self.count = 0
        self.address = 0
        self.si = 0
        self.sck = 0
        self.cs = 1
        self.wel = 0

    def select(self, level: int) -> None:
        self.cs = level
        if level:
            self.count = 0
            self.state = "idle"

    @property
    def so(self) -> int:
        if self.state in ("reading", "rdsr"):
            return (self.shift >> 8) & 1
        return 0

    def clock(self, level: int) -> None:
        rising = level and not self.sck
        self.sck = level
        if self.cs or not rising:
            return
        state = self.state
        if state == "idle":
            self.shift = (self.shift << 1) | self.si
            self.count += 1
            if self.count == 8:
                self.count = 0
                self.command(self.shift & 0xFF)
        elif state == "wrsr":
            self.count += 1
            if self.count == 8:
                self.state = "idle"
                self.count = 0
        elif state == "rdsr":
            self.shift <<= 1
            self.count += 1
            if self.count == 8:
                self.state = "idle"
                self.count = 0
        elif state in ("read", "write"):
            self.shift = (self.shift << 1) | self.si
            self.count += 1
            if self.count == 16:
                self.address = self.shift & (self.SIZE - 1)
                if state == "read":
                    self.shift = self.memory[self.address]
                self.state = "reading" if state == "read" else "writing"
                self.count = 0
        elif state == "reading":
            self.shift <<= 1
            self.count += 1
            if self.count == 8:
                self.address = (self.address + 1) & (self.SIZE - 1)
                self.shift |= self.memory[self.address]
                self.count = 0
        elif state == "writing":
            self.shift = (self.shift << 1) | self.si
            self.count += 1
            if self.count == 8:
                self.memory[self.address] = self.shift & 0xFF
                self.address = (self.address + 1) & (self.SIZE - 1)
                self.count = 0

    def command(self, opcode: int) -> None:
        if opcode == 0x01:
            if self.wel:
                self.state = "wrsr"
            self.wel = 0
        elif opcode == 0x02:
            if self.wel:
                self.state = "write"
            self.shift = 0
            self.wel = 0
        elif opcode == 0x03:
            self.state = "read"
            self.shift = 0
        elif opcode == 0x04:
            self.wel = 0
        elif opcode == 0x05:
            self.state = "rdsr"
            self.shift = self.wel << 1
        elif opcode == 0x06:
            self.wel = 1


@board("rom_stm95", "rom_psolar")
class PierSolar(Board):
    """Pier Solar (WaterMelon T-5740): 8 MB in 512 KB pages and an M95320 on /TIME.

    $000000-$27FFFF is pages 0-4; the three windows above it are $A13003, $A13005 and
    $A13007 (low four bits) and start on pages 5, 6 and 7. $A13009 drives the EEPROM
    (bit 0 SI, bit 1 SCK, bit 3 /CS) and $A1300B bit 0 reads SO. $A13001 is a mode
    byte MAME ignores (t5740.cpp); the one hack is that a byte read of
    $018100-$0181FF sees the first 32 KB mirrored, which the cartridge header read at
    the "not endorsed by Sega" screen relies on.
    """

    def start(self) -> None:
        self.pages = max(1, -(-len(self.padded) >> 19))
        self.select = [0, 1, 2, 3, 4, 5, 6, 7]
        self.sram = bytearray(b"\xff" * M95320.SIZE)
        self.sram_start = self.sram_end = 0
        self.chip = M95320(self.sram)

    def rb(self, address: int) -> int:
        if address < ROM_WINDOW:
            page = self.select[address >> 19] % self.pages
            return self.padded[(page << 19) | (address & 0x7FFFF)]
        return 0xFF

    def read_byte(self, address: int) -> int:
        if 0x18100 <= address < 0x18200:
            return self.padded[address & 0x7FFF]
        return self.rb(address)

    def read_time_word(self, address: int) -> int | None:
        if address & 0xFE == 0x0A:
            return 0xFF00 | self.chip.so
        return None

    def time_byte(self, offset: int, value: int) -> None:
        if offset in (3, 5, 7):
            self.select[4 + (offset >> 1)] = value & 0xF
        elif offset == 9:
            self.chip.si = value & 1
            self.chip.clock((value >> 1) & 1)
            self.chip.select((value >> 3) & 1)
