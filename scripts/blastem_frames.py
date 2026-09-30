"""BlastEm's picture of chosen frames, through its libretro core: a reference to set the
board's frames against (BlastEm is a **detector**, docs/validation.md).

A minimal libretro frontend of our own (ctypes): it loads the core that
``validation/build_blastem_libretro.sh`` builds from the pinned tree, runs the
cartridge with no input, and writes each wanted frame as ``fNNNNN.png`` -- the
numbering the board's tools use, counted from the first frame the core presents.
BlastEm's frame is 320x240 with an 8-line top border: board row y is its row y + 8
(``scripts/compare_frames.py`` knows). ``--states`` also saves the core's serialized
state at those frames, where ``vdp_serialize`` (``vdp.c``) writes VRAM, CRAM, VSRAM,
the sprite cache and the registers in that order -- the way BlastEm's own sprite cache
was read in docs/worklog.md, "The rain".

Environment calls are answered only as far as the core needs: the save and system
directories point at ``--work`` (so no SRAM is read from or written to ~/.local), the
pixel format is accepted, and everything else is refused so the core uses its defaults
(its region is the cartridge header's, RAM starts zeroed).

    python scripts/blastem_frames.py CORE ROM OUT --frames 4600,4625 [--states]
    python scripts/blastem_frames.py CORE ROM OUT --range 4100:5200:25
    python scripts/blastem_frames.py CORE ROM OUT --range 6980:7300:1 --reset-frame 25

``--reset-frame N`` presses the console's reset button before frame N, as the referee's
Overdrive 2 run does (docs/worklog.md, Session 13). BlastEm's soft reset lands at a
random point in the frame that follows (``genesis.c`` ``soft_reset``: ``rand()``), so the
C library's generator is seeded first (``--seed``) and the run repeats exactly; the
reset is placed to the frame, not to the clock.
"""

from __future__ import annotations

import argparse
import ctypes as C
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from megadrive_python.png import write_png


class GameInfo(C.Structure):
    _fields_ = [("path", C.c_char_p), ("data", C.c_void_p), ("size", C.c_size_t),
                ("meta", C.c_char_p)]  # fmt: skip


ENVIRONMENT = C.CFUNCTYPE(C.c_bool, C.c_uint, C.c_void_p)
VIDEO = C.CFUNCTYPE(None, C.c_void_p, C.c_uint, C.c_uint, C.c_size_t)
SAMPLE = C.CFUNCTYPE(None, C.c_int16, C.c_int16)
BATCH = C.CFUNCTYPE(C.c_size_t, C.c_void_p, C.c_size_t)
POLL = C.CFUNCTYPE(None)
INPUT = C.CFUNCTYPE(C.c_int16, C.c_uint, C.c_uint, C.c_uint, C.c_uint)

GET_SYSTEM_DIRECTORY, GET_SAVE_DIRECTORY, SET_PIXEL_FORMAT, GET_CAN_DUPE = 9, 31, 10, 3


def run(core_path: Path, rom: Path, out: Path, wanted: set[int], states: bool,
        work: Path, reset_frame: int = 0, seed: int = 1) -> int:  # fmt: skip
    out.mkdir(parents=True, exist_ok=True)
    core = C.CDLL(str(core_path))
    directory = C.c_char_p(str(work).encode())
    state = {"frame": 0, "format": None}

    @ENVIRONMENT
    def environment(command, data):
        command &= 0xFFFF  # the EXPERIMENTAL / PRIVATE flags
        if command in (GET_SYSTEM_DIRECTORY, GET_SAVE_DIRECTORY):
            C.cast(data, C.POINTER(C.c_char_p))[0] = directory.value
            return True
        if command == SET_PIXEL_FORMAT:
            state["format"] = C.cast(data, C.POINTER(C.c_uint))[0]
            return True
        if command == GET_CAN_DUPE:
            C.cast(data, C.POINTER(C.c_bool))[0] = True
            return True
        return False

    @VIDEO
    def video(data, width, height, pitch):
        state["frame"] += 1
        if not data or state["frame"] not in wanted:
            return
        if state["format"] != 1:  # RETRO_PIXEL_FORMAT_XRGB8888
            raise SystemExit(f"unexpected pixel format {state['format']}")
        raw = C.string_at(data, pitch * height)
        rows = []
        for y in range(height):
            line = raw[y * pitch : y * pitch + width * 4]
            rows.append([int.from_bytes(line[x : x + 4], "little") & 0xFFFFFF
                         for x in range(0, width * 4, 4)])  # fmt: skip
        write_png(out / f"f{state['frame']:05d}.png", rows)

    callbacks = (environment, video, SAMPLE(lambda left, right: None),
                 BATCH(lambda data, frames: frames), POLL(lambda: None),
                 INPUT(lambda port, device, index, ident: 0))  # fmt: skip
    core.retro_set_environment(callbacks[0])
    core.retro_set_video_refresh(callbacks[1])
    core.retro_set_audio_sample(callbacks[2])
    core.retro_set_audio_sample_batch(callbacks[3])
    core.retro_set_input_poll(callbacks[4])
    core.retro_set_input_state(callbacks[5])
    core.retro_init()
    image = rom.read_bytes()
    buffer = C.create_string_buffer(image, len(image))
    info = GameInfo(str(rom).encode(), C.cast(buffer, C.c_void_p), len(image), None)
    if not core.retro_load_game(C.byref(info)):
        raise SystemExit("the core refused the cartridge")
    core.retro_serialize_size.restype = C.c_size_t
    core.retro_serialize.restype = C.c_bool
    last = max(wanted)
    while state["frame"] < last:
        if reset_frame and state["frame"] == reset_frame - 1:
            C.CDLL(None).srand(seed)  # the core's rand() is the process's
            core.retro_reset()
        core.retro_run()
        if states and state["frame"] in wanted:
            size = core.retro_serialize_size()
            blob = C.create_string_buffer(size)
            if core.retro_serialize(blob, C.c_size_t(size)):
                (out / f"s{state['frame']:05d}.bin").write_bytes(blob.raw)
    return state["frame"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("core", type=Path, help="blastem_libretro.so (.dll on Windows)")
    parser.add_argument("rom", type=Path, help="the cartridge image, uncompressed")
    parser.add_argument("out", type=Path)
    parser.add_argument("--frames", default="", help="comma-separated frame numbers")
    parser.add_argument("--range", default="", help="first:last:step, inclusive")
    parser.add_argument("--states", action="store_true", help="save serialized states too")
    parser.add_argument("--work", type=Path, help="save/system directory (default: temp)")
    parser.add_argument("--reset-frame", type=int, default=0, help="press reset before it")
    parser.add_argument("--seed", type=int, default=1, help="rand() seed for the reset")
    args = parser.parse_args()
    wanted = {int(n) for n in args.frames.split(",") if n}
    if args.range:
        first, last, step = (int(n) for n in args.range.split(":"))
        wanted |= set(range(first, last + 1, step))
    if not wanted:
        parser.error("no frames asked for")
    with tempfile.TemporaryDirectory() as temp:
        presented = run(args.core, args.rom, args.out, wanted, args.states,
                        args.work or Path(temp), args.reset_frame, args.seed)  # fmt: skip
    print(f"{len(wanted)} frames wanted, {presented} presented")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
