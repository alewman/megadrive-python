#!/usr/bin/env python3
"""Run Nemesis's VDPFIFOTesting on the board and print every test's result.

VDPFIFOTesting (2013; forum t=1291; ``tests/test_roms/nemesis/VDPFIFOTesting.zip``,
ROM and source) runs 122 self-checking tests of the VDP's ports and shows them a
page at a time: each test's number and name, then its result bytes, each byte a tile
coloured by whether it matched the expected byte the ROM carries (``WriteResult`` and
``WriteDataToVRAM`` in ``VDPFIFOTesting.asm``: the name table entry's high byte is
$21 for a match, $41 for a mismatch, $01 when the test has no expected data). Each
page waits for Start. This script presses Start through the joypad model and reads
each page back from the plane A name table at $A000 (64 columns), as sms-python's
``run_vdptest.py`` reads FluBBa's test.

Nemesis on the hardware (t=1291, second page): the results "all pass on the
hardware, with a few exceptions": a Genesis 3 "fails quite a few tests, as it
actually has an 0x80 byte VSRAM"; "the three tests which perform data port writes
during a DMA fill operation fail occasionally"; "the DMA Fill FIFO usage test gets
the first write byte order reversed intermittently". So: every test but those four
must pass; the four are listed with the board's result; ``--machine genesis3`` must
fail the VSRAM tests.

    python scripts/run_vdpfifotesting.py                     # a Model 1 Genesis
    python scripts/run_vdpfifotesting.py --machine genesis3
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from megadrive_python.cartridge import Cartridge  # noqa: E402
from megadrive_python.io import START  # noqa: E402
from megadrive_python.machine import MACHINES  # noqa: E402
from megadrive_python.system import System  # noqa: E402

ARCHIVE = ROOT / "tests" / "test_roms" / "nemesis" / "VDPFIFOTesting.zip"
MEMBER = "VDPFIFOTesting/VDPFIFOTesting.bin"
NAME_TABLE = 0xA000
COLUMNS = 64
#: The tests Nemesis reports as intermittent on hardware (t=1291, second page).
INTERMITTENT = (
    "DMA Fill FIFO Usage",
    "DP Writes During DMA Fill VRAM",
    "DP Writes During DMA Fill CRAM",
    "DP Writes During DMA Fill VSRAM",
)


def cell(system: System, row: int, column: int) -> int:
    """The name table word at a row and column of plane A."""
    vram = system.vdp.vram
    address = NAME_TABLE + row * COLUMNS * 2 + column * 2
    return (vram[address] << 8) | vram[address + 1]


def page(system: System) -> tuple[list[dict], str]:
    """The tests on the page on screen, and its results line. A test's line is its
    number ("  1.") in columns 2-5 and its name from column 6; its data rows follow, a byte a
    tile from column 2 (tile index $100 + byte, palette 1 matched, 2 mismatched)."""
    tests: list[dict] = []
    footer = ""
    for row in range(28):
        words = [cell(system, row, column) for column in range(COLUMNS)]
        if any(w & 0x0100 for w in words):
            if tests:
                data = [w for w in words if w & 0x0100]
                tests[-1]["bytes"] += [w & 0xFF for w in data]
                tests[-1]["palettes"] += [(w >> 13) & 3 for w in data]
            continue
        text = "".join(chr(w & 0xFF) if 32 <= (w & 0xFF) < 127 and w < 0x100 else " "
                       for w in words).rstrip()  # fmt: skip
        match = re.match(r"\s*(\d+)\.\s*(\S.*)$", text)
        if match and row >= 3:
            tests.append({"number": int(match.group(1)), "name": match.group(2).strip(),
                          "bytes": [], "palettes": []})  # fmt: skip
        elif "Results:" in text:
            footer = text.strip()
    return tests, footer


def verdict(test: dict) -> str:
    palettes = test["palettes"]
    if 2 in palettes:
        return "FAIL"
    if 1 in palettes:
        return "pass"
    return "no expected data"


def run(machine: str, pages: int, frames_per_page: int,
        verbose: bool = False) -> tuple[list[dict], list[str], float]:  # fmt: skip
    with zipfile.ZipFile(ARCHIVE) as archive:
        rom = archive.read(MEMBER)
    system = System(MACHINES[machine], Cartridge(rom))
    pad = system.board.io.ports[0].device
    results: dict[int, dict] = {}
    footers: list[str] = []
    started = time.perf_counter()
    for _ in range(pages):
        # Run until the page stops changing (the ROM waits for a button), then read it.
        last, steady = None, 0
        for _ in range(frames_per_page):
            system.run_frame()
            shown = page(system)
            steady = steady + 1 if shown == last else 0
            last = shown
            if steady >= 20 and shown[1]:
                break
        tests, footer = last
        if results and tests and min(t["number"] for t in tests) <= max(results):
            break  # the ROM has started its tests again: every page has been read
        footers.append(footer)
        for test in tests:
            results[test["number"]] = test
        if verbose:
            print(f"page {len(footers)}: {footer}; tests {[t['number'] for t in tests]}; "
                  f"68000 at ${system.m68k.PC:06X}", flush=True)  # fmt: skip
        pad.pressed = START
        for _ in range(8):
            system.run_frame()
        pad.pressed = 0
        for _ in range(8):
            system.run_frame()
    return [results[k] for k in sorted(results)], footers, time.perf_counter() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--machine", default="genesis", choices=sorted(MACHINES))
    parser.add_argument("--pages", type=int, default=40)
    parser.add_argument("--frames", type=int, default=3000, help="per page, at most")
    parser.add_argument("--verbose", action="store_true", help="a line per page")
    args = parser.parse_args()
    tests, footers, seconds = run(args.machine, args.pages, args.frames, args.verbose)
    genesis3 = MACHINES[args.machine].vdp.vsram_bytes == 0x80
    failed_required = failed_vsram = 0
    for test in tests:
        name = test["name"]
        result = verdict(test)
        note = " (intermittent on hardware)" if name in INTERMITTENT else ""
        if genesis3 and "VSRAM" in name and not note:
            note = " (a VSRAM test: a Genesis 3 fails some)"
            failed_vsram += result == "FAIL"
        elif result == "FAIL" and not note:
            failed_required += 1
        print(f"{test['number']:>4} {name:40} {result}{note}")
    print(footers[-1] if footers else "no results line")
    if genesis3:
        print(f"{failed_vsram} VSRAM tests fail, as on a Genesis 3")
    print(f"{len(tests)} tests read; {failed_required} fail that pass on hardware; "
          f"{args.machine}; {seconds:.0f} s, {sys.implementation.name} "
          f"{sys.version.split()[0]}")  # fmt: skip
    return 0 if failed_required == 0 and (failed_vsram > 0 or not genesis3) else 1


if __name__ == "__main__":
    sys.exit(main())
