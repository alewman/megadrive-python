#!/usr/bin/env python3
"""Run MoD's ``gen_test_z80_bus`` on the board and print what it puts on the screen.

The ROM (Mask of Destiny, 2015; forum t=3021;
``tests/test_roms/mask-of-destiny/gen_test_z80_bus.{md,s68}``) checks what the 68000
sees of the Z80's side in each combination of RESET and BUSREQ, how byte and word
accesses to the Z80's space behave, which bits of $A11100 and $A11200 are driven, how
long BUSACK takes, and whether the Z80 can write to and read from the 68000's work RAM
through its bank. Every check carries the value the hardware gives, so the ROM prints
PASS or FAIL itself; MoD reports of the last two, "tested across multiple machines ...
writes always worked and reads always failed".

The screen is read out of the plane A name table, where the ROM's font puts each
character at the tile of its ASCII code (its ``print_string``/``print_hexw``).

    python scripts/run_z80_bus_test.py
    python scripts/run_z80_bus_test.py --machine megadrive2
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from megadrive_python.cartridge import Cartridge  # noqa: E402
from megadrive_python.machine import MACHINES  # noqa: E402
from megadrive_python.system import System  # noqa: E402

ROM = ROOT / "tests" / "test_roms" / "mask-of-destiny" / "gen_test_z80_bus.md"
#: The ROM sets plane A's name table to $8000 and 64 columns ("move.w #$8220").
NAME_TABLE = 0x8000
COLUMNS = 64
LINE = re.compile(r"^(?P<name>.+?):\s+(?P<value>[0-9A-F]{4})(?:\s+(?P<verdict>PASS|FAIL))?$")


def run_rom(machine: str, frames: int = 30) -> list[str]:
    """The ROM's screen, a line of text per row of plane A."""
    system = System(MACHINES[machine], Cartridge(ROM.read_bytes()))
    for _ in range(frames):
        system.run_frame()
    vram = system.vdp.vram
    lines = []
    for row in range(28):
        text = ""
        for column in range(COLUMNS):
            address = NAME_TABLE + (row * COLUMNS + column) * 2
            tile = ((vram[address] << 8) | vram[address + 1]) & 0x7FF
            text += chr(tile) if 32 <= tile < 127 else " "
        lines.append(text.rstrip())
    return lines


def results(lines: list[str]) -> list[tuple[str, tuple[str, str]]]:
    """Each result line as (name, (verdict, value)); the verdict is "" where the ROM
    only prints a number (the two BUSACK delays and the work RAM values)."""
    found = []
    for line in lines:
        match = LINE.match(line.strip())
        if match:
            found.append((match["name"].strip(), (match["verdict"] or "", match["value"])))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--machine", default="genesis", choices=sorted(MACHINES))
    parser.add_argument("--frames", type=int, default=30)
    args = parser.parse_args()
    started = time.perf_counter()
    lines = run_rom(args.machine, args.frames)
    checks = results(lines)
    for name, (verdict, value) in checks:
        print(f"{name + ':':30} {value}  {verdict}")
    failed = [name for name, (verdict, _) in checks if verdict == "FAIL"]
    values = dict(checks)
    writes = values.get("Z80 writes to work RAM", ("", ""))[1]
    reads = values.get("Z80 reads to work RAM", ("", ""))[1]
    report = "ED: " + ", ".join(failed) if failed else ""
    print(f"{len(checks)} results, {len(failed)} FAIL{report}; "
          f"the Z80's writes to 68000 work RAM read back as {writes} and its reads give "
          f"{reads} (MoD: writes always worked, reads always failed); "
          f"{time.perf_counter() - started:.0f} s, {sys.implementation.name} "
          f"{sys.version.split()[0]}")  # fmt: skip
    return 1 if failed or reads != "FFFF" else 0


if __name__ == "__main__":
    sys.exit(main())
