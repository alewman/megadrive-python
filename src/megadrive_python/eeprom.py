"""The serial EEPROMs a few cartridges carry instead of battery SRAM.

The research document is Eke-Eke's *Serial EEPROMs in Sega Genesis / Mega Drive
cartridges*, version 2 (2010), which is ``gen_eeprom.pdf`` inside
``reference/docs/md_tech.zip``. It describes the two-wire (I2C) protocol between the
68000, which is always the master, and the chip, and gives a table of which game uses
which chip at which address; MAME 0.285 repeats that table in
``src/devices/bus/megadrive/eeprom.cpp`` (its header comment credits Eke) and
``jcart.cpp``, and its software list says which game is on which board. CMD's
``gen-eeprom.txt`` names the actual chips found on several boards (a Xicor X24C01 in
Monster World 3 and Mega Man - The Wily Wars, an X24C02 in NBA Jam).

What the chip sees is two lines the cartridge decodes out of the 68000's address and
data buses:

- **/SCL**, the clock, write only: "A clock cycle starts by a transition of /SCL from
  LOW to HIGH and ends by a transition of /SCL from HIGH to LOW."
- **/SDA**, the data line, driven by whichever of the two is sending.

and the framing:

- **START** is /SDA going high to low while /SCL is high, **STOP** low to high.
- A word is eight clock cycles, then a ninth in which the receiver acknowledges by
  holding /SDA low.
- After START the master sends one, two or three address words, depending on the
  chip's mode, the last bit of the first of them saying whether this is a read or a
  write. Then data words follow until a STOP.
- A written address wraps inside the chip's **page**; a read address wraps at the end
  of the chip ("The address will roll up to base memory address when maximal write
  page size has been reached" and "will roll up to 0 when maximal memory size has been
  reached").

The three modes, as Eke names them:

============ ====================================== ===========================
mode         chips                                  address words
============ ====================================== ===========================
1            X24C01                                 one: 7 address bits + R/W
2            24C01-24C16                             one device word, one low word
3            24C32 and up                            one device word, two address
============ ====================================== ===========================

Nothing here is copied from any emulator: this is written from the document, and the
one place a choice had to be made that the document does not cover is named in the
code.
"""

from __future__ import annotations

MODE1, MODE2, MODE3 = 1, 2, 3

# What the machine is doing between a START and a STOP.
_IDLE = "idle"
_DEVICE = "device"  # the first word: device address, upper address bits, R/W
_WORD_HIGH = "word-high"  # mode 3's second word
_WORD_LOW = "word-low"  # the word holding the low eight address bits
_WRITE = "write"  # data the master is sending
_READ = "read"  # data the chip is sending


class SerialEEPROM:
    """One serial EEPROM, driven by its two lines.

    ``size_mask`` and ``page_mask`` are Eke's own fields (``SIZE_MASK``,
    ``PAGE_MASK``): the chip is ``size_mask + 1`` bytes and a write wraps inside
    ``page_mask + 1`` of them.

    An unwritten chip reads $FF, which is what an erased EEPROM holds and what
    Eke's note about Micro Machines 2 assumes ("requires backup memory to be
    initialized with 0xFF").
    """

    def __init__(self, mode: int, size_mask: int, page_mask: int,
                 data: bytes | None = None) -> None:  # fmt: skip
        self.mode = mode
        self.size_mask = size_mask
        self.page_mask = page_mask
        self.memory = bytearray(b"\xff" * (size_mask + 1))
        if data:
            self.memory[: len(data)] = data[: size_mask + 1]
        self.scl = 1
        self.sda = 1
        #: What the chip drives on /SDA: 1 is the released line, which the pull-up
        #: takes high, so a chip that is saying nothing reads as 1.
        self.out = 1
        self.state = _IDLE
        self.bit = 0
        self.latch = 0
        self.shift = 0  # the byte being sent, most significant bit first
        self.acknowledging = False  # the ninth clock of the word
        self.ack_step = 0  # 0 drive it, 1 it is on the line, 2 it has been read
        self.address = 0
        self.reading = False
        self._master_ack = False
        self.writes = 0  # bytes written since power-on, for a report

    # -- the two lines ---------------------------------------------------------

    def update(self, scl: int, sda: int) -> None:
        """The cartridge presents the levels the 68000 just wrote."""
        scl, sda = 1 if scl else 0, 1 if sda else 0
        if self.scl and scl:
            # START and STOP are the only /SDA changes allowed while /SCL is high.
            if self.sda and not sda:
                self._start()
            elif not self.sda and sda:
                self._stop()
        elif scl and not self.scl:
            self._rising(sda)
        elif self.scl and not scl:
            self._falling()
        self.scl, self.sda = scl, sda

    def read_sda(self) -> int:
        """What the chip is driving on /SDA (1 when it has let go of the line)."""
        return self.out

    # -- the machine ------------------------------------------------------------

    def _start(self) -> None:
        self.state = _DEVICE
        self.bit = 0
        self.latch = 0
        self.acknowledging = False
        self.ack_step = 0
        self.reading = False
        self.out = 1

    def _stop(self) -> None:
        self.state = _IDLE
        self.acknowledging = False
        self.ack_step = 0
        self.out = 1

    def _rising(self, sda: int) -> None:
        """/SCL went high: the bit on /SDA is valid, so this is where one is counted.

        A word is eight of these, then a ninth for the acknowledge; the falling edges
        in between are where whoever is *sending* puts the next bit on the line, which
        is why nothing is counted there.
        """
        if self.state == _IDLE:
            return
        if self.acknowledging:
            if self.ack_step == 1:
                if self.state == _READ:
                    # The master acknowledges by holding /SDA low; if it does not,
                    # "the READ operation ends and the EEPROM waits for STOP".
                    self._master_ack = sda == 0
                self.ack_step = 2
            return
        if self.state != _READ:
            self.latch = ((self.latch << 1) | sda) & 0xFF
        self.bit += 1
        if self.bit == 8:
            self.bit = 0
            self.acknowledging = True
            self.ack_step = 0

    def _falling(self) -> None:
        """/SCL went low: the sender puts the next bit, or the word is over."""
        if self.state == _IDLE:
            return
        if self.acknowledging:
            if self.ack_step == 0:
                # The receiver drives the acknowledge through the ninth clock: the
                # chip when it is being addressed or written to, the master when it
                # is reading.
                self.out = 0 if self.state != _READ else 1
                self.ack_step = 1
            elif self.ack_step == 2:
                self.acknowledging = False
                self._word_done()
            return
        if self.state == _READ:
            self.shift = (self.shift << 1) & 0xFF
            self.out = (self.shift >> 7) & 1

    def _word_done(self) -> None:
        """The acknowledge cycle is over: act on the word and set up the next."""
        if self.state == _READ:
            if self._master_ack:
                self.address = (self.address + 1) & self.size_mask
                self._send(self.memory[self.address])
            else:
                self.state = _IDLE  # it waits for STOP; nothing is driven meanwhile
                self.out = 1
            return
        word = self.latch
        self.latch = 0
        if self.state == _DEVICE:
            self.reading = bool(word & 1)
            if self.mode == MODE1:
                # "The 7 first bits set the memory address to be read/write and the
                # 8th bit set the operation type" (Eke, mode 1).
                self.address = (word >> 1) & self.size_mask
                self._begin_data()
            else:
                # Mode 2 carries the upper address bits in the device field; mode 3
                # uses that field for the device number alone and sends the address
                # in the next two words.
                if self.mode == MODE2:
                    # The device field carries the address's upper bits, and only
                    # those: "before a READ operation, only the 1st word is actually
                    # sent, the EEPROM contains an address counter that saves the
                    # address of the next word to be accessed" (Eke), so the low eight
                    # bits stay where the last access left them.
                    self.address = (
                        (((word >> 1) & 0x07) << 8) | (self.address & 0xFF)
                    ) & self.size_mask
                if self.reading:
                    self._begin_data()
                else:
                    self.state = _WORD_HIGH if self.mode == MODE3 else _WORD_LOW
                    self.out = 1
        elif self.state == _WORD_HIGH:
            self.address = ((word << 8) | (self.address & 0xFF)) & self.size_mask
            self.state = _WORD_LOW
            self.out = 1
        elif self.state == _WORD_LOW:
            self.address = ((self.address & ~0xFF) | word) & self.size_mask
            self._begin_data()
        elif self.state == _WRITE:
            self.memory[self.address] = word
            self.writes += 1
            # "The address will roll up to base memory address when maximal write
            # page size has been reached": only the low bits move.
            self.address = (self.address & ~self.page_mask) | ((self.address + 1) & self.page_mask)
            self.out = 1

    def _begin_data(self) -> None:
        if self.reading:
            self.state = _READ
            self._send(self.memory[self.address])
        else:
            self.state = _WRITE
            self.out = 1

    def _send(self, value: int) -> None:
        self.shift = value & 0xFF
        self.out = (self.shift >> 7) & 1
