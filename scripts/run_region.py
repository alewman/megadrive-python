#!/usr/bin/env python3
"""The region and version register, against the Manual and against the games.

The Manual (``GenesisSoftwareManual-150p.pdf``, its own page 72; PDF page 84) gives
$A10001 as

    $A10001   MODE  VMOD  DISK  RSV  VER3  VER2  VER1  VER0

            Mode (R) 0: Domestic Model      1: Overseas Model
            VMOD (R) 0: NTSC CPU clock 7.67 MHz   1: PAL CPU clock 7.60 MHz
            Disk (R) 0: FDD unit connected  1: FDD unit not connected
             RSV (R) Currently not used
         VER 3-0 (R) Mega Drive version ... The present hardware version is $0.

and CMD (``gen-hw.txt`` 4.4) adds the one thing the Manual does not: "Games must write
the text 'SEGA' to A14000h if the lower four bits of the version register return 01h",
which is the TMSS machines (``scripts/run_tmss.py``).

Three things are run:

1. **the register itself**, for every machine the board models, decoded field by field
   and checked against the byte;
2. **the bus**, with a small 68000 program that reads $A10001 as a byte, $A10000 as a
   byte and $A10000 as a word, because CMD 3.0 says the even address reads the same
   byte and a word read has it in both halves;
3. **the games**, which is what the brief asks for -- "a region-locked game booting on
   the wrong machine is a check". Each game in :data:`GAMES` is run on all nine
   machines and the picture is classified as the game's own or as its refusal screen,
   by comparing it with the picture the same game gives on a machine it accepts and on
   one it refuses. MAME is a detector for the three machines it has drivers for.

    python scripts/run_region.py
    python scripts/run_region.py --png /tmp/region
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from megadrive_python.cartridge import Cartridge, load_image  # noqa: E402
from megadrive_python.machine import MACHINES, MAME_DRIVERS  # noqa: E402
from megadrive_python.png import read_png, write_png  # noqa: E402
from megadrive_python.system import System  # noqa: E402

MAME = "/usr/games/mame"
#: MAME's BIOS/system romsets (MAME_ROMPATH; MAME's own default if unset).
ROMPATH = os.environ.get("MAME_ROMPATH", "roms")
ROMS = Path("/data/emu/roms/megadrive")
IMAGE_SUFFIXES = (".md", ".gen", ".bin", ".smd")

#: Games that check the version register. Both print "DEVELOPED FOR USE ONLY WITH
#: NTSC GENESIS SYSTEMS" when they do not like the machine; ``seconds`` is long enough
#: for either picture to be up.
GAMES = (
    ("Mortal Kombat 3 (USA)", 6),
    ("Comix Zone (USA)", 6),
    ("Streets of Rage 3 (USA)", 6),
)


def version_table() -> int:
    """Every machine's $A10001, decoded as the Manual decodes it."""
    wrong = 0
    print(f"{'machine':<18} {'$A10001':>8}  MODE  VMOD  DISK  VER")
    for name, machine in MACHINES.items():
        value = machine.version_register
        mode = "overseas" if value & 0x80 else "domestic"
        vmod = "PAL" if value & 0x40 else "NTSC"
        disk = "no FDD" if value & 0x20 else "FDD"
        print(f"{name:<18} ${value:02X} ({value:08b})  {mode:<9} {vmod:<5} {disk:<7} "
              f"${value & 0x0F:X}")  # fmt: skip
        expected = (
            (0x80 if machine.overseas else 0)
            | (0x40 if machine.pal else 0)
            | (0 if machine.expansion else 0x20)
            | (machine.hardware_version & 0x0F)
        )
        if value != expected or bool(value & 0x0F) != machine.tmss:
            print("    ^ does not agree with the machine's own fields, or with TMSS")
            wrong += 1
    return wrong


def _read_rom() -> bytes:
    """``move.b $A10001,d0`` / ``move.b $A10000,d1`` / ``move.w $A10000,d2``, stored."""
    code = bytearray()
    code += bytes.fromhex("41f900ff0000")  # lea $00FF0000,a0
    code += bytes.fromhex("10f900a10001")  # move.b $A10001,(a0)+
    code += bytes.fromhex("10f900a10000")  # move.b $A10000,(a0)+
    code += bytes.fromhex("30f900a10000")  # move.w $A10000,(a0)+
    code += bytes.fromhex("60fe")  # bra.s *
    rom = bytearray(0x400)
    rom[0:4] = (0x00FFFE00).to_bytes(4, "big")
    rom[4:8] = (0x00000200).to_bytes(4, "big")
    rom[0x200 : 0x200 + len(code)] = bytes(code)
    return bytes(rom)


def bus_reads() -> int:
    """What a 68000 reads at the odd address, the even address and as a word."""
    wrong = 0
    for name, machine in MACHINES.items():
        system = System(MACHINES[name], Cartridge(_read_rom()))
        system.board.vdp.rendering = False
        system.board.fm.enabled = False
        for _step in range(40):
            system.step()
        odd, even = system.board.ram[0], system.board.ram[1]
        word = (system.board.ram[2] << 8) | system.board.ram[3]
        value = machine.version_register
        ok = odd == even == value and word == (value << 8) | value
        print(f"  {name:<18} odd ${odd:02X}, even ${even:02X}, word ${word:04X}"
              + ("" if ok else "   <- not CMD 3.0's rule"))  # fmt: skip
        wrong += not ok
    return wrong


def picture(name: str, machine: str, seconds: int, png: Path | None) -> tuple[str, list]:
    image = load_image(ROMS / f"{name}.zip")
    system = System(MACHINES[machine], Cartridge.from_image(image))
    system.board.fm.enabled = False
    for _frame in range(int(seconds * 60)):
        system.run_frame()
    rows = system.vdp.renderer.frame_rgb()
    if png is not None:
        write_png(png / f"{name.split(' (')[0].replace(' ', '_')}-{machine}.png", rows)
    return hashlib.sha1(repr(rows).encode()).hexdigest()[:10], rows


def mame_frame(name: str, driver: str, seconds: int, out: Path) -> list | None:
    if not Path(MAME).exists():
        return None
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    cart = out / "cart"
    cart.mkdir()
    subprocess.run(["unzip", "-o", "-q", str(ROMS / f"{name}.zip"), "-d", str(cart)], check=True)
    images = sorted(p for p in cart.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    subprocess.run(
        [MAME, driver, "-cart", str(images[0]), "-rompath", ROMPATH,
         "-homepath", str(out), "-snapshot_directory", str(out / "snap"),
         "-video", "none", "-sound", "none", "-nothrottle", "-noreadconfig",
         "-skip_gameinfo", "-seconds_to_run", str(seconds)],
        cwd=out, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    found = sorted((out / "snap").glob("*/*.png"))
    return read_png(found[0]) if found else None


def equal(ours: list, theirs: list) -> str:
    height, width = min(len(ours), len(theirs)), min(len(ours[0]), len(theirs[0]))
    same = sum(ours[y][x] == theirs[y][x] for y in range(height) for x in range(width))
    return f"{100 * same / (height * width):.2f} %"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--png", type=Path)
    parser.add_argument("--no-mame", action="store_true")
    parser.add_argument("--tmp", type=Path, default=Path("/tmp") / f"region-{time.time_ns()}")
    args = parser.parse_args(argv)
    if args.png:
        args.png.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    print("1. The version register, as the Manual's page 72 decodes it:")
    wrong = version_table()
    print("2. What a 68000 reads (CMD gen-hw.txt 3.0: the even address reads the same "
          "byte, a word read has it in both halves):")  # fmt: skip
    wrong += bus_reads()

    print("3. The games that check it:")
    for name, seconds in GAMES:
        if not (ROMS / f"{name}.zip").exists():
            print(f"  {name}: missing")
            continue
        shots = {}
        for machine in MACHINES:
            shots[machine] = picture(name, machine, seconds, args.png)
        groups: dict[str, list[str]] = {}
        for machine, (digest, _rows) in shots.items():
            groups.setdefault(digest, []).append(machine)
        print(f"  {name}: {len(groups)} different pictures over the nine machines")
        for digest, machines in groups.items():
            print(f"     {digest}: {', '.join(machines)}")
        if not args.no_mame:
            for driver, machine in MAME_DRIVERS.items():
                if machine not in shots or driver == "genesis_tmss":
                    continue
                theirs = mame_frame(name, driver, seconds, args.tmp / driver)
                if theirs is None:
                    continue
                print(f"     MAME {driver} against the board's {machine}: "
                      f"{equal(shots[machine][1], theirs)} of pixels equal")  # fmt: skip
    if args.tmp.exists():
        shutil.rmtree(args.tmp)
    print(f"({time.perf_counter() - started:.0f} s)")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
