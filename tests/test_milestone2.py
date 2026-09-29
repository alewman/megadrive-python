"""Milestone 2's acceptance: Nemesis's HV/status captures and VDPFIFOTesting, both
hardware judges (docs/validation.md). They need the fetched archives
(``python scripts/fetch_references.py``) and skip without them."""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest
from conftest import NEMESIS, ROOT

CAPTURES = NEMESIS / "sr logic analyser results.zip"
FIFO_TESTING = NEMESIS / "VDPFIFOTesting.zip"
BASELINE = Path(__file__).with_name("hv_captures_baseline.json")


@pytest.mark.skipif(not CAPTURES.exists(), reason="Nemesis's captures are not fetched")
def test_hv_captures():
    """Every capture's words equal at least as many as the recorded run, and no more
    differences away from the phases where a counter steps (docs/timing.md)."""
    import run_hv_captures

    baseline = json.loads(BASELINE.read_text())
    _, dumps = run_hv_captures.load_archives()
    assert sorted(dumps) == sorted(baseline)
    with multiprocessing.Pool(min(16, os.cpu_count() or 1)) as pool:
        results = [r for rs in pool.map(run_hv_captures.work, [(c, []) for c in dumps])
                   for r in rs]  # fmt: skip
    for capture, expected in baseline.items():
        mine = [r for r in results if r["capture"] == capture]
        equal = sum(r["samples"] - r["differ"] for r in mine)
        away = sum(
            (r["samples"] if r["params"] is None else 0)
            + sum(n for k, n in r["kinds"].items() if "phase" not in k and k != "not fitted")
            for r in mine
        )
        assert len(mine) == expected["dumps"]
        assert equal >= expected["equal"], capture
        assert away <= expected["not_at_a_step"], capture


@pytest.mark.skipif(not FIFO_TESTING.exists(), reason="VDPFIFOTesting is not fetched")
@pytest.mark.parametrize("machine", ["genesis", "megadrive", "genesis3"])
def test_vdpfifotesting(machine):
    """Every test that passes reliably on hardware passes (the intermittent ones --
    Nemesis's four and "FIFO Wait States", which the die passes once per ~800
    iterations -- are exempt); a Genesis 3 (0x80-byte VSRAM) fails VSRAM tests and only
    those."""
    import run_vdpfifotesting

    tests, footers, _ = run_vdpfifotesting.run(machine, 40, 3000)
    assert len(tests) == 122, footers[-1]
    failed = {t["name"] for t in tests if run_vdpfifotesting.verdict(t) == "FAIL"}
    failed -= set(run_vdpfifotesting.INTERMITTENT)
    if machine == "genesis3":
        assert failed and all("VSRAM" in name for name in failed)
    else:
        assert not failed


def test_baseline_is_the_script_output_shape():
    assert ROOT.joinpath("scripts", "run_hv_captures.py").exists()
    baseline = json.loads(BASELINE.read_text())
    assert sum(v["dumps"] for v in baseline.values()) == 288
