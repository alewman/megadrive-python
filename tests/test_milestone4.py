"""Milestone 4's acceptance: the PSG and the YM2612 against the die-derived models.

The judges here are **Nuked-PSG**, **Nuked-OPN2** and **YM2608-LLE**: no hardware
capture or hardware-corrected test covers the chips' arithmetic (there is no YM2612
datasheet and no self-checking FM test ROM), so the tier rule makes a die-derived
model the judge (docs/validation.md, "The tier rule").  They are C programs built
from the pinned sources by ``sh validation/build_referees.sh``; a test skips when
they are not built, as the other milestones' tests skip without their fetched ROMs.

The long runs -- Nemesis's test ROMs, five cartridges, YM2608-LLE -- are
``scripts/run_fm_streams.py --roms --games --lle`` and are recorded in
docs/worklog.md; what is here is the part that runs in a few seconds on every push.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT

from megadrive_python.audio import AudioPath
from megadrive_python.machine import MACHINES, YM3438_ASIC
from megadrive_python.machine import YM2612 as FM_DISCRETE
from megadrive_python.psg import PSG, VOLUME_LEVELS
from megadrive_python.ym2612 import EXP, LOG_SIN, YM2612

REFEREES = ROOT / "reference" / "build-referees"
NUKED_OPN2 = ROOT / "reference" / "src" / "Nuked-OPN2" / "ym3438.c"
NUKED_PSG = ROOT / "reference" / "src" / "Nuked-PSG" / "ympsg.c"


def _table(source: Path, name: str) -> list[int]:
    text = source.read_text()
    body = re.search(rf"{name}\[\d+\] = \{{(.*?)\}};", text, re.S)
    assert body, f"{name} not found in {source}"
    return [int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]+)", body.group(1))]


# -- the chip's ROMs, generated here and checked against the die's -------------------


@pytest.mark.skipif(not NUKED_OPN2.exists(), reason="Nuked-OPN2 is not fetched")
def test_log_sin_rom_matches_the_die() -> None:
    """All 256 entries of the generated quarter-sine table equal the ones read off the
    YM3438's die (``ym3438.c`` ``logsinrom``)."""
    assert list(LOG_SIN) == _table(NUKED_OPN2, "logsinrom")


@pytest.mark.skipif(not NUKED_OPN2.exists(), reason="Nuked-OPN2 is not fetched")
def test_exp_rom_matches_the_die() -> None:
    assert list(EXP) == _table(NUKED_OPN2, "exprom")


@pytest.mark.skipif(not NUKED_PSG.exists(), reason="Nuked-PSG is not fetched")
def test_psg_volume_curve_matches_the_die() -> None:
    """The board's attenuation curve is Nuked-PSG's measured one, not 2 dB a step."""
    body = re.search(r"ympsg_vol\[17\] = \{(.*?)\};", NUKED_PSG.read_text(), re.S)
    assert body
    values = [float(value) for value in body.group(1).replace("\n", "").split(",")]
    assert list(VOLUME_LEVELS) == values[:16]
    # And it is not the ideal ladder: the third step is 0.622, not 10 ** -0.2 = 0.631.
    assert VOLUME_LEVELS[2] != pytest.approx(10**-0.2, rel=1e-3)


# -- the chips' own rules --------------------------------------------------------------


def test_psg_powers_up_part_way_round_its_rotation() -> None:
    """/IC leaves the counters at 1 and the one-hot selector on the noise channel, not
    everything at zero (``ympsg.c`` ``YMPSG_Init``)."""
    psg = PSG()
    assert psg.counter == [1, 1, 1, 1]
    assert psg.chan_sel == 0x08
    assert psg.counter_of == 0x0F
    assert psg.volume == [15, 15, 15, 15]
    assert psg.freq == [0, 0, 0]


def test_psg_makes_one_sample_every_sixteen_input_clocks() -> None:
    psg = PSG()
    psg.run(16 * 100)
    assert len(psg.levels) == 100


def test_ym2612_busy_lasts_32_internal_cycles() -> None:
    """Eke's measurement: BUSY is set by a *data* write and lasts 32 internal clocks,
    which are 6 68000 cycles each (forum t=386 start=810; docs/timing.md)."""
    chip = YM2612(FM_DISCRETE)
    chip.write(0, 0x22)  # an address write: no BUSY
    for _ in range(4):
        chip.cycle()
    assert chip.busy == 0
    chip.write(1, 0x00)  # a data write
    chip.cycle()
    assert chip.busy == 0  # it appears on the cycle after the strobe
    high = 0
    for _ in range(64):
        chip.cycle()
        high += chip.busy
    assert high == 32
    assert chip.busy == 0


def test_ym2612_busy_reads_on_port_0_only_on_the_discrete_chip() -> None:
    """The ASIC answers on every port, the discrete YM2612 on port 0 (Eke)."""
    for variant, ports in ((FM_DISCRETE, [0]), (YM3438_ASIC, [0, 1, 2, 3])):
        chip = YM2612(variant)
        chip.write(1, 0x00)
        for _ in range(3):
            chip.cycle()
        assert chip.busy == 1
        for port in range(4):
            expected = 0x80 if port in ports else 0x00
            assert chip.read(port) & 0x80 == expected or port not in ports


def test_ym2612_timer_a_overflows_after_its_reload() -> None:
    """Timer A counts one per output sample and overflows at 1024 - reload
    (the Manual; ``ym3438.c`` ``OPN2_DoTimerA``)."""
    chip = YM2612(FM_DISCRETE)
    reload_value = 1000
    for register, value in ((0x24, reload_value >> 2), (0x25, reload_value & 3), (0x27, 0x05)):
        chip.write(0, register)
        chip.cycle()
        chip.cycle()
        chip.write(1, value)
        for _ in range(4):
            chip.cycle()
    samples = 0
    while not chip.timer_a_overflow_flag and samples < 200:
        for _ in range(24):
            chip.cycle()
        samples += 1
    assert chip.timer_a_overflow_flag
    assert 1024 - reload_value - 2 <= samples <= 1024 - reload_value + 2


def test_ladder_is_a_variant_field_and_changes_the_output() -> None:
    """The discrete chip drives the sign in the gaps and multiplies by three; the ASIC
    drives zero and does not (``ym3438.c`` ``OPN2_ChOutput``)."""
    silent_discrete = YM2612(FM_DISCRETE)
    silent_asic = YM2612(YM3438_ASIC)
    for _ in range(96):
        silent_discrete.cycle()
        silent_asic.cycle()
    assert silent_asic.mol == 0
    assert silent_discrete.mol == 3  # +1 sign, amplified
    assert FM_DISCRETE.ladder and not YM3438_ASIC.ladder


def test_fm_is_held_by_the_z80s_reset_line() -> None:
    """ "For as long as the Z80 is reset, the YM2612 cannot be used" (CMD 2.2)."""
    chip = YM2612(FM_DISCRETE)
    chip.reset_line(True)
    chip.write(0, 0x28)
    chip.write(1, 0xF0)
    assert chip.writes == 0
    chip.reset_line(False)
    chip.write(0, 0x28)
    assert chip.writes == 1


def test_audio_path_mixes_and_resamples() -> None:
    path = AudioPath(MACHINES["genesis"], sample_rate=44_100)
    fm = [(100, -100)] * 5000
    psg = [0.0] * 21000
    out = path.mix(fm, psg)
    # 5,000 samples at 53,267 Hz is 0.0939 s, which is 4,141 at 44,100.
    assert 4100 <= len(out) <= 4180
    assert all(-32768 <= left <= 32767 and -32768 <= right <= 32767 for left, right in out)


# -- against the referees ---------------------------------------------------------------


@pytest.mark.skipif(
    not (REFEREES / "psg_ref").exists(), reason="run `sh validation/build_referees.sh`"
)
def test_psg_matches_nuked_psg() -> None:
    """The synthetic streams of ``scripts/run_psg_compare.py``: every input clock."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_psg_compare.py")],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 not identical" in result.stdout


@pytest.mark.skipif(
    not (REFEREES / "opn2_ref").exists(), reason="run `sh validation/build_referees.sh`"
)
def test_ym2612_matches_nuked_opn2() -> None:
    """The synthetic streams of ``scripts/run_fm_streams.py``: every internal cycle of
    both DAC pins and the status byte, in both chip variants."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_fm_streams.py")],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 comparisons not identical" in result.stdout
