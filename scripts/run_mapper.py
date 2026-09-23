#!/usr/bin/env python3
"""The Sega (SSF2) mapper, judged by Super Street Fighter II itself.

The research document is Bart Trzynadlowski's *SSFII Genesis Technical Information*,
second edition (2000), ``ssf2tnc.txt`` inside ``reference/docs/md_tech.zip``, with
Plutiedev's "Going beyond 4MB" beside it. What it says, and what this checks:

- the cartridge is 5 MB where the 68000 sees 4, and "views the addressable 4 megabytes
  of ROM as 8 512KB regions. The first area, 0x000000-0x07FFFF is fixed and cannot be
  remapped because that is where the vector table resides";
- seven registers page the other seven regions, $A130F3 for $080000-$0FFFFF up to
  $A130FF for $380000-$3FFFFF, each taking a 6-bit page number ("bits 7 and 6 are
  always 0");
- and the game proves it to itself: "SSFII contains a checksum routine which
  calculates the checksum of the first 4MB of the ROM; it then pages in the last 1MB
  of ROM to 0x300000-0x3FFFFF. It does this by writing to the bank registers", with
  the code quoted from the US dump:

        0x003BEC:       move.b  #$08, ($A130FD)
        0x003BF4:       move.b  #$09, ($A130FF)
        0x003BFC:       lea     ($300000), a0

  "The bankswitching hardware must be properly emulated before the game starts
  working. If it is not, the checksum will fail and the game will hang with a red
  screen."

So the judge is the game: it either pages in its top megabyte, checksums it, and goes
on to its title screen, or it hangs. This runs it and reports **every write the game
makes to $A130xx, in order**, so that the two writes above can be seen happening at
the value the document quotes, then runs on to the title screen.

Overdrive 2 is on the same mapper (MAME's ``rom_titan``, whose own comment says "This
is same as SSF2 mapper"); milestone 5 already runs it to its end, and ``--titan``
repeats the bank-register part of that here.

    python scripts/run_mapper.py
    python scripts/run_mapper.py --png /tmp/ssf2 --frames 2400
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from megadrive_python.cartridge import Cartridge, load_image  # noqa: E402
from megadrive_python.machine import MACHINES  # noqa: E402
from megadrive_python.png import write_png  # noqa: E402
from megadrive_python.system import System  # noqa: E402

ROMS = Path("/data/emu/roms/megadrive")
SSF2 = "Super Street Fighter II (USA)"
TITAN = "Overdrive 2 (World) (Revision 2017) (Aftermarket) (Unl)"

#: ``ssf2tnc.txt``'s table: the register that pages each 512 KB window.
REGISTERS = {
    0xF3: 0x080000, 0xF5: 0x100000, 0xF7: 0x180000,
    0xF9: 0x200000, 0xFB: 0x280000, 0xFD: 0x300000, 0xFF: 0x380000,
}  # fmt: skip


def run(name: str, machine: str, frames: int, png: Path | None) -> int:
    path = ROMS / f"{name}.zip"
    if not path.exists():
        print(f"{name}: missing")
        return 1
    image = load_image(path)
    cartridge = Cartridge.from_image(image)
    print(f"{name}: {len(image):,} bytes, MAME's software list calls the board "
          f"{cartridge.slot!r} ({cartridge.name})")  # fmt: skip
    if not cartridge.mapper:
        print("  the board is not the mapper: nothing to measure")
        return 1

    writes: list[tuple[int, int]] = []
    original = cartridge.write_time

    def watch(address: int, value: int) -> None:
        writes.append((address & 0xFF, value))
        original(address, value)

    cartridge.write_time = watch  # type: ignore[method-assign]
    system = System(MACHINES[machine], cartridge)
    system.board.fm.enabled = False
    started = time.perf_counter()

    def shot(frame: int) -> None:
        rows = system.vdp.renderer.frame_rgb()
        if png is not None and rows and rows[0]:
            write_png(png / f"{name.split(' (')[0].replace(' ', '_')}-{frame}.png", rows)

    for frame in range(frames):
        system.run_frame()
        if png is not None and frame % (frames // 4 or 1) == 0:
            shot(frame)
    shot(frames)

    print(f"  {len(writes)} writes to $A130xx in {frames} frames; the first eight:")
    for register, value in writes[:8]:
        window = REGISTERS.get(register)
        where = f"-> ${window:06X}" if window else ("battery/mapping" if register == 0xF1 else "")
        print(f"     $A130{register:02X} = ${value:02X}  {where}")
    checksum = [(r, v) for r, v in writes if (r, v) in ((0xFD, 0x08), (0xFF, 0x09))]
    print(f"  ssf2tnc.txt's checksum pair ($A130FD = $08, $A130FF = $09): "
          f"{'both seen' if len(checksum) >= 2 else 'NOT seen'}")  # fmt: skip
    print(f"  banks at the end: {cartridge.banks}")
    print(f"  ({time.perf_counter() - started:.0f} s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--frames", type=int, default=2400)
    parser.add_argument("--png", type=Path)
    parser.add_argument("--titan", action="store_true", help="also run Overdrive 2")
    args = parser.parse_args(argv)
    if args.png:
        args.png.mkdir(parents=True, exist_ok=True)
    failures = run(SSF2, "genesis", args.frames, args.png)
    if args.titan:
        failures += run(TITAN, "megadrive", args.frames, args.png)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
