"""Run a cartridge: ``python -m megadrive_python CART --frames N --png out.png
--wav out.wav``."""

from __future__ import annotations

import argparse
import sys
import time
import zipfile
from pathlib import Path

from .cartridge import Cartridge
from .io import BUTTONS
from .machine import MACHINES
from .png import write_png
from .system import System
from .wav import write_wav

IMAGE_SUFFIXES = (".md", ".gen", ".bin", ".smd")


def load_image(path: Path) -> bytes:
    """The cartridge image, out of a zip if that is what was given."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if Path(name).suffix.lower() in IMAGE_SUFFIXES:
                    return archive.read(name)
            raise SystemExit(f"{path}: no cartridge image inside")
    return path.read_bytes()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("cartridge", type=Path)
    parser.add_argument("--machine", default="genesis", choices=sorted(MACHINES))
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--png", type=Path, help="write the last frame here")
    parser.add_argument("--wav", type=Path, help="write the sound here (16-bit stereo)")
    parser.add_argument(
        "--rate", type=int, default=44_100, help="sample rate of --wav (default 44100)"
    )
    parser.add_argument(
        "--no-sound",
        action="store_true",
        help="do not run the YM2612's generator (it dominates the time)",
    )
    parser.add_argument(
        "--press", default="", help="buttons held from --press-at on (e.g. start,a)"
    )
    parser.add_argument("--press-at", type=int, default=0, help="frame to press them at")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    system = System(MACHINES[args.machine], Cartridge(load_image(args.cartridge)))
    system.audio.sample_rate = args.rate
    if args.no_sound:
        system.board.fm.enabled = False
    pad = system.board.io.ports[0].device
    pressed = 0
    for name in (button.strip().lower() for button in args.press.split(",") if button.strip()):
        if name not in BUTTONS:
            raise SystemExit(f"unknown button {name!r}: one of {', '.join(sorted(BUTTONS))}")
        pressed |= BUTTONS[name]
    started = time.perf_counter()
    audio: list[tuple[int, int]] = []
    for frame in range(args.frames):
        if pressed and frame == args.press_at:
            pad.pressed = pressed
        system.run_frame()
        if args.wav:
            audio.extend(system.take_audio())
        else:
            # Keep the sample queues from growing over a long run.
            system.board.fm.take_samples()
            system.board.psg.take_samples()
    seconds = time.perf_counter() - started
    if args.wav:
        write_wav(args.wav, audio, args.rate)
    if args.png:
        write_png(args.png, system.vdp.renderer.frame_rgb())
    if not args.quiet:
        width, height, _rows = system.vdp.renderer.frame()
        print(
            f"{args.frames} frames of {args.machine}, {width}x{height}, "
            f"{system.m68k_instructions:,} 68000 and {system.z80_instructions:,} Z80 "
            f"instructions in {seconds:.2f} s ({args.frames / seconds:.1f} fps, "
            f"{sys.implementation.name} {sys.version.split()[0]})"
            + (f"; {len(audio):,} samples at {args.rate} Hz" if args.wav else "")
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
