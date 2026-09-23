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

## Session 3 (2026-09-22): milestone 4, sound

### How the brief was read

Scope is milestone 4 only: the PSG after sms-python's, judged by Nuked-PSG; the
YM2612 with all six channels, channel 3's special mode, CSM, SSG-EG, the LFO, both
timers, BUSY, the DAC and the ladder effect as a variant field, judged by
Nuked-OPN2's YM2612 mode and YM2608-LLE's YM2612 path sample for sample at the chip's
native rate for register streams from Nemesis's test ROMs and five games; Nemesis's
SSG-EG test against his MD1600 recording by measurement; the MDFourier signal against
the recordings as a detector of the analogue model. Record the FM's cost per
emulated second on PyPy. Then stop.

The tier rule for this milestone, as the brief states it: the recordings are
hardware-captured and judge; the die-derived models judge where no capture covers
the question; MAME and Genesis Plus GX are detectors. There is no YM2612 datasheet
and no self-checking FM test ROM (docs/validation.md), so the chips' *arithmetic* is
exactly the case where the die-derived models judge, and that is how the FM and PSG
comparisons below are stated.

### Environment

Same as the previous sessions: `.venv` CPython 3.14.4, `.venv-pypy` PyPy 7.3.23
(Python 3.11.15), m68000-python pinned at 51ee1a5 (unchanged: docs/polish.md),
z80-python 0.4.0, MAME 0.285, the 13th-generation Core i9-13900K. gcc 15.2.0
(Ubuntu 15.2.0-16ubuntu1) for the referees. `unrar` 7.20 and `ffmpeg` for the
recordings. The machine was shared with other work for part of the session, which
the wall times below reflect.

The large material was already fetched (`python scripts/fetch_references.py --large`
in an earlier session): `tests/test_roms/nemesis/ssg-eg md1600*.rar`,
`tests/test_roms/mdfourier/MDFourier-Genesis-SegaCD-Recordings-14062020.zip`,
`tests/test_roms/aly-james/MD1_LADDER.wav`. Everything unpacked from them was
deleted at the end of the session and none of it is committed.

### The three referees

`sh validation/build_referees.sh` builds all three into `reference/build-referees/`
(git-ignored, beside the previous session's `reference/build-nuked-md/`):

```sh
gcc -O2 -w -I reference/src/Nuked-OPN2  -o reference/build-referees/opn2_ref \
    validation/opn2_ref.c  reference/src/Nuked-OPN2/ym3438.c
gcc -O2 -w -I reference/src/Nuked-PSG   -o reference/build-referees/psg_ref \
    validation/psg_ref.c   reference/src/Nuked-PSG/ympsg.c
gcc -O2 -w -I reference/src/YM2608-LLE  -o reference/build-referees/lle2612_ref \
    validation/lle2612_ref.c reference/src/YM2608-LLE/fmopna_2612.c
```

Each takes a binary event file of `{u64 clock; u8 port; u8 data}` and writes a binary
result; the clock unit is documented in each `validation/*.c`. Nothing of the
referees' source is copied into this repository -- the three `.c` files here only
drive them, and the licences (LGPL-2.1, GPL-2.0+, GPL-2.0) stay with the sources in
`reference/`, which is ignored.

`lle2612_ref` is a *pin-level* harness: /IC held for 4,096 chip clocks, then /CS and
/WR driven low for 16 chip clocks per write, with A0 and A1 from the port. That
matters below.

### The PSG

#### Decisions

- **It is sms-python's, adapted, not imported** (the brief's rule): same three tone
  channels, one noise channel, one write-only port, 16-bit LFSR. Cited in the module
  docstring by file.
- **The generator runs the die's rotation, not a four-channels-at-once tick.**
  sms-python advances all four channels together once per 16 input clocks; that is
  right for its own judge. Here the judge is Nuked-PSG and matching it needs the
  shape the die has, which turned out to be four things sms-python does differently:
  two internal phases a quarter of the input clock apart with one counter stepped per
  phase pair; the *period* taken from a one-hot selector that runs one place behind
  the counter index (so counter 0 is the noise channel's and counter 1 tone 1's); an
  up-counter compared with the period rather than a down-counter reloaded at zero;
  and a left-shifting LFSR tapped at bits 15 and 12 with a zero-detect, its output
  bit 14 latched a phase behind.
- **A write reaches the register on the next input clock -- or on the same one.**
  Nuked-PSG updates the registers at the head of every clock *and* again inside the
  second internal phase (`ympsg.c` lines 285 and 208), so a write landing on that
  phase is a clock earlier than one that does not. Found by measurement: without it,
  19 of 102,569 input clocks differed on the first random stream, every one of them
  at a write. Modelled.
- **Power-on is the /IC sequence, run, not asserted.** Nuked-PSG's `YMPSG_Init`
  holds /IC over 16 clocks and releases it, which leaves the counters at 1, the
  overflow shift register full (0x0F) and the one-hot selector at bit 3 -- not a
  zeroed machine. `PSG.reset` runs the same 16 clocks and the state then matches
  Nuked's field for field (checked in `tests/test_milestone4.py`).
- **The attenuation curve is the measured one** (`ympsg_vol`: 1.0, 0.772, 0.622,
  0.485, ...), not the ideal 2 dB a step sms-python uses (1.0, 0.794, 0.631, ...).
  Recorded in docs/undocumented-behavior.md.
- **The port decode is CMD's**: "Doing byte-wide writes to even PSG addresses has no
  effect", and a word write must carry the data in the LSB (CMD `gen-hw.txt` 1 note
  3). Reading the port locks up, so there is no read.

#### What is compared, exactly

`scripts/run_psg_compare.py` compares **the four attenuations the chip is driving, at
every input clock** (MCLK / 15, 3.58 MHz) -- sixteen times finer than the chip's own
output rate. That is the whole digital state; nothing is averaged and nothing is
rounded. Nuked's *analogue* level is not compared, because the board uses Nuked's own
measured curve and comparing it would be circular; the curve itself is checked
entry by entry in the tests.

### The YM2612

#### The one decision everything else follows from

**The model is the chip's pipeline, not a per-sample FM loop.** The acceptance is
sample-for-sample equality with two die-derived models, and a textbook OPN2 cannot
meet that bar however carefully it is written. The real chip serves one operator per
internal cycle, 24 cycles to an output sample, and every stage sits a fixed number of
cycles behind the slot being served: the phase generator 19 and 20, the envelope 22
and 23, the modulation 6 and 18, the channel accumulator 18. A register written
between two cycles therefore reaches one operator this sample and the next operator
only next sample, and the envelope's total level and sustain level are each delayed a
further cycle. Only the same pipeline gives the same samples.

So `src/megadrive_python/ym2612.py` runs the chip's cycle: `cycles` counts 0-23,
slots 0-5 are the six channels' operator 1, 6-11 operator 3, 12-17 operator 2, 18-23
operator 4. Its shape, its stage offsets and its constant tables are read from
**Nuked-OPN2** (`ym3438.c`, LGPL-2.1, the YM3438's die) and **YM2608-LLE**
(`fmopna_impl.c`, GPL-2.0, the NMOS YM2612's die), cited by file and line in every
method. No code is copied: the models were read and this was written from what they
say, as the working agreement requires.

The two ROM tables are **generated**, not transcribed:
`round(-log2(sin((i + 0.5) * pi / 512)) * 256)` and `round((2 ** (i / 256) - 1) *
1024)`, the functions Gambrell and Niemitalo recovered from the OPL2's ROM and which
Nuked-OPN2 credits for the same tables. `tests/test_milestone4.py` checks all 512
entries against the pinned model's own. The other constants (the detune increments,
the envelope step table, the two LFO shift tables, the LFO reloads, the algorithm
matrix) are measurements of the chip carried as data, each with the line they were
read from.

#### Decisions and their sources

- **BUSY** is set only by a *data* write and lasts 32 internal cycles, which at 6
  68000 clocks a cycle is Eke's "32 internal clocks (32 x 6 68000 cycles)" read the
  other way round (forum t=386 start=810, hardware-captured). The model is
  structural, not a timer: the die's own counter.
- **BUSY reads from port 0 only on the discrete chip, from every port on the ASIC**
  (Eke, same measurement), which is `FMVariant.busy_on_all_ports`. What a discrete
  chip's other three ports read is not in any pinned source; the board returns 0,
  which is Genesis Plus GX's choice (`core/sound/sound.c` line 152), labelled
  emulator-derived and listed as an open item.
- **Kabuto's "wait about 33.6 Z80 cycles between register writes"** is modelled by
  construction, not by a delay: the chip has no register RAM the CPU can reach, and a
  write waits in a latch until the cycle that serves the addressed slot comes round.
- **The ladder effect is `FMVariant.ladder`**, as the brief asks. With it, the
  discrete chip's output stage drives the channel's value on one of the channel's
  four cycles and the bare sign (+1 or -1) on the other three, adds 1 to every
  non-negative sample and triples everything; without it, the ASIC drives the value
  on three of the four and zero on the fourth, unamplified.
- **The DAC** is 9 bits: register $2A carries the top eight and the test register
  $2C bit 3 the ninth, which is where Kabuto's "9 bits" comes from. Its *sampling
  point* within a cycle, which he says differs between Model 1 and Model 2 and did
  not publish, is not modelled: the board sums a sample's 24 multiplexed values, the
  same on both.
- **The /RESET line is shared with the Z80** (CMD 2.2), so the board holds the chip
  at power-on and a game that pulses it clears the chip -- but not the samples
  already made, which are the output queue and not chip state.

#### What is compared, exactly

`scripts/run_fm_streams.py` runs each register stream from power-on and compares
**every value of both DAC pins, one per internal cycle** -- 24 to an output sample,
so a stricter test than sample-for-sample at the chip's native rate -- plus the
status byte (BUSY and both timer overflow flags) on the same cycles. Nothing is
averaged, nothing is rounded, and a stream either matches on every cycle or the first
differing cycle is printed with both values. Both chip variants are run against the
matching Nuked-OPN2 mode.

The streams are: two seeded random walks over the whole register space; six aimed
streams (channel 3's special mode and CSM at three timer-A rates, all eight SSG-EG
shapes keyed on and off, the LFO at all eight speeds against every PMS and AMS depth,
both timers at four reloads, the DAC through every value and its ninth bit); Nemesis's
six YM2612 test ROMs run on the board; and five cartridges run on the board. The ROM
and game streams are *the board's own*: the writes its YM2612 received, at the
internal cycle it received them on, captured with the generator turned off (the
register stream does not depend on it). So a difference there would be the board's
scheduling as much as the chip's.

#### YM2608-LLE: what it agrees with and what it does not

YM2608-LLE is a **pin-level** model at MCLK / 7 of the NMOS YM2612's own die, so it
is the better authority for the discrete chip's output stage than Nuked-OPN2, whose
YM2612 mode its own comment calls "YM2612 DAC emulation(not verified)" (`ym3438.c`
line 966). Comparing the two showed one real difference and one artefact of the
harnesses, and both are worth recording.

**The real one.** The six channels are multiplexed onto MOL/MOR four cycles each.
YM2608-LLE drives the channel's value on the *first* of its four cycles and the bare
sign (+1 or -1) on the other three (`fmopna_impl.c` lines 5824-5853). Nuked-OPN2
drives the sign on three and the value on the *last* (`ym3438.c` lines 937-1010,
`out_en = ((cycles & 3) == 3)`). Measured on the channel-3 stream: YM2608-LLE puts
-14 at the start of the group and -1 for the next three cycles; the board (following
Nuked-OPN2) puts -1 for three cycles and -16 at the end. The 24 values of an output
sample are the same multiset either way, so the sample at the chip's native rate is
unaffected and nothing audible turns on it. The board keeps Nuked-OPN2's order,
because that is what the cycle-exact comparison above is made against, and the
difference is recorded in docs/undocumented-behavior.md ("The YM2612" item 6).

**The artefact.** A write is driven differently in the two harnesses: `lle2612_ref`
holds /CS and /WR low for 16 chip clocks, as a bus cycle does, while `OPN2_Write`
latches an edge. A write can therefore land one internal cycle apart in the two. The
comparison is then made at the chip's native rate (each model's DAC summed over a
sample, one phase and one whole-sample lag fitted over the run), and what is left
scales with how often the stream writes:

| Stream (24 x 20,000 cycles each) | samples differing |
| --- | --- |
| SSG-EG, all eight shapes | **0** of 19,898 |
| both timers | **0** |
| the DAC | **0** |
| channel 3 special mode and CSM | 1 of 19,898 (0.01 %) |
| the LFO, 8 speeds x 8 PMS x 4 AMS (292 writes) | 325 of 19,898 (1.63 %) |
| a random walk, one write per 36 cycles | 4,351-7,179 of ~20,900 (21-34 %) |
| a random walk, one write per 4,000 cycles | 25 of 67,299 (0.04 %) |
| a random walk, one write per 1,000 cycles | 12 of 17,424 (0.07 %) |

and the differences are transient: on the dense stream, 1,200 writes produce 146 runs
of differing samples, median length 1 sample and mean 4.4, after which the two models
agree again. That is what a one-cycle difference in when a write lands looks like;
it is not a difference in either chip's arithmetic. Shifting the LLE harness's write
by a few chip clocks moves the count (642 -> 547 at -9 clocks) but does not remove
it, which is expected: the landing cycle differs only for writes that fall near a
boundary.

**So, stated precisely:** at the chip's native rate the board and YM2608-LLE are
identical over the SSG-EG, timer and DAC streams and differ on 1 sample in 19,898 on
the CSM stream; on streams that write every few tens of cycles they differ in bursts
around writes that the two harnesses place a cycle apart, and re-converge. This is
*not* the literal "equal sample for sample" the brief asks for, and the reason is
measured above rather than argued.

### The analogue path

`src/megadrive_python/audio.py`. Three signals leave the board -- MOL, MOR and the
PSG's one output -- and meet in an amplifier that differs by revision. What is
modelled, and what is not:

- **The PSG is box-filtered onto the FM's grid with an integer phase.** Five FM
  samples take exactly 21 PSG samples (1,008 x 5 = 240 x 21), so nothing drifts over
  a long run.
- **A low-pass**: one pole for `model1`, two cascaded for `model2`, which is
  Kabuto's "instead of the MD1's 1st order filter there's a 2nd order filter". He
  gives no corner frequency and nothing pinned does, so the board's are **fitted**
  and labelled where they are defined.
- **A DC blocker**, which the coupling capacitor at the jack is. This is not
  cosmetic: the discrete YM2612 drives +1 in every gap between channels, which is a
  standing 72 units of every sample even in silence, so without it a recording of the
  board sits far off zero and a silence is not silent. Found by measurement: the
  first MDFourier run could not find the signal's silences at all.
- **No distortion model.** Kabuto measures the Model 2's amplifier as introducing
  "severe nonlinearities ... Distortion is quite high too, very well audible" and
  publishes no transfer curve. The board's Model 2 is therefore cleaner than the
  hardware's, and docs/undocumented-behavior.md says so.
- **The PSG-to-FM mixing ratio** is the one number the MDFourier recordings measure
  directly (below).

### The ladder effect, against Aly James's MD1_LADDER.wav (hardware-captured)

`MD1_LADDER.wav` is 3.1 seconds of a real Model 1 playing a tone whose level is
walked down. Its envelope, measured in 50 ms blocks, falls smoothly for the first
half and then **stops falling about 33 dB below its peak**, staying there for about
0.8 s before the note reaches absolute silence. That plateau is the ladder: the +1
the discrete chip adds to every non-negative sample, and the bare sign it drives
between channels, do not shrink with the note.

`python scripts/run_fm_streams.py --ladder tests/test_roms/aly-james/MD1_LADDER.wav`
walks one operator's total level from 0 to 127 on the board and prints each machine's
level profile in decibels below its own peak, at ten points along the ramp:

| | peak | 0 | 1/10 | 2/10 | 3/10 | 4/10 | 5/10 | 6/10 | 7/10 | 8/10 | 9/10 | 10/10 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| board, discrete YM2612 | 222.9 | 0.0 | -9.7 | -18.5 | -27.2 | -34.0 | **-36.1** | **-37.1** | **-37.0** | -40.4 | silence | silence |
| board, ASIC YM3438 | 128.3 | 0.0 | -9.8 | -18.7 | -28.2 | -37.9 | -46.1 | -49.0 | -48.9 | -52.4 | silence | silence |
| `MD1_LADDER.wav` (a Model 1) | 19,670 | 0.0 | -9.5 | -18.2 | -25.7 | -30.7 | **-32.8** | **-32.8** | **-33.0** | -42.6 | -61.1 | -61.2 |

The board's discrete variant plateaus at -36 to -37 dB where the Model 1 plateaus at
-33; its ASIC variant does not plateau at all, falling another 12 dB. So **the
board's ladder has the hardware's shape, within 4 dB of its depth, and the variant
field does what it is there for**. What this does *not* claim: the two ramps are not
the same ramp (the recording's ROM is not published, its console's filter and the
capture's gain are unknown), so the 4 dB is not an error figure -- it is the distance
between two differently made measurements of the same effect. Nothing here is rounded
into a pass.

### Nemesis's SSG-EG test against his MD1600 recording (hardware-captured, the judge)

`tests/test_roms/nemesis/ssg-eg md1600.rar` unpacks to 83.3 s of mono 96 kHz audio of
his MD1600 running `ym2612ssg-eg.bin`. It cannot be compared sample for sample -- it
is an analogue recording of unknown gain, with an unknown start offset, through the
console's filter -- so `scripts/run_ssg_eg.py` compares what can be measured on both:
how many notes sound, how long each lasts, how far apart they start, and the
repetition period of the envelope inside each note, by autocorrelating its amplitude
envelope (20 Hz blocks, RMS about each block's own mean, so neither signal's standing
level counts as sound).

```sh
unrar x "tests/test_roms/nemesis/ssg-eg md1600.rar" <tmp>/
.venv-pypy/bin/python scripts/run_ssg_eg.py --wav "<tmp>/ssg-eg md1600.wav"
```

| note | MD1600 start | board | MD1600 length | board | MD1600 period | board |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 0.00 | 0.00 | 0.40 | 0.45 | none | none |
| 1 | 9.50 | 9.20 | 4.10 | 4.10 | none | none |
| 2 | 18.80 | 18.40 | 0.10 | 0.10 | none | none |
| 3 | 28.10 | 27.60 | 3.80 | 3.80 | **150 ms** | **150 ms** |
| 4 | 37.35 | 36.85 | 4.35 | 4.30 | **50 ms** | **50 ms** |
| 5 | 46.70 | 46.04 | 3.80 | 3.80 | none | none |
| 6 | 55.95 | 55.24 | 4.35 | 4.30 | **50 ms** | **50 ms** |
| 7 | 65.25 | 64.44 | 4.10 | 4.10 | **150 ms** | **150 ms** |
| 8 | 74.55 | 73.64 | 0.10 | 0.10 | none | none |
| 9 | -- | 82.84 | -- | 0.40 | -- | none |

(times in seconds, relative to each machine's first note; the board's note 9 is the
ROM starting round again, which the recording, begun 4.9 s in, does not reach.)

**Every one of the eight SSG-EG shapes behaves the same way on both machines**: the
three that hold or run to silence sound for 0.10-0.45 s and show no repeating
envelope; the five that repeat sound for 3.80-4.35 s; and the four that repeat
*visibly* do so at the same rate, 150 ms or 50 ms, to the resolution of the
measurement (50 ms blocks). Every length agrees within one block.

**What does not agree: the rate.** The MD1600's notes start 9.25-9.50 s apart and the
board's 9.19-9.25 s, a steady **1.0-1.2 % difference** that accumulates to 0.91 s over
the eight gaps. That is a clock ratio, not a structural difference: the ROM's own
timing is the same number of frames on both. Two candidates, neither checkable from
here -- the recorder's sample clock (a capture nominally at 96,000 Hz running 1.2 %
slow would give exactly this) and the console's crystal against the nominal
53,693,175 Hz. It is recorded as an open item rather than guessed at.

### Milestone 4 acceptance: the runs and their results (2026-09-22)

All at commit `383992c` unless said; m68000-python 51ee1a5, z80-python 0.4.0, PyPy
7.3.23 (Python 3.11.15), referees built as above with gcc 15.2.0. The machine was
shared, so the wall times vary by up to a factor of three between runs of the same
comparison. The MDFourier numbers below are from `46528e7`, which is `383992c` plus
the two changes that run itself produced (the DC blocker and the fitted `PSG_GAIN`);
nothing else in the sound path differs between the two.

#### The PSG against Nuked-PSG (die-derived, the judge): passes

`.venv-pypy/bin/python scripts/run_psg_compare.py --games`, 47 s. Ten streams, the
four attenuations compared at **every input clock**:

| Stream | input clocks | differing |
| --- | ---: | ---: |
| random walk (seed 1, 4,829 writes) | 1,028,959 | **0** |
| random walk (seed 2, 4,812 writes) | 1,031,380 | **0** |
| every tone period (0-7 and 8-1023 in steps of 37, all three channels) | 179,016 | **0** |
| every noise setting (8 settings x 3 tone-3 periods) | 492,264 | **0** |
| every attenuation on every channel | 115,064 | **0** |
| Sonic The Hedgehog (USA, Europe) `46160baa…`, 400 frames, 12 writes | 19,567,876 | **0** |
| Streets of Rage 2 (USA) `4a314edb…`, 400 frames, 52 writes | 22,135,675 | **0** |
| Gunstar Heroes (USA) `f177810c…`, 400 frames, 16 writes | 6,903,440 | **0** |
| Castlevania - Bloodlines (USA) `5bfe171f…`, 400 frames, 8 writes | 5,044,491 | **0** |
| Phantasy Star IV (USA) `511f35cc…`, 400 frames, 447 writes | 23,802,440 | **0** |
| **Total** | **80,300,605** | **0** |

The games use the PSG sparsely (8 to 447 writes in 6.7 s), so most of those clocks
are the free-running generators agreeing, which is the part a long run tests.

#### The YM2612 against Nuked-OPN2 (die-derived, the judge): passes

`.venv-pypy/bin/python scripts/run_fm_streams.py --roms --games`, 21 min. **18
streams, 0 comparisons not identical**: every value of MOL, every value of MOR and
every value of the status byte, on every internal cycle, in **both** chip variants
against the matching Nuked-OPN2 mode.

| Stream | internal cycles | MOL | MOR | status |
| --- | ---: | --- | --- | --- |
| random walk (seed 1, 4,000 writes) | 504,939 | equal | equal | equal |
| random walk (seed 2, 4,000 writes) | 502,904 | equal | equal | equal |
| channel 3 special mode and CSM (3 timer-A rates) | 397,172 | equal | equal | equal |
| SSG-EG, all eight shapes keyed on and off | 1,884,812 | equal | equal | equal |
| the LFO, 8 speeds x 8 PMS x 4 AMS | 2,467,004 | equal | equal | equal |
| both timers, four reloads each | 336,476 | equal | equal | equal |
| the DAC, every value and the ninth bit | 31,532 | equal | equal | equal |
| `ym2612csmmode.bin` `3faaae39…` | 123,658 | equal | equal | equal |
| `ym2612ssg-eg.bin` `03a73fd6…` | 8,446,814 | equal | equal | equal |
| `ym2612attackandrelease.bin` `06b72d38…` | 8,447,837 | equal | equal | equal |
| `ym2612detune.bin` `74306d73…` | 123,557 | equal | equal | equal |
| `ym2612multiplexing.bin` `519d19a0…` | 126,636 | equal | equal | equal |
| `ym2612FrequencyWriteOrder.bin` `e79459a7…` | 123,607 | equal | equal | equal |
| Sonic The Hedgehog (USA, Europe), 720 frames, 66,944 register writes | 15,351,279 | equal | equal | equal |
| Streets of Rage 2 (USA), 720 frames, 2,750 | 15,350,206 | equal | equal | equal |
| Gunstar Heroes (USA), 720 frames, 96 | 2,466,058 | equal | equal | equal |
| Castlevania - Bloodlines (USA), 720 frames, 3,101 | 15,341,712 | equal | equal | equal |
| Phantasy Star IV (USA), 720 frames, 45,260 | 15,351,280 | equal | equal | equal |
| **Total** | **87,377,483** | | | |

87,377,483 internal cycles per variant, 174,754,966 over the two, and three values
compared on each: **524,264,898 values, none different**. That is 68 emulated seconds
of the chip's whole output, sampled 24 times per output sample.

Two notes on the streams, neither a result:

- **Gunstar Heroes reaches only 96 register writes** in 720 frames with Start pressed
  once: its driver wants Start at the title *and* at the mode select. With presses at
  frames 200, 400 and 600 it writes 176,586 registers by frame 1,400. The GAMES table
  and `capture_streams` now take several press frames; the run above predates that, so
  the Gunstar stream in it is the title screen's.
- **Four of the six test ROMs write all their registers in the first tenth of a
  second** and then play, so their streams end early and the comparison runs 0.1 s
  past the last write. `--tail` runs further on.

#### The MDFourier signal against the hardware recordings (a detector, and one fit)

The 240p Test Suite's MDFourier signal is 172 tone blocks of 20 frames between two
sync blocks: 96 FM, 40 PSG, 20 PSG ramp and 16 noise
(`reference/src/MDFourier/profiles/mdfblocksGEN.mfn`). `scripts/run_mdfourier.py`
drives the suite to it from its menus (down, down, A; down, down, A; A), records the
board's own output, decodes a recording with ffmpeg, aligns both on their two syncs
and measures each block's level (RMS about its own mean).

MDFourier's own analyser is not built here: it needs FFTW and libFLAC and neither
header is installed (docs/polish.md).

```sh
unzip -j tests/test_roms/mdfourier/MDFourier-Genesis-SegaCD-Recordings-14062020.zip \
    "Recordings-2020-06-14/A-MD1UTVA3-M192.flac" -d <tmp>
.venv-pypy/bin/python scripts/run_mdfourier.py --recording <tmp>/A-MD1UTVA3-M192.flac
```

**The signal's timing matches exactly.** Both machines show ten loud regions and
**58.40 s between the two syncs** -- the board's at 5.00 s and 63.40 s, the Model 1
VA3's at 1.50 s and 59.90 s. The signal is 3,520 frames, 58.74 s at the nominal
59.92 Hz, so both run it at the same rate and the 0.34 s is the trimming of the
syncs' attack, not drift. (Worth noting against the SSG-EG recording's 1.0-1.2 %:
whatever that was, it is not the board's frame rate.)

**The mixing ratio, measured.** On the Model 1 VA3 the 40 PSG tone blocks sit
**6.70 dB below** the 96 FM blocks. That is the one number `audio.PSG_GAIN` sets, and
it was fitted here: at the gain the board started with (1,400) its PSG sat 3.72 dB
*above* its FM, 10.42 dB too loud; at **422** (commit `46528e7`) it sits 6.66 dB
below, **0.04 dB** from the hardware. This is the only parameter of the analogue path that a pinned source
measures, and it is now measured rather than guessed.

| group | hardware, relative to its own FM | board | board minus hardware |
| --- | ---: | ---: | ---: |
| FM (96 blocks) | 0.00 dB | 0.00 dB | (the reference) |
| PSG tones (40) | -6.70 dB | -6.66 dB | **+0.04 dB** |
| PSG ramp (20) | -7.10 dB | -7.17 dB | **-0.07 dB** |
| noise (16) | -5.03 dB | -6.75 dB | **-1.72 dB** |

**The filter, as a detector.** Each group's blocks climb through the chips' range, so
the per-block difference is a frequency response. Board minus hardware, at twelve
points through each group, each normalised by its own group's mean:

```text
FM    blocks 0..95   -0.2  -0.1   0.3  -0.1  -0.4  -0.4  -0.4  -0.3   0.1   0.5   0.9   1.1
PSG   blocks 0..39   -1.6  -0.9   0.1   0.8   1.1   1.1   0.9   0.6   0.4  -0.2  -0.2  -0.5
noise blocks 0..15    1.0   0.4  -0.2  -0.0   0.4  -0.3  -0.1  -0.1  -0.3  -0.5  -0.8  -0.6
```

Everything is inside **±1.6 dB**, so the one-pole low-pass at 8 kHz is in the right
region: a corner badly wrong would tilt the FM's 96 blocks by many decibels end to
end. The residual is a slight brightness at the top of the FM's range (+1.1 dB on the
last block), which says the corner could be a little lower; that is left as a fit to
revisit with MDFourier's own analyser, which does per-frequency work this measurement
does not.

**Not explained: the noise channel is 1.72 dB quieter, relative to the FM, than the
hardware's.** The PSG's *digital* output is identical to Nuked-PSG's at every input
clock, including every noise setting, so this is in the level model -- the attenuation
curve applied to a pseudo-random bit stream against what the console's analogue stage
does with it -- and not in the generator. Left open.

### Open questions for the user

QUESTION: **YM2608-LLE cannot be matched literally.** The measurements above
attribute the remaining differences to the two harnesses placing a write one internal
cycle apart, and they re-converge. Is that characterisation acceptable, or should the
LLE harness drive /CS and /WR aligned to the chip's own /6 phase (which means finding
that phase in its state) so the comparison can be made literal?

QUESTION: **The two die-derived models disagree about the DAC's multiplexing order**
(docs/undocumented-behavior.md, "The YM2612" item 6). The board follows Nuked-OPN2,
because that is what the cycle-exact comparison is made against -- but YM2608-LLE is
a transcription of the *YM2612's* die and Nuked-OPN2's YM2612 mode is self-described
as unverified, which makes LLE the better authority for exactly this. The output
sample is the same either way. Keep Nuked-OPN2's order, switch to LLE's (and lose the
cycle-exact comparison), or carry both as a variant?

QUESTION: **The analogue path is fitted to one recording.** `PSG_GAIN` and the filter
corner come from `A-MD1UTVA3-M192.flac`, one Model 1 VA3. The archive holds 21 boards.
Fit per revision -- a table on the machine, which is what `audio_path` is there for --
or keep one Model 1 figure and one Model 2 figure?

QUESTION: **The SSG-EG recording's notes start 1.0-1.2 % further apart than the
board's**, while the MDFourier recording's syncs are exactly 58.40 s apart on both.
That points at the SSG-EG recording's own clock rather than at the board, but it is
not proved. Accept it as the recorder's, or pursue it (Nemesis's second SSG-EG
recording, `ssg-eg md1600 attack and release.rar`, is 180 MB and untouched)?

QUESTION: **Nemesis's CSM recording was not used.** docs/validation.md says to
confirm in forum t=386 that `ym2612csmmode.wav` is from hardware before judging with
it, and that was not done this session. Confirm and use it, or leave CSM judged by
the die-derived models alone (where it passes cycle for cycle)?

QUESTION: **What a discrete YM2612's ports 1-3 read** is Genesis Plus GX's 0, the only
source there is (docs/undocumented-behavior.md, "The YM2612" item 7). Accept, or
leave the board driving open bus there until someone measures it?

QUESTION: **The noise channel is 1.72 dB quiet relative to the FM** against the
MDFourier recording, while the PSG's tones are 0.04 dB out and its digital output is
identical to Nuked-PSG's at every clock. Worth running down, or accepted as part of
the analogue model's known looseness?

### What the sound costs on PyPy (the brief's speed section)

`.venv-pypy/bin/python`, 600 frames after 120 of warm-up (Start at frame 110), one
process, NTSC, at commit `2b113e5`. Four runs per game: the board as it ships; with
`YM2612.enabled` off (the bus, the register file, the timers, BUSY and the PSG, no
generator); with the YM2612 not advanced at all; and with neither chip advanced,
which is the milestone-3 board measured in the same run for comparison. The last
three columns are seconds of wall time per **emulated** second.

| Game | fps, all | fps, no FM | fps, PSG only | fps, silent | sound s/s | FM s/s | PSG s/s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Sonic The Hedgehog (USA, Europe) | 18.2 | 49.9 | 139.5 | 185.9 | 2.97 | **2.86** | 0.11 |
| Streets of Rage 2 (USA) | 21.4 | 57.3 | 177.0 | 205.4 | 2.51 | **2.46** | 0.05 |
| Gunstar Heroes (USA) | 17.7 | 53.9 | 160.1 | 192.0 | 3.08 | **3.01** | 0.06 |
| Castlevania - Bloodlines (USA) | 18.3 | 35.6 | 87.1 | 138.2 | 2.84 | **2.59** | 0.25 |
| Sonic The Hedgehog 3 (USA) | 17.1 | 49.2 | 122.8 | 180.0 | 3.17 | **3.01** | 0.16 |
| Phantasy Star IV (USA) | 17.5 | 40.8 | 124.3 | 152.7 | 3.03 | **2.94** | 0.09 |

**The FM dominates, as the brief said it might: 2.5 to 3.0 seconds of wall time per
emulated second**, against the PSG's 0.05 to 0.25 and the whole rest of the board's
0.32 (Sonic: 600 frames in 3.23 s silent). The board goes from 3.1 times real time to
0.29 times: with sound it is **17 to 21 frames a second on PyPy**, about a third of
real time.

Why, and what was done about it: the chip's grid is 1,278,409 internal cycles a
second and each one runs seventeen stages of the pipeline over 24 slots' worth of
state, so about 22 million method calls and a few hundred million integer operations
per emulated second. The number is what modelling the pipeline costs, and the
pipeline is what the acceptance above needs. One thing was cheap and is done:
`YM2612.enabled` used to skip only the four operator stages, leaving the phase
generator, the envelope generator, the LFO and the register decode running -- Sonic
at 23.6 frames a second. It now skips the whole generator and keeps only the bus, the
register file, the timers and BUSY, which is all a run without sound needs: 49.9
frames a second, and it is what `--no-sound`, the register-stream captures and a
later compatibility sweep will use.

What was *not* done, deliberately: flattening the seventeen stages into one method
would likely buy something on PyPy, and it would touch every line the cycle-exact
comparison rests on. Since that comparison is the whole evidence for this milestone
and the tests would be the only net, it is left for a session that can re-run all
eighteen streams afterwards, and recorded here rather than attempted at the end of
this one.

The machine was shared through the session: the "silent" column here (138 to 205
frames a second) is not directly comparable with milestone 3's table (148 to 294),
which is why all four columns are measured in the same run.

### Milestones 1 to 3, unchanged by all of this

`.venv-pypy/bin/python -m pytest -q` at commit `23e10d0`: **32 passed, 1 deselected**
in 146 s (the deselected one is milestone 1's replay, which needs MAME logs that are
not kept). That covers milestone 2's 288 HV/status capture dumps against
`tests/hv_captures_baseline.json` and VDPFIFOTesting, and milestone 3's sprite
masking ROM against Nemesis's MD1600 photographs and MoD's `gen_test_z80_bus`,
all with the two sound chips now on the board. Nothing moved.

The same suite on CPython 3.14.4 with the capture dumps deselected: 29 passed.

### One test failure that did not reproduce

`tests/test_milestone4.py::test_ym2612_matches_nuked_opn2` failed once, while another
comparison was running, and passed on both interpreters every other time (three runs
after it). The subprocess it runs writes the referee's event and output files into
`--tmp`, which defaults to `/tmp`, so two of them at once overwrite each other's --
the only explanation that fits. Those names now carry the process id
(`scripts/audio_refs.py`). Recorded rather than left as a shrug: it is a real race
whether or not it was this one.

**End of milestone 4.** Stopping here, as the working agreement says.

## Decisions (the user, 2026-09-22), answering milestone 4's questions

1. **The YM2608-LLE difference is accepted as characterised**, not chased: the
   differences are transient, measured, and the cycle-exact anchor is Nuked-OPN2.
   Aligning its harness's write strobe is optional work, not a requirement.
2. **The DAC's multiplexing order stays Nuked-OPN2's**, with YM2608-LLE's
   disagreement recorded beside it (the cycle-exact comparison anchors there).
3. **The analogue path is fitted to two consoles, one Model 1 and one Model 2**,
   matching the machine variants the board models, not to all 21 recordings.
4. **The SSG-EG recording's 1.0-1.2 % rate difference is accepted** as that
   recording's clock (the MDFourier recordings show no such drift).
5. **Confirm `ym2612csmmode.wav`'s provenance** in the forum snapshot we already
   have (`reference/forum/spritesmind-t386*.html`); if it is from hardware, use it
   as a judge for CSM, otherwise leave CSM to the die-derived models.
6. **A discrete YM2612's ports 1-3 read Genesis Plus GX's `0`**, labelled
   emulator-derived until something better exists.
7. **The noise channel's 1.72 dB is accepted** as the analogue model's looseness:
   the digital output matches Nuked-PSG exactly, so the difference is in the
   analogue path or in that console.

Also decided: **the FM's speed is its own milestone after the board is complete**.
This pipeline stays as the reference implementation; any faster path is measured
against it, and the cycle-exact comparison is the gate it must pass.

## Session 4 (2026-09-22): milestone 5, bus timing

### How the brief was read

Scope is milestone 5 only: the arbiter's delays for Z80 bank accesses (Kabuto; MoD's
logic analyser), the refresh steal, VDP stalls and DMA speed by the slot table, judged
by VDPFIFOTesting's timing tests, Kabuto's DMA-cost formulas "reproduced within their
stated form", and Overdrive 2 as a stress case against Kabuto's notes rather than
pass/fail. Plus the two items earlier milestones deferred here: the Z80 bus-grant
(BUSACK) delay, and the per-slot renderer with the CRAM dots, the latter to be kept or
dropped on measured frames per second. Then stop.

### Environment

Same as the previous sessions: `.venv` CPython 3.14.4, `.venv-pypy` PyPy 7.3.23
(Python 3.11.15), m68000-python pinned at 51ee1a5 (unchanged: docs/polish.md),
z80-python 0.4.0, MAME 0.285, the 13th-generation Core i9-13900K. The machine was
shared throughout, which the frame-rate numbers below reflect and which is why the
renderer's cost is also measured a second, deterministic way.

### The arbiter

`src/megadrive_python/arbiter.py` (95 lines) carries the two formulas and the
quotations behind them; `board.py` and `system.py` wire them in;
`scripts/run_bus_arbiter.py` (189 lines) measures them.

#### What a Z80 bank access costs, and how it is modelled

Kabuto's "The bus system" gives the averages ("delayed by 2 to 5 cycles ... The
average Z80 delay is around 3.3 Z80 cycles, whereas the average 68k delay is around
11 68k cycles") and his **footnote 1** gives the arithmetic behind them, measured on
his Model 1:

```text
readdelay = a + 3 + b + 2
Z80 delay = ((readdelay*7 + 4 - subcycle)/15
68k delay = ((Z80 delay) * 15 + subcycle + 46) / 7 - a
```

with `a` "time until 68k finishes its last access before releasing the bus (1 to 6; 1
to 4 if there are no refresh delays)", `b` "2 if the z80->ROM access is prolonged by a
refresh delay, 0 if not", and `subcycle` (0 to 6) "how much ahead of the starting 68k
cycle the z80's request was to begin with". That is `bank_access_delay`, verbatim.

What the board has to supply is `a`, `b` and `subcycle`:

- **`a`** needs to know where the 68000's bus cycles fall. `Board._bus_cycle` -- the
  hook the refresh already used for every 68000 access -- now records each access's
  master clock for the step in progress, and `_m68k_release` walks them: an access
  under way, or one starting within Kabuto's "2 cycles ahead or less", is finished
  first, so `a` is the clocks left of it (1 to 6).
- **`b`** is the cartridge refresh grid ticking inside the Z80's own access. The tick
  is *read*, not consumed: the 68000's accesses walk the same grid in `_bus_cycle`
  and, running ahead of the Z80 in this scheduler, would otherwise always have taken
  it first. That makes it about 14 MCLK in 896, 1.6 % of accesses.
- **`subcycle`** is the access's master clock against the 68000's 7-MCLK grid.

The Z80's share is added to its current step; **the 68000's share is owed to its next
step** (`Board.m68k_pending_stall`), because the Z80 runs in the 68000's wake, so the
instruction the delay really fell inside has already been executed. That is an
approximation and is labelled as one.

The access itself is placed at the Z80 step's start plus whatever it has already been
held, because z80-python reports only an instruction's total. That is the one thing
the model gets visibly wrong, and it is measured below rather than argued
(docs/polish.md has the core change that would fix it).

#### The bus grant (BUSACK), settled

Milestone 3 measured this, implemented it and backed it out because holding the 68000
broke the milestone-1 lockstep. It is now modelled, and here is the reasoning in full.

**What the hardware evidence is.** There is no published hardware number for this
direction. MoD's t=1661 logic-analyser counts are of the *other* direction (the Z80
reaching the 68000's bus: "!BG will go low 2 68K cycles later"), and Kabuto's averages
likewise. MoD's `gen_test_z80_bus` counts the BUSACK delay twice, but its hardware
values are not in the pinned material. What there *is* is the specification: BUSREQ
"is sampled by the CPU with the rising edge of the last clock period of any machine
cycle", and the bus is released after it (Zilog UM0080). So the grant falls at the end
of the machine cycle in progress, and a 68000 access to the Z80's space before then is
held by the arbiter rather than failing.

**What it costs.** Nothing, in the end, and that is the finding. The lockstep is not
the price: MAME models no arbiter at all -- it grants at once and charges nothing for a
Z80 bank access -- so `Board.arbiter_enabled` turns the whole of milestone 5's arbiter
off for the replay, exactly as `refresh_enabled` has been turned off since milestone 1
for the refresh MAME also does not model. With that, the milestone-1 lockstep replays
Sonic The Hedgehog instruction for instruction as before (below). And on MoD's ROM the
grant delays the model produces are **0 to 9 68000 cycles**, every one of them shorter
than the 8-cycle `move.b (a2),dN` the ROM measures with, so **its two counters still
print 0000** -- the same result as with the immediate grant. So the specification's
behaviour is modelled, the regression net is intact, and the one test that could
disagree cannot see the difference.

What *would* cost the lockstep is leaving the arbiter on during it, and that was never
the right comparison: MAME is a detector, and a detector that models none of this
milestone's subject cannot judge it.

#### The measurements, against MoD and Kabuto

`scripts/run_bus_arbiter.py` builds a small cartridge for each of MoD's experiments
(the 68000 program that copies a Z80 program into the Z80's RAM, starts it and then
holds the 68000 in the state the experiment wants) and records every delay the board's
own arbiter charges. `.venv-pypy/bin/python scripts/run_bus_arbiter.py --accesses
4000` at commit `21038dd`, seconds:

| Experiment | board, Z80 wait states | hardware | board, 68000 delay |
| --- | --- | --- | ---: |
| 68000 in `stop #$2700` (off the bus), Z80 reading the bank window | **2-3**, mean 2.27 (2: 2,923; 3: 1,077) | MoD: "2-3" | 10-12, mean 10.54 |
| 68000 in his `jmp (a0)` loop ("constantly on the bus"), Z80 reading | **3-4**, mean 3.36 (3: 2,547; 4: 1,453) | MoD: "range is 2-4 ... about 2.93"; Kabuto: 2-5, mean 3.3 | 10-13, mean 10.95 |
| the same, Z80 `pop`ping (his sound drivers' case) | 2-4, mean 2.53 | MoD: "at least 4 wait states" on the second read, mean 3.5 over 10 | 10-13, mean 10.43 |

The first experiment reproduces MoD's range exactly. The second sits between his mean
(2.93) and Kabuto's (3.3) at 3.36, inside Kabuto's 2-5 and one step narrower than MoD's
2-4. The 68000's delay is 10.4 to 11.0 cycles against Kabuto's "around 11" in all three.

**The `pop` case is the one that does not agree**, and the reason is the modelling
limitation above, not the formula: MoD saw the 68000 "sneak in a bus operation in
between the first and second Z80 read", which needs the two reads placed at their own
T-states; the board places both at nearly the same clock and charges 2.53 where he
measured 3.5. Recorded as open item 14.

#### The 68000's own rate, an independent check

Kabuto also measured the whole stall model's effect in one number: "After all these
slowdowns the 68K seems to be running at roughly 480 cycles per raster line instead of
the theoretical 488+4/7 ones." With every stall the board charges -- both refreshes,
the arbiter, the VDP -- the 68000 in the `jmp (a0)` harness keeps **481.0** of a line's
488.6 clocks for its own instructions. The six lockstep games, which also pay VDP
stalls, run at 467.9 to 475.3 (`--cartridge`, 240 frames each).

### The DMA, and one correction to the slot table

`scripts/run_dma_cost.py` (136 lines) measures the hold the VDP puts on the 68000
beyond the bus cycle of the control write that starts a transfer -- which is exactly
Kabuto's "on top of what the actual 68k instructions cost" -- for four targets and ten
word counts, and fits a line to it.

**The correction.** The first run was 25 % slow on every fast transfer and the
Manual's per-line table was out by 3 %. The cause: an idle line's access slots.
Exodus's `GetPixelClockTicksUntilNextAccessSlot` jumps from a refresh block's start to
six pixels past it, losing **two** slots per refresh, which leaves 161 (H32) and 198
(H40). Two hardware-tier sources say one: Kabuto's access pattern has a single `r` per
group, and the Manual's DMA transfer capacity table gives 167 and 205 bytes a line in
blanking, which is 171 - 4 and 210 - 5. The board now loses one slot per refresh, at
the H counter values Exodus's own tables name as the refresh slots (five in H32, six in
H40, "one more than there is during an active line"), giving **166 and 204**. Also
changed: a transfer fetches its first word once the control write that started it is
over, which is what `_run_transfer`'s docstring already claimed and its code did not.

**After that** (`.venv-pypy/bin/python scripts/run_dma_cost.py`, commit `21038dd`,
30 s), fitted over 64 to 512 words with the display off in H40:

| Transfer | board, 68000 cycles a word | Kabuto | board intercept | Kabuto |
| --- | ---: | ---: | ---: | ---: |
| fast (CRAM, VSRAM, VRAM in 128 KB mode) | **2.38** | 2.4 | -3.5 | +5.6 |
| slow (VRAM in 64 KB mode) | **4.78** | 4.7 | -14.3 | -6 |

and the Manual's table for 68000 memory to VRAM, in bytes a raster line:

| | H32, display | H32, blanking | H40, display | H40, blanking |
| --- | ---: | ---: | ---: | ---: |
| the board | **16.1** | **167.4** | **18.1** | **205.9** |
| the Manual (PDF page 44) | 16 | 167 | 18 | 205 |

So Kabuto's formulas are reproduced within their stated form: the right two slopes,
within 2 % of both, with the `max()` shape falling out of the FIFO filling up, and the
negative intercept he explains ("the DMA ends while there is still data in the FIFO to
be processed, this is why there are seemingly negative costs"). **What is not
reproduced is his fast-transfer intercept**: +5.6 against the board's -3.5, so a
one-word transfer costs 8 cycles on his hardware and about 1 here. That is a fixed
start-up the board has no mechanism for; adding 5.6 cycles to match a fitted constant
would be fitting to a fit, so it is left as open item 15 instead.

### The per-slot renderer, and the CRAM dots

Milestone 3 deferred this here, and the brief asks for the decision to be made with
frames per second on PyPy before and after.

**The design.** A line is still set up once, at the point its active display begins:
its horizontal scroll entry (the hardware's own `H` access is the first of the line),
its window columns, and its sprites, which the hardware fetched during the preceding
horizontal blank. Its *pixels* are now drawn lazily. Before anything the rest of the
line would be drawn from changes -- a FIFO write landing at an access slot, a DMA fill
or copy, a register write -- the VDP calls `Renderer.catch_up` with that slot's master
clock and the line is drawn up to the pixel it falls on. A line nothing is written into
is drawn in one piece, exactly as before. That is what keeps it affordable: the split
points are access slots, of which an active line has 16 or 18, not arbitrary pixels.

A CRAM write that lands in the active display also leaves its dot -- the colour it
wrote, for the pixel of its slot. Dots are kept per row and laid over the picture at
the end, as are the palettes each part of a row was drawn with, so a CRAM change along
a line no longer recolours the pixels before it.

**Where the pixels are.** The active display is external H $00-$9F in H40 and $00-$7F
in H32, internal H $000 onwards. That is what makes Kabuto's access-slot groups fall
into place: his 15 "mid-tiles" slots are the ones inside $000-$13F, his two
"between-tiles-and-sprites" ones are $140 and $142 just past it, and his single
"mid-sprite" one is $1CE, in the blanking. His remark that the second mid-tiles slot
lands "at the pixel with X=-3 (display area coordinates)" would put the output about 21
pixels behind the H counter; that pipeline delay is **not** modelled, and is recorded
as item 20 rather than guessed at.

**What it costs.** Two measurements, because this machine's frame rate varies by up to
10 % between identical runs.

Deterministic first -- how many pieces a line is drawn in, 100 frames after 400 of
warm-up, `--no-sound`:

| Game | draws a frame | draws a line |
| --- | ---: | ---: |
| Sonic The Hedgehog (USA, Europe) | 529 | 2.36 |
| Streets of Rage 2 (USA) | 130 | 0.58 |
| Gunstar Heroes (USA) | 278 | 1.24 |
| Castlevania - Bloodlines (USA) | 224 | 1.00 |
| Sonic The Hedgehog 3 (USA) | 224 | 1.00 |
| Phantasy Star IV (USA) | 361 | 1.61 |
| Overdrive 2 (PAL) | 224 | 0.93 |

So most lines are still drawn in one piece and the worst game adds 1.4 extra pieces a
line. Then frames per second, 900 frames, `--no-sound`, measured by **user CPU time**
rather than wall time and taking the better of two runs each, the two configurations
run back to back:

| Game | per line | per slot | change |
| --- | ---: | ---: | ---: |
| Sonic The Hedgehog (USA, Europe) | 51.3 | 52.3 | +2.0 % |
| Streets of Rage 2 (USA) | 64.0 | 61.6 | -3.8 % |
| Gunstar Heroes (USA) | 51.6 | 50.2 | -2.7 % |
| Castlevania - Bloodlines (USA) | 52.0 | 50.3 | -3.3 % |
| Sonic The Hedgehog 3 (USA) | 50.8 | 49.0 | -3.5 % |
| Phantasy Star IV (USA) | 53.7 | 47.1 | -12.3 % |
| Overdrive 2 (PAL) | 38.4 | 38.4 | 0.0 % |

**Decision: the per-slot path stays.** A few percent, with the one outlier inside the
spread two identical runs of the *same* build show on this machine, is not ruinous, and
the deterministic count says why: the machinery only runs where a program actually
writes to the VDP part way along a line. What it buys is mid-line writes landing where
they land on the hardware and the CRAM dots, which Overdrive 2 produces (below) and the
six games do not (they write CRAM in the vertical blank, where the hardware makes no
dot either).

**What it does not buy, yet: r57shell's redraw-timing tests.** They are still not
judged, and not for a rendering reason: the ROM polls the pad seven times a frame --
the 6-button handshake -- so neither the board nor MAME leaves its first screen. Both
render that screen identically (100.00 % of pixels). The test waits for milestone 7.

### Milestone 5 acceptance: the runs and their results (2026-09-22)

All at commit `21038dd` unless said; m68000-python 51ee1a5, z80-python 0.4.0, PyPy
7.3.23 (Python 3.11.15), MAME 0.285.

**VDPFIFOTesting's timing tests** (hardware-corrected; ROM SHA-256 `8b0ed394...`).
`.venv-pypy/bin/python scripts/run_vdpfifotesting.py --machine M`, 7-8 s each:
**122 of 122** on genesis, genesis-tmss, genesis2, megadrive, megadrive-tmss,
megadrive2, megadrive-japan and megadrive2-japan; **114 of 122** on genesis3, the
eight DMA Fill to VSRAM tests, as a Genesis 3 fails VSRAM tests on hardware. The
timing tests among them all pass: FIFO Buffer Size, FIFO Wait States, DMA Transfer
Bus Locking, FIFO Full Before DMA Transfer, DMA Transfer using FIFO and the seven DMA
Busy Flag tests. Unchanged from milestone 2 through the slot-table correction and the
first-fetch change, both of which move DMA timing.

**Kabuto's DMA-cost formulas**: reproduced within their stated form -- the table above.

**The arbiter's delays**: the three experiments above.

**MoD's `gen_test_z80_bus`** (hardware-corrected): all eight checks PASS, the Z80's
writes to 68000 work RAM read back $4243 and its reads give $FFFF, and the two BUSACK
delay counters print **0000** with the grant now modelled (the reason is above).

**Milestones 1 to 4, unchanged.**

- The milestone-1 lockstep: Sonic The Hedgehog (USA, Europe) re-recorded from MAME
  0.285 (30 emulated seconds, `scripts/mame_trace.py`) and replayed with the arbiter
  and the per-slot renderer in the tree: **23,286,815 68000 and 12,728,465 Z80
  instructions identical, 0 unexplained**, 122 s. The trace (684 MB) was deleted.
- `.venv-pypy/bin/python -m pytest -q`: **42 passed, 1 deselected** in 125 s -- the 288
  HV/status capture dumps still at 8,762,415 of 8,847,360 words, VDPFIFOTesting, the
  sprite masking ROM at 0 of 18 cells differing, `gen_test_z80_bus`, milestone 4's FM
  and PSG referee comparisons, and milestone 5's ten new tests.
- The nine milestone-3 test ROMs against MAME's own frames after 3 emulated seconds
  (`scripts/compare_mame_frames.py`): 100.00 % for `wtest`, `wtest_i1`, `stetest` and
  r57shell's `vdp_tests`, 99.84 % `wbug`, 96.93 % `vctest`, 96.25 % `wtest_i2`,
  69.64 % `SHLTEST`, 41.97 % `STETEST2` -- every figure identical to milestone 3's.
- MAME's exit frames for the six lockstep games at 20 emulated seconds (`--record`):
  92.38, 99.91, 82.75, 91.10, 99.90 and 93.03 % of pixels, against milestone 3's
  92.10, 99.91, 82.75, 91.10, 99.90 and 93.03. Only Sonic The Hedgehog moved, by a
  quarter of a percent, in the animation that was already a few frames out of phase.

### Overdrive 2, the stress case

`.venv-pypy/bin/python`, `megadrive` (the demo prints "THIS DEMO REQUIRES PAL/50 HZ"
on an NTSC machine and stops), 12,000 frames -- 240 emulated seconds, 1,075 s of wall
time with sound -- writing a PNG every 600 frames and the whole soundtrack as a WAV.

The demo needed one thing before it would run at all: its cartridge board. MAME's
software list gives Overdrive 2 the slot `rom_titan`, a device whose own comment says
"This is same as SSF2 mapper, merge at cart slot rewrite" and whose registers are the
same words of $A130F0; the board now accepts that slot as the mapper it already had.
Without it the demo prints "YOUR CARTRIDGE OR EMULATOR DOES NOT SUPPORT SSF2-STYLE
BANK SWITCHING" and stops. `python -m megadrive_python` also had to identify the
cartridge (`Cartridge.from_image`) rather than load every image as plain ROM.

**It runs to its end.** 12,000 frames of PAL is 240.4 s, and the last sampled frame
(11,400) is the credits. 139,236,731 68000 and 116,596,672 Z80 instructions;
6,519,808 Z80 bank accesses, costing the Z80 23,962,067 of its cycles and the 68000
70,597,738 of its own.

**The music is at the right speed, measured against Kabuto's own note.** He wrote the
demo's sound driver: "I developed it from scratch to support 26 kHz sample playback
with nearly no jitter". Counting the driver's writes to the YM2612's DAC register
($2A) over 600 frames of the demo:

| | DAC writes in 12.07 s | realised sample rate |
| --- | ---: | ---: |
| the board, arbiter on | 317,648 | **26,313 Hz** |
| the board, arbiter off | 326,282 | 27,028 Hz |
| Kabuto | | "26 kHz" |

So the arbiter's bank-access delays are what bring the driver from 27.0 kHz to
26.3 kHz -- the number he designed for. That is the clearest single piece of evidence
this milestone produced: the delays are not a decoration, they set the pitch of this
demo's samples, and they set it where its author says it belongs.

**CRAM dots: 33,733 of them** over the run, all after frame 9,000, which is the
sphere scene -- the one that paints a full-colour background by writing CRAM during
the display. The six lockstep games make none (they write CRAM in the vertical blank,
where the hardware makes no dot either).

**The picture.** Of the twenty sampled frames, most are the scene they should be: the
arcade, the spaceship, the robot, the city flyby, the sunset, the Overdrive 2 logo
over concentric rings, the Mega Drive console, the sphere, the robot's head, and the
credits. Four (frames 7,200, 8,400, 9,000 and 10,800) show a plaid or blocky pattern
where a scene belongs. Overdrive 2 never enters 128 KB mode, so that is not the cause;
it uses the debug register's plane forcing in four windows (frames 6,528-7,064 forcing
sprites, 7,064 on with it clear, 8,128-9,130 and 9,132-9,740 and 10,144-11,082 forcing
plane B), and the bad frames are not confined to any one of them.

For a detector, MAME 0.285 was run on the same demo from its software list
(`mame megadriv -cart d_titov2`) at 144, 192 and 216 emulated seconds. **MAME gets
those sections wrong too, and differently**: at 144 s it shows a blocky pattern of its
own, at 216 s red horizontal stripes where the board shows a red plaid, and at 192 s
it draws the sphere over concentric rings where the board draws it over a diagonal
weave. So all three are cases where no emulator here is right and the two are wrong in
different ways, which is what the demo's readme predicted ("At the moment, this demo
will not run on ANY emulator") and what its dependence on the VDP revision means. As
the brief says, this is a stress case compared with Kabuto's notes, not a pass or a
fail; the sections are recorded here for a later session.

### Open questions for the user

QUESTION: **An idle line's access slots were changed from Exodus's 2-per-refresh to
1**, on the strength of Kabuto's access pattern and the Manual's DMA transfer capacity
table, both hardware-tier, against Exodus, which is emulator-derived but written by
the author of the hardware captures. It puts the board's DMA rates on Kabuto's
measured formulas and reproduces the Manual's whole table. Accept, or is Exodus's
table the one to follow where he and Kabuto disagree?

QUESTION: **Kabuto's DMA start-up cost is not modelled.** His fast-transfer formula
has a +5.6-cycle intercept where the board charges -3.5, so a one-word transfer costs
8 68000 cycles on his hardware and about 1 here; the slopes agree within 2 %. Adding a
fixed 5.6 cycles would be fitting to a fitted constant, so it is left open. Pursue it,
or accept the slopes?

QUESTION: **The bus grant is the specification's alone.** No hardware measurement of
the Mega Drive's BUSACK delay is published in the pinned material, and MoD's ROM
cannot see the one the board now produces (0 to 9 68000 cycles). Keep UM0080's rule
unjudged, or take it back out until something can judge it?

QUESTION: **The 68000's share of a Z80 bank access is charged to its next
instruction**, because the Z80 runs in the 68000's wake in this scheduler. Accept, or
is it worth running the Z80 ahead of the 68000 (or rolling the 68000 back, as MoD
describes wanting to do in BlastEm) so the delay lands inside the instruction it
really fell in?

QUESTION: **Kabuto's "X = -3" is not modelled.** He places the second of a line's
mid-tiles access slots at display X = -3, which would put the VDP's output about 21
pixels behind its H counter; the board lands a mid-line write at the pixel its slot's
own H counter names. Nothing pinned measures the offset, and r57shell's redraw tests
cannot be reached until the 6-button pad exists. Leave it, or fit the 21 pixels from
Overdrive 2's sphere scene (where it would move the colour bands)?

QUESTION: **Four of Overdrive 2's twenty sampled frames are wrong**, in sections MAME
also gets wrong differently. Run them down in a session of their own, or leave them
until milestone 8's sweep gives more cases of the same kind?

**End of milestone 5.** Stopping here, as the working agreement says.
