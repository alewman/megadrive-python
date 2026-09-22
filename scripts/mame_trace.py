#!/usr/bin/env python3
"""Record MAME 0.285's instruction traces of both Mega Drive CPUs and their bus traffic.

MAME evaluates a trace or watchpoint action against the debugger's *visible* CPU,
not the CPU being traced (docs/mame-oracle.md, trap 1): a Z80 trace action that
logs ``pc`` while the 68000 is visible logs the 68000's PC. So each CPU gets its own
run, with itself visible; MAME is deterministic for a cartridge without input, so
the two runs are the same run (checked: both record the same 6,091 VDP writes in
3 s of Sonic). Each run's ``error.log`` is kept as ``m68k.log`` and ``z80.log`` in
a fresh directory (default ``tests/mame_traces/<driver>-<cart stem>``, gitignored),
with the first run's exit snapshot under ``snap/``.

    python scripts/mame_trace.py genesis "/data/emu/roms/megadrive/Sonic ... (USA, Europe).zip"
    python scripts/mame_trace.py megadrij game.md --seconds 10 --only m68k

``m68k.log`` holds, in execution order:

- ``M pc sr d0-d7 a0-a6 usp ssp totalcycles beamy beamx``: one line per 68000
  instruction, before it runs (hex, then decimal);
- ``V|A|I|B address size data totalcycles`` (``R``/``W`` suffix: ``VR``, ``VW``...)
  for each 68000 access to the VDP ($C00000-$C0001F), the Z80 area and YM2612
  ($A00000-$A0FFFF), the I/O chip ($A10000-$A1001F) and the Z80 bus request and
  reset registers ($A11100, $A11200).

``z80.log`` holds:

- ``Z pc af bc de hl ix iy sp af' bc' de' hl' totalcycles``: one line per Z80
  instruction (13 values: under MAME's 14-value ``logerror`` limit, trap 2);
- ``YR|YW address data totalcycles`` for each Z80 access to $4000-$7FFF (YM2612,
  the bank register at $6000, the VDP and PSG at $7F00-$7F1F);
- ``KR|KW address data totalcycles`` for each access to the banked 68000 window
  ($8000-$FFFF).

``totalcycles`` counts each CPU's own clocks (68000: master / 7; Z80: master / 15).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAME = "/usr/games/mame"
ROMPATH = "/data/emu/source/myrient.erista.me/files/MAME/ROMs (non-merged)"
DRIVERS = ("genesis", "megadriv", "megadrij", "genesis_tmss")
CART_SUFFIXES = (".md", ".gen", ".bin", ".smd")

# (tag, start, length, access) for each watched range: the 68000's device windows,
# and the Z80's ($4000-$7FFF: YM2612, bank register, VDP and PSG; $8000-: the bank).
M68K_WATCHES = [
    ("VR", "c00000", "20", "r"), ("VW", "c00000", "20", "w"),
    ("AR", "a00000", "10000", "r"), ("AW", "a00000", "10000", "w"),
    ("IR", "a10000", "20", "r"), ("IW", "a10000", "20", "w"),
    ("BR", "a11100", "2", "r"), ("BW", "a11100", "2", "w"), ("BW", "a11200", "2", "w"),
]  # fmt: skip
Z80_WATCHES = [
    ("YR", "4000", "4000", "r"), ("YW", "4000", "4000", "w"),
    ("KR", "8000", "8000", "r"), ("KW", "8000", "8000", "w"),
]  # fmt: skip
M68K_FIELDS = (
    "curpc,sr,d0,d1,d2,d3,d4,d5,d6,d7,a0,a1,a2,a3,a4,a5,a6,usp,sp",
    "totalcycles,beamy,beamx",
)
Z80_FIELDS = ("pc,af,bc,de,hl,ix,iy,sp,af2,bc2,de2,hl2", "totalcycles")


def lua_script(cpu: str, kind: str, fields: tuple[str, str], watches: list, size: bool) -> str:
    """The autoboot script for one CPU's run: select it, trace it, watch its windows.

    ``fields``: the hex fields, then the decimal ones. ``size``: log the access
    width too (the 68000 makes byte and word accesses to the same ports).
    """
    hex_count, dec_count = (len(group.split(",")) for group in fields)
    trace_format = " ".join([kind] + ["%X"] * hex_count + ["%d"] * dec_count)
    lines = [
        "local dbg = manager.machine.debugger",
        f'dbg.visible_cpu = manager.machine.devices["{cpu}"]',
        f"dbg:command('trace {kind}.trace,{cpu},noloop,"
        f'{{logerror "{trace_format}\\\\n",{fields[0]},{fields[1]}}}\')',
    ]
    access = "wpaddr,wpsize,wpdata" if size else "wpaddr,wpdata"
    access_format = "%X %X %X %d" if size else "%X %X %d"
    for tag, start, length, rw in watches:
        lines.append(
            f"dbg:command('wpset {start},{length},{rw},1,"
            f'{{logerror "{tag} {access_format}\\\\n",{access},totalcycles; g}}\')'
        )
    lines.append('dbg:command("go")')
    return "\n".join(lines) + "\n"


M68K_LUA = lua_script(":maincpu", "M", M68K_FIELDS, M68K_WATCHES, size=True)
Z80_LUA = lua_script(":genesis_snd_z80", "Z", Z80_FIELDS, Z80_WATCHES, size=False)


def extract(cart: Path, out: Path) -> Path:
    """The cartridge image itself: MAME loads a zip's *first* member (trap 4)."""
    if not zipfile.is_zipfile(cart):
        shutil.copyfile(cart, out / cart.name)
        return out / cart.name
    with zipfile.ZipFile(cart) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(CART_SUFFIXES)]
        member = names[0] if names else archive.namelist()[0]
        image = out / Path(member).name
        image.write_bytes(archive.read(member))
    return image


def run(args: argparse.Namespace, image: Path, out: Path, name: str, lua: str) -> None:
    script = out / f"{name}.lua"
    script.write_text(lua, encoding="utf-8")
    (out / "error.log").unlink(missing_ok=True)
    subprocess.run(
        [args.mame, args.driver, "-cart", str(image), "-rompath", args.rompath,
         "-homepath", str(out), "-snapshot_directory", str(out / f"snap-{name}"),
         "-video", "none", "-sound", "none", "-nothrottle", "-noreadconfig",
         "-skip_gameinfo", "-debug", "-debugger", "none", "-log",
         "-seconds_to_run", str(args.seconds), "-autoboot_script", str(script)],
        cwd=out, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    (out / "error.log").replace(out / f"{name}.log")
    (out / f"{name[0].upper()}.trace").unlink(missing_ok=True)  # the disassembly; the log has all


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("driver", choices=DRIVERS)
    parser.add_argument("cart", type=Path, help="cartridge image, or a zip holding one")
    parser.add_argument("--seconds", type=int, default=3, help="emulated seconds")
    parser.add_argument("--only", choices=("m68k", "z80"), help="record one CPU's run only")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--mame", default=MAME)
    parser.add_argument("--rompath", default=ROMPATH)
    args = parser.parse_args()
    cart = args.cart.resolve()
    out = (args.out or ROOT / "tests" / "mame_traces" / f"{args.driver}-{cart.stem}").resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    image = extract(cart, out)
    for name, lua in (("m68k", M68K_LUA), ("z80", Z80_LUA)):
        if args.only in (None, name):
            run(args, image, out, name, lua)
    snaps = sorted(out.glob("snap-*/*/*.png"))
    if snaps:
        (out / "snap").mkdir()
        shutil.copyfile(snaps[0], out / "snap" / "exit.png")
    version = subprocess.run(
        [args.mame, "-version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    for name in ("m68k", "z80"):
        log = out / f"{name}.log"
        if log.exists():
            counts: dict[str, int] = {}
            with log.open("rb") as handle:
                for line in handle:
                    kind = line.split(b" ", 1)[0].decode("ascii", "replace")
                    counts[kind] = counts.get(kind, 0) + 1
            summary = ", ".join(f"{k} {v:,}" for k, v in sorted(counts.items()) if len(k) <= 2)
            print(f"MAME {version}: {log} ({summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
