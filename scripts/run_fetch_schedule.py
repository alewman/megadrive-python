#!/usr/bin/env python3
"""When the VDP fetches each plane's tiles, read off the die model's own VRAM accesses.

Nothing hardware-captured gives the fetch schedule. Kabuto's access pattern gives the
order of a group's slots; Nuked-MD, the die-derived referee, gives the times. Our
driver (``validation/nuked_md_headless.c``, ``NMD_VRAMSIG``) rebuilds each access from
the VDP's DRAM pins -- the row on /RAS falling, the column on /CAS falling -- and logs
the raw chip address with the die's H and V counters, and each write's bank and data.
The chip address maps to VRAM as

    linear = (a & 3) | ((a >> 6) & 0x3FC) | ((a & 0xFC) << 8)

which this script does not assume: it checks that every access of a display line lands
where the test cartridge's layout says it must (names at $C000 and $E000, patterns at
$0020 and $0000, sprites at $F000, the scroll table at $FC00).

The cartridge: plane A tile 1 (red) everywhere, tile 2 green; on each line the 68000
rewrites one cell's name (column c of the cell row the line shows) to tile 1 or 2 by
V's parity, at a delay that sweeps across the line.

It prints the schedule, then, per column, where the board's picture and the die's
differ and whether every such line is one where the two committed the write in
different access slots (the 68000's timing, not the fetch). Exits 1 if an access lands
off the layout or a difference is not a commit difference.

    python scripts/run_fetch_schedule.py
"""

from __future__ import annotations

import collections
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from run_vscroll_latch import _w, nuked_md_cartridge  # noqa: E402

FRAME = 262 * 3420
RED, GREEN = (255, 0, 0), (0, 255, 0)
COLUMNS = {True: (16, 20, 22, 23), False: (12, 14, 16, 19)}


def linear(address: int) -> int:
    return (address & 3) | ((address >> 6) & 0x3FC) | ((address & 0xFC) << 8)


def cartridge(c: int, h40: bool) -> bytes:
    code = bytearray()
    code += _w(0x46FC, 0x2700)
    code += bytes.fromhex("23fc5345474100a14000")
    code += _w(0x41F9, 0x00C0, 0x0000, 0x43F9, 0x00C0, 0x0004, 0x45F9, 0x00C0, 0x0008)
    for reg, value in ((0x00, 0x04), (0x01, 0x04), (0x02, 0x30), (0x04, 0x07),
                       (0x05, 0x78), (0x07, 0x00), (0x0A, 0xFF), (0x0B, 0x00),
                       (0x0C, 0x81 if h40 else 0x00), (0x0D, 0x3F), (0x0F, 0x02),
                       (0x10, 0x01), (0x11, 0x00), (0x12, 0x00)):  # fmt: skip
        code += _w(0x32BC, 0x8000 | reg << 8 | value)
    code += _w(0x22BC, 0xC000, 0x0000)
    code += _w(0x30BC, 0x0000, 0x30BC, 0x000E, 0x30BC, 0x00E0)
    code += _w(0x22BC, 0x4000, 0x0000, 0x303C, 0x7FFF)  # clear VRAM
    code += _w(0x30BC, 0x0000, 0x51C8, 0xFFFA)
    code += _w(0x22BC, 0x4020, 0x0000)  # tiles 1 (red) and 2 (green)
    code += _w(0x303C, 0x000F, 0x30BC, 0x1111, 0x51C8, 0xFFFA)
    code += _w(0x303C, 0x000F, 0x30BC, 0x2222, 0x51C8, 0xFFFA)
    code += _w(0x22BC, 0x4000, 0x0003)  # plane A: every cell tile 1
    code += _w(0x303C, 0x07FF, 0x30BC, 0x0001, 0x51C8, 0xFFFA)
    code += _w(0x32BC, 0x8144)
    code += _w(0x7800, 0x7200)
    loop = bytearray()
    loop += _w(0x1012, 0xB001, 0x67FA, 0x1200, 0x3400, 0x0242, 0x003F)
    loop += _w(0x1A01, 0x0285, 0x0000, 0x00F8, 0xE98D)  # d5 = (V & $F8) << 4
    loop += _w(0x0685, 0x0000, 2 * c, 0x4845)  # + 2c, swap
    loop += _w(0x0085, 0x4000, 0x0003)  # VRAM write at $C000 + that
    loop += _w(0x1801, 0x0244, 0x0001, 0x5244)  # d4 = (V & 1) + 1
    loop += _w(0x51CA, 0xFFFE)  # D: dbra d2,D
    loop += _w(0x2285, 0x3084)  # move.l d5,(a1); move.w d4,(a0)
    n = len(loop)
    loop += _w(0x6000, (-(n + 2)) & 0xFFFF)
    return nuked_md_cartridge(bytes(code + loop))


def referee(rom: bytes, c: int, h40: bool) -> tuple[dict, list, list]:
    """The column's colour by V, the log's accesses, and the tile-index writes by V."""
    binary = ROOT / "reference" / "build-nuked-md" / "nukedmd-headless"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "rom.bin").write_bytes(rom)
        env = dict(os.environ, NMD_STOP=str(13 * FRAME * 2), NMD_PLOT=str(work / "p.bin"),
                   NMD_PLOT_FRAME="11", NMD_VDPSIG="/dev/null",
                   NMD_VDPSIG_FROM=str(10 * FRAME * 2), NMD_VDPSIG_TO=str(13 * FRAME * 2),
                   NMD_VRAMSIG=str(work / "v.txt"))  # fmt: skip
        subprocess.run(["nice", str(binary), str(work / "rom.bin")], env=env, cwd=work,
                       check=True, capture_output=True)  # fmt: skip
        plot = (work / "p.bin").read_bytes()
        log = [line.split() for line in (work / "v.txt").read_text().splitlines()]
    lines, samples = int.from_bytes(plot[4:8], "little"), int.from_bytes(plot[8:12], "little")
    counts = [int.from_bytes(plot[12 + 2 * i : 14 + 2 * i], "little") for i in range(lines)]
    base = 12 + 2 * lines
    vbase = base + lines * samples * 3
    vcount = [
        int.from_bytes(plot[vbase + 2 * i : vbase + 2 * i + 2], "little") for i in range(lines)
    ]
    first = 154 if h40 else 130
    cell = {}
    for i in range(lines):
        if vcount[i] < 224 and counts[i] >= 600:
            at = base + (i * samples + first + 2 * (8 * c + 4)) * 3
            pixel = tuple(plot[at : at + 3])
            cell[vcount[i]] = "R" if pixel == RED else "G" if pixel == GREEN else "?"
    frames: list[dict[int, int]] = []
    current: dict[int, int] = {}
    last_v = None
    for field in log:
        if field[0] == "W" and int(field[5]) in (1, 2):
            v = int(field[3])
            if last_v is not None and v < last_v - 100:
                frames.append(current)
                current = {}
            current.setdefault(v, int(field[2]))
            last_v = v
    frames.append(current)
    return cell, log, frames


def schedule(log: list, line: int = 100) -> tuple[dict, int]:
    """Each fetch of one display line by role, and how many accesses land off layout."""
    row = line >> 3
    roles: dict[str, list[tuple[int, int]]] = collections.defaultdict(list)
    off = 0
    for field in log:
        # The serial port's transfers are the fetches; the random port's reads (one a
        # group, of a plane A name, three counts after its transfer) are listed apart.
        if field[0] != "T" or int(field[3]) != line:
            continue
        where, h = linear(int(field[1])), int(field[2])
        if 0xC000 + 128 * row <= where < 0xC000 + 128 * (row + 1):
            roles["A name"].append((h, (where - 0xC000 - 128 * row) // 4))
        elif 0xE000 + 128 * row <= where < 0xE000 + 128 * (row + 1):
            roles["B name"].append((h, (where - 0xE000 - 128 * row) // 4))
        elif where in (0x20 + 4 * (line & 7), 0x40 + 4 * (line & 7)):
            roles["A pattern"].append((h, -1))  # tile 1's row, or tile 2's where named
        elif where == 0x00 + 4 * (line & 7):
            roles["B pattern"].append((h, -1))  # plane B names tile 0 everywhere
        elif where == 0x0000:
            pass  # the transfers of an idle stretch of the line, at address 0
        elif 0xF000 <= where < 0xF280 or where == 0xFC00:
            pass
        else:
            off += 1
    return roles, off


def board(rom: bytes, c: int) -> tuple[dict, dict]:
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES
    from megadrive_python.system import System

    system = System(MACHINES["genesis"], Cartridge(rom))
    system.board.fm.enabled = False
    vdp = system.vdp
    for _ in range(11):
        system.run_frame()
    writes: dict[int, int] = {}
    store = vdp._vram_write_byte

    def record(address: int, value: int) -> None:
        if vdp.frame == 11 and 0xC000 <= address < 0xD000 and value in (1, 2):
            at = vdp.hmode.position(vdp.mclk - vdp.line_start)
            writes.setdefault(vdp.v, vdp.hmode.hvals[at])
        store(address, value)

    vdp._vram_write_byte = record
    system.run_frame()
    rows = vdp.renderer.frame_rgb()
    cell = {
        y: "R" if row[8 * c + 4] == 0xFF0000 else "G" if row[8 * c + 4] == 0x00FF00 else "?"
        for y, row in enumerate(rows[:224])
    }
    return cell, writes


def explained(writes: dict, cell: dict, c: int) -> int:
    """How many lines of the die's picture its own write times and the fetch schedule
    explain -- used only to pick which frame of the log the picture is."""
    name_at = 16 * (c >> 1) + 3
    good = 0
    for v, colour in cell.items():
        if v and v in writes and colour in ("R", "G"):
            new = order(writes[v]) < name_at
            good += colour == (("G" if v & 1 else "R") if new else ("R" if v & 1 else "G"))
    return good


def order(h: int) -> int:
    """H in line order: the counter line runs from the V increment at $14A."""
    return h - 0x200 if h >= 0x14A else h


def main() -> int:
    failed = False
    for h40 in (True, False):
        mode = "H40" if h40 else "H32"
        label = 3 if h40 else 0
        for n, c in enumerate(COLUMNS[h40]):
            rom = cartridge(c, h40)
            die_cell, log, frames = referee(rom, c, h40)
            if n == 0:
                roles, off = schedule(log)
                print(f"{mode}: accesses of line 100 off the cartridge's layout: {off}")
                for role, fetches in roles.items():
                    shown = [f"${h:03X}" + (f":{g}" if g >= 0 else "") for h, g in fetches[:6]]
                    print(f"  {role:9} (die H[:group]) " + " ".join(shown))
                failed |= off != 0
            board_cell, board_writes = board(rom, c)
            die_writes = max(frames, key=lambda writes: explained(writes, die_cell, c))
            shown = ("R", "G")
            differ = [
                v for v in range(1, 224)
                if die_cell.get(v) in shown and board_cell.get(v) in shown
                and die_cell[v] != board_cell[v]
            ]  # fmt: skip
            unexplained = [
                v for v in differ
                if v not in die_writes or v not in board_writes
                or abs(order(die_writes[v]) - (order(board_writes[v]) + label)) <= 3
            ]  # fmt: skip
            print(f"{mode} column {c}: board and die differ on {len(differ)} lines; "
                  f"{len(differ) - len(unexplained)} where the write was committed in "
                  f"another slot, {len(unexplained)} otherwise")  # fmt: skip
            failed |= bool(unexplained)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
