#!/usr/bin/env python3
"""Where Nuked-MD takes the work-RAM refresh after an idle stretch or a transfer.

A test cartridge reads work RAM every 56 MCLK (eight ``move.w (a0),d0`` a pass), so
every refresh is taken by a read and the timer's phase is known; then either spins in
ROM (``--idle N``: N ``dbra`` passes of 70 MCLK), makes N data-port CRAM reads, each
held for the read-ahead (``--vdp N``), or runs a 68000-to-VDP transfer of N words from
the cartridge (``--dma N``, to VRAM or ``--cram``, with ``--display-on``, and
``--idle-after M`` passes of idle after it); then, after ``--nops`` NOPs (the first an
``exg``, 14 MCLK longer), reads work RAM densely again. Printed per run: the first read
held after the stretch, against the last refresh taken before it (idle), the last
data-port read's end (vdp) or the transfer's last memory select (DMA) --
docs/worklog.md, "The work-RAM refresh after a transfer", "The refresh half a period
out".

    python scripts/run_ram_refresh.py --dma 100,1000 --nops 3,4,5
    python scripts/run_ram_refresh.py --dma 64 --cram --display-on --idle-after 15000
    python scripts/run_ram_refresh.py --idle 10,100,1000
    python scripts/run_ram_refresh.py --vdp 5,50,400
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


DISPLAY = {"on": False}  # --display-on: register 1 $54 for the transfers, not $14
IDLE_AFTER = {"passes": 0}  # --idle-after N: N dbra passes between the transfer and the reads


def rom(kind: str, n: int, nops: int, cram: bool) -> bytes:
    body = rn.UNLOCK + bytes.fromhex("41f900ff0000")  # lea $FF0000,a0
    if kind == "idle":
        spin = bytes.fromhex("343c") + n.to_bytes(2, "big") + bytes.fromhex("51cafffe")
        body += dense(40) + spin
    elif kind == "vdp":
        # The display on, a CRAM read address, then n data-port reads, each held for the
        # read-ahead: move.w #n-1,d2 / L: move.w $C00000,d0 / dbra d2,L.
        body += control(0x8104 | 0x40) + control(0x8C81) + control(0x0000) + control(0x0020)
        reads = bytes.fromhex("343c") + (n - 1).to_bytes(2, "big")
        reads += bytes.fromhex("303900c00000") + bytes.fromhex("51cafff8")
        body += dense(40) + reads
    else:
        source = 0x8000 >> 1
        mode1 = 0x54 if DISPLAY["on"] else 0x14
        for reg, value in ((0x00, 0x04), (0x01, mode1), (0x0C, 0x81), (0x0F, 0x02),
                           (0x13, n & 0xFF), (0x14, n >> 8 & 0xFF), (0x15, source & 0xFF),
                           (0x16, source >> 8 & 0xFF), (0x17, source >> 16 & 0x7F)):  # fmt: skip
            body += control(0x8000 | reg << 8 | value)
        body += control(0xC000 if cram else 0x4000) + dense(40) + control(0x0080)
        if IDLE_AFTER["passes"]:
            passes = IDLE_AFTER["passes"].to_bytes(2, "big")
            body += bytes.fromhex("343c") + passes + bytes.fromhex("51cafffe")
    body += pause(nops) + dense(10) + STOP
    return rn.cartridge(body, size=0x20000)


def one(job: tuple) -> str:
    kind, n, nops, cram, display, idle_after = job
    DISPLAY["on"] = display
    IDLE_AFTER["passes"] = idle_after
    window = 80_000 + n * (400 if kind == "vdp" else 70) + idle_after * 70
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
    elif kind == "vdp":
        port = [e for e in cycles if e.addr == 0xC00000 and before[-1][0] < e.t < after[0][0]]
        ref = port[-1].t + port[-1].length  # the last data-port read's end
        taken = max(t for t, h in before if h > 0)
        chain = held - taken
        what = f"last data-port read's end ({len(port)} reads; {chain} after the last refresh)"
    else:
        lo, hi = before[-1][0], after[0][0]
        ref = max(e.t for e in run.events
                  if e.type == nmd.EV_MEM and e.bus_granted and lo < e.t < hi)  # fmt: skip
        what = "transfer's last select"
    line = (f"{kind} {n:5d} nops {nops}: after the {what}, first read {after[0][0] - ref:7d}, "
            f"first held {held - ref:7d}")  # fmt: skip
    if kind == "dma" and idle_after:
        # Where the held read falls on a chain restarted 238 after the last select.
        periods, into = divmod(held - ref - 238, 931)
        line += f"  (a chain from +238: {periods} periods + {into})"
    return line


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--idle", help="dbra passes, comma-separated")
    group.add_argument("--dma", help="transfer lengths in words, comma-separated")
    group.add_argument("--vdp", help="held data-port reads, comma-separated counts")
    parser.add_argument("--nops", default="0", help="NOPs before the reads resume")
    parser.add_argument("--cram", action="store_true", help="transfer to CRAM, not VRAM")
    parser.add_argument("--display-on", action="store_true", help="the display on for it")
    parser.add_argument("--idle-after", default="0",
                        help="dbra passes after the transfer, comma-separated")  # fmt: skip
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()
    kind = "idle" if args.idle else "vdp" if args.vdp else "dma"
    counts = args.idle or args.vdp or args.dma
    jobs = [(kind, int(n), int(k), args.cram, args.display_on, int(i))
            for n in counts.split(",") for k in args.nops.split(",")
            for i in args.idle_after.split(",")]  # fmt: skip
    with Pool(args.jobs) as pool:
        for line in pool.map(one, jobs):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
