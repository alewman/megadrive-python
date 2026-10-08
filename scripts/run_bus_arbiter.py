#!/usr/bin/env python3
"""Measure the arbiter's delays on the board, and compare them with the hardware.

Milestone 5's first subject is what a Z80 access through its bank window costs each
CPU. Two hardware reports measure it, and this script runs their experiments on the
board (``src/megadrive_python/arbiter.py`` has the model and the quotations):

- **MoD's logic analyser on a Genesis 2** (forum t=1661; hardware-captured). He ran a
  Z80 loop reading the bank window with the 68000 in ``stop #$2700`` -- off the bus --
  and got "2-3" wait states on the Z80; then with the 68000 in an infinite ``jmp
  (a0)`` loop -- "This instruction takes 8 cycles and consists of 2 reads so the 68K
  is constantly on the bus" -- "a total of 4 wait states. The average delay across 16
  accesses appeared to be just shy of 3 cycles", and later "The range is 2-4 Z80
  cycles with an average of a little less than 3 (seemed about 2.93)". On the sound
  drivers' ``pop`` pairs, "The second read from the POP seems to always have at least
  4 wait states and in one case there were 5 wait states. The average across 10 reads
  was 3.5 cycles."
- **Kabuto's averages** ("The bus system"; hardware-measured research): "the Z80's 68k
  bus accesses will be delayed by 2 to 5 cycles. The average Z80 delay is around 3.3
  Z80 cycles, whereas the average 68k delay is around 11 68k cycles."

Each experiment is a small 68000 program (built below, byte by byte) that copies a
small Z80 program into the Z80's RAM, lets the Z80 go, and then holds the 68000 in the
state the experiment wants. The board's own arbiter then runs, and every delay it
charges is recorded.

It also measures what all the board's bus stalls together do to the 68000's rate,
against Kabuto's other number: "After all these slowdowns the 68K seems to be running
at roughly 480 cycles per raster line instead of the theoretical 488+4/7 ones."

    python scripts/run_bus_arbiter.py
    python scripts/run_bus_arbiter.py --accesses 20000
    python scripts/run_bus_arbiter.py --cartridge "/data/emu/roms/megadrive/GAME.zip"
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from megadrive_python.cartridge import Cartridge, load_image  # noqa: E402
from megadrive_python.io import START  # noqa: E402
from megadrive_python.machine import M68K_DIVIDER, MACHINES, MCLK_PER_LINE  # noqa: E402
from megadrive_python.system import System  # noqa: E402

# -- the 68000 programs ---------------------------------------------------------------

#: ``stop #$2700``: the 68000 leaves the bus alone entirely (MoD's first experiment).
M68K_STOP = bytes.fromhex("4e722700")
#: ``lea (0,pc),a0`` (pointing at the next instruction) then ``jmp (a0)``, which is
#: MoD's second experiment verbatim: "an infinite loop using jmp (a0). This instruction
#: takes 8 cycles and consists of 2 reads so the 68K is constantly on the bus".
M68K_SPIN = bytes.fromhex("41fa00024ed0")

# -- the Z80 programs -----------------------------------------------------------------

#: ``ld hl,$8000`` / ``ld a,(hl)`` / ``jp $0003``: one bank read every 17 T-states.
Z80_READ = bytes.fromhex("2100807ec30300")
#: ``ld sp,$8002`` / ``pop hl`` / ``jp $0003``: two bank reads in one machine-cycle
#: pair, which is the case MoD measured on the sound drivers.
Z80_POP = bytes.fromhex("310280e1c30300")
#: ``jr $``: a Z80 that never leaves its own RAM, so the 68000 pays only the refreshes
#: (and the Z80-space clock it never uses here).
Z80_IDLE = bytes.fromhex("18fe")


def _rom(z80_program: bytes, m68k_loop: bytes) -> bytes:
    """A cartridge that loads ``z80_program`` into the Z80's RAM, starts it, and then
    runs ``m68k_loop`` for ever."""
    setup = bytearray()
    setup += bytes.fromhex("33fc010000a11100")  # move.w #$0100,$A11100  request the bus
    # "The Z80 bus can only be accessed by the 68000 when the Z80 is running and the
    # 68000 has the bus" (CMD 2.2), so the reset line is released before the copy.
    setup += bytes.fromhex("33fc010000a11200")  # move.w #$0100,$A11200  release reset
    lea_at = len(setup)
    setup += bytes.fromhex("43fa0000")  # lea (source,pc),a1  -- filled in below
    setup += bytes.fromhex("247c00a00000")  # lea $00A00000,a2
    setup += bytes([0x70, len(z80_program) - 1])  # moveq #n-1,d0
    setup += bytes.fromhex("14d951c8fffc")  # move.b (a1)+,(a2)+ / dbf d0,-4
    setup += bytes.fromhex("33fc000000a11100")  # move.w #$0000,$A11100  release the bus
    setup += m68k_loop  # and there the 68000 stays
    # The Z80's program sits past the loop, where the 68000 never walks into it; a
    # ``lea (d16,pc)`` counts its displacement from its own extension word.
    displacement = len(setup) - (lea_at + 2)
    setup[lea_at + 2 : lea_at + 4] = displacement.to_bytes(2, "big")
    body = bytes(setup) + z80_program
    rom = bytearray(0x400)
    rom[0:4] = (0x00FFFE00).to_bytes(4, "big")  # the initial stack pointer
    rom[4:8] = (0x00000200).to_bytes(4, "big")  # the reset vector
    rom[0x200 : 0x200 + len(body)] = body
    return bytes(rom)


def run(z80_program: bytes, m68k_loop: bytes, accesses: int) -> list[tuple[int, int]]:
    """Run one experiment until ``accesses`` bank accesses have been charged; return
    each one's (Z80 cycles, 68000 cycles)."""
    system = System(MACHINES["genesis"], Cartridge(_rom(z80_program, m68k_loop)))
    board = system.board
    board.vdp.rendering = False
    board.fm.enabled = False
    log: list[tuple[int, int]] = []
    charged = board._bank_delay  # the script watches the board's own model

    def record() -> None:
        before = (board.bank_z80_cycles, board.bank_m68k_cycles)
        charged()
        log.append((board.bank_z80_cycles - before[0], board.bank_m68k_cycles - before[1]))

    board._bank_delay = record  # type: ignore[method-assign]
    while len(log) < accesses and system.m68k_instructions < 4_000_000:
        system.step()
    return log[:accesses]


def report(name: str, log: list[tuple[int, int]], expected: str) -> None:
    if not log:
        print(f"{name}: no bank accesses were made")
        return
    z80 = [entry[0] for entry in log]
    m68k = [entry[1] for entry in log]
    counts = ", ".join(f"{value}: {n}" for value, n in sorted(Counter(z80).items()))
    print(f"{name} ({len(log)} accesses)")
    print(f"  Z80 wait states : {min(z80)}-{max(z80)}, mean {sum(z80) / len(z80):.2f}  ({counts})")
    print(f"  68000 delay     : {min(m68k)}-{max(m68k)} cycles, mean {sum(m68k) / len(m68k):.2f}")
    print(f"  hardware        : {expected}")


def cycles_per_line(cartridge: str | None, machine: str, frames: int,
                    z80: bytes = Z80_READ) -> tuple[float, str]:  # fmt: skip
    """The 68000 clocks a raster line leaves for instructions, after every stall the
    board charges: 3,420 / 7 master clocks a line less the stalls (Kabuto's "roughly
    480 cycles per raster line instead of the theoretical 488+4/7"). Counted from the
    other side: each step's own clocks are its time less its stalls, and whatever of
    the run's time they do not fill was lost -- holds paid inside a step and those owed
    between steps alike (a count of the owed part as its change from step to step
    summed to nothing; docs/worklog.md, "The Z80's grant inside an instruction")."""
    if cartridge is None:
        image, name = _rom(z80, M68K_SPIN), "the jmp (a0) harness"
    else:
        image, name = load_image(cartridge), Path(cartridge).stem
    system = System(MACHINES[machine], Cartridge.from_image(image))
    system.board.fm.enabled = False
    pad = system.board.io.ports[0].device
    own = 0  # the 68000's own clocks, in master clocks
    start = system.mclk
    for frame in range(frames):
        if frame == frames // 4:
            pad.pressed = START
        lines = system.vdp.frame
        while system.vdp.frame == lines:
            elapsed = system.step()
            own += elapsed - system.board.stall
    per_line = own / (system.mclk - start) * MCLK_PER_LINE / M68K_DIVIDER
    return per_line, name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--accesses", type=int, default=2000)
    parser.add_argument("--cartridge", help="measure the 68000's rate on this image")
    parser.add_argument("--machine", default="genesis", choices=sorted(MACHINES))
    parser.add_argument("--frames", type=int, default=240)
    args = parser.parse_args()
    report(
        "68000 stopped, Z80 reading the bank window (MoD's first experiment)",
        run(Z80_READ, M68K_STOP, args.accesses),
        'MoD: "2-3" wait states',
    )
    report(
        "68000 in a jmp (a0) loop, Z80 reading the bank window (MoD's second)",
        run(Z80_READ, M68K_SPIN, args.accesses),
        'MoD: "range is 2-4 Z80 cycles with an average of a little less than 3 '
        '(seemed about 2.93)"; Kabuto: 2-5, mean 3.3 Z80 and about 11 68000 cycles',
    )
    report(
        "68000 in a jmp (a0) loop, Z80 popping from the bank window (MoD's drivers)",
        run(Z80_POP, M68K_SPIN, args.accesses),
        'MoD: "the second read from the POP seems to always have at least 4 wait '
        'states ... The average across 10 reads was 3.5 cycles"',
    )
    for z80, what in ((Z80_IDLE, "the Z80 in its own RAM"),
                      (Z80_READ, "the Z80 reading the bank window")):  # fmt: skip
        per_line, name = cycles_per_line(args.cartridge, args.machine, args.frames, z80)
        if args.cartridge:
            what = "the cartridge's own Z80 program"
        print(f"the 68000's rate on {name}, {what} ({args.frames} frames): {per_line:.1f} "
              f"clocks a raster line for instructions, of 488.6")  # fmt: skip
        if args.cartridge:
            break
    print('  hardware        : Kabuto, "roughly 480 cycles per raster line instead of '
          'the theoretical 488+4/7 ones" (the refreshes; the Z80 off the bus)')  # fmt: skip
    print(f"{sys.implementation.name} {sys.version.split()[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
