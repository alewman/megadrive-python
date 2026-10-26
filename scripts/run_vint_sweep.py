#!/usr/bin/env python3
"""At which instruction boundary the 68000 takes a VINT, on Nuked-MD and on the board.

A test cartridge turns the display and the V interrupt on, unmasks it, and loops on
``nop; nop; bra.s``; its interrupt handler counts frames and spends a different number
of ``dbra`` passes each time, so that from frame to frame the next VINT lands at every
point of the loop's instruction boundaries. Both machines run it from power-on with
every phase on the referee's (``cold_boot_drift``); for each VINT, the time the 68000
begins stacking for it is compared, and tabulated by how long before the instruction
boundary at which the board took it the board's VDP had raised it (docs/worklog.md,
"The first interrupt").

    python scripts/run_vint_sweep.py [--frames 60]
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

import cold_boot_drift  # noqa: E402
from run_vscroll_latch import _w, nuked_md_cartridge  # noqa: E402

PAL_FRAME_MCLK = 313 * 3420
STACK = 0xFFFE00
HANDLER = 0x400


def cartridge() -> bytes:
    """The test cartridge, hand-assembled; each encoding is in its comment."""
    code = bytearray()
    code += _w(0x46FC, 0x2700)  # move.w #$2700,sr
    code += bytes.fromhex("23fc5345474100a14000")  # move.l #'SEGA',$A14000
    for reg, value in ((0x00, 0x04), (0x01, 0x64), (0x0C, 0x81), (0x0F, 0x02)):
        # mode 5; display on, VINT on; H40; auto-increment 2
        code += _w(0x33FC, 0x8000 | reg << 8 | value, 0x00C0, 0x0004)  # move.w #r,$C00004
    code += _w(0x7E00)  # moveq #0,d7
    code += _w(0x46FC, 0x2000)  # move.w #$2000,sr: VINT unmasked
    code += _w(0x4E71, 0x4E71, 0x60FA)  # loop: nop; nop; bra.s loop
    rom = bytearray(nuked_md_cartridge(bytes(code)))
    handler = bytearray()
    handler += _w(0x5247)  # addq.w #1,d7
    handler += _w(0x3C07, 0x0246, 0x000F)  # move.w d7,d6; andi.w #15,d6
    handler += _w(0x51CE, 0xFFFE)  # dbra d6,*
    handler += _w(0x4A79, 0x00C0, 0x0004)  # tst.w $C00004: the status, as a handler reads it
    handler += _w(0x4E73)  # rte
    rom[HANDLER : HANDLER + len(handler)] = handler
    rom[0x78:0x7C] = HANDLER.to_bytes(4, "big")  # level 6 autovector
    return bytes(rom)


def stackings(stream: list[tuple]) -> list[int]:
    """The time of the first stack write of each exception: a write just below the
    stack followed by the level-6 vector fetch."""
    out = []
    for i, cycle in enumerate(stream):
        if cycle[1] == 0x78 and cycle[2] == "r":
            j = i - 1
            # The stacking writes, and the referee's acknowledge (a read at $FFFFF0 and up).
            while j > 0 and (
                (stream[j - 1][2] == "w" and STACK - 16 <= stream[j - 1][1] < STACK)
                or (stream[j - 1][2] == "r" and stream[j - 1][1] >= 0xFFFFF0)
            ):
                j -= 1
            while stream[j][2] == "r":
                j += 1
            out.append(stream[j][0])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--rom", type=Path, help="write the cartridge here too")
    args = parser.parse_args()
    rom_bytes = cartridge()
    path = (args.rom or ROOT / "build" / "vint_sweep.bin").resolve()
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(rom_bytes)
    until = (args.frames + 2) * PAL_FRAME_MCLK
    theirs, events, run = cold_boot_drift.referee(path, until)

    import od2_boot_drift as judge

    import megadrive_python.system as system_module

    log = judge.log_bus()
    system = cold_boot_drift.board(path, events, run, quiet=True)
    raised: list[tuple[int, int]] = []  # (the step start the 68000 saw it at, VINT's time)
    step = system_module.System.step
    seen = {"level": 0}

    def watched(self):
        vdp = self.vdp
        vdp.sync(self.mclk)
        level = vdp.ipl
        if level == 6 and not seen["level"]:
            raised.append((self.mclk, vdp.vint_at))
        seen["level"] = level
        return step(self)

    system_module.System.step = watched
    try:
        while system.mclk < until - cold_boot_drift.VDP_OFFSET_MCLK:
            system.step()
    finally:
        system_module.System.step = step
    offset = cold_boot_drift.VDP_OFFSET_MCLK
    die, ours = stackings(theirs), stackings([tuple(c) for c in log])
    table: collections.Counter = collections.Counter()
    rows: list[tuple[int, int, int]] = []
    for d, b in zip(die, ours, strict=False):  # the counts may differ; both are printed
        boards_raise = [r for r in raised if r[0] <= b]
        if not boards_raise:
            continue
        seen_at, vint_at = boards_raise[-1]
        margin = seen_at - vint_at
        table[(margin // 7 * 7, (b + offset) - d)] += 1
        rows.append((margin, d - (b + offset), d - (vint_at + offset)))
    print(f"{len(die)} VINTs on the referee, {len(ours)} on the board")
    print("MCLK from the board's VDP raising VINT to the boundary the board took it at,"
          " and the board's stacking against the referee's: count")  # fmt: skip
    for key in sorted(table):
        print(f"  {key[0]:4d}-{key[0] + 6:4d}  {key[1]:+5d}: {table[key]}")
    after = sorted(r[2] for r in rows)
    margins = sorted(r[0] for r in rows)
    print(f"the referee's stacking began {after[0]}-{after[-1]} MCLK after the board's VDP raised"
          f" VINT; the board's {margins[0]}-{margins[-1]} after it"
          " plus the acknowledge's lead")  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
