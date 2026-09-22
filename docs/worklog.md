# Work log

What was run, when, with what command and result; every decision with its reason;
every open question for the user, marked `QUESTION:`. Newest entries at the end of
each section. The reviewing session reads this first.

## Session 1 (2026-09-22): milestones 1 and 2

### How the brief was read

Scope is milestones 1 and 2 only: the board with both CPUs checked in a two-CPU MAME
lockstep (at least five games, at least 30 s each), then the VDP's ports, FIFO, DMA,
counters and interrupts judged by Nemesis's HV/status captures and VDPFIFOTesting.
No pixels, no sound. Stop after milestone 2 and write the summary.

### Environment

- Repository at `9459975` when the session started; m68000-python pinned at
  `51ee1a5a1ac9be2a773476d264e859adb2fb6c94` (its HEAD, clean, "Add the claim
  boundary: docs/claims.md"); z80-python 0.4.0 from PyPI.
- `.venv`: CPython 3.14.4. `.venv-pypy`: PyPy 7.3.23 (Python 3.11.15), from
  `~/.local/bin/pypy3.11`. Both made with
  `python -m venv`, then `pip install /data/emu/m68000-python "z80-python>=0.4.0"
  pytest ruff==0.16.8` and `pip install --no-deps -e .` (the local clone at the pinned
  revision stands in for the git URL pinned in `pyproject.toml`, which CI uses).
- `python scripts/fetch_references.py --verify`: "all pins satisfied".
- MAME 0.285 (`/usr/games/mame -version`: `0.285 (unknown)`).

### Proposed core change (m68000-python)

The board places each 68000 bus access at its own clock inside the instruction.
m68000-python adds 4 to `_cycles` before calling the host for each access
(`_core.py`, `_read_word` and friends), so inside a bus callback `_cycles - 4` is the
access's start, counted from the start of the step. The board reads that private
attribute in exactly one place, `Board.m68k_access_clock`, labelled as a workaround.

Proposed PR for m68000-python: a read-only property

```python
@property
def step_clocks(self) -> int:
    """Clocks spent so far in the step in progress; inside a bus callback, the
    clocks before this access plus its own 4 (the access is counted as it starts)."""
    return self._cycles
```

documented in the embedding contract, with a test that a host callback sees 4, 8, 12
for the three reads of `MOVE.L (A0),D0`'s prefetch-read-read pattern (or whatever
order the corpus records). Not made from here: the brief forbids changing the core.

Update (2026-09-22, from the coordinator): the change is proposed as **m68000-python
PR #1** (branch `step-clocks`, CI green, not merged): a read-only property
`M68000CPU.step_clocks` returning `_cycles`; inside a bus callable the access occupies
`step_clocks - 4 .. step_clocks`; inside `acknowledge` the acknowledge cycle is not yet
counted; after `step()` it equals the step's total; the core models no wait states,
so the host adds its stalls to `step()`'s total (as `System.step` does). The board
keeps its labelled workaround in `Board.m68k_access_clock` (one line to change to
`cpu.step_clocks` when the PR merges); the pinned revision is unchanged and the
branch is not installed.

### Milestone 1: the board and both CPUs

#### Decisions

- **Scheduler** (`src/megadrive_python/system.py`, docstring): master clocks; the
  68000 leads; the VDP catches up lazily (when touched, and at the start of each
  68000 step); each 68000 access is placed at its own clock
  (`Board.m68k_access_clock`); stalls are added to the step of the CPU that suffers
  them (`Board.stall`); the Z80 runs in the 68000's wake and is caught up before
  every 68000 access that changes what it can see (its RAM, bus request, reset, the
  YM2612, the bank). Reason: the 68000's accesses are the ones that must land on the
  right clock (FIFO and slots), and m68000-python can say where each falls;
  z80-python reports only instruction totals.
- **The Z80 gives up its bus at the end of a machine cycle**, not of an instruction:
  "BUSREQ ... is sampled by the CPU with the rising edge of the last clock period of
  any machine cycle" (Zilog UM0080, specification tier). z80-python has no
  machine-cycle hook, so `System.z80_access_offsets` runs the next instruction once
  on a probe bus (side-effect free: RAM and ROM only, the CPU state restored) and
  estimates when each data access starts (4 T for M1 fetches, 3 for other fetches
  and data accesses, internal T-states unplaced, so each estimate is the earliest).
  An instruction under way at a bus request whose accesses are not all started is
  run after the bus comes back. An approximation, to be judged in milestone 3/5 by
  MoD's `gen_test_z80_bus` ("BUSACK delay", "Delay with 6T M-cycle").
- **The bus grant is immediate** in the board (the $A11100 bit reads "granted" as
  soon as the request is written), as in MAME. MoD measured 2 cycles from /BR to
  /BG with a logic analyser (forum t=1661); the board does not model that yet.
  Open until `gen_test_z80_bus` is run.
- **Byte and word rules** follow CMD `gen-hw.txt` 1.2 (hardware-checked) where MAME
  differs: a word read of Z80 RAM duplicates the even byte (MAME returns both bytes);
  a word write to Z80 space writes the MSB; the Z80 area without the bus reads the
  next opcode's MSB with a zero LSB (MAME: `machine().rand()`); $A11100 reads the
  next opcode's MSB with bit 0 (byte) or bit 8 (word) the grant, LSB zero (MAME:
  rand in the other bits). A 68000 byte write drives the byte on both halves of the
  data bus (68000 UM 5.1.2), so $A11100/$A11200 see bit 0 of a byte written to
  either address.
- **Open bus is IRC**: CMD's "next instruction to be fetched" is, at a data access in
  m68000-python, `irc` (the queue has moved past the extension words).
- **The Z80 reading 68000 RAM through its bank returns $FF** (MoD's test, several
  machines: reads fail; CMD: "returns FFh"). MAME returns the RAM.
- **Cartridge ROM** is padded with $FF to a power of two and mirrored through 4 MB,
  as MAME loads it (detector; no hardware source in reference/ says what a board
  decodes).
- **The I/O chip's power-on values** are CMD 3.0's ($7F data, 0 control, $FF TxData).

#### The lockstep and what it found

`scripts/mame_trace.py` (recording) and `scripts/lockstep_mame.py` +
`scripts/mame_log.py` (replay). Findings, in the order they were diagnosed:

1. **The Z80's `totalcycles` is not a master-clock time.** It stands still while the
   Z80 is held in reset and restarts from about 0 when MAME resets it (Sonic: the
   first Z80 instruction at count 0 although the 68000 released it at its cycle 984;
   after a later reset the count goes from 168,824 to 10). While the 68000 holds the
   bus it keeps counting, but by MAME's timeslices, not exactly the hold (a 347.2-
   cycle hold shows as a 348-354-cycle jump; a 389.2 one as 392). So
   `docs/mame-oracle.md` trap 2's "the 68000's totalcycles × 7 and the Z80's × 15 are
   both master clocks from power-on" is wrong for the Z80; fixed in its own commit.
   An attempt to merge by count with re-anchoring at each release drifted by tens of
   cycles a frame and, once a stall was attributed to the wrong request, stayed a
   whole frame out.
2. **MAME's Z80 honours BUSREQ only at an instruction boundary** (MAME 0.285
   `src/devices/cpu/z80/z80.lst`, the `ffff` block before each opcode fetch), but an
   instruction under way when the 68000 writes the request finishes in the Z80's
   next timeslice, *after* the 68000's own: Sonic's 68000 reads $A01FFD and gets $FD
   while the Z80's `LD (HL),H` at $00A2 (writing $1F there) was logged before the
   request (MAME Z80 cycle 10,242,048, 68000 cycle 66,545,929). Hardware releases at
   the end of the machine cycle (UM0080), which gives the same result here.
3. **The fix for the merge: markers.** `scripts/mame_trace.py` now also watches, in
   the Z80's run, the 68000's accesses to $A00000-$A0FFFF, $A11100 and $A11200
   (`wpset a00000:maincpu,...`), logged with the Z80's own count. Each marker places
   one 68000 access in the Z80's stream exactly where MAME interleaved it. The
   lockstep runs the Z80 up to the marker of each such 68000 access; a record the
   marker falls inside is run before it if the board's access-offset model has all
   its accesses started by the marker's count, after it otherwise. Between markers
   the Z80 runs by its count, anchored at the last marker.
4. MAME adds one 68000 clock to every access to $A00000-$A0FFFF
   (`before_delay(1)`, `megadriv.cpp`, "required by pacman2 intro", "TODO:
   unverified"), and stalls the 68000 for length / 3.5 ns on a 68000-to-VDP DMA
   ("a hack until real DMA timings are implemented", `315_5313.cpp`). Both are
   sorted as named classes of clock difference.
5. **MAME clears the Z80's R (and I) on reset; z80-python 0.4.0 does not.** Zilog
   UM0080: RESET "clears the Program Counter and registers I and R". Found when R was
   added to the trace (Sonic 3's driver does `LD A,R`). The board clears I and R
   itself after a reset (`System._z80_reset_line`, labelled). **Proposed change for
   z80-python** (not made from here): `_accept_reset` should set `self.i = 0` and
   `self.r = 0`, with a test citing UM0080's reset description.
6. **MAME's Z80 count need not restart at a reset** (Phantasy Star IV: 168,824 then
   230,489), so a record the reset cut short is recognised by its markers (a reset
   written while the record is under way), not by the count alone; the order the
   Z80's next registers require (finished before the reset, or cut) is then checked.
7. The in-flight instruction around a 68000 access: the markers' Z80 count does not
   say how far into its instruction the Z80 was (MAME stops a slice mid-instruction),
   so the lockstep decides with the board's machine-cycle estimate and, if the Z80's
   next registers or the 68000's read of Z80 RAM show MAME ran it on the other side,
   replays it in that order and counts a named difference. Totals below.
8. Battery RAM starts at $FF (as MAME: Sonic 3 reads its FRAM with `MOVEP.W` before
   writing it). No hardware source says what an unwritten chip holds.

#### Milestone 1 acceptance: result (2026-09-22)

Command, per game: `python scripts/mame_trace.py genesis GAME.zip --seconds 30 --out
tests/mame_traces/genesis-NAME` (MAME 0.285, `genesis` driver, 6-7 min each), then
`.venv-pypy/bin/python scripts/lockstep_mame.py tests/mame_traces/genesis-NAME`
(PyPy 7.3.23 / Python 3.11.15; the six in parallel under `nice`). Code at the commit
that adds this entry; m68000-python 51ee1a5, z80-python 0.4.0.

| Game (No-Intro, local romset) | image SHA-256 (first 16) | 68000 instr. | Z80 instr. | 68000 ints | Z80 ints | unexplained | wall |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Sonic The Hedgehog (USA, Europe) | 46160baa06362c71 | 23,286,815 | 12,728,465 | 1,495 | 0 | 0 | 111 s |
| Streets of Rage 2 (USA) | 4a314edbfee92282 | 21,274,844 | 11,269,553 | 1,647 | 0 | 0 | 106 s |
| Gunstar Heroes (USA) | f177810ce614be21 | 20,854,523 | 15,425,681 | 48,152 | 0 | 0 | 112 s |
| Castlevania - Bloodlines (USA) | 5bfe171f5c251600 | 11,343,385 | 8,460,818 | 3,233 | 0 | 0 | 75 s |
| Sonic The Hedgehog 3 (USA) (FRAM board) | d6404843c5ba3248 | 21,741,798 | 11,837,738 | 1,787 | 1,688 | 0 | 109 s |
| Phantasy Star IV (USA) (SRAM board) | 511f35cc11f88316 | 21,254,417 | 13,280,703 | 1,792 | 0 | 0 | 105 s |
| **Total** | | **119,755,782** | **73,002,958** | | | **0** | |

Every register of both CPUs identical before every logged instruction (the 68000's
19 values, the Z80's 13 including R), every 68000 access to the VDP, Z80 space, I/O
and bus/reset registers and every Z80 access to $4000-$FFFF matched in kind, address
and size, every write in value. Interrupts are taken where MAME took them (the VDP is
a stub). Named differences (MAME's value given to the CPU), summed over the six:
YM2612 status replayed (stub) 90,707; $A11100's non-grant bits (MAME `rand()`, board
next opcode's MSB) 47,067 (per-game counts in `tests/mame_traces/results/`,
not committed); VDP reads replayed (stub) 10,879; Z80-RAM reads inside a Z80
instruction, sorted to MAME's side by value 516; Z80 instructions replayed in the other
order around a 68000 access 47; the I/O data latch's bit 7 before any write (MAME 1,
CMD 0) 1. Every 68000 clock interval equals the core's, or differs by MAME's
`before_delay(1)` per $A0xxxx access, or by MAME's DMA spin, or is an interrupt entry
that agrees; every Z80 interval agrees or spans a bus-request hold. What is *not*
claimed: the timing of the Z80's bus handover (MAME's is scheduler-shaped; the board's
is an estimate), the arbiter's delays, the VDP, the YM2612, and anything the stubs
replay.

**End of milestone 1.** Carrying on with milestone 2.
