"""The consoles as data: one :class:`Machine` per model, with its chips as variants.

Every field that differs between models names its source and that source's tier
(docs/validation.md, "The tier rule"). The fields the first milestones read are the
region, the version register, TMSS, and the VSRAM size; the FM, audio-path and VDP
revision fields are here so that later milestones fill them in rather than invent
them.

Clocks: MAME 0.285 ``sega/megadriv.h`` (``MASTER_CLOCK_NTSC``/``_PAL``), agreeing
with the Manual's CPU clocks (docs/start-here.md, "Clocks").
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# The FM chip's two variants are the core's (ym2612-python, docs/contract.md, "One
# chip, two variants"); a Machine names one.
from ym2612_python import YM2612_DISCRETE, YM3438_ASIC, FMVariant

MCLK_NTSC = 53_693_175
MCLK_PAL = 53_203_424

#: Master clocks per 68000 clock and per Z80 clock (docs/timing.md).
M68K_DIVIDER = 7
Z80_DIVIDER = 15

#: A line is 3,420 MCLK in both horizontal modes (Jorge Nuno's oscilloscope, forum
#: t=519; Kabuto, "Picture size"). docs/timing.md, "The line and the frame".
MCLK_PER_LINE = 3420


@dataclass(frozen=True)
class VDPVariant:
    """The 315-5313 (YM7101) as one model has it.

    ``vsram_bytes``: 0x50 (40 words) on the Model 1 and Model 2; 0x80 on the
    Genesis 3, which is why it fails VDPFIFOTesting's VSRAM tests ("it actually has
    an 0x80 byte VSRAM, not a 0x50 byte VSRAM like earlier VDP models": Nemesis,
    forum t=1291 start=30; hardware-corrected).

    ``revision``: "315-5313" or "315-5313A", the manufacturing change behind
    Overdrive 2's glitches on ~30 % of Model 1s ("VA4 and prior use this chip,
    while VA6 and higher use the 5313A or newer", Kabuto, "Plane masking
    instability"; research). Not read before milestone 5.
    """

    name: str
    vsram_bytes: int = 0x50
    revision: str = "315-5313A"


VDP_5313 = VDPVariant("315-5313", revision="315-5313")
VDP_5313A = VDPVariant("315-5313A", revision="315-5313A")
VDP_GENESIS3 = VDPVariant("Genesis 3 (FF1004 VDP)", vsram_bytes=0x80, revision="Genesis 3")


@dataclass(frozen=True)
class Machine:
    """One console model.

    ``overseas`` and ``pal`` are bits 7 and 6 of the version register $A10001
    (Manual PDF page 84); ``hardware_version`` its bits 3-0: 0 on consoles without
    TMSS, nonzero on those with it (from Model 1 VA6; Manual PDF page 94, TmEE's
    dumps in forum t=3197). ``expansion`` is an attached expansion unit (the Mega
    CD), which clears bit 5. ``audio_path`` names the analogue mixer ("model1": a
    first-order filter; "model2": second-order and more distortion; Kabuto,
    measured on several boards; research), read from milestone 4.

    ``hv_unit`` names the measured console whose HV counter reads a machine copies at
    the one sampling phase where a read catches the counters changing
    (``vdp.HV_UNITS``): Nemesis's PAL Model 1 or his NTSC-J Model 1. The two differ
    there, by analogue timing; which other consoles behave like either is not known.
    """

    name: str
    pal: bool
    overseas: bool
    vdp: VDPVariant
    fm: FMVariant
    tmss: bool
    hardware_version: int
    audio_path: str
    expansion: bool = False
    hv_unit: str = ""
    #: Where Kabuto's hardware-measured DMA figures and Nuked-MD's disagree -- a
    #: transfer's start-up (5.6 against 8.0 68000 cycles) and the fast pace (2.4 against
    #: 2.47 cycles a word) -- whose the board uses: "kabuto", the tier rule's choice, or
    #: "die", for comparisons against Nuked-MD (Aubrey's option 3; docs/worklog.md,
    #: "Kabuto's figures or the die's").
    dma_figures: str = "kabuto"

    @property
    def hv_reads(self) -> str:
        return self.hv_unit or ("pal-model1" if self.pal else "ntscj-model1")

    @property
    def mclk(self) -> int:
        return MCLK_PAL if self.pal else MCLK_NTSC

    @property
    def lines_per_frame(self) -> int:
        """262 NTSC, 313 PAL (Kabuto, "Picture size"; docs/timing.md)."""
        return 313 if self.pal else 262

    @property
    def version_register(self) -> int:
        """$A10001: overseas, PAL, no expansion unit, hardware version (Manual p. 84)."""
        return (
            (0x80 if self.overseas else 0)
            | (0x40 if self.pal else 0)
            | (0 if self.expansion else 0x20)
            | (self.hardware_version & 0x0F)
        )


def _model(name: str, *, pal: bool, overseas: bool, **fields: object) -> Machine:
    return Machine(name=name, pal=pal, overseas=overseas, **fields)  # type: ignore[arg-type]


# The Model 1 before VA6: no TMSS, version 0, the 315-5313, the discrete YM2612.
_MODEL1_EARLY = {
    "vdp": VDP_5313, "fm": YM2612_DISCRETE, "tmss": False, "hardware_version": 0,
    "audio_path": "model1",
}  # fmt: skip
# The Model 1 from VA6: TMSS and a nonzero version; the 315-5313A.
_MODEL1_TMSS = {**_MODEL1_EARLY, "vdp": VDP_5313A, "tmss": True, "hardware_version": 1}
# The Model 2 (from its VA1 on; its earliest boards keep the discrete YM2612 -- a
# variant a caller builds with dataclasses.replace): TMSS, the ASIC FM.
_MODEL2 = {
    "vdp": VDP_5313A, "fm": YM3438_ASIC, "tmss": True, "hardware_version": 1,
    "audio_path": "model2",
}  # fmt: skip
_GENESIS3 = {**_MODEL2, "vdp": VDP_GENESIS3}

MACHINES: dict[str, Machine] = {
    # MAME's `genesis` driver: export NTSC, no TMSS (the lockstep's machine).
    "genesis": _model("Genesis (Model 1, no TMSS)", pal=False, overseas=True, **_MODEL1_EARLY),
    "genesis-tmss": _model(
        "Genesis (Model 1 VA6+, TMSS)", pal=False, overseas=True, **_MODEL1_TMSS
    ),
    "genesis2": _model("Genesis 2 (Model 2)", pal=False, overseas=True, **_MODEL2),
    "genesis3": _model("Genesis 3", pal=False, overseas=True, **_GENESIS3),
    "megadrive": _model("Mega Drive (Model 1, PAL)", pal=True, overseas=True, **_MODEL1_EARLY),
    "megadrive-tmss": _model(
        "Mega Drive (Model 1 VA6+, PAL)", pal=True, overseas=True, **_MODEL1_TMSS
    ),
    "megadrive2": _model("Mega Drive 2 (PAL)", pal=True, overseas=True, **_MODEL2),
    "megadrive-japan": _model(
        "Mega Drive (Model 1, Japan)", pal=False, overseas=False, **_MODEL1_EARLY
    ),
    "megadrive2-japan": _model("Mega Drive 2 (Japan)", pal=False, overseas=False, **_MODEL2),
}  # fmt: skip
#: The die model's console -- a PAL Model 1 with TMSS (its version register reads 1) --
#: with its DMA figures where they and Kabuto's disagree (``Machine.dma_figures``).
MACHINES["nuked-md"] = replace(
    MACHINES["megadrive-tmss"],
    name="Nuked-MD (Model 1 VA6+, PAL, the die's DMA figures)",
    dma_figures="die",
)

#: The machine each MAME 0.285 driver emulates (sega/mdconsole.cpp; version nibbles
#: from megadriv.cpp ``init_megadriv``, ``init_megadrij``, ``init_megadrie``).
MAME_DRIVERS = {
    "genesis": "genesis",
    "megadriv": "megadrive",
    "megadrij": "megadrive-japan",
    "genesis_tmss": "genesis-tmss",
}
