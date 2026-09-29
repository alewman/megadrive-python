"""Milestone 9: the desktop frontend.

Its conversions need no window and are tested as they are (pygame is not imported by
``megadrive_python.frontend`` itself). The window loop runs headless under SDL's dummy
drivers where pygame-ce is installed (the ``frontend`` extra) and is skipped where it
is not, as in CI, which installs only ``dev``.
"""

from __future__ import annotations

import os
from array import array

import pytest

from megadrive_python.frontend import (
    KEYS,
    SoundBuffer,
    frame_bytes,
    machine_for,
    sound_bytes,
)
from megadrive_python.io import BUTTONS


def test_frame_bytes_are_opaque_bgra() -> None:
    assert frame_bytes([[0x123456, 0xFFFFFF]]) == bytes([0x56, 0x34, 0x12, 0xFF]) + b"\xff" * 4


def test_sound_bytes_interleave_and_clamp() -> None:
    out = array("h", sound_bytes([(100, -100), (40_000, -40_000)]))
    assert list(out) == [100, -100, 32767, -32768]


def test_sound_buffer_pads_with_silence() -> None:
    buffer = SoundBuffer()
    buffer.push(b"\x01\x02\x03")
    stream = memoryview(bytearray(b"\xaa" * 6))
    buffer.fill(None, stream)
    assert bytes(stream) == b"\x01\x02\x03\x00\x00\x00" and len(buffer) == 0


def _header(country: str) -> bytes:
    image = bytearray(0x200)
    image[0x1F0 : 0x1F0 + len(country)] = country.encode()
    return bytes(image)


def test_the_machine_follows_the_headers_country_codes() -> None:
    assert machine_for(_header("JUE")) == "genesis"  # the USA's NTSC console first
    assert machine_for(_header("E")) == "megadrive"
    assert machine_for(_header("J")) == "megadrive-japan"
    assert machine_for(_header("4")) == "genesis"  # a later code the Manual lacks
    assert machine_for(b"") == "genesis"


def test_every_pad_button_has_a_key() -> None:
    assert set(KEYS) == set(BUTTONS.values())


def test_the_window_runs_headless(tmp_path) -> None:
    pytest.importorskip("pygame")
    import pygame
    import pygame._sdl2.controller

    from megadrive_python.frontend import Frontend

    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    # A cartridge that loops for ever: the window, the loop and the pads, not a game.
    rom = bytearray(0x10000)
    rom[0:4] = (0x00FFFE00).to_bytes(4, "big")
    rom[4:8] = (0x00000200).to_bytes(4, "big")
    rom[0x100:0x110] = b"SEGA GENESIS    "
    rom[0x200:0x202] = bytes.fromhex("60fe")  # bra.s *
    path = tmp_path / "loop.bin"
    path.write_bytes(bytes(rom))
    pygame.display.init()
    pygame._sdl2.controller.init()
    try:
        frontend = Frontend(pygame, path, "genesis", scale=1, audio=False, fullscreen=False)
        frontend.run(max_frames=3)
        assert frontend.frame_size == (len(frontend.frame[0]), len(frontend.frame))
        frontend._apply_inputs()  # no key held: the pad reads nothing pressed
        assert frontend.system.board.io.ports[0].device.pressed == 0
        frontend.close()
    finally:
        pygame.quit()


def test_the_window_survives_a_resolution_change(tmp_path) -> None:
    """H32/H40, V28/V30 or interlace changing mid-game calls ``_open_window`` again
    (``draw``, "H32/H40, V28/V30 or interlace changed"). SDL/pygame-ce 2.5.8 segfaults
    on a second ``set_mode()`` resize of a window opened with the ``RESIZABLE`` flag
    (reproduced with no Frontend and no Window API at all: three plain ``set_mode()``
    calls at changing sizes is enough); this is why ``_open_window`` does not pass it."""
    pytest.importorskip("pygame")
    import pygame
    import pygame._sdl2.controller

    from megadrive_python.frontend import Frontend

    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    rom = bytearray(0x10000)
    rom[0:4] = (0x00FFFE00).to_bytes(4, "big")
    rom[4:8] = (0x00000200).to_bytes(4, "big")
    rom[0x100:0x110] = b"SEGA GENESIS    "
    rom[0x200:0x202] = bytes.fromhex("60fe")  # bra.s *
    path = tmp_path / "loop.bin"
    path.write_bytes(bytes(rom))
    pygame.display.init()
    pygame._sdl2.controller.init()
    try:
        frontend = Frontend(pygame, path, "genesis", scale=1, audio=False, fullscreen=False)
        for width, height in [(256, 224), (352, 240), (320, 224), (320, 448), (256, 224)]:
            frontend.frame = [[0] * width for _ in range(height)]
            frontend.draw()  # would segfault the whole process if it regressed
            assert frontend.frame_size == (width, height)
        frontend.close()
    finally:
        pygame.quit()


def test_the_title_bar_names_the_frame_and_the_emulated_time():
    from megadrive_python.frontend import where

    assert where(6800, 49.70) == "frame 6,800 (2:16)"
    assert where(0, 59.92) == "frame 0 (0:00)"
