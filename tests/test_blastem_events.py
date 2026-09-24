"""The BlastEm event-log reader, on logs built here so nothing has to be fetched.

BlastEm is a detector (docs/validation.md): these check that the *format* is decoded,
not that anything it says is right.
"""

from __future__ import annotations

import itertools
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import blastem_events as be  # noqa: E402


def _log(events: bytes, *, name: bytes = b"TEST", video: int = 0) -> bytes:
    """A whole log: the magic, the version, the uncompressed start block, then zlib."""
    head = b"BLSTEL" + b"\x02\x00" + bytes([1, video, len(name)]) + name
    return head + zlib.compress(events)


def _short(kind: int, delta: int) -> bytes:
    """The one-byte header: type in the high nibble, delta - 16 in the low."""
    assert 16 <= delta < 32
    return bytes([kind << 4 | (delta - 16)])


def test_the_header_is_read():
    reader = be.Reader(_log(b"", name=b"EARTHION", video=1))
    assert reader.name == "EARTHION"
    assert reader.video_std == 1
    assert reader.frame_cycles == 313 * 3420  # PAL: a cycle is a master clock
    assert list(reader) == []


def test_a_short_header_carries_type_and_delta():
    body = _short(be.VDP_REG, 20) + bytes([12, 0x81])
    (event,) = list(be.Reader(_log(body)))
    assert event.name == "vdp-reg"
    assert (event.cycle, event.address, event.value) == (20, 12, 0x81)


def test_a_three_byte_header_carries_a_sixteen_bit_delta():
    body = bytes([0xE0 | be.VDP_REG]) + (5000).to_bytes(2, "big") + bytes([1, 0x74])
    (event,) = list(be.Reader(_log(body)))
    assert event.cycle == 5000
    assert (event.address, event.value) == (1, 0x74)


def test_a_four_byte_header_carries_a_signed_twenty_four_bit_delta():
    body = bytes([0xF0 | be.VDP_REG, 0x01]) + (0x0000).to_bytes(2, "big") + bytes([0, 4])
    (event,) = list(be.Reader(_log(body)))
    assert event.cycle == 0x10000
    # A negative delta is sign-extended rather than read as a huge positive one.
    body = bytes([0xF0 | be.VDP_REG, 0xFF]) + (0xFFFF).to_bytes(2, "big") + bytes([0, 4])
    reader = be.Reader(_log(body))
    (event,) = list(reader)
    assert event.cycle == -1  # absolute time follows the delta
    assert reader.raw == 0xFFFFFFFF  # the counter BlastEm holds wraps


def test_a_multi_header_repeats_the_event_with_its_own_payload():
    # MULTI says the next event's type and delta repeat; each repetition still
    # carries its own payload bytes.
    body = bytes([be.MULTI << 4 | 2]) + _short(be.VDP_REG, 16)
    body += bytes([15, 0x02]) + bytes([15, 0x03]) + bytes([15, 0x04])
    events = list(be.Reader(_log(body)))
    assert [e.value for e in events] == [0x02, 0x03, 0x04]
    assert [e.cycle for e in events] == [16, 32, 48]  # the delta repeats too


def test_vram_byte_addresses_are_carried_as_deltas():
    body = _short(be.VDP_REG, 16) + bytes([15, 2])
    body += _short(be.VRAM_BYTE, 16) + (0x1000).to_bytes(2, "big") + bytes([0xAA])
    body += _short(be.VRAM_BYTE_ONE, 16) + bytes([0xBB])
    body += _short(be.VRAM_BYTE_AUTO, 16) + bytes([0xCC])
    body += _short(be.VRAM_BYTE_DELTA, 16) + bytes([0x10, 0xDD])
    events = list(be.Reader(_log(body)))[1:]
    assert [e.address for e in events] == [0x1000, 0x1001, 0x1003, 0x1013]
    assert [e.value for e in events] == [0xAA, 0xBB, 0xCC, 0xDD]


def test_an_auto_step_follows_register_fifteen():
    """BlastEm logs ``VRAM_BYTE_AUTO`` when a write lands register 15's value past the
    last one, whatever that value is; 2 was once assumed here and is not the only one."""
    body = _short(be.VDP_REG, 16) + bytes([15, 0x80])
    body += _short(be.VRAM_BYTE, 16) + (0x1000).to_bytes(2, "big") + bytes([0xAA])
    body += _short(be.VRAM_BYTE_AUTO, 16) + bytes([0xBB])
    body += _short(be.VDP_REG, 16) + bytes([15, 0x40])
    body += _short(be.VRAM_BYTE_AUTO, 16) + bytes([0xCC])
    events = [e for e in be.Reader(_log(body)) if e.kind != be.VDP_REG]
    assert [e.address for e in events] == [0x1000, 0x1080, 0x10C0]


def test_word_addresses_are_carried_as_deltas_too():
    body = _short(be.VRAM_WORD, 16) + bytes([0x01]) + (0x2000).to_bytes(2, "big")
    body += (0x1234).to_bytes(2, "big")
    body += _short(be.VRAM_WORD_DELTA, 16) + bytes([0x08]) + (0x5678).to_bytes(2, "big")
    events = list(be.Reader(_log(body)))
    assert [e.address for e in events] == [0x12000, 0x12008]
    assert [e.value for e in events] == [0x1234, 0x5678]


def test_a_frame_is_taken_from_the_cycle_and_not_from_flush():
    # EVENT_FLUSH is an output-buffer flush; its count lands near the frame count,
    # which is what made it look like a frame marker. frame_of is the honest one.
    reader = be.Reader(_log(b"", video=0))
    assert reader.frame_of(0) == 0
    assert reader.frame_of(reader.frame_cycles - 1) == 0
    assert reader.frame_of(reader.frame_cycles) == 1
    assert reader.frame_cycles == 262 * 3420  # NTSC


def test_register_writes_are_grouped_by_frame():
    frame = be.FRAME_CYCLES[0]
    body = bytes([0xE0 | be.VDP_REG]) + (20).to_bytes(2, "big") + bytes([0, 0x04])
    body += bytes([0xF0 | be.VDP_REG, frame >> 16])
    body += (frame & 0xFFFF).to_bytes(2, "big") + bytes([1, 0x74])
    grouped = be.register_writes(be.Reader(_log(body)))
    assert grouped == {0: [(0, 0x04)], 1: [(1, 0x74)]}


def test_a_file_that_is_not_a_log_is_refused():
    with pytest.raises(ValueError, match="BLSTEL"):
        be.Reader(b"not a blastem log at all")


def test_the_cycle_counter_is_masked_to_thirty_two_bits():
    # BlastEm's counter is uint32 and wraps; its own reader does the arithmetic that
    # way. Python's unbounded integers do not, which is why it has to be masked here.
    body = bytes([0xF0 | be.VDP_REG, 0xFF]) + (0xFFF0).to_bytes(2, "big") + bytes([0, 4])
    reader = be.Reader(_log(body))
    (event,) = list(reader)
    assert event.cycle == -16  # absolute time follows the delta
    assert reader.raw == 0xFFFFFFF0  # the wrapped counter does not


def test_an_adjust_deducts_from_the_counter_and_absolute_time_keeps_it():
    # EVENT_ADJUST is how BlastEm stops its 32-bit counter overflowing: it deducts a
    # large amount from every component at once. Real time does not go backwards, so
    # Event.cycle carries the deduction forward and keeps rising across it. A
    # 12,000-frame Overdrive 2 log read without this appears to stop at frame 4,008.
    deduction = 4_278_391_830
    body = bytes([0xE0 | be.VDP_REG]) + (1000).to_bytes(2, "big") + bytes([0, 4])
    body += bytes([0xE0 | be.ADJUST]) + (500).to_bytes(2, "big")
    body += deduction.to_bytes(4, "big")
    body += bytes([0xE0 | be.VDP_REG]) + (250).to_bytes(2, "big") + bytes([1, 0x74])
    events = list(be.Reader(_log(body)))
    assert [e.name for e in events] == ["vdp-reg", "adjust", "vdp-reg"]
    # Absolute time is monotonic across the adjust: 1000, then 1500, then 1750.
    assert [e.cycle for e in events] == [1000, 1500, 1750]


def test_absolute_time_survives_a_wrap_of_the_raw_counter():
    # A delta is at most 24 bits, so the counter can only reach 2**32 by accumulating.
    # Once it wraps, the counter BlastEm holds starts again from a small number while
    # absolute time keeps climbing -- which is the whole point of keeping both.
    step = 0x7FFFF0
    count = (1 << 32) // step + 2
    body = b""
    for _ in range(count):
        body += bytes([0xF0 | be.VDP_REG, step >> 16])
        body += (step & 0xFFFF).to_bytes(2, "big") + bytes([0, 4])
    reader = be.Reader(_log(body))
    events = list(reader)
    assert len(events) == count
    assert events[-1].cycle == step * count  # absolute time never wraps
    assert events[-1].cycle > 1 << 32
    assert reader.raw == (step * count) & 0xFFFFFFFF  # the counter does
    assert reader.raw < 1 << 32
    assert all(b.cycle > a.cycle for a, b in itertools.pairwise(events))
