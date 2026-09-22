"""Milestone 1's acceptance: the two-CPU lockstep against MAME's logs
(``scripts/mame_trace.py`` records them, locally: they need MAME and the commercial
romset, and are hundreds of MB, so CI does not run this). ``pytest -m slow``."""

from __future__ import annotations

import subprocess
import sys

import pytest
from conftest import ROOT

TRACES = ROOT / "tests" / "mame_traces"
RECORDED = sorted(p for p in TRACES.glob("genesis-*") if (p / "m68k.log").exists())


@pytest.mark.slow
@pytest.mark.skipif(not RECORDED, reason="no MAME logs recorded (scripts/mame_trace.py)")
@pytest.mark.parametrize("trace", RECORDED, ids=[p.name for p in RECORDED])
def test_lockstep(trace):
    """Every register of both CPUs equal before every logged instruction; every
    difference named (docs/worklog.md, milestone 1)."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "lockstep_mame.py"), str(trace)],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert "unexplained differences: 0" in result.stdout, result.stdout[-2000:]
    assert result.returncode == 0
