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

### Milestone 2: the VDP's ports, FIFO, DMA, counters and interrupts

#### Decisions

- **The VDP is Exodus's model, written anew.** Exodus (MIT, by Nemesis, the author
  of both judges) is the documented source for the port, FIFO, read-cache, DMA and
  slot rules; `vdp.py` cites file and line for each (`S315-5313_Ports.cpp`,
  `S315-5313_General.cpp`, `S315-5313_Timing.cpp`). No code is copied (Exodus is MIT,
  but the rule is to write our own); GPL emulators were not read for this.
- **The refresh stalls** (`board.py`). Kabuto describes the cartridge-side one
  ("every time 128 cycles have passed the next bus access is slowed down by 2
  cycles") and says work RAM has its own. To place both to the MCLK I built
  Nuked-MD (die-derived, GPL-2.0, `reference/src/Nuked-MD` @ 594ca2a) headless in
  `reference/build-nuked-md/` (ignored: `headless.c` replaces its SDL audio/video,
  logs the 68000 bus, `-DTMSS_ENABLE=0`, and the capture ROMs patched with a
  `move.l #'SEGA',$A14000` stub at $1F000; `gcc -O2 -w
  -Wno-incompatible-pointer-types`), ran probe loops and the capture ROMs, and fitted
  the rules to its bus logs: a fixed 896-MCLK grid whose tick the next bus cycle
  absorbs, +14 MCLK only if that cycle is a cartridge access (670 of 670 ticks in
  four probe loops); the next work-RAM access once due +21 MCLK, due again ~805 MCLK
  after (the capture loop allows 743-826). On Nuked-MD's own run of the capture ROM
  the rules reproduce every one of its 76,800 bus cycles' stretches (log of one run,
  2.2 M MCLK). Nuked-MD is a detector here; the judge is the captures, whose loop
  rate (143.588 MCLK an instruction) the rules give. Nuked-MD shows other RAM-stretch
  lengths (7/35 MCLK) in other loops, not modelled.
- **What a capture fit may set, and nothing else**: where in its line the VDP is,
  the 9-bit V and ODD, a pending H mode, the two refresh phases. Everything else is
  the board's, the same for every dump. The fit first missed on some dumps with
  cartridge stalls every few ticks: running Nuked-MD on the capture ROM at 16 button
  times reproduced the misses, the board's rules matched Nuked-MD's bus to the
  cycle, and the fault was the fitter's coordinate descent; it is now a search over
  all phase pairs.
- **HV sampling.** A read samples at the first pixel-clock edge at or after 3 MCLK
  into its bus cycle (`HV_SAMPLE_DELAY`); fitted, as no document gives it. The reads
  that disagreed then all fell at one phase each (the counter stepping as sampled)
  and showed ripple-counter values; the two consoles differ there, so the board
  carries both measured units (`HV_UNITS`) and picks one by region
  (`Machine.hv_unit`). Where the hardware's own reads at a phase vary, the majority
  is taken and the rest counted apart. This is fitting the judge's data to the
  judge; it is stated so wherever it appears.
- **Later runs.** Nemesis's ROM runs the loop, sends RAM, waits for C and jumps back
  to the loop, so dumps 2-9 of a capture start from the first run's state: F set, V
  mode and interlace latched (`later_run`). Dump 1 is the only one after power-on:
  there F reads clear until the first VBlank, in 27 of the 28 SR-type captures.
  Nuked-MD probes showed why: in mode 4 (power-on) any register write clears F; in
  mode 5 none does. Modelled in `VDP._control_write`.
- **VDPFIFOTesting is read from VRAM**, page by page with Start, each result byte's
  palette giving match or mismatch, as sms-python's `run_vdptest.py`.

#### Runs and results (2026-09-22)

- `.venv-pypy/bin/python scripts/run_hv_captures.py` (commit 4743f9f; 288 dumps,
  PyPy 7.3.23 / Python 3.11.15, 16 processes, 76-78 s): **8,762,415 of 8,847,360
  words equal (99.04 %)**. The rest: 61,440 in two unplaced dumps ("HV noint intdub
  NTSCJ H40V28" 1, whose H low nibble reads 3 in every sample and whose status shows
  DMA busy; "HV noint intinv PAL H40V30" 1); 11,103 in "HV noint intinv PAL H40V28"
  6 (H one step behind in every second read); 9,869 reads at a stepping phase; 1,288
  the F flag in "HV noint intnorm PAL H32V30" 1 (set from the start: the one first
  run that is); 705 VBlank (668 in the first runs of "intnorm NTSCJ H32V28" and
  "H40V28", set before the first VBlank); 228 HBlank at its edges; 302 H/V reads,
  mostly interlaced NTSC V30 H32; 10 sprite overflow. Per capture in
  `tests/hv_captures_baseline.json`.
- `.venv-pypy/bin/python scripts/run_vdpfifotesting.py --machine M` (commit 4743f9f;
  ROM `VDPFIFOTesting/VDPFIFOTesting.bin` SHA-256 8b0ed3944893d679…, 3-4 s): 122 of 122
  on genesis, genesis-tmss, genesis2, megadrive, megadrive-tmss, megadrive2,
  megadrive-japan, megadrive2-japan; genesis3 114 of 122 (the eight DMA Fill to VSRAM
  tests). The four intermittent tests pass. On the way: 110 of 122 as first written;
  the VSRAM-past-the-end read (+8), the transfer's start address (+3), and the
  transfer holding the 68000's next bus cycle rather than the write that started it
  (+1, FIFO Wait States, which Exodus fails) fixed it.
- Lockstep sanity with the new VDP (commit f2055a5): Sonic The Hedgehog (USA,
  Europe) and Gunstar Heroes (USA) re-recorded (MAME 0.285, 30 s) and replayed:
  23,286,815 / 20,854,523 68000 and 12,728,465 / 15,425,681 Z80 instructions, 0
  unexplained each, 44 s / 47 s. Traces deleted.
- `pytest` (commit 8babd34): 14 passed on PyPy (84 s) and CPython 3.14.4 (178 s),
  the milestone-1 replay skipped (no logs kept).
- A standalone check: Sonic The Hedgehog runs 300 frames with the Z80 driving the
  YM2612 (27,032 FM writes by frame 300), 686,617 68000 steps a second of emulated
  time; 60 NTSC frames in 0.36 s on PyPy.

#### Milestone 2 acceptance: what passes and what does not

- **VDPFIFOTesting: passes** as the brief states it: every test Nemesis reports
  passing on hardware passes; the intermittent ones are listed (they pass); the
  Genesis 3 variant fails VSRAM tests. Which VSRAM tests a Genesis 3 fails is not
  published, so only "some, and only VSRAM ones" is checked.
- **The captures: does not pass as stated** ("equal the captures sample for sample
  in every mode captured"): 99.04 % of words. Most of the rest is not deterministic
  on the hardware (the same console reads differently at the same phase, between and
  within runs) or is in four anomalous dumps; about 2,500 differences remain
  unexplained, listed above and in docs/timing.md. The per-mode tables the brief
  asked for are in docs/timing.md, "What the captures say".

QUESTION: The capture acceptance cannot be met literally; the board takes the
majority reading at the phases where the hardware's own reads vary. Is 99.04 % with
every remaining difference classified acceptable, or should the unexplained ~2,500
(HBlank edges, first-run VBlank, NTSC interlaced V30) be pursued before milestone 3?

QUESTION: Two captured consoles read the counters differently where they step. The
board picks the PAL unit's rules for PAL machines and the NTSC-J unit's for NTSC
ones (`Machine.hv_unit`). Keep that, or make one the default for all?

QUESTION: The work-RAM refresh interval (805 MCLK) and stretch (21 MCLK) come from
Nuked-MD, judged only by the capture loop's rate. Accept Nuked-MD as the basis for
the work-RAM refresh until a hardware measurement exists?

QUESTION: CI needs read access to the private m68000-python repository: add a
secret `M68000_PYTHON_TOKEN` (a fine-grained token with read access to it), or
make m68000-python public, or vendor a wheel?

QUESTION (from milestone 1, still open): the Z80's interrupt length and the Z80
bus-grant delay (MoD: 2 cycles /BR to /BG) are not modelled; they wait for MoD's
`gen_test_z80_bus` in milestone 3.

**End of milestone 2.** Stopping here, as the working agreement says.

## Decisions (the user, 2026-09-22), answering milestone 2's questions

1. **The captures' 99.04 % is accepted for milestone 2.** The claim is stated as
   what it is ("8,762,415 of 8,847,360 words, every difference classified"), never
   as "sample for sample". The four anomalous dumps are worth a cheap look later;
   the ~2,500 unexplained words do not block milestone 3.
2. **The per-console H/V stepping tables stay**, chosen by region
   (`Machine.hv_unit`), **labelled as an assumption**: two consoles were captured,
   so it is not known whether the difference follows the region or the individual
   chip. Say so where the tables are defined and in undocumented-behavior.md.
3. **Nuked-MD is accepted as the basis for the work-RAM refresh** until a hardware
   measurement exists, as the tier rule allows for a die-derived model where no
   hardware source covers the question; the capture loop's rate agrees with it
   independently. Keep it labelled.
4. **CI reaches m68000-python with a read-only deploy key** (an SSH key whose
   public half is a read-only deploy key on m68000-python, private half the
   `M68000_PYTHON_KEY` secret here): narrower than a personal token, since it
   reaches that one repository and nothing else.
5. **The Z80's interrupt length and bus-grant delay** stay for milestone 3, judged
   by MoD's `gen_test_z80_bus`.

Also decided: z80-python's reset not clearing I and R is being proposed as a PR
there by the reviewing session; keep the board's labelled workaround until it lands.

## Session 2 (2026-09-22): milestone 3, the VDP's picture

### How the brief was read

Scope is milestone 3 only: planes A and B with every scroll mode, the window,
sprites (the cache, the phases, the per-line and per-pixel limits, masking, the X = 0
rule), priority, shadow/highlight, H32/H40, V28/V30, both interlace modes, 128 KB
mode; judged by Nemesis's sprite masking ROM against his MD1600 chart, r57shell's VDP
tests against the MD2 report in forum t=1585, CMD's window and colour tests against
his capture photos, MoD's `gen_test_z80_bus`, and MAME's exit frames as a detector.
Render per line, and record frames per second on PyPy. Stop after that.

Also inherited: the Z80's interrupt length and the Z80 bus-grant delay to settle; the
core-side workarounds stay (docs/polish.md, the user's decision to batch them).

### Environment

Same as session 1: `.venv` CPython 3.14.4, `.venv-pypy` PyPy 7.3.23 (Python 3.11.15),
m68000-python pinned at 51ee1a5 (unchanged, as docs/polish.md says), z80-python 0.4.0,
MAME 0.285, machine a 13th-generation Core i9-13900K with 32 threads.
`python scripts/fetch_references.py --verify`: "all pins satisfied" (with the two new
pins below).

### The renderer

`src/megadrive_python/render.py` (560 lines), called by the VDP at each line's
HBlank-clear point -- where its active display begins -- so that an H interrupt
handler's writes, made in the horizontal blank just before, are already in the
registers. Decisions and their sources:

- **A line at a time, not a pixel at a time**, as the brief requires: the line's
  scroll values, both planes' names and patterns, the sprites and the mixing are done
  in one pass. What this loses is written in the module's docstring: a CRAM or register
  write part way *along* a line lands at that line's start (CRAM dots are milestone 5),
  and the sprite phases are evaluated together rather than spread over the two
  preceding lines.
- **Sprites follow Kabuto's phases**, with his X = 0 rule carried from line to line:
  "Rendering of further sprite pixels is disabled whenever a sprite is read from a
  cache slot with X=0 when the previous sprite read (no matter whether that happened on
  the same line, previous line or even a previous frame) had X != 0", and "When there
  are less than 40 sprite tiles on a raster line then further dummy sprites act as if
  they had X=0". Nemesis's four masking rules (forum t=541 start=30) then fall out of
  that one mechanism instead of being coded one by one -- including the one he says
  "Nothing emulates", a mask in the first slot becoming effective when the previous
  line ended on a dot overflow.
- **The sprite cache** is written through by VRAM writes that land in the sprite table,
  checked over the whole 128 KB range and never filled by moving the table (Kabuto).
  It is checked against the address as the program gives it, not the byte-swapped one
  the VDP stores at.
- **Shadow/highlight is Kabuto's three steps** in order, which reproduce his table
  (colour 63 shadows, 62 highlights or returns a shadowed pixel to normal, colours
  14/30/46 cancel a shadow) and Genesis Plus GX's `make_lut_bgobj_ste` case for case.
- **The colour ramp** is TmEE's oscilloscope measurement (forum t=2188, "All MD models
  I tested ... seemed to have identical output"), which Plutiedev's page and MAME's
  palette both come from, so MAME's frames compare as they stand.
- **The debug register's plane forcing** (bits 8-6) is Kabuto's mixer
  `(A|!a)&(B|!b)&(S|!s)&(G|!g)` with one plane held visible; the other bits are not
  modelled.
- **Status bits 6 and 5** (sprite overflow, collision) are set by the renderer and
  cleared when the status register is read, as Genesis Plus GX does; CMD says only that
  both are "most likely cleared at the end of the frame", so this is emulator-derived
  and labelled as such in `vdp.status`.

Found by running the test ROMs against MAME's frames (all of these are in the second
commit's message with their numbers):

1. **Plane sizes.** The invalid width (HSZ = 10b) shows the name table's *first row* on
   every line (CMD), which a 32-cell wrap does not give; the vertical wrap is a mask
   (0x2FF for the invalid height), not a modulus. The row shift and masks are Genesis
   Plus GX's tables, the only statement of the invalid sizes' shape; CMD's sentence
   agrees where the two overlap. `wbug.bin` 91.73 % -> 99.84 % of MAME's pixels.
2. **The window bug** hits the *partial* column the fine scroll puts before plane A's
   first whole 2-cell column, so that column's two cells repeat in the column after it
   -- CMD's "1 column that has identical patterns as the next column". Genesis Plus GX
   reads it the same way (`render_bg_m5`, the `if (start)` branch); MAME reads it as a
   half-column rule with a `(hscroll & 0xf) < 8` test, which is the 112 pixels of
   `wbug.bin` still differing.
3. **Interlace mode 2 addresses patterns as (name & 0x3FF) * 64.** CMD says "Bit zero
   of the name is ignored in interlace mode 2", which would be (name & 0x7FE) * 32;
   Exodus (`CalculatePatternDataRowAddress`), Genesis Plus GX (`GET_LSB_TILE_IM2`) and
   MAME (`get_nametable`, `m_imode == 3`) all take the low bits and multiply by 64.
   Three against one, one of them Nemesis: CMD is wrong here. `wtest_i2.bin`
   74.78 % -> 96.25 %.
4. **Base address masks** as CMD gives them: plane A and B to 0xE000, the window to
   0xF800 and to 0xF000 in H40 ("In 40-cell mode, A11 is always forced to zero" -- the
   board was clearing A10), the scroll table to 0xFC00.
5. **Each row keeps the palette it was drawn with**, so a game that rewrites CRAM part
   way down the frame keeps each band's colours, and a frame is as wide as its widest
   row, so a mid-frame H32/H40 switch is not cropped (CMD photographed one).

### 128 KB mode

Writes only: one byte per word (the other goes to the chip that is not there), bit 0 of
the address ignored, the address bits rearranged by Kabuto's formula, and one access
slot instead of two, which is why "write/DMA speed to VRAM increases to that of CRAM
and VSRAM". What the mode does to the *picture* is not modelled: with one VRAM chip
fitted the hardware shows "garbled display contents", and no pinned test covers it.
Overdrive 2 (milestone 5) is the judge. The board's address register is 16 bits, so
bit 16 -- which 128 KB mode stops ignoring -- is carried only in the sprite cache's
check.

### Milestone 3 acceptance: the runs and their results (2026-09-22)

All at commit `4890f03` unless said; m68000-python 51ee1a5, z80-python 0.4.0,
MAME 0.285.

**Nemesis's sprite masking ROM against his MD1600** (hardware-captured; the judge).
His two photographs are now pinned (`reference/images/MD1600SpriteMask{H32,H40}.png`,
SHA-256 in `scripts/pinned_files.json`) and neither contains a single red pixel, so all
nine tests pass on his machine in both modes. `python scripts/run_sprite_masking.py`
(ROM SHA-256 in the pin file, 2 s): **0 of 18 result cells differ** -- nine tests, two
cells each, H32 and H40. That covers the per-line sprite limit, both dot-overflow
tests, masking in and out of the first slot, masking after a dot overflow, the
non-existent masking mode 2, and the per-frame limits (64 entries in H32, 80 in H40).
The mode switch: the ROM polls bit 5 of $A10003 without making TH an output, so on a
3-button pad it is reading C rather than Start, and the harness presses both.

**MoD's `gen_test_z80_bus`** (hardware-corrected; `gen_test_z80_bus.md`).
`python scripts/run_z80_bus_test.py` (2 s): the eight checks the ROM makes all print
PASS -- RESET and not BUSREQ $00DE, RESET and BUSREQ $0000, BUSREQ and not RESET $0078,
normal operation $0034, byte writes + word read $7878, word write + byte reads $BE89,
unused BUSACK bits $00C0, unused RESET bits $00C0 -- and the two it only prints:
**Z80 writes to work RAM $4243, Z80 reads to work RAM $FFFF**, which is what MoD
reports from several machines ("writes always worked and reads always failed"). Its
two delay counts print **0000 and 0000** (below).

**CMD's window and colour tests, and TmEE's** (`md_test.zip`), each against MAME's own
frame after the same 3 emulated seconds (`scripts/compare_mame_frames.py --record`;
MAME is a detector, never a judge):

| ROM | pixels equal | what the rest is |
| --- | ---: | --- |
| `wtest.bin` (window) | 100.00 % | |
| `wtest_i1.bin` (interlace 1) | 100.00 % | |
| `wtest_i2.bin` (interlace 2) | 96.25 % | the window's vertical range in interlace mode 2: the board covers V counter lines 0..WVP*8, as Genesis Plus GX does; MAME halves it |
| `wbug.bin` (the window bug) | 99.84 % | the bug's 112 pixels, above |
| `stetest.bin` (S/H) | 100.00 % | |
| `STETEST2.bin` (S/H) | 41.97 % | two thirds shadow-versus-normal (below); the rest a palette index difference in a mid-frame raster test, not run down |
| `SHLTEST.BIN` (TmEE) | 69.64 % | every one of the 21,760 differing pixels is the board's shadow of MAME's colour |
| `vctest.bin` (V counter) | 96.93 % | every differing pixel likewise |
| r57shell `vdp_tests.bin` | 100.00 % | |

The shadow-versus-normal difference is deliberate: in shadow/highlight mode the board
darkens the backdrop and every pixel where both planes are low priority, which is
Kabuto's measured rule ("If both A+B are low prio, the visible color becomes
shadowed"), CMD's `ste.htm` ("The changes in intensity due to a tile's priority setting
will also affect any parts of the backdrop color that can be seen") and Genesis Plus GX
(`make_lut_bg_ste`: "Half intensity when both pixels are low priority"). MAME's
backdrop pixels carry its normal-intensity flag instead. Three sources against one
detector: the board keeps its rule and the difference is named here.

**r57shell's VDP tests** (hardware-corrected by the MD2 report in forum t=1585): the
ROM runs and its first screen, "VDP Redraw Time Test 1", renders pixel for pixel as
MAME renders it. It says "You MUST SEE right column of spheres" and neither the board
nor MAME shows them: the test wants VRAM writes to land *within* the line being drawn,
which a per-line renderer cannot do. **Not passed**, and not comparable either: the
thread's hardware evidence is photographs of a CRT plus prose (MrTamk1s, an MD2 VA1.8
with a 315-5708-01: "VDP Redraw Test #4 acted differently on my smaller CRT and on the
Big CRT"), and no pad button the board models moves the ROM off its first screen -- it
polls the pad seven times a frame, which is the 6-button handshake (milestone 7).

**MAME's exit frames on the lockstep games** (detector).
`scripts/compare_mame_frames.py --record` at four lengths, six games, `genesis`:

| Game | 2 s | 5 s | 10 s | 20 s |
| --- | ---: | ---: | ---: | ---: |
| Sonic The Hedgehog (USA, Europe) | 98.65 / 100.00 | 100.00 | 72.46 / 77.31 | 92.10 / 93.10 |
| Streets of Rage 2 (USA) | 100.00 | 100.00 | 99.97 / 100.00 | 99.91 / 99.96 |
| Gunstar Heroes (USA) | 100.00 | 100.00 | 100.00 | 82.75 / 86.71 |
| Castlevania - Bloodlines (USA) | 100.00 | 100.00 | 98.90 / 99.31 | 91.10 / 92.90 |
| Sonic The Hedgehog 3 (USA) | 100.00 | 28.87 | 99.87 | 99.90 / 99.95 |
| Phantasy Star IV (USA) | 100.00 | 92.57 / 100.00 | 100.00 | 93.03 / 93.54 |

(exact frame / best of the eight frames around it, where they differ). Every static
screen is identical; every lower figure is a moving demo or animation a few frames out
of phase -- Sonic 3 at 5 s is its transformation sequence, the same picture at a
different phase. Nothing structural showed up in any of the 24 comparisons.

**Milestone 1 and 2, unchanged by all of this**: Sonic The Hedgehog re-recorded from
MAME 0.285 (30 emulated seconds) and replayed: 23,286,815 68000 and 12,728,465 Z80
instructions identical, 0 unexplained, 44 s (the named differences: 5,258 YM2612 status
replayed, 3,165 $A11100 non-grant bits, 1,477 VDP reads replayed, 41,274 steps
differing by MAME's `before_delay(1)`, 3,276 by its DMA stall). VDPFIFOTesting 122 of
122 on `genesis`; the HV captures 8,762,415 of 8,847,360 words, the same as the
milestone-2 baseline to the word. `pytest`: 19 passed on PyPy (96 s). The trace was
deleted (3.4 GB).

### Frames per second on PyPy (the brief's speed section)

`.venv-pypy/bin/python`, 900 frames after 120 frames of warm-up, one process, NTSC
(59.92 frames a second is real time):

| Game | fps | fps with the renderer turned off | 68000 instructions a second |
| --- | ---: | ---: | ---: |
| Sonic The Hedgehog (USA, Europe) | 147.7 | 367.2 | 1,882,988 |
| Streets of Rage 2 (USA) | 215.3 | 434.0 | 2,397,718 |
| Gunstar Heroes (USA) | 163.0 | 294.1 | 1,741,528 |
| Castlevania - Bloodlines (USA) | 293.6 | 705.0 | 1,304,430 |
| Sonic The Hedgehog 3 (USA) | 181.5 | 411.0 | 2,046,804 |
| Phantasy Star IV (USA) | 223.9 | 425.4 | 2,519,624 |

So 2.5 to 4.9 times real time with the picture, and the picture costs between half and
two thirds of the time. On CPython 3.14.4 the same Sonic run is 23.9 fps, as expected.
The YM2612 (milestone 4) has yet to be paid for.

### The two open items from milestone 1

**The Z80's interrupt length: kept at one line.** No hardware measurement is pinned;
CMD (`gen-hw.txt` 2.4) gives the line and says the interrupt is missed if the Z80 has
interrupts disabled, and Genesis Plus GX holds it to the end of that line, which is
what the board does (`VDP.z80_irq_at`, labelled). What can be said for it: in the
milestone-1 lockstep Sonic The Hedgehog 3 takes 1,688 Z80 interrupts in 30 seconds, all
of them at the instruction MAME took them at, which a much shorter or longer window
would not give. It stays emulator-derived.

**The Z80 bus-grant delay: measured, not modelled, and here is why.** The board grants
the bus at once, as MAME does, so MoD's two counts print 0000. A delayed grant was
written and tried: the Z80 samples BUSREQ "with the rising edge of the last clock
period of any machine cycle" (Zilog UM0080), so the grant falls at the end of the
machine cycle in progress, which the board can estimate from `System.z80_probe` (its
machine-cycle ends, the last one being the step's own length so that trailing internal
cycles belong to it). With that in place the $A11100 read reports "busy" until the
grant, and a 68000 access to the Z80's space in between has to *wait* -- the arbiter
holds it, it does not drop the access (Kabuto: the 68000 waits about 11 of its cycles
when both want the bus). Holding the 68000 moves its later accesses in the same
instruction to later clocks, which moves the points at which the Z80 is caught up, and
Sonic's Z80 then runs one instruction ahead of MAME's (a write to the bank register
that MAME had not made yet): the milestone-1 lockstep diverges after 912,165
instructions. MAME grants the bus at once and models no arbiter stall, so the two
cannot both hold. Since no hardware value for either count is published in the pinned
material -- MoD's thread reports the ROM but not its numbers, and his logic-analyser
measurement (2 68000 cycles from /BR to /BG, forum t=1661) is of the *other* direction,
the Z80 reaching the 68000's bus -- there is nothing to judge the model by, and the
lockstep is a real detector today. The delay is therefore left to milestone 5, whose
subject is exactly the arbiter's delays and which will have to teach the lockstep about
board stalls anyway. Recorded in docs/undocumented-behavior.md.

QUESTION: is that the right call? The alternative is to model the BUSACK wait now and
accept that the milestone-1 lockstep can no longer be replayed instruction for
instruction against MAME (it would need a "the board waits for BUSACK, MAME does not"
class, and the Z80 interleaving would have to be compared some other way).

QUESTION: r57shell's redraw-timing tests want VRAM writes to land inside the line being
drawn. Rendering per line is what the brief asks for and what pays for the frame rate
above; drawing per pixel-clock would cost a lot of it. Leave those tests failing, or
plan a per-slot renderer for milestone 5 (it is the same machinery CRAM dots need)?

QUESTION: the window's vertical range in interlace mode 2 differs between Genesis Plus
GX (the board's choice: V counter lines, so twice as many field lines) and MAME. No
hardware source covers it and CMD's `wtest_i2.bin` is a picture, not a self-check.
Accept the current choice?

QUESTION: 128 KB mode's writes are implemented from Kabuto's formulas with nothing to
judge them until Overdrive 2 in milestone 5, and its effect on the picture is not
modelled at all. Accept, or leave 128 KB mode out until there is a judge?

## Decisions (the user, 2026-09-22), answering milestone 3's questions

1. **The Z80 BUSACK delay waits for milestone 5** (the arbiter), where the bus
   timing is settled as a whole. Modelling it now would cost the instruction-exact
   milestone-1 replay, which is the regression net for everything else. The test
   ROM's own delay counts (0000/0000) stay recorded as a known difference from
   MoD's hardware report.
2. **The per-slot renderer is planned for milestone 5**, with the CRAM dots: the
   same machinery serves r57shell's redraw-timing tests, and its cost in frames
   per second is measured once, there.
3. **The IM2 window vertical range stays Genesis Plus GX's**, labelled as a
   detector-tier choice with MAME's alternative recorded beside it, until a
   hardware source settles it.
4. **128 KB mode stays**, judged by Overdrive 2 in milestone 5.
