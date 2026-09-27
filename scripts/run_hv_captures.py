#!/usr/bin/env python3
"""Run Nemesis's HV/status capture ROMs on the board and compare every sample.

Nemesis's captures (2010; forum t=768; ``tests/test_roms/nemesis/sr logic analyser
{results,roms,source}.zip``) come from ROMs whose 68000 reads the VDP in an unbroken
run of 15,360 ``MOVE.L (A0),(A1)+`` and stores what it read in work RAM: the "hv" ROMs
read $C00008 twice (two HV samples 4 clocks apart), the "sr" ROMs read $C00006 and
$C00008 (the status register, then the HV counter). He sent the RAM back from a PAL
Model 1 and an NTSC-J Model 1: 288 dumps over H32/H40, V28/V30, no interlace and three
interlace settings. They are the hardware judge of the VDP's counters and flags, and,
because the sampling loop's timing is the 68000's, of the board's bus timing as well.

For each dump the board runs the ROM from power-on (with the C button held, which the
ROM's wait loop polls) up to the first sampling instruction. What is not the board's
to know is then set from the dump: where in its line the VDP is (the H/V position of
the first sample, and the sub-pixel phase), whether the H40 mode written just before
the loop has been latched yet, and the phases of the two refreshes that slow the 68000
(``Board.cart_refresh_due``, ``Board.ram_refresh_due``). Those are fitted on the first
samples by a fast model of the loop that uses the board's own tables and rules; then
the whole board runs the 15,360 instructions from that state and its RAM is compared,
word for word, with the dump.

    python scripts/run_hv_captures.py                 # every dump, 16 processes
    python scripts/run_hv_captures.py --only "HV noint PAL H32V28"
"""

from __future__ import annotations

import argparse
import collections
import copy
import json
import multiprocessing
import os
import re
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from megadrive_python.board import (  # noqa: E402
    CART_REFRESH_MCLK,
    CART_REFRESH_STALL,
    RAM_REFRESH_AFTER_MCLK,
    RAM_REFRESH_STALL,
)
from megadrive_python.cartridge import Cartridge  # noqa: E402
from megadrive_python.io import C  # noqa: E402
from megadrive_python.machine import MACHINES, MCLK_PER_LINE  # noqa: E402
from megadrive_python.system import System  # noqa: E402
from megadrive_python.vdp import (  # noqa: E402
    H32,
    H40,
    HV_SAMPLE_DELAY,
    V28_NTSC,
    V28_PAL,
    V30_NTSC,
    V30_PAL,
    hv_word,
)

NEMESIS = ROOT / "tests" / "test_roms" / "nemesis"
SAMPLES = 0xF000 // 2  # words of RAM the ROM fills ($FF0000-$FFEFFF)


def load_archives() -> tuple[dict[str, bytes], dict[str, list[tuple[int, bytes]]]]:
    """The capture ROMs by name, and the dumps grouped by capture."""
    with zipfile.ZipFile(NEMESIS / "sr logic analyser roms.zip") as archive:
        roms = {name: archive.read(name) for name in archive.namelist()}
    dumps: dict[str, list[tuple[int, bytes]]] = collections.defaultdict(list)
    with zipfile.ZipFile(NEMESIS / "sr logic analyser results.zip") as archive:
        for name in archive.namelist():
            match = re.match(r"output - (.*) - (\d+)\.bin$", name)
            if match:
                dumps[match.group(1)].append((int(match.group(2)), archive.read(name)))
    return roms, dumps


def rom_for(capture: str) -> str:
    """'HV noint PAL H32V28' -> the hv ROM; 'SR noint ...' and 'HV noint intXXX ...' ->
    the sr ROMs (their names say HV, but they sample the status register then the HV
    counter: every such ROM loads A0 with $C00006)."""
    words = capture.split()
    mode = words[-1]
    interlace = [w for w in words if w.startswith("int")]
    if words[0] == "HV" and not interlace:
        return f"hv logic analyser noint - {mode}.bin"
    suffix = f" {interlace[0]}" if interlace else ""
    return f"sr logic analyser noint{suffix} - {mode}.bin"


def machine_for(capture: str):
    return MACHINES["megadrive" if "PAL" in capture else "megadrive-japan"]


class Loop:
    """Where the sampling loop is in a ROM."""

    def __init__(self, rom: bytes) -> None:
        run = bytes.fromhex("22D0") * 64
        self.first = rom.find(run)
        end = self.first
        while rom[end : end + 2] == b"\x22\xd0":
            end += 2
        self.end = end
        self.status_first = rom.find(bytes.fromhex("207C00C00006")) >= 0


def boot(machine, rom: bytes, loop: Loop) -> System:
    """Power on with C held and run to the first sampling instruction."""
    system = System(machine, Cartridge(rom))
    system.board.io.ports[0].device.pressed = C
    cpu = system.m68k
    for _ in range(20_000_000):
        if loop.first == cpu.PC:
            return system
        system.step()
    raise RuntimeError("the capture ROM never reached its sampling loop")


def later_run(first: System) -> System:
    """The state a later run starts in. The ROM runs the loop, sends the RAM, waits for
    C and jumps back to the loop (source lines 16420-16437), so every run after the
    first starts with the registers the first one wrote and with the F flag set by the
    VBlanks the previous run and the transfer passed (the first run's register writes,
    made in mode 4, cleared it: VDP._control_write). What is not the board's to know,
    where the VDP is, is fitted as for the first run."""
    system = copy.deepcopy(first)
    system.vdp.f_flag = True
    system.vdp._latch_vmode()  # V30 and interlace, latched at those VBlanks
    return system


# -- the fast model of the sampling loop, used only to fit the phases -------------------


def sample_times(start: int, count: int, cart_due: int, ram_due: int) -> list[int]:
    """The MCLK at which each of ``count`` instructions reads its two samples, under the
    board's refresh rules (Board._refresh): accesses at clocks 0, 4 (reads), 8, 12 (work
    RAM writes) and 16 (the ROM prefetch) of a 20-clock instruction."""
    times = []
    t = start
    for _ in range(count):
        for kind in range(5):
            if t >= cart_due:
                cart_due += ((t - cart_due) // CART_REFRESH_MCLK + 1) * CART_REFRESH_MCLK
                if kind == 4:
                    t += CART_REFRESH_STALL
            if kind in (2, 3) and t >= ram_due:
                t += RAM_REFRESH_STALL
                ram_due = t + RAM_REFRESH_AFTER_MCLK
            if kind < 2:
                times.append(t)
            t += 28
    return times


class Counters:
    """The VDP's H/V counters as a function of time, from its own tables, sampled as a
    port read samples them (``VDP.sample_point``)."""

    def __init__(self, hmode, vmode, v: int, line_start: int, latch_at: int | None,
                 new_hmode, new_vmode=None, lsm=(False, False), new_lsm=None,
                 odd: bool = False) -> None:  # fmt: skip
        self.hmode = hmode
        self.vmode = vmode
        self.new_vmode = new_vmode or vmode  # latched at VBlank, as the VDP does
        self.lsm = lsm  # (interlace, double), latched at VBlank too
        self.new_lsm = new_lsm or lsm
        self.odd = odd  # toggled on the VBlank line in interlace (at line start here)
        self.hmode = _timing(hmode, vmode, v, lsm[0], odd)
        self.v = v
        self.line_start = line_start
        self.latch_at = latch_at  # when the pending H mode takes over (at HBlank)
        self.new_hmode = new_hmode

    def hv(self, t: int) -> tuple[int, int]:
        t += HV_SAMPLE_DELAY
        on_edge = False
        while True:
            if self.latch_at is not None:
                latch = self.line_start + self.hmode.offset_of[self.hmode.hblank_set]
                if latch < self.line_start:
                    latch += MCLK_PER_LINE
                if t >= latch and latch >= self.latch_at:
                    h = self.hmode.hblank_set
                    self.hmode = _timing(self.new_hmode, self.vmode, self.v, self.lsm[0], self.odd)
                    self.line_start = latch - self.hmode.offset_of[h]
                    self.latch_at = None
                    continue
            if t >= self.line_start + self.hmode.length:
                self.line_start += self.hmode.length
                self.v = self.vmode.next(self.v, self.lsm[0], self.odd)
                if self.v == self.vmode.vblank_set:
                    self.vmode = self.new_vmode
                    self.lsm = self.new_lsm
                    if self.lsm[0]:
                        self.odd = not self.odd
                self.hmode = _timing(self.hmode, self.vmode, self.v, self.lsm[0], self.odd)
                continue
            if not on_edge:
                t = self.line_start + self.hmode.next_edge(t - self.line_start)
                on_edge = True
                continue
            break
        mode = self.hmode
        return self.v, mode.hvals[mode.position(t - self.line_start)]

    def word(self, t: int) -> int:
        """The HV counter as the port gives it at a read at ``t``."""
        v, h = self.hv(t)
        return hv_word(v, h, *self.lsm)


def _timing(mode, vmode, v: int, interlace: bool = False, odd: bool = False):
    """The H mode table line ``v`` runs on."""
    if mode.name != "H40":
        return mode
    return vmode.h40_line(v, interlace, odd)


def _in_sequence(vmode, v: int) -> bool:
    """Whether the V counter sequence of ``vmode`` has ``v`` (an odd interlaced field
    adds ``jump`` - 1)."""
    return v <= vmode.last or (vmode.jump - 1 <= v <= 0x1FF)


def _h_table(mode) -> list[int]:
    """The H counter a read sees (as the port gives it, H >> 1), by the MCLK offset of
    the read's bus cycle into the line."""
    table = []
    for x in range(MCLK_PER_LINE):
        edge = (
            mode.next_edge(x + HV_SAMPLE_DELAY)
            if x + HV_SAMPLE_DELAY < MCLK_PER_LINE
            else MCLK_PER_LINE + mode.next_edge(x + HV_SAMPLE_DELAY - MCLK_PER_LINE)
        )
        index = mode.position(edge % MCLK_PER_LINE)
        table.append((mode.hvals[index] >> 1) & 0xFF)
    return table


class _HPlan:
    """The H counter a read sees against the offset of its bus cycle from the first
    sample's line start, for one H mode history: ``old`` throughout, or ``old`` until
    its first HBlank at or after ``pos`` and then ``new`` (a pending mode write)."""

    def __init__(self, old, new, pending: bool, pos: int, tables: dict) -> None:
        self.old = tables[old.name]
        self.new = tables[new.name]
        self.pending = pending
        self.latch = None
        self.shift = 0
        if pending:
            hblank = old.offset_of[old.hblank_set]
            self.latch = hblank if hblank >= pos else hblank + MCLK_PER_LINE
            self.shift = self.latch - new.offset_of[old.hblank_set]

    def h(self, x: int) -> int:
        if self.latch is None or x < self.latch:
            return self.old[x % MCLK_PER_LINE]
        return self.new[(x - self.shift) % MCLK_PER_LINE]


#: Fitting stages: H samples compared, and how many more mismatches than the best a
#: candidate may have and still go on (hardware reads catch a counter mid-change now
#: and then, so the best is not always a perfect score).
_STAGES = ((40, 3), (300, 8))
#: Survivors of the stages scored in full (H and V, every V mode) over this many
#: instructions; the best is the fit.
_FINALISTS, _FULL = 12, 2400


def fit(capture: str, dump: bytes, system: System, loop: Loop) -> list[dict]:
    """Fit the VDP position and the two refresh phases to the dump.

    Every phase pair is tried (the refreshes, 7-MCLK steps over their periods) with
    every position the first sample allows, by the H counter alone, keeping only what
    stays within a small mismatch allowance over a growing number of samples; the best
    survivors are then scored on H and V with each V mode and 9-bit V. Returns the
    placements that score best (the 8-bit V the port shows can leave the ninth bit, and
    so VBlank, undecided: run_dump runs each)."""
    words = [int.from_bytes(dump[i : i + 2], "big") for i in range(0, SAMPLES * 2, 2)]
    step = 2 if loop.status_first else 1
    hv_index = list(range(1 if loop.status_first else 0, SAMPLES, step))
    vdp = system.vdp
    t0 = system.mclk
    new_hmode = H40 if vdp.registers[12] & 0x81 == 0x81 else H32
    pal = system.machine.pal
    written = (
        (V30_PAL if pal else V30_NTSC)
        if vdp.registers[1] & 0x08
        else (V28_PAL if pal else V28_NTSC)
    )
    tables = {mode.name: _h_table(mode) for mode in (H32, H40)}
    histories = [(vdp.hmode, new_hmode, new_hmode.name != vdp.hmode.name)]
    if new_hmode.name != vdp.hmode.name:
        histories.append((new_hmode, new_hmode, False))

    def candidates(anchor: int, spread: int) -> list:
        """(history, pos, plan) for every pos (the first sample's bus cycle offset into
        its line, times[0] - line start) that puts HV sample ``anchor`` within
        ``spread`` of what it read; its offset from the first sample is taken as the
        loop's without refresh (20 clocks an instruction, the second read 4 in)."""
        index = hv_index[anchor]
        dt = (index // 2) * 140 + (index % 2) * 28
        seen = words[index] & 0xFF
        near = {(seen + d) & 0xFF for d in range(-spread, spread + 1)}
        return [((old, new, pending), pos, _HPlan(old, new, pending, pos, tables))
                for old, new, pending in histories for pos in range(MCLK_PER_LINE)
                if tables[old.name][(pos + dt) % MCLK_PER_LINE] in near]  # fmt: skip

    observed = [words[i] & 0xFF for i in hv_index]

    def differs(predicted: int, seen: int, tolerant: bool) -> bool:
        if predicted == seen:
            return False
        # In tolerant mode a read that looks like a counter caught changing (low bits
        # cleared, or the value before) does not count against a placement.
        return not (
            tolerant
            and (
                (seen & ~predicted == 0 and predicted ^ seen < 0x40)
                or seen == (predicted - 1) & 0xFF
            )
        )

    def survivors(pool, n, slack, tolerant):
        results = []
        best = n
        for ram, cart, plans in pool:
            times = sample_times(t0, (n * step) // 2 + 2, t0 + cart, t0 + ram)
            base = times[0]
            alive = [(plan, 0) for plan in plans]
            for k, index in enumerate(hv_index[:n]):
                d = times[index] - base
                want = observed[k]
                limit = min(best + slack, 3 + k // 6)
                alive = [(plan, bad + differs(plan[2].h(d + plan[1]), want, tolerant))
                         for plan, bad in alive]  # fmt: skip
                alive = [(plan, bad) for plan, bad in alive if bad <= limit]
                if not alive:
                    break
            if alive:
                low = min(bad for _, bad in alive)
                best = min(best, low)
                alive.sort(key=lambda item: item[1])
                close = [plan for plan, bad in alive if bad <= low + slack]
                results.append((ram, cart, close, low))
        return [item for item in results if item[3] <= best + slack]

    # Anchored on the first sample, strictly; if nothing survives (a first read caught
    # mid-change, or a dump whose reads catch the counters changing far more often
    # than the unit's rules say), tolerating such reads, then anchored on the second.
    kept = []
    for anchor, spread in ((0, 1), (1, 3)):
        plans = candidates(anchor, spread)
        for tolerant in (False, True):
            kept = [(ram, cart, plans, 0) for ram in range(0, 875, 7)
                    for cart in range(0, CART_REFRESH_MCLK, 7)]  # fmt: skip
            for n, slack in _STAGES:
                kept = survivors([(r, c, p) for r, c, p, _ in kept], n, slack, tolerant)
                if not kept:
                    break
            if kept:
                break
        if kept:
            break
    else:
        raise RuntimeError(f"{capture}: no refresh phases and position fit the dump")
    finalists = sorted(
        ((bad, ram, cart, plan) for ram, cart, plans, bad in kept for plan in plans),
        key=lambda item: item[0],
    )[:_FINALISTS]  # fmt: skip

    lsm_now = (vdp.interlace, vdp.interlace_double)
    lsm_new = (bool(vdp.registers[12] & 0x02), (vdp.registers[12] >> 1) & 3 == 3)

    def score(ram, cart, plan, vmode, v9, odd) -> int:
        (old, new, pending), pos, _ = plan
        times = sample_times(t0, _FULL, t0 + cart, t0 + ram)
        counters = Counters(old, vmode, v9, times[0] - pos, t0 if pending else None, new,
                            written, lsm_now, lsm_new, odd)  # fmt: skip
        total = 0
        for index in hv_index:
            if index >= len(times):
                break
            total += counters.word(times[index]) == words[index]
        return total

    first_v = words[hv_index[0]] >> 8
    vmodes = (V28_PAL, V30_PAL) if pal else (V28_NTSC, V30_NTSC)
    # The V counter at the first sample's line: the first HV sample shows it, or the
    # next line's when the sample is read after the line changed; the port does not
    # show all nine bits, so every V that could be is tried, with either ODD flag.
    odds = (False, True) if lsm_now[0] or lsm_new[0] else (False,)
    starts = {(vmode, odd): [v for v in range(0x200) if _in_sequence(vmode, v) and first_v in (
        hv_word(v, 0, *lsm_now) >> 8,
        hv_word(vmode.next(v, lsm_now[0], odd), 0, *lsm_now) >> 8)]
        for vmode in vmodes for odd in odds}  # fmt: skip
    fits = [(score(ram, cart, plan, vmode, v9, odd), ram, cart, plan, vmode, v9, odd)
            for _, ram, cart, plan in finalists for (vmode, odd), v9s in starts.items()
            for v9 in v9s]  # fmt: skip
    fits.sort(key=lambda item: -item[0])
    results = []
    for long_score, ram, cart, plan, vmode, v9, odd in fits:
        if long_score < fits[0][0] or len(results) == 8:
            break
        (old, _, pending), pos, _ = plan
        results.append({"hmode": old.name, "pending": pending, "vmode": vmode, "v9": v9,
                        "odd": odd,
                        "pos": pos, "ram_step": ram, "cart_step": cart,
                        "fit_first": len(kept), "fit_long": long_score})  # fmt: skip
    return results


def run_dump(capture: str, number: int, dump: bytes, rom: bytes, booted: System,
             loop: Loop) -> dict:  # fmt: skip
    """Fit, then run the whole board from each best placement and compare every word;
    the placement the board agrees with best is reported."""
    started = time.perf_counter()
    try:
        placements = fit(capture, dump, booted, loop)
    except RuntimeError as error:
        return {"capture": capture, "number": number, "params": None, "samples": SAMPLES,
                "differ": SAMPLES, "kinds": {"not fitted": 1}, "examples": [],
                "seconds": time.perf_counter() - started, "error": str(error)}  # fmt: skip
    words = [int.from_bytes(dump[i : i + 2], "big") for i in range(0, SAMPLES * 2, 2)]
    best = None
    for params in placements:
        board_words, phases = run_placed(booted, loop, params)
        differ = [i for i in range(SAMPLES) if board_words[i] != words[i]]
        if best is None or len(differ) < len(best[1]):
            best = (params, differ, board_words, phases)
    params, differ, board_words, phases = best
    kinds = collections.Counter(
        classify(words[i], board_words[i], loop.status_first and i % 2 == 0,
                 phases[i] if i < len(phases) else "")
        for i in differ
    )  # fmt: skip
    return {
        "capture": capture, "number": number, "params": params,
        "samples": SAMPLES, "differ": len(differ), "kinds": dict(kinds),
        "examples": [(i, words[i], board_words[i]) for i in differ[:8]],
        "seconds": time.perf_counter() - started,
    }  # fmt: skip


def run_placed(booted: System, loop: Loop, params: dict) -> tuple[list[int], list[str]]:
    """Run the sampling loop on a copy of ``booted`` with the VDP and the refreshes
    placed as ``params`` says; the words it leaves in RAM, and the phase each read
    sampled at (``VDP.sample_point``: "edge", "next" or "")."""
    system = copy.deepcopy(booted)
    board, vdp = system.board, system.vdp
    phases: list[str] = []
    sample_point = vdp.sample_point

    def logged(mclk: int) -> str:
        phase = sample_point(mclk)
        phases.append(phase)
        return phase

    vdp.sample_point = logged
    t0 = system.mclk
    board.cart_refresh_due = t0 + params["cart_step"]
    board.ram_refresh_due = t0 + params["ram_step"]
    # The first sample is read at the loop's first access, t0 (the instruction's first
    # bus cycle), ``pos`` MCLK into its line.
    vdp.mclk = t0
    if not params["pending"]:
        vdp.hmode = H40 if params["hmode"] == "H40" else H32
    vdp.vmode = params["vmode"]
    vdp.v = params["v9"]
    vdp.odd = params["odd"]
    vdp.hmode = _timing(vdp.hmode, vdp.vmode, vdp.v, vdp.interlace, vdp.odd)
    vdp.line_start = t0 - params["pos"]
    # The border as a VDP arriving at this line in an ordinary frame has it: on from the
    # blanking line to the end of the frame (VDP.border).
    vdp.border = vdp.vmode.vblank_set <= vdp.v
    vdp._border_started = vdp.v == vdp.vmode.vblank_set
    vdp._vint_done = vdp._odd_done = False
    vdp._line_events()
    if not params["pending"]:
        vdp.hblank_at = -1 if vdp.hblank_at < t0 else vdp.hblank_at
    cpu = system.m68k
    while loop.end != cpu.PC:
        system.step()
    ram = board.ram
    return [(ram[i] << 8) | ram[i + 1] for i in range(0, SAMPLES * 2, 2)], phases


def _ripple(board: int, observed: int) -> bool:
    """Whether ``observed`` is a counter caught changing between ``board`` and the value
    next to it: the bits that clear have cleared and those that set have not, or the
    other way round."""
    for other in ((board + 1) & 0xFF, (board - 1) & 0xFF):
        if observed in (board & other, board | other):
            return True
    return False


def classify(observed: int, board: int, status: bool, phase: str = "") -> str:
    """What kind of difference a sample is. A read at a phase where it catches a
    counter changing (``HVUnit``) is counted as such: the hardware's own reads vary
    there."""
    if phase:
        return ("status" if status else "HV") + " read at the " + phase + " phase"
    if status:
        diff = observed ^ board
        return f"status bits {diff:04X}"
    ov, oh, bv, bh = observed >> 8, observed & 0xFF, board >> 8, board & 0xFF
    if ov == bv and _ripple(bh, oh):
        return "H read mid-change"
    if oh == bh and _ripple(bv, ov):
        return "V read mid-change"
    if ov == bv and abs(oh - bh) == 1:
        return "H off by one"
    if oh == bh and abs(ov - bv) == 1:
        return "V off by one"
    return "other"


def work(job: tuple[str, list[int]]) -> list[dict]:
    capture, numbers = job
    roms, dumps = load_archives()
    rom = roms[rom_for(capture)]
    loop = Loop(rom)
    first = boot(machine_for(capture), rom, loop)
    results = []
    for number, dump in sorted(dumps[capture]):
        if numbers and number not in numbers:
            continue
        booted = first if number == 1 else later_run(first)
        results.append(run_dump(capture, number, dump, rom, booted, loop))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", help="one capture, e.g. 'HV noint PAL H32V28'")
    parser.add_argument("--dumps", default="", help="dump numbers, e.g. 1,2")
    parser.add_argument("--processes", type=int, default=16)
    parser.add_argument("--verbose", action="store_true", help="a line per dump")
    parser.add_argument("--json", type=Path, help="write every dump's result here")
    args = parser.parse_args()
    _, dumps = load_archives()
    captures = sorted(dumps) if not args.only else [args.only]
    numbers = [int(n) for n in args.dumps.split(",") if n]
    started = time.perf_counter()
    with multiprocessing.Pool(min(args.processes, len(captures))) as pool:
        all_results = [r for rs in pool.map(work, [(c, numbers) for c in captures]) for r in rs]
    if args.json:
        args.json.write_text(json.dumps(all_results, indent=1, default=str))
    by_capture: dict[str, list[dict]] = collections.defaultdict(list)
    for r in all_results:
        by_capture[r["capture"]].append(r)
        if args.verbose:
            p = r["params"]
            if p is None:
                print(f"{r['capture']:36} {r['number']}: not fitted")
                continue
            print(f"{r['capture']:36} {r['number']}: {r['samples'] - r['differ']:>6}/"
                  f"{r['samples']}  {p['hmode']}{'*' if p['pending'] else ''} pos {p['pos']}"
                  f" ram {p['ram_step']} cart {p['cart_step']}  {r['kinds']}")  # fmt: skip
    print(f"{'capture':36} {'dumps':>5} {'words equal':>17} {'at a step':>9}  other differences")
    totals = collections.Counter()
    for capture, results in by_capture.items():
        kinds: collections.Counter = collections.Counter()
        for r in results:
            kinds.update(r["kinds"])
        samples = sum(r["samples"] for r in results)
        differ = sum(r["differ"] for r in results)
        stepping = sum(n for k, n in kinds.items() if "phase" in k)
        other = {k: n for k, n in kinds.items() if "phase" not in k}
        totals.update(samples=samples, differ=differ, stepping=stepping)
        print(f"{capture:36} {len(results):>5} {samples - differ:>8,}/{samples:,}"
              f" {stepping:>9,}  {other or ''}")  # fmt: skip
    total, wrong = totals["samples"], totals["differ"]
    print(f"{len(all_results)} dumps, {total - wrong:,} of {total:,} words equal; of the "
          f"{wrong:,} others {totals['stepping']:,} are reads at a phase where a counter "
          f"steps; {time.perf_counter() - started:.0f} s, {sys.implementation.name} "
          f"{sys.version.split()[0]}, {os.cpu_count()} CPUs")  # fmt: skip
    return 0 if wrong == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
