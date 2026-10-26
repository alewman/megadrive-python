#!/usr/bin/env python3
"""Battery SRAM and the serial EEPROMs, judged by the games that use them.

Milestone 7's rule is that a cartridge feature is judged by the game doing the right
thing -- here, "saving and reloading" -- with MAME as a detector where nothing better
exists. Both halves are run for every game in :data:`GAMES`:

1. **Cold start.** The board runs the game for a few emulated seconds from a blank
   battery ($FF, as an unwritten chip reads). Some games write their save structure
   during their own start-up with no input at all, which is what makes this
   mechanical: Sonic The Hedgehog 3 lays out its FRAM, NBA Jam writes its "Snake
   1993" header into its EEPROM. The battery is then compared **byte for byte** with
   the one MAME 0.285 writes after the same emulated time (``-nvram_directory``).

   MAME keeps a battery as 16-bit words, one per address the 8-bit chip answers at
   ($200001, $200003, ...), with the byte in both halves of each word, so its file is
   twice the board's and the comparison unpacks it; that its two halves are equal is
   checked too, since it is what says the chip is eight bits wide.

2. **Warm start.** The battery is written to a file, a fresh machine is built with it
   loaded, and the game is run again for the same time. What is checked is what the
   game does about it: a game that reads its save back finds it valid and leaves it
   alone, so the battery is unchanged and, for an EEPROM, **no byte is written at
   all** -- which is the read path being exercised, and the only thing that
   distinguishes a right and a wrong /SDA bit (see NBA Jam below).

MAME is a detector, never the judge (docs/mame-oracle.md). Where it disagrees with the
game's own behaviour the game wins: MAME loads these images from a file and guesses
the board (``md_slot.cpp`` ``get_cart_type``), which is not always the board its own
software list names -- it gives NBA Jam ``rom_nbajam`` where the list says
``rom_nbajam_alt``.

    python scripts/run_battery.py
    python scripts/run_battery.py --no-mame
    python scripts/run_battery.py --game sonic3
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from megadrive_python.cartridge import Cartridge, load_image  # noqa: E402
from megadrive_python.machine import MACHINES  # noqa: E402
from megadrive_python.system import System  # noqa: E402

MAME = "/usr/games/mame"
#: MAME's BIOS/system romsets (MAME_ROMPATH; MAME's own default if unset).
ROMPATH = os.environ.get("MAME_ROMPATH", "roms")
ROMS = Path("/data/emu/roms/megadrive")
IMAGE_SUFFIXES = (".md", ".gen", ".bin", ".smd")

#: Games that write their battery during start-up, with no input: the only ones this
#: comparison can be made mechanical for. ``seconds`` is emulated seconds, chosen as
#: the shortest that gets the game past its own initialisation.
GAMES = {
    "sonic3": ("Sonic The Hedgehog 3 (USA)", 40),
    "nbajam": ("NBA Jam (USA, Europe) (Rev 1)", 40),
    "pstar4": ("Phantasy Star IV (USA)", 40),
    "wboymw": ("Wonder Boy in Monster World (USA, Europe)", 40),
    "evander": ("Evander Holyfield's 'Real Deal' Boxing (World)", 40),
}


def run_board(image: bytes, seconds: int, battery: bytes | None = None) -> Cartridge:
    """Run the game from a blank battery, or from ``battery``, and hand back the
    cartridge so that the caller can look at what it holds."""
    cartridge = Cartridge.from_image(image)
    if battery is not None:
        cartridge.load_bytes(battery)
    system = System(MACHINES["genesis"], cartridge)
    system.board.fm.enabled = False  # the sound chip is not what is being measured
    for _frame in range(int(seconds * 60)):
        system.run_frame()
    return cartridge


def run_mame(path: Path, seconds: int, out: Path) -> bytes | None:
    """MAME's own battery after the same emulated time, or None if it wrote none."""
    if shutil.which(MAME) is None and not Path(MAME).exists():
        return None
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    image = out / "cart"
    image.mkdir()
    subprocess.run(["unzip", "-o", "-q", str(path), "-d", str(image)], check=True)
    images = sorted(p for p in image.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    if not images:
        return None
    subprocess.run(
        [MAME, "genesis", "-cart", str(images[0]), "-rompath", ROMPATH,
         "-homepath", str(out), "-video", "none", "-sound", "none", "-nothrottle",
         "-noreadconfig", "-skip_gameinfo", "-nvram_directory", str(out / "nvram"),
         "-seconds_to_run", str(seconds)],
        cwd=out, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    files = [p for p in (out / "nvram").rglob("*") if p.is_file()]
    return files[0].read_bytes() if len(files) == 1 else None


def unpack(mame: bytes, size: int) -> tuple[bytes, bool | None]:
    """MAME's battery as the chip's own bytes, and whether its words' halves agree."""
    if len(mame) == 2 * size:
        return bytes(mame[1::2]), mame[0::2] == mame[1::2]
    return mame, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--game", action="append", choices=sorted(GAMES))
    parser.add_argument("--no-mame", action="store_true", help="skip the detector")
    parser.add_argument("--tmp", type=Path, default=Path("/tmp") / f"battery-{time.time_ns()}")
    args = parser.parse_args(argv)

    failures = 0
    for key in args.game or sorted(GAMES):
        name, seconds = GAMES[key]
        path = ROMS / f"{name}.zip"
        if not path.exists():
            print(f"{key}: {path} is missing")
            continue
        started = time.perf_counter()
        image = load_image(path)
        cold = run_board(image, seconds)
        saved = cold.save_bytes()
        warm = run_board(image, seconds, battery=saved)
        writes = warm.eeprom.writes if warm.eeprom is not None else None
        print(f"{key}: {name}")
        print(f"  board       : slot {cold.slot}, {cold.battery} of {len(saved)} bytes, "
              f"{sum(byte != 0xFF for byte in saved)} written")  # fmt: skip
        again = "unchanged" if warm.save_bytes() == saved else "CHANGED"
        eeprom = "" if writes is None else f", {writes} EEPROM bytes written on the warm start"
        print(f"  reloaded    : battery {again}{eeprom}")
        if warm.save_bytes() != saved:
            failures += 1
        if not args.no_mame:
            mame = run_mame(path, seconds, args.tmp / key)
            if mame is None:
                print("  MAME        : no battery file")
            else:
                packed, halves = unpack(mame, len(saved))
                equal = sum(a == b for a, b in zip(saved, packed, strict=False))
                word = (
                    ""
                    if halves is None
                    else (f", the two halves of each word {'equal' if halves else 'DIFFER'}")
                )
                print(
                    f"  MAME        : {len(mame)} bytes{word}; "
                    f"{equal} of {len(packed)} bytes equal to the board's"
                )
                if equal != len(packed) or len(packed) != len(saved):
                    failures += 1
        print(f"  ({time.perf_counter() - started:.0f} s)")
    if args.tmp.exists():
        shutil.rmtree(args.tmp)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
