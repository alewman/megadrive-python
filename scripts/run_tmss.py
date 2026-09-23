#!/usr/bin/env python3
"""TMSS: the boot ROM running, handing off to a licensed cartridge, and the two locks.

A console with the Trademark Security System (Model 1 VA6 and later, every Model 2 and
Genesis 3) powers up running a 16 KB ROM of its own instead of the cartridge, and
holds the VDP shut until the word "SEGA" is written to $A14000. The board models both,
and both are checked here against the two sources that describe them:

- **the boot ROM itself**, the local No-Intro dump (2,048 bytes, SHA-1 ``3f50b76b...``),
  whose disassembly ``reference/src/TMSS-Disassembly`` rebuilds bit for bit and is what
  the board's behaviour is read from: the program copies itself into work RAM, sets bit
  0 of $A14101 to look at the cartridge, compares the long at $100 with 'SEGA' and then
  with ' SEG'/'A', draws the licence message for about 60 frames, writes 0 to $A14000
  to lock the VDP again, sets bit 0 of $A14101 for good and jumps to the cartridge's
  own start vector. A cartridge that fails the test leaves the boot ROM switched back
  in, the VDP locked and the 68000 in ``FailLoop``.
- **CMD** (``gen-hw.txt`` 4.4, "checked on the real thing"): "Games must write the text
  'SEGA' to A14000h if the lower four bits of the version register return 01h";
  "Writing 01h to A14101h disables the OS ROM and swaps in the cart ROM"; "The OS ROM
  checks for 'SEGA' or ' SEGA' at offset 100h in the cart ROM."

Four things are run:

1. a licensed cartridge boots through the licence screen and takes over;
2. MAME 0.285's ``genesis_tmss`` driver runs the same cartridge for the same emulated
   time and the two pictures are compared, as a detector;
3. a cartridge whose header says something else is refused: the boot ROM switches
   itself back in, locks the VDP and stops;
4. on a TMSS machine *without* a boot ROM fitted, a cartridge that touches the VDP
   before writing "SEGA" locks the machine up, which is the DTACK the VDP does not
   give.

    python scripts/run_tmss.py
    python scripts/run_tmss.py --png /tmp/tmss --no-mame
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from megadrive_python.board import Lockup  # noqa: E402
from megadrive_python.cartridge import Cartridge, load_image  # noqa: E402
from megadrive_python.machine import MACHINES  # noqa: E402
from megadrive_python.png import read_png, write_png  # noqa: E402
from megadrive_python.system import System  # noqa: E402

MAME = "/usr/games/mame"
ROMPATH = "/data/emu/source/myrient.erista.me/files/MAME/ROMs (non-merged)"
ROMS = Path("/data/emu/roms/megadrive")
BOOT = ROMS / "[BIOS] Sega Mega Drive - Genesis - Boot ROM (World).zip"
GAME = "Sonic The Hedgehog (USA, Europe)"
IMAGE_SUFFIXES = (".md", ".gen", ".bin", ".smd")

#: A cartridge that writes to the VDP's control port at once and never writes "SEGA":
#: ``move.w #$8004,$C00004`` then a branch to itself.
NO_SEGA = bytes.fromhex("33fc800400c00004") + bytes.fromhex("60fe")


def _bare_rom(body: bytes, header: bytes = b"SEGA MEGA DRIVE ") -> bytes:
    rom = bytearray(0x400)
    rom[0:4] = (0x00FFFE00).to_bytes(4, "big")
    rom[4:8] = (0x00000200).to_bytes(4, "big")
    rom[0x100 : 0x100 + len(header)] = header
    rom[0x200 : 0x200 + len(body)] = body
    return bytes(rom)


def boot_run(image: bytes, boot: bytes, frames: int, png: Path | None,
             label: str) -> tuple[int, int, System]:  # fmt: skip
    """Run ``image`` on a TMSS machine with ``boot`` fitted; return the frame the
    licence message first appears on and the frame the cartridge takes the bus on."""
    system = System(MACHINES["genesis-tmss"], Cartridge.from_image(image), tmss_rom=boot)
    system.board.fm.enabled = False
    licence = handoff = -1
    for frame in range(frames):
        system.run_frame()
        if licence < 0:
            rows = system.vdp.renderer.frame_rgb()
            # The licence message is about a thousand lit pixels; a handful is the
            # backdrop of a frame the boot ROM is still setting the VDP up in.
            if sum(1 for row in rows for pixel in row if pixel) > 200:
                licence = frame
                if png is not None:
                    write_png(png / f"{label}-licence.png", rows)
        if handoff < 0 and system.board.tmss_cart_selected:
            handoff = frame
    if png is not None:
        write_png(png / f"{label}-end.png", system.vdp.renderer.frame_rgb())
    return licence, handoff, system


def mame_frame(path: Path, seconds: int, out: Path) -> list[list[int]] | None:
    """MAME's ``genesis_tmss`` exit frame, as a detector."""
    if not Path(MAME).exists():
        return None
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    cart = out / "cart"
    cart.mkdir()
    subprocess.run(["unzip", "-o", "-q", str(path), "-d", str(cart)], check=True)
    images = sorted(p for p in cart.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    subprocess.run(
        [MAME, "genesis_tmss", "-cart", str(images[0]), "-rompath", ROMPATH,
         "-homepath", str(out), "-snapshot_directory", str(out / "snap"),
         "-video", "none", "-sound", "none", "-nothrottle", "-noreadconfig",
         "-skip_gameinfo", "-seconds_to_run", str(seconds)],
        cwd=out, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    found = sorted((out / "snap").glob("*/*.png"))
    return read_png(found[0]) if found else None


def compare(ours: list[list[int]], theirs: list[list[int]]) -> str:
    height = min(len(ours), len(theirs))
    width = min(len(ours[0]), len(theirs[0]))
    equal = sum(ours[y][x] == theirs[y][x] for y in range(height) for x in range(width))
    total = height * width
    return (f"{len(ours[0])}x{len(ours)} against {len(theirs[0])}x{len(theirs)}, "
            f"{equal} of {total} pixels equal ({100 * equal / total:.2f} %)")  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--boot", type=Path, default=BOOT)
    parser.add_argument("--game", default=GAME)
    parser.add_argument("--frames", type=int, default=420)
    parser.add_argument("--png", type=Path)
    parser.add_argument("--no-mame", action="store_true")
    parser.add_argument("--tmp", type=Path, default=Path("/tmp") / f"tmss-{time.time_ns()}")
    args = parser.parse_args(argv)
    if not args.boot.exists():
        print(f"{args.boot} is missing")
        return 1
    if args.png:
        args.png.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    boot = load_image(args.boot)
    print(f"boot ROM: {len(boot)} bytes, padded to 16 KB with zeros as the hardware does")
    path = ROMS / f"{args.game}.zip"
    image = load_image(path)

    licence, handoff, system = boot_run(image, boot, args.frames, args.png, "licensed")
    print(f"1. {args.game}: the licence message is on screen at frame {licence} "
          f"({licence / 59.92:.2f} s), the cartridge takes the bus at frame {handoff} "
          f"({handoff / 59.92:.2f} s), and at frame {args.frames} the picture is the "
          f"game's")  # fmt: skip

    if not args.no_mame:
        seconds = round(args.frames / 59.92)
        theirs = mame_frame(path, seconds, args.tmp / "mame")
        if theirs is None:
            print("2. MAME: no snapshot")
        else:
            ours = System(MACHINES["genesis-tmss"], Cartridge.from_image(image), tmss_rom=boot)
            ours.board.fm.enabled = False
            for _frame in range(int(seconds * 59.92)):
                ours.run_frame()
            print(f"2. MAME genesis_tmss at {seconds} s (detector): "
                  f"{compare(ours.vdp.renderer.frame_rgb(), theirs)}")  # fmt: skip

    unlicensed = _bare_rom(bytes.fromhex("60fe"), header=b"NOT A SEGA GAME ")
    licence2, handoff2, refused = boot_run(unlicensed, boot, 240, args.png, "unlicensed")
    print(f"3. an unlicensed header: licence message at frame {licence2} (-1 is never), "
          f"the cartridge takes the bus at frame {handoff2}, $A14000 reads "
          f"${refused.board.tmss_register:08X} (0 is locked)")  # fmt: skip

    system = System(MACHINES["genesis-tmss"], Cartridge(_bare_rom(NO_SEGA)))
    system.board.fm.enabled = False
    locked = ""
    try:
        for _step in range(200):
            system.step()
    except Lockup as error:
        locked = str(error)
    print(
        f"4. no boot ROM, a cartridge that touches the VDP without writing SEGA: "
        f"{locked or 'the machine did NOT lock up'}"
    )

    if args.tmp.exists():
        shutil.rmtree(args.tmp)
    print(f"({time.perf_counter() - started:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
