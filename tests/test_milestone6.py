"""Milestone 6's acceptance: the board's bus stream against Nuked-MD's.

Everything here needs the referee, which is built locally from the fetched
``reference/src/Nuked-MD`` (GPL-2.0) by ``validation/build_nuked_md.sh`` and is
git-ignored, so every test skips without it.  What is checked is the cheap, decisive
end of milestone 6: three probe loops whose whole 68000 bus stream the board
reproduces cycle for cycle, and the cartridge refresh grid, which the referee's own
reported bus-cycle lengths confirm exactly.  The measurements that take minutes -- the
ten test ROMs, the DMA against its word count, the three arbiter experiments, the bus
grant and the FM's wiring -- are ``scripts/run_nuked_md.py``'s and are recorded in
docs/worklog.md.
"""

from __future__ import annotations

import nuked_md as nmd
import pytest
from conftest import ROOT
from run_nuked_md import (
    CART_PERIOD,
    PROBES,
    REFEREE_RESET,
    REFEREE_VERSION,
    cartridge,
    compare_rom,
    loop,
    referee_refresh,
)

pytestmark = pytest.mark.skipif(
    not (ROOT / "reference" / "src" / "Nuked-MD" / "md.c").exists(),
    reason="reference/src/Nuked-MD is not fetched (scripts/fetch_references.py)",
)

#: The probes whose streams the board reproduces exactly (docs/worklog.md, milestone
#: 6).  The work-RAM ones do not, for a reason that is an open item, and the VDP
#: counter ones are not the referee's to judge.
EXACT = (
    "nop loop (ROM fetches only)",
    "ROM reads",
    "VDP data writes",
)

WINDOW = 20_000
CYCLES = 200


def _probe(name: str) -> bytes:
    setup, body = next((s, b) for n, s, b in PROBES if n == name)
    return cartridge(loop(setup, body))


@pytest.fixture(scope="module")
def referee():
    nmd.ensure_built()
    return nmd


@pytest.mark.parametrize("name", EXACT)
def test_probe_bus_stream_matches_the_referee(referee, name: str) -> None:
    """Every 68000 bus cycle: address, direction, size, the stall charged and the
    data read."""
    rom = _probe(name)
    run = referee.run(rom, stop=REFEREE_RESET + WINDOW + 5000,
                      trace=(REFEREE_RESET, REFEREE_RESET + WINDOW), trace_mask=1)  # fmt: skip
    reference = run.m68k(dma=False)[:CYCLES]
    assert len(reference) >= CYCLES // 2, "the referee traced almost nothing"
    n, diffs, _, _, _ = compare_rom(reference, rom, version=REFEREE_VERSION)
    assert n >= CYCLES // 2
    assert diffs == [], f"{name}: {diffs[:4]}"


def test_the_cartridge_refresh_grid_is_the_referees(referee) -> None:
    """The board's rule -- a fixed 896-MCLK grid, the next bus cycle absorbs the tick,
    and it costs 14 MCLK only if that cycle is a cartridge access -- reproduces every
    stretch the referee reports, at one phase."""
    rom = _probe("nop loop (ROM fetches only)")
    run = referee.run(rom, stop=REFEREE_RESET + 100_000 + 5000,
                      trace=(REFEREE_RESET, REFEREE_RESET + 100_000), trace_mask=1)  # fmt: skip
    reference = run.m68k(dma=False)
    stretched = [e for e in reference if e.length == nmd.AS_LOW + 14]
    assert len(stretched) > 50, "the window is too short to say anything"
    _, cart_miss, _, _ = referee_refresh(reference)
    assert cart_miss == 0
    assert CART_PERIOD == 896
