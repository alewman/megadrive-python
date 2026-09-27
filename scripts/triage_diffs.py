#!/usr/bin/env python3
"""Milestone 8: triage every cartridge whose frame differed in the compatibility sweep.

Adapted from sms-python's ``scripts/triage_diffs.py`` (same author, MIT). A differing
frame is a question; this answers it with a named cause, in two passes over each
cartridge of ``tests/compat/megadrive.csv`` that is not ``same``:

**1. The lockstep** (``--lockstep``). Record MAME's trace of both CPUs for the
sweep's time (``scripts/mame_trace.py``) and replay it against the board
(``scripts/lockstep_mame.py --patch``): every register of both CPUs before every
instruction, every access in kind, address and size, every write in value, and every
read of a device compared with the board's own. The verdict is then

- ``cpu``: the two machines diverge, or a read differs with no named reason. The
  first divergence is reported;
- ``vdp-reads``: the board's VDP status or HV counter answers differently from
  MAME's, so the programs take different branches -- the picture follows from that;
- ``fm-status``: likewise for the YM2612's status byte;
- ``mame-rand``: the only differing reads are the ones MAME answers with
  ``machine().rand()`` (the bits of $A11100 that are not the grant; the Z80's space
  with the bus taken), which the board answers from the 68000's prefetch (CMD);
- ``z80-order``: the only differing reads are of Z80 RAM inside a Z80 instruction,
  where MAME's timeslices put the instruction's write on the other side;
- ``identical``: 10 emulated seconds of both CPUs identical, every read included.
  The difference is then in the picture alone, not in what the program did.

The lockstep turns the refresh and the arbiter off, because MAME models neither
(``lockstep_mame.py``), so it cannot see a difference the board's own bus timing
causes. That is what the second pass is for.

**2. Sensitivity** (``--sensitivity``). Re-run the sweep's own comparison with the
board's work-RAM refresh off, with the cartridge refresh off, with the arbiter off,
with Kabuto's DMA start-up cost out of the VDP, and with the cartridge put on MAME's
``rom_sramsafe`` fallback board (64 KB of battery RAM at $200000, which its *file*
loader gives every cartridge whose header declares none: docs/mame-oracle.md, trap
10), and report which of them changes the frame. The last is the whole question for
an unlisted image, and the one before it is what milestone 5's open item 15 was left
here for -- both answered by making the board do the other thing rather than by
argument. The work-RAM refresh is
the board's weakest timing model -- its rule does not generalise and it models only
the 21-MCLK stretch where Nuked-MD also shows 7 and 35 (docs/undocumented-behavior.md,
open item 5; the user's decision 5 after the timing session asks this milestone to
watch for it) -- so a cartridge whose picture moves when it is turned off is a
cartridge where that model is a candidate cause, and one whose picture does not is
one where it is not.

Traces are deleted after each cartridge. Results: ``tests/compat/megadrive-triage.csv``.

    .venv-pypy/bin/python scripts/triage_diffs.py --sensitivity --jobs 16
    .venv-pypy/bin/python scripts/triage_diffs.py --lockstep --jobs 8 --seconds 10
"""

from __future__ import annotations

import argparse
import collections
import csv
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(Path(__file__).resolve().parent)]

import compat_sweep  # noqa: E402

ROMS = Path("/data/emu/roms/megadrive")
RESULTS = ROOT / "tests" / "compat"
PYTHON = sys.executable
DRIVER = "genesis"

#: The lockstep's named read differences, by the substring its report prints, in the
#: order the verdict prefers them (the first non-zero one names the verdict).
CLASSES = [
    ("vdp-reads", "VDP read replayed"),
    ("fm-status", "YM2612 status replayed"),
    ("io-latch", "I/O data register bit 7"),
    ("z80-order", "Z80 RAM read by the 68000 inside a Z80 instruction"),
    ("z80-word", "Z80 RAM word read"),
    ("mame-rand", "$A11100 read, bits other than the grant"),
    ("mame-rand", "Z80 space read without the bus"),
]


def lockstep(name: str, seconds: int) -> dict:
    """Record MAME's two traces for one cartridge and replay them against the board."""
    row = {"name": name, "verdict": "", "detail": ""}
    cart = ROMS / f"{name}.zip"
    work = Path(tempfile.mkdtemp(prefix="triage-", dir=RESULTS / "work"))
    try:
        trace = subprocess.run(
            [PYTHON, str(ROOT / "scripts" / "mame_trace.py"), DRIVER, str(cart),
             "--seconds", str(seconds), "--out", str(work)],
            capture_output=True, text=True, timeout=7200,
        )  # fmt: skip
        if trace.returncode:
            row["verdict"], row["detail"] = "trace-failed", trace.stderr[-300:]
            return row
        replay = subprocess.run(
            [PYTHON, str(ROOT / "scripts" / "lockstep_mame.py"), str(work),
             "--driver", DRIVER, "--patch"],
            capture_output=True, text=True, timeout=7200,
        )  # fmt: skip
        out = replay.stdout
        if "DIVERGENCE" in out:
            row["verdict"] = "cpu"
            row["detail"] = " | ".join(out[out.index("DIVERGENCE") :].splitlines()[:3])[:300]
            return row
        unexplained = re.search(r"^unexplained differences: (\d+)", out, re.M)
        instructions = re.search(r"^68000: ([\d,]+) instructions identical", out, re.M)
        if unexplained is None or instructions is None:
            row["verdict"] = "lockstep-failed"
            row["detail"] = (replay.stderr or out)[-300:]
            return row
        counts: collections.Counter[str] = collections.Counter()
        for line in out.splitlines():
            found = re.match(r"^\s+([\d,]+)\s\s(.*)$", line)
            if not found:
                continue
            for verdict, text in CLASSES:
                if text in found.group(2):
                    counts[verdict] += int(found.group(1).replace(",", ""))
        row["detail"] = f"{instructions.group(1)} 68000 instructions; " + (
            ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "no read differs"
        )
        if int(unexplained.group(1)):
            row["verdict"] = "cpu"
            row["detail"] = f"{unexplained.group(1)} unexplained; " + row["detail"]
        else:
            row["verdict"] = next(
                (verdict for verdict, _ in CLASSES if counts.get(verdict)), "identical"
            )
    except Exception as error:
        row["verdict"], row["detail"] = "error", f"{type(error).__name__}: {error}"[:300]
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return row


def sensitivity(name: str, seconds: int, window: int) -> dict:
    """Run the sweep's own comparison again with one timing model turned off at a time.

    The frame MAME showed is recorded once more here rather than kept from the sweep,
    so that the two pictures come from the same pair of runs.
    """
    row = {"name": name, "verdict": "", "detail": ""}
    knobs = {
        "work-RAM refresh": "work_refresh",
        "cartridge refresh": "cart_refresh",
        "arbiter": "arbiter",
        "the DMA's start-up": "dma_startup",
        "MAME's fallback SRAM": "mame_sram",
        "the vertical scroll latch": "vscroll_latch",
        "the colour output's delay": "output_delay",
        "the 2-cell column latch": "column_latch",
        "the planes' fetch schedule": "fetch_schedule",
        "the 17-bit command address": "address_16",
    }
    try:
        results = {}
        for label, knob in [("as it ships", ""), *knobs.items()]:
            result = compat_sweep.run_one(
                ROMS / f"{name}.zip", seconds, window, keep_png=False, disable=knob
            )
            results[label] = (result["status"], result["differ"])
        base = results["as it ships"]
        moved = [label for label in knobs if results[label] != base]
        row["verdict"] = ("moves: " + ", ".join(moved)) if moved else "unmoved"
        row["detail"] = "; ".join(f"{k}: {v[0]} {v[1]}" for k, v in results.items())[:400]
    except Exception as error:
        row["verdict"], row["detail"] = "error", f"{type(error).__name__}: {error}"[:300]
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--window", type=int, default=4)
    parser.add_argument("--jobs", type=int, default=16)
    parser.add_argument("--lockstep", action="store_true", help="pass 1")
    parser.add_argument("--sensitivity", action="store_true", help="pass 2")
    parser.add_argument("--status", default="differs,phase,size,mame-board,lockup",
                        help="which sweep statuses to triage")  # fmt: skip
    parser.add_argument("--sweep", type=Path, default=RESULTS / "megadrive.csv")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sample", type=int, metavar="N",
                        help="a spread sample of N of them, not the first N")  # fmt: skip
    args = parser.parse_args()
    if not (args.lockstep or args.sensitivity):
        parser.error("one of --lockstep or --sensitivity")
    wanted = set(args.status.split(","))
    names = [
        row["name"]
        for row in csv.DictReader(args.sweep.open(encoding="utf-8"))
        if row["status"].split(":")[0] in wanted
    ]
    if args.sample and args.sample < len(names):
        # Every kth, so the sample spreads over the romset rather than over its
        # first letter; the CSV is in name order.
        names = names[:: max(1, len(names) // args.sample)][: args.sample]
    if args.limit:
        names = names[: args.limit]
    which = "lockstep" if args.lockstep else "sensitivity"
    out = args.out or RESULTS / f"megadrive-triage-{which}.csv"
    (RESULTS / "work").mkdir(parents=True, exist_ok=True)
    started = time.time()
    rows = []
    with ProcessPoolExecutor(args.jobs) as pool:
        futures = [
            pool.submit(lockstep, name, args.seconds)
            if args.lockstep
            else pool.submit(sensitivity, name, args.seconds, args.window)
            for name in names
        ]
        for done, future in enumerate(as_completed(futures), 1):
            row = future.result()
            rows.append(row)
            print(f"[{done}/{len(names)}] {row['verdict']:<22} {row['name']}", flush=True)
    rows.sort(key=lambda r: (r["verdict"], r["name"]))
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["name", "verdict", "detail"])
        writer.writeheader()
        writer.writerows(rows)
    counts = collections.Counter(row["verdict"] for row in rows)
    for verdict, count in counts.most_common():
        print(f"  {count:>5}  {verdict}")
    print(f"written to {out}; {time.time() - started:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
