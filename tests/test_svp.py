"""The SVP board: the memory controller's registers and the idle-loop skip."""

import pytest

from megadrive_python.cartridge import BOARDS, Cartridge

pytest.importorskip("ssp1601_python")


def svp(program: list[int]) -> Cartridge:
    """A 2 MB image with ``program`` at the DSP's entry point (word $400 = byte $800)."""
    image = bytearray(0x200000)
    for index, word in enumerate(program):
        image[0x800 + 2 * index : 0x802 + 2 * index] = word.to_bytes(2, "big")
    return BOARDS["rom_svp"](bytes(image), slot="rom_svp")


# ld X, EXT0 ; ld A, X ; ori 0 ; bra z=1, $400 ; ld EXT3, X ; bra always, $405
WAIT_FOR_68K = [0x0018, 0x0031, 0xD800, 0x4D50, 0x0400, 0x00B1, 0x4C00, 0x0405]


def drive(cart: Cartridge, steps: list[tuple[int, str]]) -> tuple:
    for mclk, what in steps:
        cart.catch_up(mclk)
        if what == "write":
            cart.write_a15(0xA15000, 0x12, 0x34, True)
    cart.catch_up(steps[-1][0])
    cart.wake()  # bring an idle DSP up to date before looking
    d = cart.dsp
    return (cart.dsp_cycles, d.pc, d.x, d.a, d.st, cart.xst, cart.xst2)


def test_a_waiting_dsp_is_found_idle_and_woken_where_it_would_have_been():
    steps = [(m, "") for m in range(100, 40_000, 97)] + [(40_000, "write")]
    steps += [(m, "") for m in range(40_100, 50_000, 31)]
    fast, slow = svp(WAIT_FOR_68K), svp(WAIT_FOR_68K)
    slow.look = lambda: None  # never notices it is idle: runs every instruction
    assert drive(fast, steps) == drive(slow, steps)
    assert fast.idle_period == 0  # drive() woke it; but it had been idle before the write
    assert fast.dsp.pc in (0x405, 0x406, 0x407)  # past the wait: the write was seen
    assert fast.xst == slow.xst == 2  # the DSP read the status (the 68000 wrote) and echoed it


def test_the_skip_really_skips():
    cart = svp(WAIT_FOR_68K)
    executed = []
    step = cart.dsp.step
    cart.dsp.step = lambda: executed.append(1) or step()
    for mclk in range(100, 200_000, 50):
        cart.catch_up(mclk)
    assert cart.idle_period
    assert len(executed) < 2_000  # of the ~85,000 the DSP's clock allows


def test_dram_and_the_status_words_from_the_68000():
    cart = svp([0])
    cart.write_word(0x300010, 0xBEEF)
    cart.write_byte(0x300011, 0x12)
    assert cart.read_word(0x300010) == 0xBE12
    assert cart.read_byte(0x300010) == 0xBE
    assert cart.read_word(0x320000) == 0xFFFF
    assert cart.read_word(0x4) == 0  # ROM
    cart.write_a15(0xA15003, 0, 0x55, False)
    assert cart.read_a15_word(0xA15000) == 0x0055 and cart.xst2 & 2
    assert cart.read_a15_word(0xA15004) & 2  # reading the status clears bit 0 only
