"""The VDP's write streams, from the board and from a BlastEm event log, and where they
first part.

This is the instrument for chasing a picture fault back to data: not "which frame looks
wrong" but "which write to VDP memory is the first one the two machines disagree on".
BlastEm is a **detector** (docs/validation.md): a divergence found here says where to
look, never which side is right.

Four streams, each compared in its own order, because a register write reaches the VDP
at once while a memory write waits in the FIFO, so the two kinds interleave differently
on two machines that agree on both:

========  ============================  ==============================================
stream    one entry                     canonical form
========  ============================  ==============================================
``reg``   a register write              ``number << 8 | value`` (register 7 masked to
                                        6 bits, as BlastEm logs it)
``vram``  one byte reaching VRAM        ``index << 8 | byte``, ``index`` being where it
                                        lands in the VDP's byte-swapped store: the
                                        board's ``vram`` and BlastEm's ``vdpmem`` use
                                        the same layout, so it is one number for both
``cram``  a CRAM word                   ``index << 16 | (value & 0x0EEE)``
``vsram`` a VSRAM word                  ``index << 16 | (value & 0x07FF)``
========  ============================  ==============================================

Every entry also carries *where* it happened, packed ``frame << 10 | line``: on the
board the VDP's frame count and V counter, in BlastEm's log the frame and line its
clock falls in (:meth:`blastem_events.Reader.frame_of`). Frame numbers are **not** a
common clock between the two (docs/handoff-2026-09-24.md); they are there to find the
spot in each run, and the streams are aligned by position alone.

Three things BlastEm's log cannot carry, the first two found by reading ``vdp.c`` at
the pinned revision (542164d) before building on it, the third later:

- **A VRAM copy DMA writes VRAM without logging it** (``external_slot``, 1263-1266:
  ``write_vram_byte`` with no ``event_vram_byte``, where the FIFO path at 1186-1192
  logs). So the board's copy-DMA bytes are flagged (:data:`COPY`) and left out of the
  comparison, and a count of them is reported: VRAM rebuilt from the log is short of
  exactly those.
- **In 128 KB mode BlastEm stores a write to an odd address** where the board, after
  Kabuto ("writing to odd addresses does not seem to have an effect"), stores nothing.
  Both sides compute where it *would* land and flag it (:data:`ODD128`), so the streams
  stay aligned and the difference is counted rather than hidden.
- **Address bit 16 of a 64 KB-mode write.** ``event_vram_byte`` takes a ``uint16_t``
  (``event_log.c`` 424), so a write through $10000-$1FFFF is logged at its 64 KB
  address. VRAM rebuilt from the log is still right (the chip is 64 KB), but the
  sprite cache is not: it compares all 17 bits (docs/undocumented-behavior.md, VDP
  item 24), so a cache rebuilt from the log is wrong wherever a program uses bit 16,
  as Overdrive 2's rain does. Read BlastEm's cache from a save state of its libretro
  core instead (``retro_serialize``; ``vdp_serialize`` writes ``sat_cache`` after
  VRAM, CRAM and VSRAM) -- docs/worklog.md, "The rain".
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import blastem_events as be

KINDS = ("reg", "vram", "cram", "vsram")
#: Flag bits above the canonical value.
COPY = 1 << 40
ODD128 = 1 << 41
VALUE = (1 << 40) - 1


def target_128k(address: int) -> int:
    """Where a 128 KB-mode write lands in the byte-swapped store: Kabuto's rearrangement,
    which is also BlastEm's ``write_vram_word`` (``vdp.c`` 401-407), bit 0 ignored."""
    return (
        (((address & 2) >> 1) ^ 1)
        | ((address & 0x400) >> 9)
        | (address & 0x3FC)
        | ((address & 0x1F800) >> 1)
    ) & 0xFFFF


def where(frame: int, line: int) -> int:
    return frame << 10 | (line & 0x3FF)


class Streams:
    """Four streams, each a value array and a parallel *where* array."""

    def __init__(self) -> None:
        self.values = {kind: array("Q") for kind in KINDS}
        self.wheres = {kind: array("Q") for kind in KINDS}

    def add(self, kind: str, value: int, at: int) -> None:
        self.values[kind].append(value)
        self.wheres[kind].append(at)

    def save(self, directory: Path, meta: dict) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        for kind in KINDS:
            with open(directory / f"{kind}.values", "wb") as f:
                self.values[kind].tofile(f)
            with open(directory / f"{kind}.wheres", "wb") as f:
                self.wheres[kind].tofile(f)
        meta = dict(meta, counts={kind: len(self.values[kind]) for kind in KINDS})
        (directory / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")

    @classmethod
    def load(cls, directory: Path) -> tuple[Streams, dict]:
        streams = cls()
        meta = json.loads((directory / "meta.json").read_text())
        for kind in KINDS:
            for name, store in (("values", streams.values), ("wheres", streams.wheres)):
                path = directory / f"{kind}.{name}"
                data = array("Q")
                data.frombytes(path.read_bytes())
                store[kind] = data
        return streams, meta


# -- the board ---------------------------------------------------------------------------


class _Recording(list):
    """A VDP memory list that reports every store."""

    def __init__(self, items, record) -> None:
        super().__init__(items)
        self._record = record

    def __setitem__(self, index, value) -> None:
        super().__setitem__(index, value)
        self._record(index, value)


def record_board(system, streams: Streams) -> None:
    """Wrap one :class:`System`'s VDP so every write lands in ``streams``."""
    vdp = system.vdp
    add = streams.add
    state = {"copy": False}

    def at() -> int:
        return where(vdp.frame, vdp.v)

    write_register = vdp._write_register

    def _write_register(number: int, value: int) -> None:
        add("reg", number << 8 | ((value & 0x3F) if number == 7 else value & 0xFF), at())
        write_register(number, value)

    vram_write_byte = vdp._vram_write_byte

    def _vram_write_byte(address: int, value: int) -> None:
        flag = COPY if state["copy"] else 0
        add("vram", flag | ((address ^ 1) & 0xFFFF) << 8 | (value & 0xFF), at())
        vram_write_byte(address, value)

    vram_write_128k = vdp._vram_write_128k

    def _vram_write_128k(address: int, data: int) -> None:
        flag = (COPY if state["copy"] else 0) | (ODD128 if address & 1 else 0)
        add("vram", flag | target_128k(address) << 8 | (data & 0xFF), at())
        vram_write_128k(address, data)

    copy = vdp._copy

    def _copy() -> None:
        state["copy"] = True
        try:
            copy()
        finally:
            state["copy"] = False

    vdp._write_register = _write_register
    vdp._vram_write_byte = _vram_write_byte
    vdp._vram_write_128k = _vram_write_128k
    vdp._copy = _copy
    vdp.cram = _Recording(vdp.cram, lambda i, v: add("cram", i << 16 | (v & 0x0EEE), at()))
    vdp.vsram = _Recording(vdp.vsram, lambda i, v: add("vsram", i << 16 | (v & 0x07FF), at()))


def run_board(image: Path, machine: str, frames: int, fm: bool) -> tuple[Streams, dict]:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    from megadrive_python.__main__ import load_image
    from megadrive_python.cartridge import Cartridge
    from megadrive_python.machine import MACHINES
    from megadrive_python.system import System

    system = System(MACHINES[machine], Cartridge.from_image(load_image(image)))
    system.board.fm.enabled = fm
    streams = Streams()
    record_board(system, streams)
    started = time.perf_counter()
    for _ in range(frames):
        system.run_frame()
        system.board.fm.take_samples()
        system.board.psg.take_samples()
    vdp = system.vdp
    #: The VDP's memories as they stand at the end, so a state rebuilt from the
    #: streams can be checked against the real one (:func:`replay`).
    final = {
        "reg": [(r & 0x3F) if n == 7 else r for n, r in enumerate(vdp.registers[:24])],
        "vram": bytes(vdp.vram).hex(),
        "cram": [c & 0x0EEE for c in vdp.cram],
        "vsram": [v & 0x07FF for v in vdp.vsram],
    }
    meta = {
        "final": final,
        "source": "board",
        "image": image.name,
        "machine": machine,
        "frames": frames,
        "fm": fm,
        "seconds": round(time.perf_counter() - started, 1),
        "m68k_instructions": system.m68k_instructions,
        "python": f"{sys.implementation.name} {sys.version.split()[0]}",
    }
    return streams, meta


# -- BlastEm -----------------------------------------------------------------------------


def read_blastem(log: Path) -> tuple[Streams, dict]:
    reader = be.Reader(log.read_bytes())
    streams = Streams()
    add = streams.add
    frame_cycles = reader.frame_cycles
    mode5 = False
    line_cycles = be.LINE_CYCLES
    for event in reader:
        kind = event.kind
        cycle = event.cycle
        at = where(cycle // frame_cycles, (cycle % frame_cycles) // line_cycles)
        if kind == be.VDP_REG:
            if event.address == 1:
                mode5 = bool(event.value & 0x04)
            add("reg", event.address << 8 | event.value, at)
        elif be.VRAM_BYTE <= kind <= be.VRAM_BYTE_AUTO:
            if not mode5:
                raise ValueError(f"a mode 4 VRAM write at cycle {cycle}: not mapped here")
            add("vram", ((event.address ^ 1) & 0xFFFF) << 8 | event.value, at)
        elif kind in (be.VRAM_WORD, be.VRAM_WORD_DELTA):
            flag = ODD128 if event.address & 1 else 0
            add("vram", flag | target_128k(event.address) << 8 | (event.value & 0xFF), at)
        elif kind == be.VDP_INTRAM:
            if event.address < 128:
                add("cram", ((event.address >> 1) & 63) << 16 | (event.value & 0x0EEE), at)
            else:
                add("vsram", (event.address & 63) << 16 | (event.value & 0x07FF), at)
    if reader.pos != len(reader.body):
        raise ValueError("the log's body was not consumed exactly: the decode desynced")
    meta = {
        "source": "blastem",
        "log": log.name,
        "name": reader.name,
        "video_std": reader.video_std,
        "frames": reader.frame_of(reader.cycle) + 1,
        "body_bytes": len(reader.body),
    }
    return streams, meta


# -- state -------------------------------------------------------------------------------

SIZES = {"reg": 24, "vram": 0x10000, "cram": 64, "vsram": 64}


def replay(streams: Streams, kind: str, frames: list[int], *, skip_copy: bool = False):
    """The state one stream builds, as it stands at the end of each frame in ``frames``
    (every write stamped with that frame or earlier), from a zeroed memory.

    Yields ``(frame, state)`` in order; ``state`` is a fresh copy each time.
    """
    state = bytearray(SIZES[kind]) if kind == "vram" else [0] * SIZES[kind]
    values, wheres = streams.values[kind], streams.wheres[kind]
    targets = sorted(frames)
    t = 0
    for value, at in zip(values, wheres, strict=True):
        while t < len(targets) and at >> 10 > targets[t]:
            yield targets[t], state[:]
            t += 1
        if t == len(targets):
            return
        if skip_copy and value & COPY:
            continue
        value &= VALUE
        if kind in ("vram", "reg"):
            state[value >> 8] = value & 0xFF
        else:
            state[value >> 16] = value & 0xFFFF
    while t < len(targets):
        yield targets[t], state[:]
        t += 1


# -- comparing ---------------------------------------------------------------------------


def _describe(kind: str, value: int) -> str:
    flags = ("+copy" if value & COPY else "") + ("+odd128" if value & ODD128 else "")
    v = value & VALUE
    if kind == "reg":
        return f"r{v >> 8}={v & 0xFF:02X}"
    if kind == "vram":
        return f"[{v >> 8:04X}]={v & 0xFF:02X}{flags}"
    return f"[{v >> 16:02X}]={v & 0xFFFF:03X}"


def _at(value: int) -> str:
    return f"f{value >> 10}:l{value & 0x3FF}"


def first_divergence(board: Streams, blastem: Streams, kind: str, context: int = 8) -> dict:
    """Where one stream first parts, with the board's copy-DMA bytes left out (BlastEm
    does not log them)."""
    bv, bw = board.values[kind], board.wheres[kind]
    keep = [i for i in range(len(bv)) if not bv[i] & COPY] if kind == "vram" else None
    if keep is not None and len(keep) == len(bv):
        keep = None
    ev, ew = blastem.values[kind], blastem.wheres[kind]
    n_board = len(keep) if keep is not None else len(bv)

    def b(i: int) -> int:
        return bv[keep[i]] if keep is not None else bv[i]

    def bwhere(i: int) -> int:
        return bw[keep[i]] if keep is not None else bw[i]

    limit = min(n_board, len(ev))
    i = 0
    while i < limit and (b(i) & VALUE) == (ev[i] & VALUE):
        i += 1
    result = {
        "kind": kind,
        "board": n_board,
        "board_copy_excluded": len(bv) - n_board,
        "blastem": len(ev),
        "agree": i,
    }
    if i == limit:
        result["divergence"] = None
        return result
    lo = max(0, i - context)
    result["divergence"] = i
    result["board_at"] = _at(bwhere(i))
    result["blastem_at"] = _at(ew[i])
    result["board_context"] = [
        f"{j}: {_describe(kind, b(j))} @{_at(bwhere(j))}"
        for j in range(lo, min(n_board, i + context))
    ]
    result["blastem_context"] = [
        f"{j}: {_describe(kind, ev[j])} @{_at(ew[j])}" for j in range(lo, min(len(ev), i + context))
    ]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    rb = sub.add_parser("board", help="run the board and save its streams")
    rb.add_argument("image", type=Path)
    rb.add_argument("--out", type=Path, required=True)
    rb.add_argument("--machine", default="megadrive")
    rb.add_argument("--frames", type=int, required=True)
    rb.add_argument("--fm", action="store_true", help="run the FM generator too")
    rl = sub.add_parser("blastem", help="read a BlastEm log and save its streams")
    rl.add_argument("log", type=Path)
    rl.add_argument("--out", type=Path, required=True)
    rd = sub.add_parser("diff", help="first divergence of each stream")
    rd.add_argument("board", type=Path)
    rd.add_argument("blastem", type=Path)
    rd.add_argument("--context", type=int, default=8)
    args = parser.parse_args()

    if args.command == "board":
        streams, meta = run_board(args.image, args.machine, args.frames, args.fm)
        streams.save(args.out, meta)
        print(json.dumps(dict(meta, counts={k: len(streams.values[k]) for k in KINDS})))
    elif args.command == "blastem":
        streams, meta = read_blastem(args.log)
        streams.save(args.out, meta)
        print(json.dumps(dict(meta, counts={k: len(streams.values[k]) for k in KINDS})))
    else:
        board, board_meta = Streams.load(args.board)
        blastem, blastem_meta = Streams.load(args.blastem)
        print(json.dumps({"board": board_meta, "blastem": blastem_meta}, indent=1))
        for kind in KINDS:
            print(json.dumps(first_divergence(board, blastem, kind, args.context), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
