"""The VDP-stream instrument's canonical form, on logs and writes built here.

BlastEm is a detector (docs/validation.md): these check that the two sides are put
into one form, not that either is right.
"""

from __future__ import annotations

import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import blastem_events as be  # noqa: E402
import vdp_streams as vs  # noqa: E402


def _log(events: bytes, tmp_path: Path) -> Path:
    path = tmp_path / "test.bel"
    path.write_bytes(b"BLSTEL\x02\x00" + bytes([1, 1, 4]) + b"TEST" + zlib.compress(events))
    return path


def _event(kind: int, payload: bytes) -> bytes:
    return bytes([kind << 4]) + payload  # a short header, delta 16


def test_the_128k_rearrangement_is_kabutos():
    # "Writes to address a in 128K mode end up in the 68k VRAM address
    # (((a & 2) >> 1) ^ 1) | ((a & $400) >> 9) | a & $3FC | ((a & $1F800) >> 1)"
    assert vs.target_128k(0x0000) == 0x0001
    assert vs.target_128k(0x0002) == 0x0000
    assert vs.target_128k(0x0400) == 0x0003
    assert vs.target_128k(0x0800) == 0x0401
    assert vs.target_128k(0x0001) == vs.target_128k(0x0000)  # bit 0 is ignored


def test_blastem_events_take_the_boards_store_layout(tmp_path):
    body = _event(be.VDP_REG, bytes([1, 0x04]))  # mode 5
    body += _event(be.VRAM_BYTE, bytes([0x12, 0x34, 0xAB]))
    body += _event(be.VRAM_WORD, bytes([0x00, 0x04, 0x00, 0x12, 0xCD]))
    body += _event(be.VDP_INTRAM, bytes([0x06, 0x0E, 0xEE]))  # CRAM byte address 6
    body += _event(be.VDP_INTRAM, bytes([128 + 5, 0x07, 0xFF]))  # VSRAM word 5
    streams, meta = vs.read_blastem(_log(body, tmp_path))
    assert list(streams.values["reg"]) == [1 << 8 | 0x04]
    # a byte at address a lands at a ^ 1, as the board's _vram_write_byte stores it
    assert list(streams.values["vram"]) == [0x1235 << 8 | 0xAB, 0x0003 << 8 | 0xCD]
    assert list(streams.values["cram"]) == [3 << 16 | 0x0EEE]
    assert list(streams.values["vsram"]) == [5 << 16 | 0x07FF]
    assert meta["frames"] == 1


def test_replay_gives_the_state_at_the_end_of_each_frame():
    streams = vs.Streams()
    streams.add("cram", 1 << 16 | 0x0AA, vs.where(0, 10))
    streams.add("cram", 1 << 16 | 0x0BB, vs.where(1, 10))
    streams.add("cram", 2 << 16 | 0x0CC, vs.where(3, 10))
    states = dict(vs.replay(streams, "cram", [0, 1, 2, 5]))
    assert states[0][1] == 0x0AA
    assert states[1][1] == 0x0BB
    assert states[2][2] == 0
    assert states[5][2] == 0x0CC


def test_copy_bytes_can_be_left_out_of_a_replay():
    streams = vs.Streams()
    streams.add("vram", 0x10 << 8 | 0x11, vs.where(0, 0))
    streams.add("vram", vs.COPY | 0x10 << 8 | 0x22, vs.where(0, 1))
    ((_, kept),) = vs.replay(streams, "vram", [0], skip_copy=True)
    ((_, all_),) = vs.replay(streams, "vram", [0])
    assert kept[0x10] == 0x11
    assert all_[0x10] == 0x22
