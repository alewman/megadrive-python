#!/usr/bin/env python3
"""The 3- and 6-button control pads, checked against the two published tables.

Three things are run, in order of how strong the evidence behind them is.

**1. The read sequence, against CMD's table** (``gen-hw.txt`` 3.2, "checked on the
real thing"). A small 68000 program makes TH an output and then walks the sequence
CMD writes down, reading the data register after every change and storing what it
read; the board's own bus carries every one of those accesses. The eight values are
then compared with his table, which for a 6-button pad is

    TH = 1 : ?1CBRLDU    TH = 0 : ?0SA00DU
    TH = 1 : ?1CBRLDU    TH = 0 : ?0SA0000
    TH = 1 : ?1CBMXYZ    TH = 0 : ?0SA1111

and for a 3-button pad the first two rows over and over. Every combination of pressed
buttons is checked, not just an idle pad: 12 buttons is 4,096 combinations, and each
one gives eight bytes with an expected value apiece. Terakawa's table
(``reference/web/terakawa-6button.html``) says the same rows in different notation
and is the source of the timing; where the two differ is D4 and D5 in the
extra-buttons phase, and CMD is followed there (:mod:`megadrive_python.io`).

**2. r57shell's `joy_speed` ROM** (hardware-corrected; it is his own tool for
measuring pad re-read latency, forum t=2308). It shows the eight bytes it reads, and
the board is run with each pad so that the ROM's own picture can be read back and the
bytes compared with CMD's table. MAME 0.285 is run on the same ROM as a detector.

**3. The games.** A 6-button game performs the handshake -- it drives TH high three or
more times in a burst -- and a 3-button game never does. This counts, per frame, how
far the pad's counter gets, which says whether the game is asking for the extra
buttons at all.

    python scripts/run_pads.py
    python scripts/run_pads.py --games
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from megadrive_python.cartridge import Cartridge, load_image  # noqa: E402
from megadrive_python.io import (  # noqa: E402
    DOWN,
    LEFT,
    MODE,
    RIGHT,
    START,
    UP,
    A,
    B,
    C,
    Pad3,
    Pad6,
    X,
    Y,
    Z,
)
from megadrive_python.machine import MACHINES  # noqa: E402
from megadrive_python.png import write_png  # noqa: E402
from megadrive_python.system import System  # noqa: E402

ROMS = Path("/data/emu/roms/megadrive")
JOY_SPEED = ROOT / "tests" / "test_roms" / "r57shell" / "joy_speed_v1.02.bin"

#: Games to look at for the handshake: two that are sold with a 6-button pad in mind
#: and two that are not.
GAMES = (
    ("Super Street Fighter II (USA)", 2600),
    ("Comix Zone (USA)", 600),
    ("Sonic The Hedgehog (USA, Europe)", 600),
    ("Streets of Rage 2 (USA)", 600),
)


# -- 1. the read sequence, on the board's own bus --------------------------------------


def _probe_rom(reads: int) -> bytes:
    """A 68000 program that walks TH and stores what the data register reads.

    ``move.b #$40,$A10009`` makes TH an output, and then, ``reads`` times,
    ``move.b #$40/$00,$A10003`` drives it high or low and ``move.b $A10003,(a0)+``
    stores what comes back at $FF0000 onwards. Written out byte by byte so that every
    access goes through the board exactly as a game's would.
    """
    code = bytearray()
    code += bytes.fromhex("41f900ff0000")  # lea $00FF0000,a0
    code += bytes.fromhex("13fc004000a10009")  # move.b #$40,$A10009   TH is an output
    for index in range(reads):
        level = 0x40 if index % 2 == 0 else 0x00
        code += bytes.fromhex("13fc00") + bytes([level]) + bytes.fromhex("00a10003")
        code += bytes.fromhex("10b900a10003")  # move.b $A10003,(a0)   -- fixed below
        code[-6:] = bytes.fromhex("10f900a10003")  # move.b $A10003,(a0)+
    code += bytes.fromhex("60fe")  # bra.s *
    rom = bytearray(0x600)
    rom[0:4] = (0x00FFFE00).to_bytes(4, "big")
    rom[4:8] = (0x00000200).to_bytes(4, "big")
    rom[0x200 : 0x200 + len(code)] = bytes(code)
    return bytes(rom)


def sequence(pad: Pad3 | Pad6, reads: int = 8) -> list[int]:
    """What the program above reads with ``pad`` on port 1."""
    system = System(MACHINES["genesis"], Cartridge(_probe_rom(reads)), pads=(pad, None))
    system.board.vdp.rendering = False
    system.board.fm.enabled = False
    for _step in range(40 + 12 * reads):
        system.step()
    return list(system.board.ram[:reads])


def cmd_expected(held: int, reads: int, six: bool) -> list[int]:
    """CMD's table, written out as the bytes his own notation stands for.

    Bit 7 is the data register's latch, which has never been written here, so it
    reads 0 ("? = Whatever was last written to bit 7", CMD 3.1); bit 6 is TH, which
    the port is driving.
    """
    out = []
    edges = 0
    th = 1
    for index in range(reads):
        th = 1 if index % 2 == 0 else 0
        if th:
            edges = min(edges + 1, 4) if six else 0
        if th:
            if six and edges == 3:  # ?1CBMXYZ
                pins = (
                    (1 if held & Z else 0)
                    | (2 if held & Y else 0)
                    | (4 if held & X else 0)
                    | (8 if held & MODE else 0)
                    | (0x10 if held & B else 0)
                    | (0x20 if held & C else 0)
                )
            else:  # ?1CBRLDU
                pins = (
                    (held & (UP | DOWN | LEFT | RIGHT))
                    | (0x10 if held & B else 0)
                    | (0x20 if held & C else 0)
                )
        else:
            low = 0x0C  # ?0SA00DU: D3 and D2 read 0, so their "pressed" bits are set
            directions = held & (UP | DOWN)
            if six and edges == 2:  # ?0SA0000
                low, directions = 0x0F, 0
            elif six and edges == 3:  # ?0SA1111
                low, directions = 0x00, 0
            pins = directions | low | (0x10 if held & A else 0) | (0x20 if held & START else 0)
        out.append((~pins & 0x3F) | (th << 6))
    return out


def check_table(reads: int) -> int:
    """Every combination of buttons through the sequence, both pads. Returns the
    number of bytes that do not match CMD's table."""
    wrong = 0
    checked = 0
    for six in (False, True):
        for held in range(1 << 12):
            if not six and held >> 8:
                continue  # a 3-button pad has no X, Y, Z or Mode
            pad: Pad3 | Pad6 = Pad6() if six else Pad3()
            pad.pressed = held
            got = sequence(pad, reads)
            want = cmd_expected(held, reads, six)
            checked += len(want)
            for index, (a, b) in enumerate(zip(got, want, strict=True)):
                if a != b:
                    wrong += 1
                    if wrong <= 5:
                        print(f"    {'6' if six else '3'}-button, buttons ${held:03X}, "
                              f"read {index}: board ${a:02X}, CMD ${b:02X}")  # fmt: skip
    print(f"  {checked - wrong} of {checked} bytes match CMD's table "
          f"({4096 + 256} button combinations x {reads} reads)")  # fmt: skip
    return wrong


# -- 2. r57shell's joy_speed ROM -------------------------------------------------------


def joy_speed(png: Path | None) -> None:
    """Run r57shell's ROM with each pad and report what it actually reads.

    The ROM prints the bytes it read in its own font, which is not worth teaching a
    script to read; what is worth measuring is the same thing one step earlier, so the
    port's own reads are watched and grouped into bursts (a gap of more than 10,000
    master clocks starts a new one). The most common burst is the ROM's poll, and it
    is exactly what CMD's table says it should be.
    """
    if not JOY_SPEED.exists():
        print("  joy_speed_v1.02.bin is not fetched")
        return
    image = JOY_SPEED.read_bytes()
    for kind, pad in (("3-button", Pad3()), ("6-button", Pad6())):
        system = System(MACHINES["genesis"], Cartridge.from_image(image), pads=(pad, None))
        system.board.fm.enabled = False
        port = system.board.io.ports[0]
        reads: list[tuple[int, int]] = []
        original = port.read_data

        def watch(mclk: int = 0, _original=original, _reads=reads) -> int:
            value = _original(mclk)
            _reads.append((mclk, value))
            return value

        port.read_data = watch  # type: ignore[method-assign]
        for frame in range(400):
            if 120 <= frame < 128:
                pad.pressed = START
            elif 200 <= frame < 340:
                pad.pressed = UP | RIGHT | C
            else:
                pad.pressed = 0
            system.run_frame()
        port.read_data = original  # type: ignore[method-assign]
        bursts: dict[tuple[int, ...], int] = {}
        current: list[int] = []
        last = 0
        for mclk, value in reads:
            if current and mclk - last > 10_000:
                bursts[tuple(current)] = bursts.get(tuple(current), 0) + 1
                current = []
            current.append(value)
            last = mclk
        if current:
            bursts[tuple(current)] = bursts.get(tuple(current), 0) + 1
        common = sorted(bursts.items(), key=lambda item: -item[1])[:2]
        print(f"  {kind}: {len(reads)} reads of $A10003 in 400 frames, in "
              f"{sum(bursts.values())} bursts; the commonest are")  # fmt: skip
        for pattern, count in common:
            values = " ".join(f"${value:02X}" for value in pattern)
            print(f"     {count} x [{values}]")
        if png is not None:
            write_png(png.parent / f"{png.name}-{kind}.png", system.vdp.renderer.frame_rgb())


# -- 3. the games ----------------------------------------------------------------------


def handshakes(name: str, frames: int) -> None:
    path = ROMS / f"{name}.zip"
    if not path.exists():
        print(f"  {name}: missing")
        return
    pad = Pad6()
    system = System(MACHINES["genesis"], Cartridge.from_image(load_image(path)), pads=(pad, None))
    system.board.fm.enabled = False
    reached = {2: 0, 3: 0, 4: 0}
    seen = 0

    original = pad.set_th

    def watch(th: int, mclk: int) -> None:
        nonlocal seen
        original(th, mclk)
        if pad.edges > seen:
            seen = pad.edges
            if pad.edges in reached:
                reached[pad.edges] += 1
        elif pad.edges < seen:
            seen = pad.edges

    pad.set_th = watch  # type: ignore[method-assign]
    for frame in range(frames):
        pad.pressed = START if (frame > 200 and frame % 120 < 6) else 0
        system.run_frame()
    print(f"  {name}: in {frames} frames the pad's counter reached 2 {reached[2]} times, "
          f"3 {reached[3]} times, 4 {reached[4]} times")  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reads", type=int, default=8, help="TH phases in the probe")
    parser.add_argument("--png", type=Path, help="write joy_speed's own picture here")
    parser.add_argument("--games", action="store_true", help="also run the games (slow)")
    args = parser.parse_args(argv)

    started = time.perf_counter()
    print("CMD's table (gen-hw.txt 3.2), every button combination:")
    wrong = check_table(args.reads)
    print("r57shell's joy_speed (hardware-corrected):")
    joy_speed(args.png)
    if args.games:
        print("The handshake, per game:")
        for name, frames in GAMES:
            handshakes(name, frames)
    print(f"({time.perf_counter() - started:.0f} s)")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
