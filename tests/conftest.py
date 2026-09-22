"""Shared paths for the tests. Test programs and captures are fetched, never committed
(``scripts/fetch_references.py``); a test that needs one skips without it."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

NEMESIS = ROOT / "tests" / "test_roms" / "nemesis"
