"""A desktop window: play a cartridge with picture, sound, keyboard and gamepads.

    megadrive-python-play "Sonic The Hedgehog (USA, Europe).zip"
    megadrive-python-play game.md --machine megadrive --scale 4
    megadrive-python-play game.md --no-fm --skip 1

Milestone 9. The design is sms-python's frontend (``sms_python/frontend.py``, same
author, MIT), copied and adapted rather than imported, as the brief asks: pygame-ce
(the ``frontend`` extra) for SDL's window, audio and game controllers, a window that
presents at the display's refresh, and timing driven by the audio queue.

**Speed, honestly.** Timing is audio-driven: SDL's audio thread drains a short
queue, and the main loop runs the console one frame at a time only while that queue
is short, so the console's own 59.92 Hz (49.70 Hz PAL) sets the pace. That holds only
while the board keeps up, and on PyPy it does not yet: measured on this machine, 16-21
frames a second with the YM2612 running and 40-55 without it (docs/worklog.md,
milestone 9). When the board is behind, the loop runs flat out, the sound comes in
bursts, and the title bar says how fast the console is going. ``--no-fm`` turns the FM
generator off (its registers, timers and BUSY still work, so games run the same) and
``--skip N`` draws one frame in N + 1; together they come close to full speed.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from array import array
from collections.abc import Callable
from itertools import chain
from pathlib import Path

from .__main__ import load_image
from .cartridge import Cartridge
from .io import BUTTONS, DOWN, LEFT, MODE, PAD_TYPES, RIGHT, START, UP, A, B, C, X, Y, Z
from .machine import MACHINES
from .png import write_png
from .system import System

SAMPLE_RATE = 44_100
#: Frames of sound kept queued ahead of the speaker: the latency, and the margin a
#: slow frame (a garbage collection) can use before the sound gaps.
BUFFER_FRAMES = 3

HELP = """\
Arrows       D-pad              Z X C / J K L   A B C          A S D   X Y Z
Enter        Start              Q               Mode
Tab (hold)   fast forward       F3              pause          F5      power cycle
F11          fullscreen         F12             screenshot     Esc     quit"""

#: The keyboard, by pad button (pygame key names, looked up when pygame is loaded).
KEYS = {
    UP: ("UP",), DOWN: ("DOWN",), LEFT: ("LEFT",), RIGHT: ("RIGHT",),
    A: ("z", "j"), B: ("x", "k"), C: ("c", "l"),
    X: ("a",), Y: ("s",), Z: ("d",), START: ("RETURN", "KP_ENTER"), MODE: ("q",),
}  # fmt: skip

#: A game controller, by pad button (SDL's controller button names). The face buttons
#: follow the Mega Drive's own row -- A, B, C along the bottom as X, A, B -- and the
#: 6-button pad's top row X, Y, Z sits on left shoulder, Y and right shoulder.
CONTROLLER = {
    A: "X", B: "A", C: "B", X: "LEFTSHOULDER", Y: "Y", Z: "RIGHTSHOULDER",
    START: "START", MODE: "BACK",
}  # fmt: skip


# -- conversions (pure, tested without pygame) ---------------------------------------


def frame_bytes(frame: list[list[int]]) -> bytes:
    """Rows of 0xRRGGBB as opaque BGRA bytes (little-endian 0xFFRRGGBB words)."""
    pixels = array("I", [0xFF000000 | p for p in chain.from_iterable(frame)])
    if sys.byteorder == "big":
        pixels.byteswap()
    return pixels.tobytes()


def sound_bytes(samples: list[tuple[int, int]]) -> bytes:
    """Stereo pairs of signed 16-bit values, as the board's audio path mixes them, as
    interleaved little-endian bytes for SDL."""
    out = array("h")
    for left, right in samples:
        out.append(max(-32768, min(32767, int(left))))
        out.append(max(-32768, min(32767, int(right))))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


class SoundBuffer:
    """Bytes queued for SDL's audio thread, which calls :meth:`fill`."""

    def __init__(self) -> None:
        self._data = bytearray()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._data)

    def push(self, data: bytes) -> None:
        with self._lock:
            self._data += data

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def fill(self, _device: object, stream: memoryview) -> None:
        """SDL's audio callback: copy what is queued, then silence."""
        size = len(stream)
        with self._lock:
            chunk = bytes(self._data[:size])
            del self._data[:size]
        stream[: len(chunk)] = chunk
        if len(chunk) < size:
            stream[len(chunk) :] = bytes(size - len(chunk))


def machine_for(image: bytes) -> str:
    """The machine a cartridge's header suggests: its "Country in which the product
    can be released" at $1F0 -- "Japan: J, USA: U, Europe: E" (the Manual, PDF page
    130). An NTSC Genesis if it names the USA, a PAL Mega Drive if Europe but not the
    USA, a Japanese one if only Japan, and the Genesis for anything else (the later
    hexadecimal codes, which the Manual does not describe)."""
    field = image[0x1F0:0x200].decode("latin1", "replace").upper()
    if "U" in field:
        return "genesis"
    if "E" in field:
        return "megadrive"
    if "J" in field:
        return "megadrive-japan"
    return "genesis"


# -- the window ------------------------------------------------------------------------


class Frontend:
    """One console in one window. ``pygame`` is imported by :func:`main`."""

    def __init__(self, pygame, rom: Path, machine: str, *, scale: int, audio: bool,
                 fullscreen: bool, fm: bool = True, skip: int = 0, pad: str = "3button",
                 pad2: str = "3button", save: Path | None = None,
                 tmss_rom: Path | None = None) -> None:  # fmt: skip
        self.pg = pygame
        self.rom, self.machine_name = rom, machine
        self.fm, self.skip = fm, max(0, skip)
        self.pad_types = (pad, pad2)
        self.save_path, self.tmss_path = save, tmss_rom
        self.system = self._power_on()
        # The renderer hands out finished frames only, and at power-on none has
        # finished; a black one stands in until a frame has.
        self.frame = [[0] * 320 for _ in range(224)]
        for _ in range(3):
            self.system.run_frame()
            finished = self.system.vdp.renderer.frame_rgb()
            if finished and finished[0]:
                self.frame = finished
                break
        self.frame_size = (len(self.frame[0]), len(self.frame))
        self.scale, self.fullscreen = scale, fullscreen
        self.window = self._open_window(self.frame_size)
        pygame.display.set_caption(f"{rom.stem} — megadrive-python")
        self.sound = SoundBuffer()
        self.device = self._open_audio() if audio else None
        self.paused = False
        self.fast = False
        self.controllers: dict[int, object] = {}
        self._keys = {button: [getattr(pygame, f"K_{name}") for name in names]
                      for button, names in KEYS.items()}  # fmt: skip
        self._buttons = {button: getattr(pygame, f"CONTROLLER_BUTTON_{name}")
                         for button, name in CONTROLLER.items()}  # fmt: skip
        self._frames = 0
        self._console_frames = 0
        self._fps_since = time.perf_counter()

    def _open_window(self, size: tuple[int, int]):
        """A window whose surface is one frame: SDL scales it to the window on the GPU
        (SCALED, nearest-neighbour) and presents it at the display's refresh (vsync),
        so a frame is never shown half-drawn. pygame offers vsync only with SCALED."""
        pg = self.pg
        flags = pg.SCALED | pg.RESIZABLE | (pg.FULLSCREEN if self.fullscreen else 0)
        try:
            surface = pg.display.set_mode(size, flags, vsync=1)
        except pg.error:  # no vsync on this display: tearing, but a picture
            surface = pg.display.set_mode(size, flags)
        if not self.fullscreen:
            height = size[1] if size[1] < 400 else size[1] // 2  # interlace mode 2
            # Deprecated in pygame-ce 2.5 in favour of its Window API, which asks for
            # vsync differently; kept, as sms-python keeps it, until that move is made.
            pg.Window.from_display_module().size = (size[0] * self.scale, height * self.scale)
        return surface

    # -- console ------------------------------------------------------------------

    def _power_on(self) -> System:
        cartridge = Cartridge.from_image(load_image(self.rom))
        if self.save_path is not None and self.save_path.exists() and cartridge.battery:
            cartridge.load_bytes(self.save_path.read_bytes())
        self.cartridge = cartridge
        system = System(
            MACHINES[self.machine_name], cartridge,
            pads=tuple(PAD_TYPES[kind]() for kind in self.pad_types),
            tmss_rom=load_image(self.tmss_path) if self.tmss_path else None,
        )  # fmt: skip
        system.audio.sample_rate = SAMPLE_RATE
        # With the generator off the FM's registers, timers and BUSY still run, so a
        # game behaves the same; only its sound is silent (ym2612.py, ``enabled``).
        system.board.fm.enabled = self.fm
        return system

    def close(self) -> None:
        """Save the battery RAM and let go of the audio device."""
        self._save_battery()
        if self.device is not None:
            self.device.close()
            self.device = None

    def _save_battery(self) -> None:
        if self.save_path is not None and self.cartridge.battery:
            self.save_path.write_bytes(self.cartridge.save_bytes())

    def _open_audio(self):
        from pygame._sdl2.audio import AUDIO_S16, AudioDevice
        from pygame._sdl2.sdl2 import INIT_AUDIO, init_subsystem

        try:
            init_subsystem(INIT_AUDIO)
            device = AudioDevice(
                devicename=None, iscapture=False, frequency=SAMPLE_RATE,
                audioformat=AUDIO_S16, numchannels=2, chunksize=512, allowed_changes=0,
                callback=self.sound.fill,
            )  # fmt: skip
        except Exception as error:  # pygame._sdl2's own error type; no device, no sound
            print(f"no sound: {error}", file=sys.stderr)
            return None
        device.pause(0)
        return device

    @property
    def rate(self) -> float:
        """The console's own frame rate."""
        return 49.70 if self.system.board.machine.pal else 59.92

    @property
    def _frame_bytes(self) -> int:
        """Bytes of sound one frame makes: stereo 16-bit at the host rate."""
        return int(SAMPLE_RATE / self.rate) * 4

    def run_frame(self, draw: bool = True) -> None:
        self._apply_inputs()
        self.system.vdp.rendering = draw
        self.system.run_frame()
        samples = self.system.take_audio()
        if self.device is not None and not self.fast:
            self.sound.push(sound_bytes(samples))
        if draw:
            finished = self.system.vdp.renderer.frame_rgb()
            if finished and finished[0]:
                self.frame = finished
        self._console_frames += 1

    # -- input --------------------------------------------------------------------

    def _pressed(self, controller=None) -> int:
        """Pad buttons held, as the board's bit mask: from the keyboard (with
        ``controller`` None) or from one game controller."""
        pg = self.pg
        mask = 0
        if controller is None:
            keys = pg.key.get_pressed()
            for button, codes in self._keys.items():
                if any(keys[code] for code in codes):
                    mask |= button
            return mask
        for button, name in self._buttons.items():
            if controller.get_button(name):
                mask |= button
        stick_x = controller.get_axis(pg.CONTROLLER_AXIS_LEFTX) / 32768
        stick_y = controller.get_axis(pg.CONTROLLER_AXIS_LEFTY) / 32768
        for button, name, stick in (
            (UP, pg.CONTROLLER_BUTTON_DPAD_UP, stick_y < -0.5),
            (DOWN, pg.CONTROLLER_BUTTON_DPAD_DOWN, stick_y > 0.5),
            (LEFT, pg.CONTROLLER_BUTTON_DPAD_LEFT, stick_x < -0.5),
            (RIGHT, pg.CONTROLLER_BUTTON_DPAD_RIGHT, stick_x > 0.5),
        ):
            if stick or controller.get_button(name):
                mask |= button
        return mask

    def _apply_inputs(self) -> None:
        ports = self.system.board.io.ports
        controllers = list(self.controllers.values())
        first = self._pressed() | (self._pressed(controllers[0]) if controllers else 0)
        second = self._pressed(controllers[1]) if len(controllers) > 1 else 0
        for port, mask in zip(ports[:2], (first, second), strict=False):
            if port.device is not None:
                port.device.pressed = mask

    # -- events -------------------------------------------------------------------

    def _handle(self, event) -> bool:
        """Handle one event; False to quit."""
        pg = self.pg
        if event.type == pg.QUIT:
            return False
        if event.type == pg.CONTROLLERDEVICEADDED:
            controller = pg._sdl2.controller.Controller(event.device_index)
            self.controllers[controller.id] = controller
        elif event.type == pg.CONTROLLERDEVICEREMOVED:
            self.controllers.pop(event.instance_id, None)
        elif event.type == pg.KEYDOWN:
            if event.key == pg.K_ESCAPE:
                return False
            if event.key == pg.K_F3:
                self.paused = not self.paused
                self.sound.clear()
            elif event.key == pg.K_F5:
                self._save_battery()
                self.system = self._power_on()
                self.sound.clear()
            elif event.key == pg.K_F11:
                pg.display.toggle_fullscreen()
            elif event.key == pg.K_F12:
                self._screenshot()
            elif event.key == pg.K_TAB:
                self.fast = True
                self.sound.clear()
        elif event.type == pg.KEYUP and event.key == pg.K_TAB:
            self.fast = False
        return True

    def _screenshot(self) -> None:
        folder = Path("screenshots")
        folder.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = folder / f"{self.rom.stem}-{stamp}.png"
        write_png(path, self.frame)
        print(f"saved {path}")

    # -- drawing ------------------------------------------------------------------

    def draw(self) -> None:
        pg = self.pg
        frame = self.frame
        size = (len(frame[0]), len(frame))
        if size != self.frame_size:  # H32/H40, V28/V30 or interlace changed
            self.frame_size = size
            self.window = self._open_window(size)
        self.window.blit(pg.image.frombuffer(frame_bytes(frame), size, "BGRA"), (0, 0))
        pg.display.flip()  # waits for the display's refresh
        self._frames += 1
        now = time.perf_counter()
        if now - self._fps_since >= 1.0:
            elapsed = now - self._fps_since
            speed = self._console_frames / elapsed / self.rate * 100
            state = " (paused)" if self.paused else " (fast)" if self.fast else ""
            pg.display.set_caption(
                f"{self.rom.stem} — {speed:.0f}% speed, {self._frames / elapsed:.0f} fps"
                f"{state} — megadrive-python"
            )
            self._frames = self._console_frames = 0
            self._fps_since = now

    # -- the loop -----------------------------------------------------------------

    def run(self, max_frames: int | None = None, on_frame: Callable | None = None) -> None:
        pg = self.pg
        clock = pg.time.Clock()
        target = BUFFER_FRAMES * self._frame_bytes
        shown = 0
        cycle = 0
        while True:
            for event in pg.event.get():
                if not self._handle(event):
                    return
            ran = False
            if not self.paused:
                if self.fast:
                    for _ in range(4):
                        self.run_frame(draw=False)
                    self.run_frame()
                    ran = True
                elif self.device is None or len(self.sound) < target:
                    # Audio sets the pace while the board keeps up: a frame each time
                    # the queue is short of its target. Behind real time the queue is
                    # always short, and this runs flat out.
                    draw = cycle % (self.skip + 1) == 0
                    cycle += 1
                    self.run_frame(draw)
                    ran = draw
            if ran:
                self.draw()
                shown += 1
                if on_frame is not None:
                    on_frame(self)
                if max_frames is not None and shown >= max_frames:
                    return
            if self.device is None and not self.fast:
                clock.tick_busy_loop(self.rate / (self.skip + 1))
            elif not ran:
                time.sleep(0.002)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="megadrive-python-play",
        description="Play a Mega Drive / Genesis cartridge in a window.",
        epilog=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("rom", type=Path, help="cartridge image (.md/.bin/.gen, or a zip)")
    parser.add_argument("--machine", choices=sorted(MACHINES),
                        help="default: from the header's country codes")  # fmt: skip
    parser.add_argument("--scale", type=int, default=3, help="window scale (default 3)")
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--no-fm", action="store_true",
                        help="FM generator off, for speed (its registers still work)")  # fmt: skip
    parser.add_argument("--skip", type=int, default=0, metavar="N",
                        help="draw one frame in N + 1 (default 0: every frame)")  # fmt: skip
    parser.add_argument("--pad", default="3button", choices=sorted(PAD_TYPES), help="port 1")
    parser.add_argument("--pad2", default="3button", choices=sorted(PAD_TYPES), help="port 2")
    parser.add_argument(
        "--save", type=Path, help="battery RAM file, read at start, written on exit"
    )
    parser.add_argument("--tmss-rom", type=Path, help="the TMSS boot ROM, run before the cartridge")
    parser.add_argument("--fullscreen", action="store_true")
    args = parser.parse_args(argv)
    try:
        import pygame
        import pygame._sdl2.controller
    except ImportError:
        parser.error("the window needs pygame-ce: pip install 'megadrive-python[frontend]'")
    if sys.implementation.name != "pypy":
        print("note: CPython runs this emulator at a small fraction of real time; use PyPy.",
              file=sys.stderr)  # fmt: skip
    machine = args.machine or machine_for(load_image(args.rom))
    # Not pygame.init(): its mixer would take the audio device the frontend opens.
    pygame.display.init()
    pygame._sdl2.controller.init()
    print(HELP)
    print(f"{args.rom.name} on {MACHINES[machine].name}")
    frontend = Frontend(
        pygame, args.rom, machine, scale=max(1, args.scale), audio=not args.no_audio,
        fullscreen=args.fullscreen, fm=not args.no_fm, skip=args.skip, pad=args.pad,
        pad2=args.pad2, save=args.save, tmss_rom=args.tmss_rom,
    )  # fmt: skip
    try:
        frontend.run()
    finally:
        frontend.close()
        pygame.quit()
    return 0


assert set(BUTTONS.values()) == set(KEYS)  # every pad button has a key


if __name__ == "__main__":
    sys.exit(main())
