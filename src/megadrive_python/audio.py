"""The console's analogue audio path: the FM and the PSG mixed and filtered.

What is modelled, and on whose authority
----------------------------------------

The two chips leave the board as three signals: the YM2612's MOL and MOR, and the
PSG's single output.  They meet in an amplifier whose shape differs by revision --
"instead of the MD1's 1st order filter there's a 2nd order filter for no obvious
reason ... The MD2 sounds noisy, muffled and heavily distorted" (Kabuto, "The
YM2612"; his measurements of several Model 1 and Model 2 boards, research tier).
Neither he nor any pinned document gives a corner frequency, a mixing ratio or a
distortion model, so this module carries:

* a **mixing ratio** between the PSG and the FM (:data:`PSG_GAIN`), measured from
  the MDFourier recordings of real consoles (hardware-captured) by
  ``scripts/run_mdfourier.py`` and recorded in docs/worklog.md;
* a **one-pole low-pass for ``model1`` and two cascaded poles for ``model2``**
  (:data:`FILTER_HZ`), whose corner is fitted to the same recordings and is
  labelled an assumption wherever it is claimed;
* **no distortion model at all**.  Kabuto measures the Model 2's distortion as
  "quite high ... very well audible" but publishes no transfer curve, and inventing
  one would be worse than leaving it out.  The Model 2's output here is therefore
  cleaner than the hardware's, and docs/undocumented-behavior.md says so.

The DAC's own step between $7F and $80 (Kabuto's "quite a jump of 3 samples") and
the discrete chip's crossover distortion are *not* here: they belong to the chip and
are in :mod:`megadrive_python.ym2612` (:meth:`YM2612._ch_output`, the ladder).

Rates
-----

The YM2612 finishes a sample every 1,008 MCLK (53,267 Hz on NTSC) and the PSG every
240 MCLK (223,721 Hz), a ratio of exactly 21/5.  :class:`AudioPath` box-filters the
PSG down onto the FM's grid and then resamples the pair to the host's rate.
"""

from __future__ import annotations

from .machine import Machine

#: Master clocks per finished sample of each chip.
MCLK_PER_FM_SAMPLE = 1008
MCLK_PER_PSG_SAMPLE = 240

#: The PSG's level (0.0-4.0 from :mod:`megadrive_python.psg`) per unit of the FM's
#: summed DAC.  Fitted to the MDFourier recordings; see the module docstring.
PSG_GAIN = 1400.0

#: The analogue low-pass corner, in Hz, for each ``Machine.audio_path``: one pole on
#: the Model 1, two on the Model 2 (Kabuto).  Fitted; not a measured value.
FILTER_HZ = {"model1": 8000.0, "model2": 4500.0}
FILTER_POLES = {"model1": 1, "model2": 2}

#: Full scale.  Six FM channels at full volume sum to about 6,100 of the YM2612's
#: multiplexed units (24 cycles of up to 3 x 256), so this keeps a loud game inside
#: 16 bits with room for the PSG.
OUTPUT_SCALE = 4.0


class AudioPath:
    """Mixes one machine's FM and PSG streams into stereo at ``sample_rate``."""

    def __init__(self, machine: Machine, sample_rate: int = 44_100) -> None:
        self.machine = machine
        self.sample_rate = sample_rate
        self.psg_gain = PSG_GAIN
        self.cutoff = FILTER_HZ.get(machine.audio_path, 8000.0)
        self.poles = FILTER_POLES.get(machine.audio_path, 1)
        self.reset()

    def reset(self) -> None:
        self._psg_acc = 0.0
        self._psg_n = 0
        self._psg_phase = 0
        self._pending_psg: list[float] = []
        self._filter = [(0.0, 0.0)] * self.poles
        self._resample_phase = 0.0
        self._previous = (0.0, 0.0)
        self._have_previous = False

    @property
    def fm_rate(self) -> float:
        return self.machine.mclk / MCLK_PER_FM_SAMPLE

    def _alpha(self) -> float:
        """One pole's coefficient for a corner at :attr:`cutoff`, at the FM rate."""
        rc = 1.0 / (2.0 * 3.141592653589793 * self.cutoff)
        dt = 1.0 / self.fm_rate
        return dt / (rc + dt)

    def mix(self, fm: list[tuple[int, int]], psg: list[float]) -> list[tuple[int, int]]:
        """``fm`` at the chip's rate and ``psg`` at the PSG's, to stereo at
        :attr:`sample_rate`.

        The PSG is box-filtered onto the FM's grid with an integer phase (21 PSG
        samples to 5 FM ones), so nothing drifts over a long run; the sum is then
        low-passed and resampled linearly.  Leftovers of either stream are carried to
        the next call, so a caller may hand over a frame at a time.
        """
        self._pending_psg.extend(psg)
        merged = []
        psg_index = 0
        gain = self.psg_gain
        for left, right in fm:
            # Five FM samples take 21 PSG samples: an integer phase keeps the long-run
            # rate exact without a float accumulator.
            self._psg_phase += MCLK_PER_FM_SAMPLE
            level = 0.0
            taken = 0
            while self._psg_phase >= MCLK_PER_PSG_SAMPLE and psg_index < len(self._pending_psg):
                self._psg_phase -= MCLK_PER_PSG_SAMPLE
                level += self._pending_psg[psg_index]
                psg_index += 1
                taken += 1
            if taken:
                level = level / taken
            else:
                self._psg_phase = min(self._psg_phase, MCLK_PER_FM_SAMPLE)
                level = 0.0
            # The PSG is one signal mixed into both sides.
            merged.append((left + level * gain, right + level * gain))
        del self._pending_psg[:psg_index]
        return self._resample(self._filtered(merged))

    def _filtered(self, samples: list[tuple[float, float]]) -> list[tuple[float, float]]:
        alpha = self._alpha()
        for pole in range(self.poles):
            state_l, state_r = self._filter[pole]
            out = []
            for left, right in samples:
                state_l += alpha * (left - state_l)
                state_r += alpha * (right - state_r)
                out.append((state_l, state_r))
            self._filter[pole] = (state_l, state_r)
            samples = out
        return samples

    def _resample(self, samples: list[tuple[float, float]]) -> list[tuple[int, int]]:
        """Linear interpolation from the FM rate to :attr:`sample_rate`."""
        step = self.fm_rate / self.sample_rate
        out: list[tuple[int, int]] = []
        phase = self._resample_phase
        previous = self._previous
        for sample in samples:
            if not self._have_previous:
                previous = sample
                self._have_previous = True
            while phase < 1.0:
                left = previous[0] + (sample[0] - previous[0]) * phase
                right = previous[1] + (sample[1] - previous[1]) * phase
                out.append(
                    (
                        max(-32768, min(32767, int(left * OUTPUT_SCALE))),
                        max(-32768, min(32767, int(right * OUTPUT_SCALE))),
                    )
                )
                phase += step
            phase -= 1.0
            previous = sample
        self._resample_phase = phase
        self._previous = previous
        return out
