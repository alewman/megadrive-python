"""The console's reset button: the 68000, the Z80 and the YM2612, not a power cycle.

The rule is measured on hardware (forum t=1262, Charles MacDonald and Eke): VRES#
pulses the 68000 for 128 of its clocks, ZRES holds the Z80 and the YM2612 in reset
until software releases them, and SRES#, which resets the VDP and the I/O chip, comes
only at a cold boot.
"""

from __future__ import annotations

from megadrive_python.cartridge import Cartridge
from megadrive_python.machine import M68K_DIVIDER, MACHINES
from megadrive_python.system import RESET_PULSE_CLOCKS, System


def _cartridge() -> Cartridge:
    """At every boot: release the Z80's reset, count the boot in work RAM, loop."""
    rom = bytearray(0x10000)
    rom[0:4] = (0x00FFFE00).to_bytes(4, "big")  # initial SSP
    rom[4:8] = (0x00000200).to_bytes(4, "big")  # reset vector
    rom[0x100:0x110] = b"SEGA GENESIS    "
    code = bytes.fromhex(
        "33fc010000a11200"  # move.w #$0100,$A11200  -- the Z80 out of reset
        "527900ff0000"  # addq.w #1,$FF0000      -- one more boot
        "60fe"  # bra.s *
    )
    rom[0x200 : 0x200 + len(code)] = code
    return Cartridge(bytes(rom))


def _system() -> System:
    system = System(MACHINES["genesis"], _cartridge())
    system.board.fm.enabled = False
    system.run_frame()
    return system


def _boots(system: System) -> int:
    return system.board.read_word(0xFF0000)


def test_the_68000_boots_again_and_work_ram_survives() -> None:
    system = _system()
    assert _boots(system) == 1 and not system.board.z80_reset
    system.press_reset()
    system.run_frame()
    assert _boots(system) == 2  # the vector taken again; RAM kept its count


def test_zres_is_held_until_the_game_releases_it() -> None:
    system = _system()
    system.press_reset()
    assert system.board.z80_reset  # at once, with the pulse
    system.step()  # the pulse and the reset exception: the game has not run yet
    assert system.board.z80_reset
    system.step()  # the game's first instruction writes $A11200
    assert not system.board.z80_reset


def test_the_vdp_is_not_reset() -> None:
    system = _system()
    system.vdp.registers[7] = 0x21  # the backdrop colour, which the game never sets
    system.press_reset()
    system.run_frame()
    assert system.vdp.registers[7] == 0x21


def test_the_pulse_holds_the_68000_for_128_clocks_before_the_exception() -> None:
    system = _system()
    fetches = []
    board = system.board

    def read_word(address):
        fetches.append(board.m68k_access_clock())
        return board.read_word(address)

    system.m68k.attach_bus(board.read_byte, read_word, board.write_byte, board.write_word)
    start = system.mclk
    system.press_reset()
    elapsed = system.step()
    # 42 clocks of reset exception (m68000-python's ``reset``) after the pulse, and the
    # refresh its bus cycles take; the vector fetch comes after the pulse, 16 clocks on
    # (docs/worklog.md, "The reset exception").
    assert elapsed == (RESET_PULSE_CLOCKS + 42) * M68K_DIVIDER + system.board.stall
    pulse_end = start + RESET_PULSE_CLOCKS * M68K_DIVIDER
    assert fetches[0] == pulse_end + 16 * M68K_DIVIDER


def test_a_halted_68000_is_brought_back() -> None:
    system = _system()
    system.m68k.halted = True  # as a double bus fault leaves it
    system.press_reset()
    system.step()
    assert not system.m68k.halted
    system.run_frame()
    assert _boots(system) == 2
