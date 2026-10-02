"""The YM2612 (OPN2): six FM channels, channel 3's special mode, CSM, SSG-EG, the
LFO, both timers, BUSY, the DAC, and the ladder effect as a variant field.

Why this is a pipeline and not a per-sample FM loop
---------------------------------------------------

Milestone 4's acceptance is sample-for-sample equality with two die-derived models
(docs/handoff-brief.md), and there is no self-checking FM test ROM and no YM2612
datasheet (docs/validation.md, "What is not available").  A textbook OPN2 -- compute
each operator once per output sample, in channel order -- cannot meet that bar: the
real chip is a *pipeline*.  One operator is served per internal cycle, 24 cycles to
an output sample, and the stages of that pipeline are several cycles apart, so a
register written between two cycles reaches one operator this sample and the next
operator only next sample.  Envelope, phase and modulation each lag by their own
fixed number of cycles.  Nothing but the same pipeline gives the same samples.

So this model runs the chip's cycle: ``cycles`` counts 0-23, ``channel`` is
``cycles % 6``, and the slot served is ``cycles`` itself, with slots 0-5 the
channels' operator 1, 6-11 operator 3, 12-17 operator 2 and 18-23 operator 4.  The
shape of the pipeline, the stage offsets and the chip's constant tables are read
from **Nuked-OPN2** (``reference/src/Nuked-OPN2/ym3438.c``, LGPL-2.1, pinned at
335747d; a transcription of the YM3438's die) and from **YM2608-LLE**
(``reference/src/YM2608-LLE/fmopna_impl.c``, GPL-2.0, pinned at 7a2aca7; the NMOS
YM2612 from HardWareMan's die shot).  Every function below names the lines it was
read from.  **No code is copied**: the models are read and cited, and this is
written from what they say, as the working agreement requires.  The constants are
measurements of the chip -- ROM contents, shift amounts, counter reloads -- and the
two ROM tables are generated here from the functions that produced them rather than
transcribed (:func:`_log_sin_rom`, :func:`_exp_rom`).

The clock
---------

The chip runs at MCLK / 7 (7.67 MHz on NTSC) and divides that by 6, so one internal
cycle is 42 MCLK and one output sample -- 24 cycles -- is 1,008 MCLK: 53,267 Hz on
NTSC, 52,781 Hz on PAL (docs/timing.md, "Frame pacing").  :meth:`YM2612.run` takes a
master clock and advances to it; :meth:`YM2612.write` and :meth:`YM2612.read` are
the board's two ports.

Cost
----

24 internal cycles a sample at 53 kHz is 1.28 million cycles of Python per emulated
second, each touching 24 slots' worth of state.  That is the dominant cost of this
board and is measured in docs/worklog.md.  :attr:`YM2612.enabled` turns the whole
generator off -- the phase generator, the envelope generator, the LFO, the operators
and the DAC -- leaving the bus, the register file, the timers and BUSY, which is what
a run that does not want the sound needs and costs a fraction as much
(:meth:`YM2612._quiet_cycle`).
"""

from __future__ import annotations

import math

from .machine import FMVariant

# -- the chip's two ROMs ------------------------------------------------------------


def _log_sin_rom() -> tuple[int, ...]:
    """The quarter-sine table in the log domain, 256 entries of 12 bits.

    ``round(-log2(sin((i + 0.5) * pi / 512)) * 256)``: the function Gambrell and
    Niemitalo recovered from the OPL2's ROM ("OPLx decapsulated"), which Nuked-OPN2
    credits for the same table (``ym3438.c`` lines 22-25, table at lines 42-74).
    Generated here rather than transcribed; ``tests/test_ym2612.py`` checks it
    against the pinned model's own table, all 256 entries.
    """
    return tuple(round(-math.log2(math.sin((i + 0.5) * math.pi / 512)) * 256) for i in range(256))


def _exp_rom() -> tuple[int, ...]:
    """The exponential table, 256 entries of 10 bits: ``round((2 ** (i / 256) - 1) *
    1024)`` (``ym3438.c`` lines 76-110).  Generated, and checked in the tests."""
    return tuple(round((2 ** (i / 256) - 1) * 1024) for i in range(256))


LOG_SIN = _log_sin_rom()
EXP = _exp_rom()

# -- the chip's constant tables -----------------------------------------------------

#: The two high bits of F-number bits 10-7 that choose the key code's note
#: (``ym3438.c`` line 113, ``fn_note``; the Manual's "F-Number and Block" table).
FN_NOTE = (0, 0, 0, 0, 0, 0, 0, 1, 2, 3, 3, 3, 3, 3, 3, 3)

#: The envelope's increment at rates 48 and above, by the rate's low two bits and
#: the timer's (``ym3438.c`` lines 116-121, ``eg_stephi``).
EG_STEPHI = ((0, 0, 0, 0), (1, 0, 0, 0), (1, 0, 1, 0), (1, 1, 1, 0))

#: How far the LFO's amplitude value is shifted down for each AMS depth
#: (``ym3438.c`` line 123, ``eg_am_shift``): 0, 1.4, 5.9 and 11.8 dB.
EG_AM_SHIFT = (7, 3, 1, 0)

#: The detune table: the increments added or subtracted, indexed by the note and the
#: low bit of the block sum, then shifted by the block (``ym3438.c`` line 128,
#: ``pg_detune``; the Manual's DT1 table in another form).
PG_DETUNE = (16, 17, 19, 20, 22, 24, 27, 29)

#: The two shifts that build the LFO's frequency modulation out of the F-number's
#: high bits, by PMS depth (rows) and the LFO's position in its quarter (columns)
#: (``ym3438.c`` lines 130-150, ``pg_lfo_sh1``/``pg_lfo_sh2``).  A shift of 7 is
#: effectively zero; the pair sums two shifted copies, which is how the die makes
#: the Manual's PMS percentages.
PG_LFO_SH1 = (
    (7, 7, 7, 7, 7, 7, 7, 7),
    (7, 7, 7, 7, 7, 7, 7, 7),
    (7, 7, 7, 7, 7, 7, 1, 1),
    (7, 7, 7, 7, 1, 1, 1, 1),
    (7, 7, 7, 1, 1, 1, 1, 0),
    (7, 7, 1, 1, 0, 0, 0, 0),
    (7, 7, 1, 1, 0, 0, 0, 0),
    (7, 7, 1, 1, 0, 0, 0, 0),
)
PG_LFO_SH2 = (
    (7, 7, 7, 7, 7, 7, 7, 7),
    (7, 7, 7, 7, 2, 2, 2, 2),
    (7, 7, 7, 2, 2, 2, 7, 7),
    (7, 7, 2, 2, 7, 7, 2, 2),
    (7, 7, 2, 7, 7, 7, 2, 7),
    (7, 7, 7, 2, 7, 7, 2, 1),
    (7, 7, 7, 2, 7, 7, 2, 1),
    (7, 7, 7, 2, 7, 7, 2, 1),
)

#: The LFO's counter reload for each of the eight speeds (``ym3438.c`` line 218,
#: ``lfo_cycles``): 3.98 Hz to 72.2 Hz in the Manual's table.
LFO_CYCLES = (108, 77, 71, 67, 62, 44, 8, 5)

#: Which register offset each of the twelve address slots answers to, and which
#: channel each of the six channel slots does (``ym3438.c`` lines 160-190).  Part I
#: is $000-$0FF, part II $100-$1FF.
OP_OFFSET = (0x000, 0x001, 0x002, 0x100, 0x101, 0x102,
             0x004, 0x005, 0x006, 0x104, 0x105, 0x106)  # fmt: skip
CH_OFFSET = (0x000, 0x001, 0x002, 0x100, 0x101, 0x102)

#: The eight algorithms as the die wires them (``ym3438.c`` lines 193-215,
#: ``fm_algorithm``), indexed [operator][row][algorithm].  The operator index is the
#: slot's group: 0 is operator 1, 1 is operator 3, 2 is operator 2, 3 is operator 4.
#: The rows are the five modulation inputs and the output enable:
#: 0 the last-but-one output of operator 1, 1 its last output, 2 operator 2's
#: output, 3 and 4 the previous slot's output into each of the two modulation
#: summands, 5 whether this operator reaches the channel's accumulator.
FM_ALGORITHM = (
    (  # operator 1: its own feedback, and it is the output only in algorithm 7
        (1, 1, 1, 1, 1, 1, 1, 1),
        (1, 1, 1, 1, 1, 1, 1, 1),
        (0, 0, 0, 0, 0, 0, 0, 0),
        (0, 0, 0, 0, 0, 0, 0, 0),
        (0, 0, 0, 0, 0, 0, 0, 0),
        (0, 0, 0, 0, 0, 0, 0, 1),
    ),
    (  # operator 3
        (0, 1, 0, 0, 0, 1, 0, 0),
        (0, 0, 0, 0, 0, 0, 0, 0),
        (1, 1, 1, 0, 0, 0, 0, 0),
        (0, 0, 0, 0, 0, 0, 0, 0),
        (0, 0, 0, 0, 0, 0, 0, 0),
        (0, 0, 0, 0, 0, 1, 1, 1),
    ),
    (  # operator 2
        (0, 0, 0, 0, 0, 0, 0, 0),
        (0, 0, 0, 0, 0, 0, 0, 0),
        (0, 0, 0, 0, 0, 0, 0, 0),
        (1, 0, 0, 1, 1, 1, 1, 0),
        (0, 0, 0, 0, 0, 0, 0, 0),
        (0, 0, 0, 0, 1, 1, 1, 1),
    ),
    (  # operator 4: always the output
        (0, 0, 1, 0, 0, 1, 0, 0),
        (0, 0, 0, 0, 0, 0, 0, 0),
        (0, 0, 0, 1, 0, 0, 0, 0),
        (1, 1, 0, 1, 1, 0, 0, 0),
        (0, 0, 1, 0, 0, 0, 0, 0),
        (1, 1, 1, 1, 1, 1, 1, 1),
    ),
)

EG_ATTACK, EG_DECAY, EG_SUSTAIN, EG_RELEASE = 0, 1, 2, 3

#: Master clocks per internal cycle, and internal cycles per output sample.
MCLK_PER_CYCLE = 42

#: Internal cycles the chip stands still after /IC is asserted. Nuked-MD's FM (``fm.c``
#: ``FM_Prescaler``) stops its prescaler while ``ic_check`` holds -- /IC high now but
#: low 12 FM clocks ago, i.e. for 12 clocks after the assertion -- and then restarts it
#: from the assertion, /IC still low: the slot-23 marker keeps its 1,008-MCLK period
#: through a long reset (docs/worklog.md, "The Z80 from the reset"). The die's prescaler
#: and slot counter clocked alone (the way ``fc1004.c`` drives them) take their next slot
#: 15 FM clocks after the clock that first samples /IC, at every phase; with /IC sampled
#: about 3 FM clocks after the write that drives it, that is 18 FM clocks, 3 internal
#: cycles, after the assertion. Die-derived: the FM's slot-23 marker after both of the
#: Overdrive 2 boot's later assertions, a reset and a pause, lands within 0.5 MCLK of the
#: die's (docs/worklog.md, "The FM's restart").
IC_FSM_HOLD = 3
#: Whether the slot counter is also reset depends on where in its slot the assertion
#: falls: ``i_fsm_reset`` (``fc1004.c``, ``ic_check`` three clocks late) resets it only
#: if a phase-1 clock still comes while it holds. Measured by clocking Nuked-MD's own
#: ``fm.c`` the way ``fc1004.c`` does through every position: sampled 1-3 FM clocks into
#: a slot, the counter restarts at slot 0; sampled 0, 4 or 5 clocks in, it keeps its
#: count, and the slot it would have gone on to next (:data:`IC_PAUSE_CARRY`) is the one
#: it takes up at the restart (docs/worklog.md, "The one-frame slip", "The FM's
#: restart"). /IC is sampled 3 FM clocks after the bus cycle that drives it begins, and
#: the die's slots begin 24 MCLK after this model's cycles.
IC_PAUSE_POSITIONS = (0, 4, 5)
IC_SLOT_LAG = 3
IC_RESUME_LOOKAHEAD = 11
IC_PAUSE_CARRY = 1
CYCLES_PER_SAMPLE = 24

#: How long a status read holds its value before the chip stops driving the bus:
#: 300,000 internal cycles on the YM2612 against 40,000,000 on the YM3438
#: (``ym3438.c`` lines 1396-1403, ``status_time``).  A YM2612 that is not read for a
#: quarter of a second reads back 0.
STATUS_TIME = (300_000, 40_000_000)


def _sign_extend(bits: int, value: int) -> int:
    return (value & ((1 << bits) - 1)) - (value & (1 << bits))


class YM2612:
    """One YM2612 or its ASIC successor, told apart by a :class:`FMVariant`.

    ``enabled``: run the generator.  With it off the registers, the timers, BUSY and
    key-on still work, :attr:`mol` and :attr:`mor` stay at 0, and the samples are
    silent -- which is what a run that only wants the picture wants.  See
    :meth:`_quiet_cycle` for what that costs and what it gives up.
    """

    def __init__(self, variant: FMVariant, *, enabled: bool = True) -> None:
        self.variant = variant
        self.enabled = enabled
        #: Master clocks of the next internal cycle, and the chip's own cycle count.
        self.mclk = 0
        self.cycles_run = 0
        #: /RESET is shared with the Z80 and is driven by the board; a chip made on
        #: its own (a referee comparison, a test) starts running.
        self.in_reset = False
        #: Internal cycles the slot counter is still held at 0 after /IC was asserted
        #: (:data:`IC_FSM_HOLD`).
        self._ic_hold = 0
        #: Finished samples, one per 24 cycles: the summed left and right DAC.  Kept
        #: out of :meth:`reset` because they are the output queue, not chip state: a
        #: game that pulses /RESET must not lose the samples already made.
        self.samples: list[tuple[int, int]] = []
        self._acc_l = 0
        self._acc_r = 0
        self._acc_n = 0
        self.reset()

    # -- state -------------------------------------------------------------------

    def reset(self) -> None:
        """Power-on and /IC: every envelope at its floor and in release, every
        multiplier 1, both pans on (``ym3438.c`` ``OPN2_Reset``, lines 1186-1203).

        :attr:`in_reset` is not touched: it is the /RESET *line*, which the board
        drives, and :meth:`reset_line` is what turns an assertion of it into a call
        of this."""
        # Per-slot registers.
        self.multi = [1] * 24
        self.dt = [0] * 24
        self.tl = [0] * 24
        self.ar = [0] * 24
        self.ks = [0] * 24
        self.dr = [0] * 24
        self.am = [0] * 24
        self.sr = [0] * 24
        self.rr = [0] * 24
        self.sl = [0] * 24
        self.ssg_eg = [0] * 24
        # Per-channel registers.
        self.fnum = [0] * 6
        self.block = [0] * 6
        self.kcode = [0] * 6
        self.fnum_3ch = [0] * 6
        self.block_3ch = [0] * 6
        self.kcode_3ch = [0] * 6
        self.connect = [0] * 6
        self.fb = [0] * 6
        self.pms = [0] * 6
        self.ams = [0] * 6
        self.pan_l = [1] * 6
        self.pan_r = [1] * 6
        self.reg_a4 = 0
        self.reg_ac = 0
        # The phase generator.
        self.pg_inc = [0] * 24
        self.pg_phase = [0] * 24
        self.pg_reset = [0] * 24
        self.pg_fnum = 0
        self.pg_block = 0
        self.pg_kcode = 0
        self.pg_read = 0
        # The envelope generator.
        self.eg_level = [0x3FF] * 24
        self.eg_state = [EG_RELEASE] * 24
        self.eg_out = [0x3FF] * 24
        self.eg_kon = [0] * 24
        self.eg_kon_latch = [0] * 24
        self.eg_kon_csm = [0] * 24
        self.eg_ssg_dir = [0] * 24
        self.eg_ssg_enable = [0] * 24
        self.eg_ssg_inv = [0] * 24
        self.eg_ssg_pgrst = [0] * 24
        self.eg_ssg_repeat = [0] * 24
        self.eg_ssg_hold_up = [0] * 24
        self.eg_cycle = 0
        self.eg_cycle_stop = 0
        self.eg_shift = 0
        self.eg_shift_lock = 0
        self.eg_timer = 0
        self.eg_timer_inc = 0
        self.eg_timer_low_lock = 0
        self.eg_quotient = 0
        self.eg_inc = 0
        self.eg_ratemax = 0
        self.eg_rate = 0
        self.eg_ksv = 0
        self.eg_lfo_am = 0
        self.eg_tl = [0, 0]
        self.eg_sl = [0, 0]
        self.eg_read = [0, 0]
        self.eg_read_inc = 0
        self.eg_custom_timer = 0
        # The operator and channel pipelines.
        self.fm_mod = [0] * 24
        self.fm_out = [0] * 24
        self.fm_op1_0 = [0] * 6
        self.fm_op1_1 = [0] * 6
        self.fm_op2 = [0] * 6
        self.ch_acc = [0] * 6
        self.ch_out = [0] * 6
        self.ch_lock = 0
        self.ch_lock_l = 0
        self.ch_lock_r = 0
        self.ch_read = 0
        # The LFO.
        self.lfo_en = 0
        self.lfo_freq = 0
        self.lfo_cnt = 0
        self.lfo_inc = 0
        self.lfo_quotient = 0
        self.lfo_pm = 0
        self.lfo_am = 0
        # Mode registers, key on, CSM.
        self.mode_test_21 = [0] * 8
        self.mode_test_2c = [0] * 8
        self.mode_ch3 = 0
        self.mode_csm = 0
        self.mode_kon = [0] * 24
        self.mode_kon_operator = [0] * 4
        self.mode_kon_channel = 0
        self.mode_kon_csm = 0
        self.dacen = 0
        self.dacdata = 0
        self.pin_test_in = 0
        # Timers.
        self.timer_a_reg = 0
        self.timer_a_cnt = 0
        self.timer_a_load = 0
        self.timer_a_load_lock = 0
        self.timer_a_load_latch = 0
        self.timer_a_enable = 0
        self.timer_a_reset = 0
        self.timer_a_overflow = 0
        self.timer_a_overflow_flag = 0
        self.timer_b_reg = 0
        self.timer_b_cnt = 0
        self.timer_b_subcnt = 0
        self.timer_b_load = 0
        self.timer_b_load_lock = 0
        self.timer_b_load_latch = 0
        self.timer_b_enable = 0
        self.timer_b_reset = 0
        self.timer_b_overflow = 0
        self.timer_b_overflow_flag = 0
        # The bus interface.
        self.write_a = 0
        self.write_d = 0
        self.write_a_en = 0
        self.write_d_en = 0
        self.write_data = 0
        self.write_fm_address = 0
        self.write_fm_data = 0
        self.write_fm_mode_a = 0
        self.address = 0
        self.data = 0
        self.busy = 0
        self.write_busy = 0
        self.write_busy_cnt = 0
        self.status = 0
        self.status_time = 0
        # The sequencer and the outputs.
        self.cycles = 0
        self.channel = 0
        self.mol = 0
        self.mor = 0
        #: Register writes counted since power-on (a cheap health check).
        self.writes = 0

    # -- the board's side ---------------------------------------------------------

    def reset_line(self, held: bool, mclk: int | None = None) -> None:
        """The YM2612 shares /RESET with the Z80: "For as long as the Z80 is reset,
        the YM2612 cannot be used" (CMD ``gen-hw.txt`` 2.2).

        Asserting /IC resets the chip and restarts its clock divider from the moment
        of assertion (``mclk``, where the caller has brought the chip to), and resets
        its slot counter or only pauses it, by where in the slot the assertion falls
        (:data:`IC_PAUSE_POSITIONS`); releasing it resets nothing. The chip stands
        still only briefly and then runs on while /IC stays low (:data:`IC_FSM_HOLD`),
        so its sample timing after a reset follows when the reset began, not when it
        ended."""
        if held and not self.in_reset:
            cycles_run, writes, cycles = self.cycles_run, self.writes, self.cycles
            start = self.mclk if mclk is None else max(mclk, self.mclk)
            phase = start - self.mclk  # into the cycle under way (the caller synced)
            self.reset()
            if ((phase - IC_SLOT_LAG) // 7) % 6 in IC_PAUSE_POSITIONS:
                ahead = (phase + IC_RESUME_LOOKAHEAD) // MCLK_PER_CYCLE + IC_PAUSE_CARRY
                self.cycles = (cycles + ahead) % CYCLES_PER_SAMPLE
            self.mclk, self.cycles_run, self.writes = start, cycles_run, writes
            self._ic_hold = IC_FSM_HOLD
        self.in_reset = held

    def write(self, port: int, value: int) -> None:
        """A write to $A04000-$A04003 (or $4000-$4003 from the Z80).

        Port 0 and 2 latch an address in part I and part II, ports 1 and 3 the data
        (``ym3438.c`` ``OPN2_Write``, lines 1344-1357: the port's bit 1 becomes bit 8
        of the written value, which is how part II is told from part I).  The write
        is taken up by the next internal cycle's :meth:`_do_io`.
        """
        if self.in_reset:
            return
        port &= 3
        self.write_data = ((port << 7) & 0x100) | (value & 0xFF)
        if port & 1:
            self.write_d |= 1
        else:
            self.write_a |= 1
        self.writes += 1

    def read(self, port: int) -> int:
        """The status byte: bit 7 BUSY, bit 1 timer B overflowed, bit 0 timer A.

        The discrete YM2612 answers on port 0 only; the ASIC answers on every port
        (Eke's counting program on a VA4 Model 1 and a VA0 Model 2, forum t=386
        start=810; hardware-captured, :class:`~megadrive_python.machine.FMVariant`).
        A port the chip does not answer on leaves the bus undriven, which on this
        board reads the 68000's prefetch -- the caller supplies that.  The value the
        chip drives decays after :data:`STATUS_TIME` cycles without a read
        (``ym3438.c`` ``OPN2_Read``, lines 1370-1427).
        """
        if (port & 3) == 0 or self.variant.busy_on_all_ports:
            if self.mode_test_21[6]:
                self.status = self._test_read()
            else:
                self.status = (
                    (self.busy << 7)
                    | (self.timer_b_overflow_flag << 1)
                    | self.timer_a_overflow_flag
                )
            self.status_time = STATUS_TIME[0 if self.variant.ladder else 1]
        return self.status if self.status_time else 0

    def _test_read(self) -> int:
        """Register $21 bit 6 puts the chip's internals on the status port instead
        (``ym3438.c`` lines 1374-1392): the phase generator's and envelope
        generator's serial bits in the top two, and either an operator's output or
        the channel accumulator's in the rest.  Nemesis's test ROMs use it."""
        slot = (self.cycles + 18) % 24
        data = ((self.pg_read & 1) << 15) | ((self.eg_read[self.mode_test_21[0]] & 1) << 14)
        if self.mode_test_2c[4]:
            data |= self.ch_read & 0x1FF
        else:
            data |= self.fm_out[slot] & 0x3FFF
        return (data & 0xFF) if self.mode_test_21[7] else (data >> 8)

    def irq(self) -> bool:
        """The /IRQ pin: either timer's overflow flag.  The Mega Drive leaves it
        unconnected (no source here wires it to either CPU), so it is exposed for
        completeness and for the referee comparison."""
        return bool(self.timer_a_overflow_flag or self.timer_b_overflow_flag)

    # -- time ---------------------------------------------------------------------

    def run(self, mclk: int) -> None:
        """Advance to master clock ``mclk``."""
        cycles = (mclk - self.mclk) // MCLK_PER_CYCLE
        if cycles <= 0:
            return
        self.mclk += cycles * MCLK_PER_CYCLE
        self.cycles_run += cycles
        if self.in_reset:
            # /IC holds the chip's state, and the output is silent, but the slot
            # counter runs on once its brief hold after the assertion is over.
            hold = min(self._ic_hold, cycles)
            self._ic_hold -= hold
            self.cycles = (self.cycles + cycles - hold) % CYCLES_PER_SAMPLE
            self._acc_n += cycles
            while self._acc_n >= CYCLES_PER_SAMPLE:
                self._acc_n -= CYCLES_PER_SAMPLE
                self.samples.append((0, 0))
            return
        for _ in range(cycles):
            self.cycle()

    def cycle(self) -> None:
        """One internal cycle: the whole chip, in the order the die runs it
        (``ym3438.c`` ``OPN2_Clock``, lines 1209-1341)."""
        if not self.enabled:
            self._quiet_cycle()
            return
        cycles = self.cycles
        self.lfo_inc = self.mode_test_21[1]
        self.pg_read >>= 1
        self.eg_read[1] >>= 1
        self.eg_cycle += 1
        # The envelope timer's shift is locked once every three passes of cycle 1.
        if cycles == 1 and self.eg_quotient == 2:
            self.eg_shift_lock = 0 if self.eg_cycle_stop else self.eg_shift + 1
            self.eg_timer_low_lock = self.eg_timer & 3
        if cycles == 0:
            # The LFO's triangle: bit 6 of the counter reflects it, bits 5-0 are the
            # amplitude and bits 6-2 the phase modulation position.
            self.lfo_pm = self.lfo_cnt >> 2
            am = self.lfo_cnt & 0x3F if self.lfo_cnt & 0x40 else (self.lfo_cnt ^ 0x3F) & 0x3F
            self.lfo_am = am << 1
        elif cycles == 1:
            self.eg_quotient = (self.eg_quotient + 1) % 3
            self.eg_cycle = 0
            self.eg_cycle_stop = 1
            self.eg_shift = 0
            self.eg_timer_inc |= self.eg_quotient >> 1
            timer = self.eg_timer + self.eg_timer_inc
            self.eg_timer_inc = timer >> 12
            self.eg_timer = timer & 0xFFF
        elif cycles == 2:
            self.pg_read = self.pg_phase[21] & 0x3FF
            self.eg_read[1] = self.eg_out[0]
        elif cycles == 13:
            self.eg_cycle = 0
            self.eg_cycle_stop = 1
            self.eg_shift = 0
            timer = self.eg_timer + self.eg_timer_inc
            self.eg_timer_inc = timer >> 12
            self.eg_timer = timer & 0xFFF
        elif cycles == 23:
            self.lfo_inc |= 1
        if self.mode_test_21[5]:
            self.eg_timer &= ~(1 << self.eg_cycle)
        if (
            (self.eg_timer >> self.eg_cycle) | (self.pin_test_in & self.eg_custom_timer)
        ) & self.eg_cycle_stop:
            self.eg_shift = self.eg_cycle
            self.eg_cycle_stop = 0

        self._do_io()
        self._timer_a()
        self._timer_b()
        self._key_on()
        self._ch_output()
        self._ch_generate()
        self._fm_prepare()
        self._fm_generate()
        self._phase_generate()
        self._phase_increment()
        self._envelope_adsr()
        self._envelope_output()
        self._envelope_ssg_eg()
        self._envelope_prepare()
        self._select_fnum(cycles)
        self._update_lfo()
        self._register_write()

        cycles = (cycles + 1) % 24
        self.cycles = cycles
        self.channel = cycles % 6
        if self.status_time:
            self.status_time -= 1
        # One output sample per 24 cycles: the multiplexed DAC summed, which is what
        # the console's sample-and-hold and low-pass do (docs/undocumented-behavior.md,
        # "The YM2612").
        self._acc_l += self.mol
        self._acc_r += self.mor
        self._acc_n += 1
        if self._acc_n == CYCLES_PER_SAMPLE:
            self.samples.append((self._acc_l, self._acc_r))
            self._acc_l = self._acc_r = self._acc_n = 0

    def _quiet_cycle(self) -> None:
        """One internal cycle with :attr:`enabled` off: the bus, the timers, key-on
        and the register file, and nothing else.

        Everything skipped -- the phase generator, the envelope generator, the LFO,
        the operators and the DAC -- is state only the sound depends on, so a caller
        that never wants the sound (a compatibility sweep, ``--no-sound``, capturing
        a register stream) pays for none of it, while a game that polls BUSY or the
        timers still sees what it would. The cost of that is that the generator's
        state stops: turning :attr:`enabled` back on part way through a run resumes
        from stale envelopes and phases, so it is a switch to set once.
        """
        self._do_io()
        self._timer_a()
        self._timer_b()
        self._key_on()
        self._register_write()
        cycles = (self.cycles + 1) % 24
        self.cycles = cycles
        self.channel = cycles % 6
        if self.status_time:
            self.status_time -= 1
        self._acc_n += 1
        if self._acc_n == CYCLES_PER_SAMPLE:
            self.samples.append((0, 0))
            self._acc_n = 0

    # -- the bus interface --------------------------------------------------------

    def _do_io(self) -> None:
        """The write strobes and the BUSY counter (``ym3438.c`` ``OPN2_DoIO``, lines
        166-178).

        BUSY follows a data write for 32 internal cycles, which at 6 68000 clocks a
        cycle is Eke's measured "32 internal clocks (32 x 6 68000 cycles)" (forum
        t=386 start=810; hardware-captured, docs/timing.md).  Only a *data* write
        sets it, which is his other finding.
        """
        self.write_a_en = 1 if (self.write_a & 3) == 1 else 0
        self.write_d_en = 1 if (self.write_d & 3) == 1 else 0
        self.write_a = (self.write_a << 1) & 3
        self.write_d = (self.write_d << 1) & 3
        self.busy = self.write_busy
        self.write_busy_cnt = (self.write_busy_cnt + self.write_busy) & 0x1F
        self.write_busy = (
            1 if (self.write_busy and self.write_busy_cnt != 0) or self.write_d_en else 0
        )

    def _register_write(self) -> None:
        """The register file, written one slot and one channel at a time.

        The chip has no register RAM the CPU can reach: a write waits in a latch
        until the cycle that serves the addressed slot comes round, which is Kabuto's
        "registers $30-$FF live in a rotating shift register; wait about 33.6 Z80
        cycles between writes" (docs/undocumented-behavior.md, "The YM2612" item 2).
        That is modelled here by construction, not by a delay.
        ``ym3438.c`` ``OPN2_DoRegWrite``, lines 180-452.
        """
        slot = self.cycles % 12
        channel = self.channel
        address = self.address
        if self.write_fm_data:
            if OP_OFFSET[slot] == (address & 0x107):
                if address & 0x08:
                    slot += 12  # operator 2 or 4 of the same channel
                data = self.data
                group = address & 0xF0
                if group == 0x30:
                    self.multi[slot] = (data & 0x0F) << 1 if data & 0x0F else 1
                    self.dt[slot] = (data >> 4) & 7
                elif group == 0x40:
                    self.tl[slot] = data & 0x7F
                elif group == 0x50:
                    self.ar[slot] = data & 0x1F
                    self.ks[slot] = (data >> 6) & 3
                elif group == 0x60:
                    self.dr[slot] = data & 0x1F
                    self.am[slot] = (data >> 7) & 1
                elif group == 0x70:
                    self.sr[slot] = data & 0x1F
                elif group == 0x80:
                    self.rr[slot] = data & 0x0F
                    level = (data >> 4) & 0x0F
                    # SL = 15 means "all the way down", carried as bit 4.
                    self.sl[slot] = level | ((level + 1) & 0x10)
                elif group == 0x90:
                    self.ssg_eg[slot] = data & 0x0F
            if CH_OFFSET[channel] == (address & 0x103):
                data = self.data
                group = address & 0xFC
                if group == 0xA0:
                    fnum = (data & 0xFF) | ((self.reg_a4 & 7) << 8)
                    self.fnum[channel] = fnum
                    self.block[channel] = (self.reg_a4 >> 3) & 7
                    self.kcode[channel] = (self.block[channel] << 2) | FN_NOTE[fnum >> 7]
                elif group == 0xA4:
                    # $A4 is a holding latch: the F-number lands only when $A0 is
                    # written, which is why the write order matters (Nemesis's
                    # ym2612FrequencyWriteOrder.bin).
                    self.reg_a4 = data & 0xFF
                elif group == 0xA8:
                    fnum = (data & 0xFF) | ((self.reg_ac & 7) << 8)
                    self.fnum_3ch[channel] = fnum
                    self.block_3ch[channel] = (self.reg_ac >> 3) & 7
                    self.kcode_3ch[channel] = (self.block_3ch[channel] << 2) | FN_NOTE[fnum >> 7]
                elif group == 0xAC:
                    self.reg_ac = data & 0xFF
                elif group == 0xB0:
                    self.connect[channel] = data & 7
                    self.fb[channel] = (data >> 3) & 7
                elif group == 0xB4:
                    self.pms[channel] = data & 7
                    self.ams[channel] = (data >> 4) & 3
                    self.pan_l[channel] = (data >> 7) & 1
                    self.pan_r[channel] = (data >> 6) & 1

        if self.write_a_en or self.write_d_en:
            if self.write_a_en:
                self.write_fm_data = 0
            if self.write_fm_address and self.write_d_en:
                self.write_fm_data = 1
            if self.write_a_en:
                if (self.write_data & 0xF0) != 0:
                    self.address = self.write_data
                    self.write_fm_address = 1
                else:
                    # $00-$0F is the SSG's address space, which the YM2612 lacks.
                    self.write_fm_address = 0
            if self.write_d_en and (self.write_data & 0x100) == 0:
                self._mode_write(self.write_fm_mode_a, self.write_data & 0xFF)
            if self.write_a_en:
                self.write_fm_mode_a = self.write_data & 0x1FF
        if self.write_fm_data:
            self.data = self.write_data & 0xFF

    def _mode_write(self, address: int, data: int) -> None:
        """Part I's $21-$2C, which are not per-slot (``ym3438.c`` lines 364-441)."""
        if address == 0x21:  # LSI test 1
            for i in range(8):
                self.mode_test_21[i] = (data >> i) & 1
        elif address == 0x22:  # LFO
            self.lfo_en = 0x7F if (data >> 3) & 1 else 0
            self.lfo_freq = data & 7
        elif address == 0x24:  # timer A, high 8 bits
            self.timer_a_reg = (self.timer_a_reg & 3) | ((data & 0xFF) << 2)
        elif address == 0x25:  # timer A, low 2 bits
            self.timer_a_reg = (self.timer_a_reg & 0x3FC) | (data & 3)
        elif address == 0x26:  # timer B
            self.timer_b_reg = data & 0xFF
        elif address == 0x27:  # channel 3 mode, CSM, timer control
            self.mode_ch3 = (data & 0xC0) >> 6
            self.mode_csm = 1 if self.mode_ch3 == 2 else 0
            self.timer_a_load = data & 1
            self.timer_a_enable = (data >> 2) & 1
            self.timer_a_reset = (data >> 4) & 1
            self.timer_b_load = (data >> 1) & 1
            self.timer_b_enable = (data >> 3) & 1
            self.timer_b_reset = (data >> 5) & 1
        elif address == 0x28:  # key on/off
            for i in range(4):
                self.mode_kon_operator[i] = (data >> (4 + i)) & 1
            if (data & 3) == 3:
                self.mode_kon_channel = 0xFF  # no channel: the write is lost
            else:
                self.mode_kon_channel = (data & 3) + ((data >> 2) & 1) * 3
        elif address == 0x2A:  # DAC data
            self.dacdata = (self.dacdata & 1) | (((data ^ 0x80) << 1) & 0x1FE)
        elif address == 0x2B:  # DAC enable
            self.dacen = data >> 7
        elif address == 0x2C:  # LSI test 2
            for i in range(8):
                self.mode_test_2c[i] = (data >> i) & 1
            # Bit 3 is the DAC's ninth bit, which is how the "9-bit DAC" of Kabuto's
            # notes is reached (docs/undocumented-behavior.md, "The YM2612" item 3).
            self.dacdata = (self.dacdata & 0x1FE) | self.mode_test_2c[3]
            self.eg_custom_timer = 1 if not self.mode_test_2c[7] and self.mode_test_2c[6] else 0

    # -- the timers ----------------------------------------------------------------

    def _timer_a(self) -> None:
        """Timer A: a 10-bit up-counter stepped once per output sample, which also
        drives CSM's key-on (``ym3438.c`` ``OPN2_DoTimerA``, lines 1047-1091)."""
        load = self.timer_a_overflow
        if self.cycles == 2:
            load |= 1 if (not self.timer_a_load_lock and self.timer_a_load) else 0
            self.timer_a_load_lock = self.timer_a_load
            self.mode_kon_csm = load if self.mode_csm else 0
        time = self.timer_a_reg if self.timer_a_load_latch else self.timer_a_cnt
        self.timer_a_load_latch = load
        if (self.cycles == 1 and self.timer_a_load_lock) or self.mode_test_21[2]:
            time += 1
        if self.timer_a_reset:
            self.timer_a_reset = 0
            self.timer_a_overflow_flag = 0
        else:
            self.timer_a_overflow_flag |= self.timer_a_overflow & self.timer_a_enable
        self.timer_a_overflow = time >> 10
        self.timer_a_cnt = time & 0x3FF

    def _timer_b(self) -> None:
        """Timer B: an 8-bit counter stepped once per 16 output samples
        (``ym3438.c`` ``OPN2_DoTimerB``, lines 1093-1136)."""
        load = self.timer_b_overflow
        if self.cycles == 2:
            load |= 1 if (not self.timer_b_load_lock and self.timer_b_load) else 0
            self.timer_b_load_lock = self.timer_b_load
        time = self.timer_b_reg if self.timer_b_load_latch else self.timer_b_cnt
        self.timer_b_load_latch = load
        if self.cycles == 1:
            self.timer_b_subcnt += 1
        if (self.timer_b_subcnt == 0x10 and self.timer_b_load_lock) or self.mode_test_21[2]:
            time += 1
        self.timer_b_subcnt &= 0x0F
        if self.timer_b_reset:
            self.timer_b_reset = 0
            self.timer_b_overflow_flag = 0
        else:
            self.timer_b_overflow_flag |= self.timer_b_overflow & self.timer_b_enable
        self.timer_b_overflow = time >> 8
        self.timer_b_cnt = time & 0xFF

    def _key_on(self) -> None:
        """Key on and off, one slot a cycle, plus CSM's key-on of channel 3 from
        timer A (``ym3438.c`` ``OPN2_KeyOn``, lines 1138-1184)."""
        slot = self.cycles
        self.eg_kon_latch[slot] = self.mode_kon[slot]
        self.eg_kon_csm[slot] = 0
        if self.channel == 2 and self.mode_kon_csm:
            self.eg_kon_latch[slot] = 1
            self.eg_kon_csm[slot] = 1
        if slot == self.mode_kon_channel:
            channel = self.channel
            operator = self.mode_kon_operator
            self.mode_kon[channel] = operator[0]
            self.mode_kon[channel + 12] = operator[1]
            self.mode_kon[channel + 6] = operator[2]
            self.mode_kon[channel + 18] = operator[3]

    # -- the phase generator --------------------------------------------------------

    def _phase_generate(self) -> None:
        """The phase accumulators, 19 and 20 cycles behind the increment's
        calculation (``ym3438.c`` ``OPN2_PhaseGenerate``, lines 452-468)."""
        slot = (self.cycles + 20) % 24
        if self.pg_reset[slot]:
            self.pg_inc[slot] = 0
        slot = (self.cycles + 19) % 24
        if self.pg_reset[slot] or self.mode_test_21[3]:
            self.pg_phase[slot] = 0
        self.pg_phase[slot] = (self.pg_phase[slot] + self.pg_inc[slot]) & 0xFFFFF

    def _phase_increment(self) -> None:
        """This slot's phase increment: F-number, the LFO's vibrato, the block, the
        detune and the multiplier (``ym3438.c`` ``OPN2_PhaseCalcIncrement``, lines
        450-518)."""
        slot = self.cycles
        fnum = self.pg_fnum
        fnum_h = fnum >> 4
        lfo = self.lfo_pm
        lfo_l = lfo & 0x0F
        pms = self.pms[self.channel]
        fnum <<= 1
        # The LFO's triangle folds in its own quarter, and the modulation is two
        # shifted copies of the F-number's high bits added together.
        if lfo_l & 8:
            lfo_l ^= 0x0F
        fm = (fnum_h >> PG_LFO_SH1[pms][lfo_l]) + (fnum_h >> PG_LFO_SH2[pms][lfo_l])
        if pms > 5:
            fm <<= pms - 5
        fm >>= 2
        fnum = (fnum - fm if lfo & 0x10 else fnum + fm) & 0xFFF
        basefreq = (fnum << self.pg_block) >> 2

        detune = 0
        dt = self.dt[slot]
        dt_l = dt & 3
        if dt_l:
            kcode = self.pg_kcode
            if kcode > 0x1C:
                kcode = 0x1C
            block = kcode >> 2
            note = kcode & 3
            total = block + 9 + ((1 if dt_l == 3 else 0) | (dt_l & 2))
            detune = PG_DETUNE[((total & 1) << 2) | note] >> (9 - (total >> 1))
        basefreq = (basefreq - detune if dt & 4 else basefreq + detune) & 0x1FFFF
        self.pg_inc[slot] = (basefreq * self.multi[slot]) >> 1 & 0xFFFFF

    def _select_fnum(self, slot: int) -> None:
        """Which channel's F-number the *next* cycle's phase increment uses.

        Normally the channel one place ahead of the current one; in channel 3's
        special mode three of its four operators take their own F-number from $A8-$AA
        instead (``ym3438.c`` lines 1295-1327).  The slot numbers below are the ones
        the die uses for channel 3's operators 1, 3 and 2; operator 4 keeps the
        channel's own registers, which is what makes the fourth operator the one that
        still follows $A0.
        """
        if self.mode_ch3:
            if slot == 1:
                self.pg_fnum = self.fnum_3ch[1]
                self.pg_block = self.block_3ch[1]
                self.pg_kcode = self.kcode_3ch[1]
                return
            if slot == 7:
                self.pg_fnum = self.fnum_3ch[0]
                self.pg_block = self.block_3ch[0]
                self.pg_kcode = self.kcode_3ch[0]
                return
            if slot == 13:
                self.pg_fnum = self.fnum_3ch[2]
                self.pg_block = self.block_3ch[2]
                self.pg_kcode = self.kcode_3ch[2]
                return
        channel = (self.channel + 1) % 6
        self.pg_fnum = self.fnum[channel]
        self.pg_block = self.block[channel]
        self.pg_kcode = self.kcode[channel]

    # -- the envelope generator ------------------------------------------------------

    def _envelope_ssg_eg(self) -> None:
        """SSG-EG: the four latches that repeat, invert, hold or reset an envelope
        that has run down (``ym3438.c`` ``OPN2_EnvelopeSSGEG``, lines 520-560).

        Judged against Nemesis's MD1600 recording of his ``ym2612ssg-eg.bin``
        (hardware-captured; docs/validation.md) by measurement, not by ear.
        """
        slot = self.cycles
        direction = 0
        self.eg_ssg_pgrst[slot] = 0
        self.eg_ssg_repeat[slot] = 0
        self.eg_ssg_hold_up[slot] = 0
        mode = self.ssg_eg[slot]
        if mode & 8:
            direction = self.eg_ssg_dir[slot]
            if self.eg_level[slot] & 0x200:
                low = mode & 3
                if low == 0:
                    self.eg_ssg_pgrst[slot] = 1
                if not mode & 1:
                    self.eg_ssg_repeat[slot] = 1
                if low == 2:
                    direction ^= 1
                elif low == 3:
                    direction = 1
            if self.eg_kon_latch[slot] and (mode & 7) in (5, 3):
                self.eg_ssg_hold_up[slot] = 1
            direction &= self.eg_kon[slot]
        self.eg_ssg_dir[slot] = direction
        self.eg_ssg_enable[slot] = (mode >> 3) & 1
        self.eg_ssg_inv[slot] = (direction ^ (((mode >> 2) & 1) & ((mode >> 3) & 1))) & self.eg_kon[
            slot
        ]

    def _envelope_adsr(self) -> None:
        """The envelope's state and level, two cycles behind the current slot
        (``ym3438.c`` ``OPN2_EnvelopeADSR``, lines 562-694)."""
        slot = (self.cycles + 22) % 24
        nkon = self.eg_kon_latch[slot]
        okon = self.eg_kon[slot]
        state = self.eg_state[slot]
        nextstate = state
        inc = 0
        self.eg_read[0] = self.eg_read_inc
        self.eg_read_inc = 1 if self.eg_inc > 0 else 0

        self.pg_reset[slot] = 1 if ((nkon and not okon) or self.eg_ssg_pgrst[slot]) else 0
        kon_event = (nkon and not okon) or (okon and self.eg_ssg_repeat[slot])
        koff_event = okon and not nkon

        level = self.eg_level[slot]
        ssg_level = level
        if self.eg_ssg_inv[slot]:
            ssg_level = (512 - level) & 0x3FF
        if koff_event:
            level = ssg_level
        eg_off = (level >> 9) if self.eg_ssg_enable[slot] else int((level & 0x3F0) == 0x3F0)
        nextlevel = level

        if kon_event:
            nextstate = EG_ATTACK
            if self.eg_ratemax:
                nextlevel = 0  # a rate of 31 attacks in one step
            elif state == EG_ATTACK and level != 0 and self.eg_inc and nkon:
                inc = (~level << self.eg_inc) >> 5
        else:
            if state == EG_ATTACK:
                if level == 0:
                    nextstate = EG_DECAY
                elif self.eg_inc and not self.eg_ratemax and nkon:
                    inc = (~level << self.eg_inc) >> 5
            elif state == EG_DECAY:
                if (level >> 4) == (self.eg_sl[1] << 1):
                    nextstate = EG_SUSTAIN
                elif not eg_off and self.eg_inc:
                    inc = 1 << (self.eg_inc - 1)
                    if self.eg_ssg_enable[slot]:
                        inc <<= 2  # SSG-EG runs the envelope four times as fast
            elif not eg_off and self.eg_inc:
                inc = 1 << (self.eg_inc - 1)
                if self.eg_ssg_enable[slot]:
                    inc <<= 2
            if not nkon:
                nextstate = EG_RELEASE
        if self.eg_kon_csm[slot]:
            # CSM's key-on carries the total level in, so a CSM note starts from the
            # operator's own attenuation rather than from silence.
            nextlevel |= self.eg_tl[1] << 3
        if not kon_event and not self.eg_ssg_hold_up[slot] and state != EG_ATTACK and eg_off:
            nextstate = EG_RELEASE
            nextlevel = 0x3FF
        nextlevel += inc
        self.eg_kon[slot] = nkon
        self.eg_level[slot] = nextlevel & 0x3FF
        self.eg_state[slot] = nextstate

    def _envelope_output(self) -> None:
        """The attenuation an operator will be given: the level, inverted for
        SSG-EG, plus the LFO's tremolo and the total level (``ym3438.c``
        ``OPN2_EnvelopeGenerate``, lines 802-834)."""
        slot = (self.cycles + 23) % 24
        level = self.eg_level[slot]
        if self.eg_ssg_inv[slot]:
            level = 512 - level
        if self.mode_test_21[5]:
            level = 0
        level &= 0x3FF
        level += self.eg_lfo_am
        if not (self.mode_csm and self.channel == 3):
            # In CSM the total level reaches channel 3 through the key-on above
            # instead, so adding it here too would apply it twice.
            level += self.eg_tl[0] << 3
        self.eg_out[slot] = 0x3FF if level > 0x3FF else level

    def _envelope_prepare(self) -> None:
        """This slot's rate, key scaling and increment for the next pass
        (``ym3438.c`` ``OPN2_EnvelopePrepare``, lines 696-800).

        The increment comes out of a 12-bit counter whose shift is locked once every
        three passes: below rate 48 a rate steps on one in 1, 2 or 4 of the counter's
        ticks, and at 48 and above it steps by 1 to 4 every tick.
        """
        slot = self.cycles
        rate = (self.eg_rate << 1) + self.eg_ksv
        if rate > 0x3F:
            rate = 0x3F
        inc = 0
        if self.eg_rate != 0 and self.eg_quotient == 2:
            total = ((rate >> 2) + self.eg_shift_lock) & 0x0F
            if rate < 48:
                if total == 12:
                    inc = 1
                elif total == 13:
                    inc = (rate >> 1) & 1
                elif total == 14:
                    inc = rate & 1
            else:
                inc = EG_STEPHI[rate & 3][self.eg_timer_low_lock] + (rate >> 2) - 11
                if inc > 4:
                    inc = 4
        self.eg_inc = inc
        self.eg_ratemax = 1 if (rate >> 1) == 0x1F else 0

        rate_sel = self.eg_state[slot]
        if (self.eg_kon[slot] and self.eg_ssg_repeat[slot]) or (
            not self.eg_kon[slot] and self.eg_kon_latch[slot]
        ):
            rate_sel = EG_ATTACK
        if rate_sel == EG_ATTACK:
            self.eg_rate = self.ar[slot]
        elif rate_sel == EG_DECAY:
            self.eg_rate = self.dr[slot]
        elif rate_sel == EG_SUSTAIN:
            self.eg_rate = self.sr[slot]
        else:
            self.eg_rate = (self.rr[slot] << 1) | 1
        self.eg_ksv = self.pg_kcode >> (self.ks[slot] ^ 3)
        if self.am[slot]:
            self.eg_lfo_am = self.lfo_am >> EG_AM_SHIFT[self.ams[self.channel]]
        else:
            self.eg_lfo_am = 0
        # Total level and sustain level are delayed a cycle each, so that the value
        # the envelope uses is the one written before this slot came round.
        self.eg_tl[1] = self.eg_tl[0]
        self.eg_tl[0] = self.tl[slot]
        self.eg_sl[1] = self.eg_sl[0]
        self.eg_sl[0] = self.sl[slot]

    # -- the LFO ---------------------------------------------------------------------

    def _update_lfo(self) -> None:
        """The LFO's counter: a 7-bit counter stepped when its quotient matches the
        reload for the chosen speed (``ym3438.c`` ``OPN2_UpdateLFO``, lines 836-848).
        Register $22 bit 3 gates it; with the LFO off the counter reads 0, which is
        the middle of the triangle."""
        reload_at = LFO_CYCLES[self.lfo_freq]
        if (self.lfo_quotient & reload_at) == reload_at:
            self.lfo_quotient = 0
            self.lfo_cnt += 1
        else:
            self.lfo_quotient += self.lfo_inc
        self.lfo_cnt &= self.lfo_en

    # -- the operators and the channel accumulator -------------------------------------

    def _fm_prepare(self) -> None:
        """The modulation input of the slot six cycles ahead, and operator 1's and
        2's outputs into their holding latches (``ym3438.c`` ``OPN2_FMPrepare``,
        lines 850-895).

        Operator 1's feedback is its last *two* outputs summed and shifted, which is
        why the feedback of 1 is a half step and not a full one.
        """
        slot = (self.cycles + 6) % 24
        channel = self.channel
        op = slot // 6
        connect = self.connect[channel]
        prevslot = (self.cycles + 18) % 24
        rows = FM_ALGORITHM[op]
        mod1 = mod2 = 0
        if rows[0][connect]:
            mod2 |= self.fm_op1_0[channel]
        if rows[1][connect]:
            mod1 |= self.fm_op1_1[channel]
        if rows[2][connect]:
            mod1 |= self.fm_op2[channel]
        if rows[3][connect]:
            mod2 |= self.fm_out[prevslot]
        if rows[4][connect]:
            mod1 |= self.fm_out[prevslot]
        mod = mod1 + mod2
        if op == 0:
            fb = self.fb[channel]
            mod = (mod >> (10 - fb)) if fb else 0
        else:
            mod >>= 1
        self.fm_mod[slot] = mod

        slot = prevslot
        group = slot // 6
        if group == 0:
            self.fm_op1_1[channel] = self.fm_op1_0[channel]
            self.fm_op1_0[channel] = self.fm_out[slot]
        elif group == 2:
            self.fm_op2[channel] = self.fm_out[slot]

    def _fm_generate(self) -> None:
        """One operator: phase plus modulation through the quarter sine, plus the
        envelope, through the exponential (``ym3438.c`` ``OPN2_FMGenerate``, lines
        1012-1045).  The result is 14 bits signed."""
        slot = (self.cycles + 19) % 24
        phase = (self.fm_mod[slot] + (self.pg_phase[slot] >> 10)) & 0x3FF
        quarter = (phase ^ 0xFF) & 0xFF if phase & 0x100 else phase & 0xFF
        level = LOG_SIN[quarter] + (self.eg_out[slot] << 2)
        if level > 0x1FFF:
            level = 0x1FFF
        output = ((EXP[(level & 0xFF) ^ 0xFF] | 0x400) << 2) >> (level >> 8)
        invert = self.mode_test_21[4] << 13
        if phase & 0x200:
            output = ((~output) ^ invert) + 1
        else:
            output ^= invert
        self.fm_out[slot] = _sign_extend(13, output & 0x3FFF)

    def _ch_generate(self) -> None:
        """The channel's accumulator: the operators the algorithm sends to the
        output, summed and clamped to nine bits (``ym3438.c`` ``OPN2_ChGenerate``,
        lines 897-935)."""
        slot = (self.cycles + 18) % 24
        channel = self.channel
        op = slot // 6
        test_dac = self.mode_test_2c[5]
        acc = self.ch_acc[channel]
        add = test_dac
        if op == 0 and not test_dac:
            acc = 0
        if FM_ALGORITHM[op][5][self.connect[channel]] and not test_dac:
            add += self.fm_out[slot] >> 5
        total = acc + add
        if total > 255:
            total = 255
        elif total < -256:
            total = -256
        if op == 0 or test_dac:
            self.ch_out[channel] = self.ch_acc[channel]
        self.ch_acc[channel] = total

    def _ch_output(self) -> None:
        """The two DAC pins.

        The six channels are multiplexed onto one pair of pins, four cycles each; the
        DAC replaces channel 6 in its slot when register $2B bit 7 is set.  The two
        chip types differ exactly here (``ym3438.c`` ``OPN2_ChOutput``, lines
        937-1010):

        * the **YM3438 / ASIC** drives the channel's value for three of its four
          cycles and zero for the fourth;
        * the **discrete YM2612** drives it for *one* of the four and, for the other
          three, the bare sign -- +1 for a non-negative sample, -1 for a negative
          one -- and adds 1 to every non-negative sample.  Averaged by the console's
          filter that is a step at the zero crossing: the **ladder effect**
          (HardWareMan's oscilloscope traces; Aly James's ``MD1_LADDER.wav``;
          docs/undocumented-behavior.md, "The YM2612" item 4).  Nuked-OPN2's comment
          calls its own version "not verified"; YM2608-LLE's die model builds the
          same value (``fmopna_impl.c`` lines 5824-5853), which is the corroboration
          this board relies on.

        The choice is :attr:`FMVariant.ladder`, a field of the machine, not a branch
        in the mixer.
        """
        cycles = self.cycles
        channel = self.channel
        test_dac = self.mode_test_2c[5]
        self.ch_read = self.ch_lock
        if cycles < 12:
            channel += 1
        if (cycles & 3) == 0:
            if not test_dac:
                self.ch_lock = self.ch_out[channel]
            self.ch_lock_l = self.pan_l[channel]
            self.ch_lock_r = self.pan_r[channel]
        if ((cycles >> 2) == 1 and self.dacen) or test_dac:
            out = _sign_extend(8, self.dacdata)
        else:
            out = self.ch_lock

        if self.variant.ladder:
            out_en = (cycles & 3) == 3 or test_dac
            sign = out >> 8
            if out >= 0:
                out += 1
                sign += 1
            self.mol = (out if self.ch_lock_l and out_en else sign) * 3
            self.mor = (out if self.ch_lock_r and out_en else sign) * 3
        else:
            out_en = (cycles & 3) != 0 or test_dac
            self.mol = out if self.ch_lock_l and out_en else 0
            self.mor = out if self.ch_lock_r and out_en else 0

    # -- output ------------------------------------------------------------------------

    def take_samples(self) -> list[tuple[int, int]]:
        """Return and clear the finished samples: one per 24 internal cycles, each
        the sum of that sample's 24 multiplexed DAC values, left and right."""
        samples, self.samples = self.samples, []
        return samples
