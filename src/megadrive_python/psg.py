"""The SN76489-style PSG inside the Mega Drive's VDP (the YM7101's PSG block).

Copied and adapted from sms-python's ``src/sms_python/psg.py`` (MIT, same author),
as the handoff brief directs: three tone channels, one noise channel, one
write-only port, a 16-bit LFSR tapped at two bits, a 4-bit attenuation each.  What
is *not* sms-python's, and why:

* **The generator runs the chip's own rotation.**  sms-python advances all four
  channels together once per 16 input clocks, which is right to the ear and right
  for its own judge.  Here the judge is Nuked-PSG (die-derived; no hardware capture
  of a Mega Drive PSG is pinned, so the tier rule makes it the judge --
  docs/validation.md), and matching it sample for sample needs the shape the die
  has: an internal clock at a quarter of the input clock alternating two phases, one
  of the four counters stepped per phase pair, and the *period* taken from a one-hot
  selector that runs one place behind the counter index -- so counter 0 is the noise
  channel's, counter 1 tone 1's, counter 2 tone 2's, counter 3 tone 3's
  (``reference/src/Nuked-PSG/ympsg.c``, ``YMPSG_ClockInternal1`` lines 105-198 and
  ``YMPSG_ClockInternal2`` lines 200-213; the pairing is visible in its state).
* **The counter is an up-counter compared with the period**, not a down-counter
  reloaded at zero: ``if counter >= period: counter = 0`` and then ``counter += 1``
  (ympsg.c lines 168-175).  The two differ for a tick after a period is written, and
  for a period of 0 or 1.
* **The noise LFSR shifts left**, tapped at bits 15 and 12, with a zero-detect that
  feeds a 1 into an all-zero register, and its output is bit 14 latched a phase
  behind (ympsg.c lines 116-132, 211).  sms-python's right-shifting register with
  taps 0 and 3 is the same machine written the other way round; this one is written
  to match the judge bit for bit.
* **A write takes effect one input clock later** (ympsg.c ``YMPSG_Clock`` lines
  283-293: the channel latch is taken on the clock of the write, the register
  updated by the next clock's ``YMPSG_UpdateRegisters``).
* **Power-on is the /IC sequence**, not an assignment: the reset leaves the
  counters at 1, the rotation part way round and the overflow shift register full,
  which a plain zeroing does not (:meth:`PSG.reset`).
* **The attenuation curve is the measured one**, not 2 dB a step
  (:data:`VOLUME_LEVELS`).

The chip is clocked at MCLK / 15, the Z80's clock (docs/timing.md), and one output
sample is one full rotation, 16 input clocks: 223,721 Hz on NTSC.
"""

from __future__ import annotations

#: Input clocks per output sample: four channels, four input clocks a phase pair.
CLOCKS_PER_SAMPLE = 16

#: The output level of each 4-bit attenuation, 15 being silence.  These are
#: Nuked-PSG's measured values (``ympsg.c`` line 7, ``ympsg_vol``), not the ideal
#: 2 dB a step of the TI data sheet that sms-python uses: the Sega part's ladder is
#: not exact (docs/undocumented-behavior.md, "The PSG").
VOLUME_LEVELS: tuple[float, ...] = (
    1.0, 0.772, 0.622, 0.485, 0.382, 0.29, 0.229, 0.174,
    0.132, 0.096, 0.072, 0.051, 0.034, 0.019, 0.009, 0.0,
)  # fmt: skip

#: Counter reload values for noise shift rates 0-2; rate 3 follows tone channel 3
#: (ympsg.c lines 146-156, where the rate ORs 16, 32 or 64 into the period).
NOISE_PERIODS = (0x10, 0x20, 0x40)


class PSG:
    """The PSG as the die has it: registers, four counters, and the rotation.

    ``volume``: the four attenuations (tone 1-3, noise).  ``freq``: the three 10-bit
    tone periods.  ``noise_data``: the 3-bit noise register (bit 2 white, bits 1-0
    the shift rate).  :meth:`attenuations` is the chip's digital output -- what each
    channel is driving this instant -- and is what the referee is compared against;
    :meth:`take_samples` turns the sampled ones into levels.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Power-on: /IC held for 16 input clocks, as a console's reset does.

        Nuked-PSG's ``YMPSG_Init`` (ympsg.c lines 253-264) holds /IC over 16 clocks
        and then releases it, which zeroes the registers through ``reg_reset``,
        silences the channels through ``data_mask``, and leaves the sequencer part
        way round: counters at 1, the overflow register full, the one-hot selector at
        bit 3.  Running the same sequence here is shorter than asserting the
        resulting state and cannot drift from it.
        """
        self.volume = [0xF, 0xF, 0xF, 0xF]
        self.freq = [0, 0, 0]
        self.noise_data = 0
        self.latch = 0
        self.test = 0
        self.counter = [0, 0, 0, 0]
        self.counter_of = 0
        #: Bit 3 tone 1, bit 2 tone 2, bit 1 tone 3, bit 0 noise (ympsg.c line 216).
        self.sign = 0
        self.noise = 0
        self.noise_of = 0
        self.noise_trig = 0
        self.noise_trig_l = 0
        self.noise_sign = 0
        #: The one-hot period selector and the counter index it runs ahead of.
        self.chan_sel = 0
        self.rot = 0
        #: The /4 prescaler and the two /IC shift registers.
        self.p1 = 0
        self.p2 = 0
        self.ic_latch1 = 0
        self.ic_latch2 = 0
        self.ic = 1
        #: The write pipeline: the byte last written, and whether it is still to be
        #: applied to a register (ympsg.c's ``data`` and ``write_flag_l``).
        self._pending: int | None = None
        self._data = 0
        self._write_flag_l = False
        self.clock = 0
        #: One entry per finished sample: the four attenuations being driven.
        self.levels: list[tuple[int, int, int, int]] = []

        self.run(16)
        self.ic = 0
        # /IC drives reg_reset and data_mask, which zero the periods and the noise
        # register and force every attenuation to 15 (ympsg.c YMPSG_UpdateRegisters,
        # lines 24-103).  Asserted here rather than run, because a write during /IC
        # -- the only thing the loop would do differently -- cannot happen on a
        # console: the board holds the PSG in reset before the CPUs run.
        self.volume = [0xF, 0xF, 0xF, 0xF]
        self.freq = [0, 0, 0]
        self.noise_data = 0
        self.clock = 0
        self.levels.clear()

    # -- the port ----------------------------------------------------------------

    def write(self, value: int) -> None:
        """$C00011 from the 68000, $7F11 from the Z80.  Reading the port locks up a
        real console (CMD ``gen-hw.txt`` 1), so there is no read."""
        self._pending = value & 0xFF

    def write_test(self, value: int) -> None:
        """The VDP debug register's PSG bits ($C0001C bits 11-9), which Nuked-PSG
        takes as ``YMPSG_Test`` (ympsg.c line 333).  Bit 0 forces every channel's
        sign high there; the other two select one channel for an analogue test
        output, which this model has no counterpart for and ignores.  Whether these
        are the bits Kabuto means by "the PSG can be made louder" ("The mighty debug
        register") is not established here."""
        self.test = (value >> 9) & 7

    # -- time --------------------------------------------------------------------

    def run(self, clocks: int) -> None:
        """Advance ``clocks`` input clocks (MCLK / 15), appending finished samples.

        The body is ``YMPSG_Clock`` (ympsg.c lines 274-302): the /4 prescaler, the
        /IC shift register that resets it, then the registers, the channel latch and
        one of the two internal phases.

        The register update happens **twice**: once at the head of the clock after
        the write, and once inside the second internal phase, which the write's own
        clock may be (ympsg.c calls ``YMPSG_UpdateRegisters`` at line 285 and again
        at line 208).  So a write that lands on that phase reaches the register a
        clock earlier than one that does not -- worth 19 samples in 102,569 of the
        first stream this was compared on, and the reason it is modelled and not
        smoothed over.
        """
        clock = self.clock
        for _ in range(clocks):
            p2_latch = self.p2
            self.p2 = (self.p2 + self.p1) & 1
            p1 = self.p1 ^ 1
            if self.ic_latch1 == 2:
                p1 = 0
                self.p2 = 0
            self.p1 = p1
            self.ic_latch1 = ((self.ic_latch1 << 1) | self.ic) & 3
            if self._write_flag_l:
                self._update_registers()
                self._write_flag_l = False
            pending = self._pending
            if pending is not None:
                # The channel latch is taken on the clock of the write itself.
                if pending & 0x80:
                    self.latch = (pending >> 4) & 7
                self._data = pending
                self._pending = None
                self._write_flag_l = True
            if p1:
                if p2_latch:
                    self._phase2()
                else:
                    self._phase1()
            clock += 1
            if clock % CLOCKS_PER_SAMPLE == 0:
                self.levels.append(self.attenuations())
        self.clock = clock

    def _update_registers(self) -> None:
        """The latched byte into the register the channel latch names (ympsg.c
        ``YMPSG_UpdateRegisters``, lines 24-103).

        A byte with bit 7 set carries the low four data bits; one with bit 7 clear
        carries the high six bits of a tone register.  Bit 7 does not select the
        register: the latch does, so a byte with bit 7 clear written while a volume
        or noise register is latched is written there whole ("The data byte is NOT
        ignored", sms-python's note from Maxim's wiki).
        """
        data = self._data
        low = bool(data & 0x80)
        latch = self.latch
        if latch & 1:  # 1, 3, 5, 7: the attenuations of tone 1-3 and noise
            self.volume[latch >> 1] = data & 0x0F
        elif latch == 6:
            self.noise_data = data & 0x07
            self.noise_trig = 1
        elif low:
            channel = latch >> 1
            self.freq[channel] = (self.freq[channel] & 0x3F0) | (data & 0x0F)
        else:
            channel = latch >> 1
            self.freq[channel] = (self.freq[channel] & 0x00F) | ((data << 4) & 0x3F0)

    def _phase1(self) -> None:
        """The first internal phase: the noise register, then one counter's step.

        ``ympsg.c`` ``YMPSG_ClockInternal1``, lines 105-198.  ``chan_sel`` is a
        one-hot shift register naming whose *period* this step compares against;
        ``rot`` names the counter.  The four expiries collected in ``counter_of``
        flip the four signs together on the step that serves tone 1.
        """
        ic_latch2 = self.ic_latch2
        noise_data = self.noise_data
        # The noise generator follows tone 3's sign in shift rate 3, its own
        # counter's otherwise, and shifts on that signal's rising edge.
        noise_of = (self.sign >> 1) & 1 if (noise_data & 3) == 3 else self.sign & 1
        if self.noise_trig_l or ic_latch2 & 1:
            self.noise = 0
        elif noise_of and not self.noise_of:
            feedback = ((self.noise >> 15) ^ (self.noise >> 12)) & 1
            bit = 1 if (feedback and (noise_data >> 2) & 1) or (self.noise & 0x7FFF) == 0 else 0
            self.noise = ((self.noise << 1) | bit) & 0xFFFF
        self.noise_of = noise_of

        chan_sel = 0 if ic_latch2 & 2 else self.chan_sel
        period = 0
        if chan_sel & 1:
            period = self.freq[0]
        if chan_sel & 2:
            period |= self.freq[1]
        if chan_sel & 4:
            period |= self.freq[2]
        if chan_sel & 8:
            rate = noise_data & 3
            if rate != 3:
                period |= NOISE_PERIODS[rate]
        if chan_sel & 1:
            self.sign ^= self.counter_of & 15
        if ic_latch2 & 2:
            self.sign = 0
        self.counter_of = (self.counter_of << 1) & 0xFF
        rot = self.rot
        counter = self.counter[rot]
        if counter >= period:
            self.counter_of |= 1
            counter = 0
        if ic_latch2 & 2:
            counter = 0
        self.counter[rot] = (counter + 1) & 0x3FF
        # The one-hot rotation: a 1 is fed in when the three tone places are clear.
        self.chan_sel = (
            (self.chan_sel << 1) | (0 if (ic_latch2 & 1) or self.chan_sel & 7 else 1)
        ) & 0xFF
        self.ic_latch2 = ((ic_latch2 << 1) | self.ic) & 0xFF
        self.noise_trig_l = self.noise_trig

    def _phase2(self) -> None:
        """The second internal phase: the counter index steps and the LFSR's output
        bit is latched (``ympsg.c`` ``YMPSG_ClockInternal2``, lines 200-213)."""
        if self.noise_trig_l:
            self.noise_trig = 0
        if self._write_flag_l:
            self._update_registers()
        self.rot = (self.rot + 1) & 3
        self.noise_sign = (self.noise >> 14) & 1

    # -- output ---------------------------------------------------------------------

    def attenuations(self) -> tuple[int, int, int, int]:
        """What each channel is driving now: its attenuation while its sign bit is
        high, silence (15) otherwise (ympsg.c ``YMPSG_UpdateSample``, lines 215-233).
        The noise channel uses the latched LFSR bit, the tone channels their signs."""
        sign = (self.sign & 14) | self.noise_sign
        if self.test & 1:
            sign |= 15
        volume = self.volume
        return (
            volume[0] if sign & 8 else 15,
            volume[1] if sign & 4 else 15,
            volume[2] if sign & 2 else 15,
            volume[3] if sign & 1 else 15,
        )

    def take_samples(self) -> list[float]:
        """Return and clear the finished samples as levels in 0.0-4.0 (four channels
        of 0.0-1.0 each), by :data:`VOLUME_LEVELS`.  The PSG is mono on this board:
        its one output is mixed into both sides in the analogue path."""
        levels, self.levels = self.levels, []
        table = VOLUME_LEVELS
        return [table[a] + table[b] + table[c] + table[d] for a, b, c, d in levels]

    def take_attenuations(self) -> list[tuple[int, int, int, int]]:
        """Return and clear the finished samples as the chip's own digital state,
        which is what the Nuked-PSG comparison is made on."""
        levels, self.levels = self.levels, []
        return levels
