#!/usr/bin/env python3
"""Milestone 8: run every cartridge of the local romset on the board and on MAME,
and compare the frame each shows after N emulated seconds.

MAME is a **detector** (docs/validation.md), never a judge: a differing frame is a
question, and the answer is a named cause (``scripts/triage_diffs.py``), not a change
to match it. Adapted from sms-python's ``scripts/compat_sweep.py`` (same author, MIT),
with the traps it found already avoided and two of this board's own added.

Each cartridge is run on MAME's ``genesis`` driver for ``--seconds`` and its exit
snapshot kept; the board runs the same image for the same emulated time and the
first frame it completes at or after that time is compared, pixel for pixel, as
``scripts/compare_mame_frames.py`` does. The frames on either side are scored too
(``--window``): MAME's exit snapshot is the frame it *completes* after the time and
the two machines need not be on the same field of an animation, so a cartridge whose
picture is identical one frame away is a different result from one that is nowhere
near, and the triage treats them differently.

Results go to ``tests/compat/megadrive.csv`` (gitignored):

    name, board, mame-board, status, differ, offset, seconds

with the statuses

    same         the exact frame is identical, pixel for pixel
    phase        a frame within --window of it is identical, the exact one is not
    differs      no frame in the window is identical
    size         the two pictures are not the same size (H32/H40, interlace)
    unsupported  the board does not model this cartridge's board
    mame-board   MAME ran the image on another board (an unlisted image: mame_media)
    lockup       the board raised Lockup (docs/undocumented-behavior.md, the bus)
    missing      a broken symlink in the local romset
    error: ...   anything else, which is a result too

    .venv-pypy/bin/python scripts/compat_sweep.py --seconds 10 --jobs 16
    .venv-pypy/bin/python scripts/compat_sweep.py --match Sonic --window 4 --offsets
"""

from __future__ import annotations

import argparse
import collections
import csv
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(Path(__file__).resolve().parent)]

from mame_media import cart_arguments, mame_board  # noqa: E402

from megadrive_python import vdp as vdp_module  # noqa: E402
from megadrive_python.board import Lockup  # noqa: E402
from megadrive_python.cartridge import Cartridge, load_image  # noqa: E402
from megadrive_python.machine import MACHINES, MCLK_PER_LINE  # noqa: E402
from megadrive_python.png import read_png, write_png  # noqa: E402
from megadrive_python.system import System  # noqa: E402

ROMS = Path("/data/emu/roms/megadrive")
MAME = "/usr/games/mame"
ROMPATH = "/data/emu/source/myrient.erista.me/files/MAME/ROMs (non-merged)"
DRIVER = "genesis"
MACHINE = "genesis"
RESULTS = ROOT / "tests" / "compat"

#: Frames to advance past the first one the board completes at or after the target
#: time before comparing. MAME's ``-seconds_to_run`` finishes the frame in progress
#: and writes its exit snapshot from it; the board's picture is a few frames *behind*
#: MAME's at the same emulated time, because the board charges the 68000 stalls MAME
#: does not model (the two refreshes, the arbiter, the DMA's start-up) and a game's
#: CPU-bound initialisation therefore ends a little later on it. Fitted once over 35
#: cartridges whose picture is identical at some offset and not at all of them, at 5,
#: 10 and 20 emulated seconds: the commonest matching offset is +4, +3 and +4, and it
#: does **not** grow with the length of the run, which is what says it is a one-off
#: shift and not a rate difference (docs/worklog.md, milestone 8). One figure for the
#: whole sweep: a search per cartridge would pick a wrong frame on a mostly uniform
#: picture, which is the trap sms-python's sweep found.
FRAME_OFFSET = 3


def compare(rows: list[list[int]], snapshot: list[list[int]]) -> tuple[int, int]:
    """(differing pixels, total). Different sizes count as wholly different."""
    if len(rows) != len(snapshot) or len(rows[0]) != len(snapshot[0]):
        return len(snapshot) * len(snapshot[0]), len(snapshot) * len(snapshot[0])
    wrong = 0
    for ours, theirs in zip(rows, snapshot, strict=True):
        if ours != theirs:
            wrong += sum(a != b for a, b in zip(ours, theirs, strict=True))
    return wrong, len(rows) * len(rows[0])


#: What ``run_one``'s ``disable`` argument turns off, for the triage's sensitivity
#: pass. None of it needs a change in ``src/``: both refreshes are due at a master
#: clock the board keeps in a field, so a clock they never reach turns them off.
DISABLE = {
    "work_refresh": lambda board: setattr(board, "ram_refresh_due", 1 << 62),
    "cart_refresh": lambda board: setattr(board, "cart_refresh_due", 1 << 62),
    "arbiter": lambda board: setattr(board, "arbiter_enabled", False),
}
#: ``disable="dma_startup"`` is not a board knob either: it takes Kabuto's measured
#: 5.6-cycle DMA start-up back out of the VDP (milestone 5's open item 15, which the
#: user's decision 2 after milestone 5 left for this milestone to judge "if a real
#: game's difference in the sweep points at it"). The constant is a module global the
#: VDP reads as it starts a transfer, so it is patched and restored around the run.
#:
#: ``disable="mame_sram"`` is not a board knob but a cartridge one: it puts the
#: board on MAME's ``rom_sramsafe``, the 64 KB of battery RAM at $200000 its file
#: loader gives every cartridge whose header declares none (docs/mame-oracle.md,
#: trap 10). MAME allocates one byte per address of $200000-$20FFFF and the chip is
#: eight bits wide on the odd byte, so that is 0x8000 bytes of chip; it is live when
#: the ROM ends at or below $200000, which is what ``Cartridge`` does with it.
MAME_FALLBACK_SRAM = 0x8000


def run_one(path: Path, seconds: int, window: int, keep_png: bool, disable: str = "") -> dict:
    row = {"name": path.stem, "board": "", "mame-board": "", "status": "",
           "differ": "", "offset": "", "seconds": ""}  # fmt: skip
    started = time.time()
    startup = vdp_module.DMA_STARTUP_MCLK
    if disable == "dma_startup":
        vdp_module.DMA_STARTUP_MCLK = 0
    try:
        if not path.exists():
            # The romset is symlinks into a local mirror; some point at files the
            # mirror does not have. 841 of this romset's 3,514 cartridge entries.
            row["status"] = "missing"
            return row
        # A mirror being synced can hide a file for a moment: retry, as sms-python does.
        for attempt in range(5):
            try:
                image = load_image(path)
                break
            except FileNotFoundError:
                if attempt == 4:
                    raise
                time.sleep(5)
        cartridge = (
            Cartridge(image, slot="rom_sram", sram_size=MAME_FALLBACK_SRAM)
            if disable == "mame_sram"
            else Cartridge.from_image(image)
        )
        row["board"] = cartridge.slot
        theirs = mame_board(image)
        row["mame-board"] = theirs if theirs != cartridge.slot else ""
        if cartridge.unsupported:
            row["status"] = "unsupported"
            return row
        with tempfile.TemporaryDirectory(dir=RESULTS / "work") as work:
            work_path = Path(work)
            media = cart_arguments(image, work_path, work_path / "cart.md", ROMPATH)
            # No autoboot script: MAME runs one after a soft reset of its own, and
            # every frame comparison this repository has made (milestone 3 on) was
            # made against a run without one. The lockstep's traces have one, so
            # the triage compares against the snapshot of *that* run instead.
            subprocess.run(
                [MAME, DRIVER, *media, "-homepath", work,
                 "-snapshot_directory", str(work_path / "snap"), "-video", "none",
                 "-sound", "none", "-nothrottle", "-noreadconfig", "-skip_gameinfo",
                 "-seconds_to_run", str(seconds)],
                cwd=work, check=True, capture_output=True, timeout=900,
            )  # fmt: skip
            snapshots = sorted((work_path / "snap").glob("*/*.png"))
            if not snapshots:
                row["status"] = "error: no MAME snapshot"
                return row
            snapshot = read_png(snapshots[-1])
            machine = MACHINES[MACHINE]
            system = System(machine, cartridge)
            # The generator is the only thing turned off: the bus, the register
            # file, the timers and BUSY still run, so nothing a game can observe
            # changes (docs/worklog.md, milestone 4). It is what makes the sweep
            # affordable -- 2.9 s of wall time per emulated second, against 0.3.
            system.board.fm.enabled = False
            if disable in DISABLE:
                DISABLE[disable](system.board)
            target = seconds * machine.mclk
            frame_mclk = MCLK_PER_LINE * machine.lines_per_frame
            while system.mclk < target - window * frame_mclk:
                system.run_frame()
            equal: list[int] = []
            exact = -1
            ours = None
            # ``window`` frames each side of the frame actually compared, which
            # FRAME_OFFSET puts that many frames after the target's own.
            for index in range(2 * window + 1 + FRAME_OFFSET):
                system.run_frame()
                picture = system.vdp.renderer.frame_rgb()
                if exact < 0 and system.mclk >= target:
                    exact = index
                if (
                    len(picture) == len(snapshot)
                    and len(picture[0]) == len(snapshot[0])
                    and picture == snapshot
                ):
                    equal.append(index)
                if index == exact + FRAME_OFFSET:
                    ours = picture
            if ours is None:  # the target frame fell outside the window
                row["status"] = "error: target frame outside the window"
                return row
            at = exact + FRAME_OFFSET
            wrong, total = compare(ours, snapshot)
            row["differ"] = f"{wrong}/{total}"
            row["offset"] = " ".join(str(i - at) for i in equal)
            if wrong == 0:
                row["status"] = "same"
            elif len(ours) != len(snapshot) or len(ours[0]) != len(snapshot[0]):
                row["status"] = "size"
                row["differ"] = f"{len(ours[0])}x{len(ours)} vs {len(snapshot[0])}x{len(snapshot)}"
            elif equal:
                row["status"] = "phase"
            else:
                row["status"] = "differs"
            if wrong and row["mame-board"]:
                row["status"] = "mame-board"
            if keep_png and wrong:
                where = RESULTS / "frames"
                where.mkdir(parents=True, exist_ok=True)
                write_png(where / f"{path.stem}.png", ours)
                shutil.copyfile(snapshots[-1], where / f"{path.stem}-mame.png")
    except Lockup as error:
        row["status"], row["differ"] = "lockup", str(error)[:120]
    except subprocess.CalledProcessError as error:
        if row["mame-board"]:
            row["status"], row["differ"] = "mame-board", "MAME failed"
        else:
            row["status"] = f"error: MAME: {error.stderr[-120:]!r}"[:200]
    except Exception as error:  # a crash is a result too
        row["status"] = f"error: {type(error).__name__}: {error}"[:200]
        traceback.print_exc(file=sys.stderr)
    finally:
        vdp_module.DMA_STARTUP_MCLK = startup
        row["seconds"] = f"{time.time() - started:.1f}"
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seconds", type=int, default=10, help="emulated seconds")
    parser.add_argument("--jobs", type=int, default=16)
    parser.add_argument("--window", type=int, default=4, help="frames either side to score")
    parser.add_argument("--limit", type=int, help="only the first N cartridges")
    parser.add_argument("--every", type=int, help="only every Nth cartridge (a sample)")
    parser.add_argument("--match", help="only cartridges whose name contains this")
    parser.add_argument("--out", type=Path, help="default: tests/compat/megadrive.csv")
    parser.add_argument("--frames", action="store_true", help="keep the differing frames")
    parser.add_argument("--offsets", action="store_true",
                        help="print how many cartridges are identical at each offset")  # fmt: skip
    parser.add_argument("--rerun", nargs="?", const="error", metavar="STATUS",
                        help="re-run only the last CSV's rows with this status")  # fmt: skip
    args = parser.parse_args()
    carts = sorted(
        p for p in ROMS.iterdir()
        if p.suffix == ".zip" and not p.name.startswith("[BIOS]")
        and (args.match is None or args.match.lower() in p.name.lower())
    )  # fmt: skip
    if args.every:
        carts = carts[:: args.every]
    if args.limit:
        carts = carts[: args.limit]
    record = args.out or RESULTS / "megadrive.csv"
    record.parent.mkdir(parents=True, exist_ok=True)
    (RESULTS / "work").mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    if args.rerun:
        previous = list(csv.DictReader(record.open(encoding="utf-8")))
        again = {r["name"] for r in previous if r["status"].startswith(args.rerun)}
        rows = [r for r in previous if r["name"] not in again]
        carts = [c for c in carts if c.stem in again]
    started = time.time()
    with ProcessPoolExecutor(args.jobs) as pool:
        futures = {
            pool.submit(run_one, cart, args.seconds, args.window, args.frames): cart
            for cart in carts
        }
        for done, future in enumerate(as_completed(futures), 1):
            row = future.result()
            rows.append(row)
            print(f"[{done}/{len(carts)}] {row['status']:<12} {row['differ']:>14} "
                  f"{row['offset']:>10} {row['name']}", flush=True)  # fmt: skip
    if not rows:
        print("no cartridges")
        return 0
    rows.sort(key=lambda r: r["name"])
    with record.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    counts: collections.Counter[str] = collections.Counter(
        row["status"].split(":")[0] for row in rows
    )
    print(f"{len(rows)} cartridges: "
          + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))  # fmt: skip
    if args.offsets:
        at: collections.Counter[str] = collections.Counter()
        for row in rows:
            for offset in row["offset"].split() or ["none"]:
                at[offset] += 1
        print("cartridges identical at each offset from the exact frame:")
        for offset, count in sorted(at.items(), key=lambda kv: kv[0].rjust(4)):
            print(f"  {offset:>5}  {count}")
    print(f"written to {record}; {time.time() - started:.0f} s, "
          f"{sys.implementation.name} {sys.version.split()[0]}")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
