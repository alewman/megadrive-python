#!/usr/bin/env python3
"""Where Nuked-MD takes the work-RAM refresh after an idle stretch or a transfer.

A test cartridge reads work RAM every 56 MCLK (eight ``move.w (a0),d0`` a pass), so
every refresh is taken by a read and the timer's phase is known; then either spins in
ROM (``--idle N``: N ``dbra`` passes of 70 MCLK) or runs a 68000-to-VDP transfer of N
words from the cartridge (``--dma N``, to VRAM or ``--cram``); then, after ``--nops``
NOPs (the first an ``exg``, 14 MCLK longer), reads work RAM densely again. Printed per
run: the first read held after the stretch, against the last refresh taken before it
(idle) or the transfer's last memory select (DMA) -- docs/worklog.md, "The work-RAM
refresh after a transfer".

    python scripts/run_ram_refresh.py --dma 100,1000 --nops 3,4,5
    python scripts/run_ram_refresh.py --idle 10,100,1000
"""

from __future__ import annotations

import argparse
import sys
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

import nuked_md as nmd  # noqa: E402
import run_nuked_md as rn  # noqa: E402

READ_RAM = bytes.fromhex("3010")  # move.w (a0),d0
STOP = bytes.fromhex("4e722700")


def dense(passes: int) -> bytes:
    """``passes`` passes of eight work-RAM reads and a ``dbra``."""
    count = bytes.fromhex("323c") + (passes - 1).to_bytes(2, "big")
    return count + READ_RAM * 8 + bytes.fromhex("51c9ffee")


def pause(nops: int) -> bytes:
    return (bytes.fromhex("c141") + rn.NOP * (nops - 1)) if nops else b""  # exg d0,d1 first


def control(word: int) -> bytes:
    return bytes.fromhex("33fc") + word.to_bytes(2, "big") + bytes.fromhex("00c00004")


def rom(kind: str, n: int, nops: int, cram: bool) -> bytes:
    body = rn.UNLOCK + bytes.fromhex("41f900ff0000")  # lea $FF0000,a0
    if kind == "idle":
        spin = bytes.fromhex("343c") + n.to_bytes(2, "big") + bytes.fromhex("51cafffe")
        body += dense(40) + spin
    else:
        source = 0x8000 >> 1
        for reg, value in ((0x00, 0x04), (0x01, 0x14), (0x0C, 0x81), (0x0F, 0x02),
                           (0x13, n & 0xFF), (0x14, n >> 8 & 0xFF), (0x15, source & 0xFF),
                           (0x16, source >> 8 & 0xFF), (0x17, source >> 16 & 0x7F)):  # fmt: skip
            body += control(0x8000 | reg << 8 | value)
        body += control(0xC000 if cram else 0x4000) + dense(40) + control(0x0080)
    body += pause(nops) + dense(10) + STOP
    return rn.cartridge(body, size=0x20000)


def one(job: tuple) -> str:
    kind, n, nops, cram = job
    window = 80_000 + n * 70
    run = nmd.run(rom(kind, n, nops, cram), stop=rn.REFEREE_RESET + window,
                  trace=(rn.REFEREE_RESET, rn.REFEREE_RESET + window), trace_mask=9)  # fmt: skip
    cycles = run.m68k(dma=False)
    ram = [(e.t, e.length - nmd.AS_LOW) for e in cycles if e.addr == 0xFF0000]
    gap = max(range(len(ram) - 1), key=lambda i: ram[i + 1][0] - ram[i][0])
    before, after = ram[: gap + 1], ram[gap + 1 :]
    held = next(t for t, h in after if h > 0)
    if kind == "idle":
        ref = max(t for t, h in before if h > 0)
        what = "last refresh taken"
    else:
        lo, hi = before[-1][0], after[0][0]
        ref = max(e.t for e in run.events
                  if e.type == nmd.EV_MEM and e.bus_granted and lo < e.t < hi)  # fmt: skip
        what = "transfer's last select"
    return (f"{kind} {n:5d} nops {nops}: after the {what}, first read {after[0][0] - ref:7d}, "
            f"first held {held - ref:7d}")  # fmt: skip


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--idle", help="dbra passes, comma-separated")
    group.add_argument("--dma", help="transfer lengths in words, comma-separated")
    parser.add_argument("--nops", default="0", help="NOPs before the reads resume")
    parser.add_argument("--cram", action="store_true", help="transfer to CRAM, not VRAM")
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()
    kind = "idle" if args.idle else "dma"
    jobs = [(kind, int(n), int(k), args.cram) for n in (args.idle or args.dma).split(",")
            for k in args.nops.split(",")]  # fmt: skip
    with Pool(args.jobs) as pool:
        for line in pool.map(one, jobs):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
