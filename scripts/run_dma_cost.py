#!/usr/bin/env python3
"""What a 68000-to-VDP DMA costs on the board, against Kabuto's formulas and the Manual.

Milestone 5's DMA acceptance. Two independent statements are checked:

- **Kabuto, "DMA speed"** (hardware-measured research): "Fast transfers cost
  words*2.4+5.6 68k cycles on top of what the actual 68k instructions cost ... Slow
  transfers cost max(words*2.4+5.6, words*4.7-6) cycles". Fast is a transfer to CRAM,
  to VSRAM, or to VRAM in 128 KB mode; slow is a transfer to VRAM in 64 KB mode.  The
  cost is the hold the VDP puts on the 68000 beyond the bus cycle of the control-port
  write that starts the transfer, which is what the board's ``VDP.hold`` is, so it is
  read straight off.  Kabuto's own measurement is of the blanking case: his fast rate
  is one word per internal cycle less "2 internal cycles 5 times per raster line",
  which is 3,420 / (3,420/16 - 10) = 16.8 MCLK, his 2.4 68000 cycles.  So the display
  is off here, and the script fits a line to the board's costs and prints its slope
  and intercept beside his.
- **The Manual's "DMA transfer capacity" table** (PDF page 44; specification), which
  says how many bytes a DMA moves per raster line with the display on and off. That is
  the same measurement seen per line instead of per word, so the script converts.

    python scripts/run_dma_cost.py
    python scripts/run_dma_cost.py --machine megadrive
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from megadrive_python.cartridge import Cartridge  # noqa: E402
from megadrive_python.machine import M68K_DIVIDER, MACHINES, MCLK_PER_LINE  # noqa: E402
from megadrive_python.system import System  # noqa: E402

#: Each case: a name, the code bits the control word carries, whether it is one of
#: Kabuto's "fast" transfers, and the extra register-1 bits it needs.
CASES = [
    ("VRAM, 64 KB mode", 0x21, False, 0x00),
    ("CRAM", 0x23, True, 0x00),
    ("VSRAM", 0x25, True, 0x00),
    ("VRAM, 128 KB mode", 0x21, True, 0x80),
]
WORD_COUNTS = (1, 2, 4, 8, 16, 32, 64, 128, 256, 512)


def transfer_cost(system: System, code: int, words: int, mode1: int, display: bool,
                  register12: int) -> int:  # fmt: skip
    """The master clocks the VDP holds the 68000 for one transfer of ``words`` words."""
    vdp = system.vdp
    mclk = vdp.mclk + 4 * MCLK_PER_LINE  # well clear of the previous transfer
    vdp.sync(mclk)
    # Register 1 carries mode 5, DMA enable, the display bit and 128 KB mode; it comes
    # first because in mode 4 the VDP ignores writes to registers $0B and above
    # (VDPFIFOTesting's "Register Write Mode4 Mask"), register 12 among them.
    for number, value in (
        (0, 0x04), (1, 0x14 | (0x40 if display else 0) | mode1), (12, register12),
        (15, 0x02), (19, words & 0xFF), (20, (words >> 8) & 0xFF),
        (21, 0x00), (22, 0x00), (23, 0x00),  # source $000000 (the cartridge)
    ):  # fmt: skip
        vdp.write_word(0x04, 0x8000 | (number << 8) | value, mclk)
        mclk += 28
    mclk += 2 * MCLK_PER_LINE  # let an H mode change latch at the next HBlank
    vdp.sync(mclk)
    vdp.write_word(0x04, (code & 3) << 14, mclk)
    mclk += 28
    vdp.write_word(0x04, (code & 0xFC) << 2, mclk)
    return vdp.hold


def fit(points: list[tuple[int, int]]) -> tuple[float, float]:
    """Least squares over (words, 68000 cycles): slope and intercept."""
    n = len(points)
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)
    slope = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    return slope, (sy - slope * sx) / n


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--machine", default="genesis", choices=sorted(MACHINES))
    parser.add_argument("--words", type=int, default=0, help="one extra word count")
    args = parser.parse_args()
    counts = (*WORD_COUNTS, args.words) if args.words else WORD_COUNTS

    for hmode, register12 in (("H32", 0x00), ("H40", 0x81)):
        print(f"\n=== {hmode}, display off (Kabuto's case) ===")
        header = "words".rjust(6) + "".join(f"{n:>10}" for n in counts)
        print(header)
        for name, code, fast, mode1 in CASES:
            system = System(MACHINES[args.machine], Cartridge(bytes(0x10000)))
            system.vdp.rendering = False
            costs = []
            for words in counts:
                held = transfer_cost(system, code, words, mode1, False, register12)
                costs.append(held / M68K_DIVIDER)
            slope, intercept = fit(list(zip(counts, costs, strict=True)))
            kabuto = [
                max(w * 2.4 + 5.6, w * 4.7 - 6) if not fast else w * 2.4 + 5.6 for w in counts
            ]
            print(f"{name:34}" + "".join(f"{c:>10.1f}" for c in costs))
            print(f"{'  Kabuto':34}" + "".join(f"{c:>10.1f}" for c in kabuto))
            print(
                f"  board fit: words x {slope:.2f} + {intercept:.1f}; "
                f"Kabuto: words x {2.4 if fast else 4.7} "
                f"{'+ 5.6' if fast else '- 6'} ({'fast' if fast else 'slow'})"
            )

    print("\n=== bytes moved per raster line (the Manual's DMA transfer capacity) ===")
    print(f"{'case':34}{'H32 display':>14}{'H32 blank':>12}{'H40 display':>14}{'H40 blank':>12}")
    #: The Manual (PDF page 44) tabulates "68000 memory to VRAM" only; a VRAM word
    #: takes two access slots, so its figures are twice the slots a line has.
    manual = {"VRAM, 64 KB mode": (16, 167, 18, 205)}
    for name, code, _fast, mode1 in CASES:
        row = []
        for register12 in (0x00, 0x81):
            for display in (True, False):
                system = System(MACHINES[args.machine], Cartridge(bytes(0x10000)))
                system.vdp.rendering = False
                words = 512
                held = transfer_cost(system, code, words, mode1, display, register12)
                row.append(2 * words * MCLK_PER_LINE / held if held else float("inf"))
        print(f"{name:34}" + "".join(f"{v:>14.1f}" for v in row))
        if name in manual:
            print(f"{'  the Manual, 68000 to VRAM':34}" + "".join(f"{v:>14}" for v in manual[name]))
    print(f"\n{sys.implementation.name} {sys.version.split()[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
