#!/usr/bin/env python3
"""When a work-RAM access whose /AS meets the refresh counter's step to 112 takes the
refresh, on Nuked-MD.

The die's work-RAM refresh is taken by the access on whose /AS ``vdp.c``'s dff13 latches
"count >= 112". When the counter steps from 111 to 112 in the very half-MCLK the /AS
falls in, Overdrive 2's boot takes the refresh at two such accesses and not at two others
(docs/worklog.md, "The tie at 112"). A test cartridge runs a loop of NOPs and a ``bra.w``
from work RAM, so every bus cycle is a work-RAM access, one every 4 68000 clocks, and
about one refresh in four meets an access at that step; the 68000's 7-MCLK clock against
the VDP's pixel (8 MCLK in H40, 10 in its slow pixels around the sync) walks the ties
through every phase of the pixel. The die is traced with ``NMD_TRACE_MASK`` bit 6 (the
count at each work-RAM /AS, a step before, the VDP's H counter and how long it has held
its value), and every tie is tabulated, taken or not, by that phase and by H.

    python scripts/run_refresh_tie.py [--nops 1-24] [--exgs 0,1] [--display-off] [--h32]
"""

from __future__ import annotations

import argparse
import collections
import os
import subprocess
import sys
import tempfile
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

import nuked_md  # noqa: E402
from run_vscroll_latch import _w, nuked_md_cartridge  # noqa: E402

PAL_FRAME_MCLK = 313 * 3420
FIRST = 4  # frames run before the trace starts


def cartridge(nops: int, exgs: int, display: bool, h40: bool, z80: int = 1) -> bytes:
    """The test cartridge, hand-assembled; each encoding is in its comment."""
    code = bytearray()
    code += _w(0x46FC, 0x2700)  # move.w #$2700,sr
    code += bytes.fromhex("23fc5345474100a14000")  # move.l #'SEGA',$A14000
    code += _w(0x43F9, 0x00C0, 0x0004)  # lea $C00004,a1 (control)
    for reg, value in ((0x00, 0x04), (0x01, 0x44 if display else 0x04), (0x0C, 0x81 if h40 else 0x00),
                       (0x0F, 0x02)):  # fmt: skip
        code += _w(0x32BC, 0x8000 | reg << 8 | value)  # move.w #$8rvv,(a1)
    # nop x n; exg d0,d1 x m (two idle clocks each); move.b d0,(a2) to Z80 RAM x k (held a
    # clock: bus cycles are an even number of clocks apart, so without an odd hold the
    # count at each /AS keeps one parity and never meets the step to 112)
    body = [0x4E71] * nops + [0xC141] * exgs + [0x1480] * z80
    loop = body + [0x6000, (-(2 * len(body) + 2)) & 0xFFFF]  # bra.w back
    code += _w(0x45F9, 0x00A0, 0x0000)  # lea $A00000,a2 (Z80 RAM)
    code += _w(0x33FC, 0x0100, 0x00A1, 0x1100)  # move.w #$0100,$A11100 (Z80 bus request)
    code += _w(0x33FC, 0x0100, 0x00A1, 0x1200)  # move.w #$0100,$A11200 (Z80 out of reset)
    code += _w(0x41F9, 0x00FF, 0x0000)  # lea $FF0000,a0
    for word in loop:
        code += _w(0x30FC, word)  # move.w #word,(a0)+
    code += _w(0x4EF9, 0x00FF, 0x0000)  # jmp $FF0000
    return nuked_md_cartridge(bytes(code))


def ties(rom: bytes, frames: int) -> list[tuple[int, int, int, int, int, int]]:
    """Each work-RAM /AS the die makes in the window: (count a step before, count,
    H counter, half-MCLK the H counter has held its value, dff13, half-MCLK time)."""
    nuked_md.ensure_built()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "rom.bin").write_bytes(rom)
        span = (FIRST * PAL_FRAME_MCLK * 2, (FIRST + frames) * PAL_FRAME_MCLK * 2)
        env = dict(os.environ, NMD_STOP=str(span[1] + 1000), NMD_TRACE=str(work / "t.bin"),
                   NMD_TRACE_MASK="96", NMD_TRACE_FROM=str(span[0]),
                   NMD_TRACE_TO=str(span[1]))  # fmt: skip
        subprocess.run(["nice", str(nuked_md.BINARY), "-pal", str(work / "rom.bin")], env=env,
                       cwd=work, check=True, capture_output=True)  # fmt: skip
        raw = (work / "t.bin").read_bytes()
    out = []
    for off in range(0, len(raw) - nuked_md.EVENT.size + 1, nuked_md.EVENT.size):
        _t, _zt, addr, data, length, kind, flags, _ = nuked_md.EVENT.unpack_from(raw, off)
        if kind == nuked_md.EV_RAM_AS:
            out.append((addr & 127, data, (addr >> 8) & 511, length, flags & 1, _t,
                        flags >> 1 & 31, flags >> 6 & 1, (addr >> 17) & 511))
    return out


def numbers(text: str) -> list[int]:
    out = []
    for part in text.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--nops", default="1-24", help="NOPs in the work-RAM loop: n, a-b or a list")
    parser.add_argument("--exgs", default="0,1", help="exg d0,d1 after them (2 idle clocks each)")
    parser.add_argument("--z80", type=int, default=1, help="Z80-RAM writes in the loop")
    parser.add_argument("--jobs", type=int, default=12)
    parser.add_argument("--display-off", action="store_true")
    parser.add_argument("--h32", action="store_true")
    parser.add_argument("--frames", type=int, default=10)
    args = parser.parse_args()
    roms = [cartridge(n, x, not args.display_off, not args.h32, args.z80)
            for n in numbers(args.nops) for x in numbers(args.exgs)]
    with Pool(min(args.jobs, 16)) as pool:
        runs = pool.starmap(ties, [(rom, args.frames) for rom in roms])
    seen = [access for run in runs for access in run]
    print(f"{len(roms)} loops, {args.frames} frames each")
    by_count = collections.Counter((before, count, taken) for before, count, _h, _p, taken, *_ in seen
                                   if 110 <= count <= 114)  # fmt: skip
    print("count a step before -> at /AS, taken: accesses")
    for key in sorted(by_count):
        print(f"  {key[0]:3d} -> {key[1]:3d}  {'taken' if key[2] else 'not  '}  {by_count[key]}")
    tie = [(h, phase, taken) for before, count, h, phase, taken, *_ in seen
           if before == 111 and count == 112]  # fmt: skip
    print(f"\nties (111 -> 112 at the /AS): {len(tie)}, taken {sum(t for *_, t in tie)}")
    clock = collections.defaultdict(collections.Counter)
    edge = collections.defaultdict(collections.Counter)
    for before, count, h, _p, taken, t, since, dclk in seen:
        if before == 111 and count == 112:
            clock[(t // 2 % 2, t % 14)][taken] += 1
            edge[(since, dclk, "sync" if h >= 0x16D else "")][taken] += 1
    print("by the master clock's parity at the /AS and its half-MCLK phase in the 68000's clock:")
    for key in sorted(clock):
        print(f"  MCLK parity {key[0]}, phase {key[1]:2d}: taken {clock[key][1]:6d}  not {clock[key][0]:6d}")
    print("by the half-MCLK since the VDP's dot clock (whose edges clock its logic) changed, and it:")
    for key in sorted(edge):
        print(f"  {key[0]:2d} half-MCLK, dclk {key[1]} {key[2]:4s}: taken {edge[key][1]:6d}  not {edge[key][0]:6d}")
    print("by the half-MCLK the VDP's H counter had held its value at the /AS:")
    phases = collections.defaultdict(collections.Counter)
    for _h, phase, taken in tie:
        phases[phase][taken] += 1
    for phase in sorted(phases):
        print(f"  {phase:3d}: taken {phases[phase][1]:5d}  not {phases[phase][0]:5d}")
    mixed = {p for p in phases if phases[p][0] and phases[p][1]}
    if mixed:
        print("phases with both outcomes, by H:")
        for p in sorted(mixed):
            hs = collections.defaultdict(collections.Counter)
            for h, phase, taken in tie:
                if phase == p:
                    hs[h][taken] += 1
            print(f"  {p}: " + ", ".join(f"H{h:03X} {c[1]}/{c[0]}" for h, c in sorted(hs.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
