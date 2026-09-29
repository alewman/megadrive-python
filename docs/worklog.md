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
check. *(Corrected 2026-09-27, Session 13: that dropped bit 16 from every write, and
the sprite cache compares all 17 bits in 64 KB mode too. The address register is now
17 bits; Overdrive 2's rain needed it. See "The rain".)*

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

## Decisions (the user, 2026-09-23), answering milestone 5's questions

1. **The one-slot-per-refresh change is accepted**: Kabuto's measured access
   pattern and Sega's own DMA table agree on one, against Exodus's two, and the
   change is what puts the DMA on Kabuto's slopes and reproduces the Manual's
   bytes-a-line table. Two sources over an emulator, as the tier rule says.
2. **Kabuto's DMA start-up cost (+5.6) is left open** and revisited in milestone
   8, where a real game difference in the sweep would point at it.
3. **The bus grant stays.** It is Zilog UM0080's rule (specification tier),
   labelled as having no hardware judge, and it costs the lockstep nothing.
4. **Charging the 68000's share of a bank access to its next instruction is
   accepted**, documented as a consequence of stepping instruction by instruction
   rather than bus cycle by bus cycle.
5. **Kabuto's "X = -3" output pipeline is left unmodelled.** Fitting 21 pixels
   from Overdrive 2's sphere would be fitting to a demo whose correct output
   depends on the VDP revision.
6. **Overdrive 2's four wrong frames get their own session after milestone 8's
   sweep**, so that it is known first whether the same cause shows in ordinary
   games. Nuked-MD (milestone 6) is the referee for it.

## Session 5 (2026-09-23): milestone 6, Nuked-MD as a referee

### How the brief was read

Scope is milestone 6 only: build Nuked-MD headless from `reference/src/Nuked-MD`
(GPL-2.0 -- run it, read it, cite it, never copy it), teach it to write a bus-level
trace, and for the test ROMs of milestones 2-5 show that the board's bus and device
timing agrees with it or sort every difference into a named cause. Keep the build
script in the repository, as milestone 4's `validation/build_referees.sh` does.
Nuked-MD's tier is the one `docs/validation.md` gives a die-derived model -- it judges
only where no hardware source covers the question -- with one caveat of its own: its
README says it models "NMOS 68k, NMOS Z80 and FC1004. No real mega drive used such
combination, but Model 1 VA7 would be closest", so anything that follows from its chip
combination, from its unimplemented parts or from this build's `-DTMSS_ENABLE=0` is a
named cause and not a board bug.

Overdrive 2's four wrong frames are explicitly *not* in scope (the user deferred them
to their own session after milestone 8); nothing here touched them.

### Environment

Same as the previous sessions: `.venv` CPython 3.14.4, `.venv-pypy` PyPy 7.3.23
(Python 3.11.15), m68000-python pinned at 51ee1a5 (unchanged: docs/polish.md),
z80-python 0.4.0, MAME 0.285, the 13th-generation Core i9-13900K. gcc 15.2.0
(Ubuntu 15.2.0-16ubuntu1) for the referee. Nuked-MD at `594ca2a` ("FJ3002 FM wip"),
the revision `scripts/pinned_repos.json` pins.

### What was built

| File | Lines | What it is |
| --- | ---: | --- |
| `validation/nuked_md_headless.c` | 342 | the three back ends Nuked-MD's `md.c` calls (`Audio_*`, `Video_*`), plus the bus tracer: one sample per half-MCLK of /AS, /UDS, /LDS, R/W, the address and data buses, the Z80's /MREQ, /IORQ, /RD, /WR and ZCLK, and eight board signals (/ZBR, /BUSAK, /ZRES, /BGACK, the arbiter's refresh strobe, /WAIT, /SOUND, the VDP's reset) |
| `validation/nuked_md_sdl.h` | 56 | a stand-in for the SDL2 declarations `md.c` and `controller.c` use, so no SDL is needed and the run is single-threaded and deterministic |
| `validation/build_nuked_md.sh` | 64 | the build, with both switches explained |
| `scripts/nuked_md.py` | 495 | drive the referee, parse its trace, take the *board's* own bus log in the same shape, align the two and compare them |
| `scripts/run_nuked_md.py` | 886 | the six comparisons below |

Both C files are this repository's own (MIT) and are compiled *against* Nuked-MD's
headers, exactly as `validation/opn2_ref.c` and the other two are compiled against
theirs. Nothing of Nuked-MD is copied here. The binary lands in
`reference/build-nuked-md/` (git-ignored) and is not distributed.

The build: `sh validation/build_nuked_md.sh`, 12 s.

Two switches, both deliberate and both recorded in the script:

- `-DTMSS_ENABLE=0`. Nuked-MD's FC1004 carries TMSS, and with it enabled the bank
  register (`tmss.c`, `dff3`) powers up *clear*, so the cartridge is not decoded until
  "SEGA" reaches $A14000 -- which no pre-TMSS test ROM writes. With it 0 the same
  flip-flop powers up set (`SDFFS` instead of `SDFFR`), which is the pre-TMSS board.
  The referee's I/O chip still reports hardware version 1, so the combination is TMSS
  hardware with TMSS off: one more of the referee's departures from any retail console.
- `-w`, as upstream builds it.

**The trace.** 24-byte packed records: half-MCLK, the ZCLK edge count, the byte
address, the data, the cycle's length, a type (68000 bus cycle, Z80 bus cycle, signal
change) and flags. A 68000 bus cycle is /AS low to /AS high; its **length** is what
makes the comparison sharp, because 18 MCLK is a cycle nothing is stretching and
anything longer is a stall the referee charged -- which is exactly what the board's
`Board.stall` counts. So each access is compared on four things: its byte address, its
direction and size, *the stall charged to it*, and the data on the bus.

The board's side needs no change to `src/`: `scripts/nuked_md.py` wraps the four bus
callables the 68000 core holds through m68000-python's public `attach_bus`, and
`board.dma_read_word` and the Z80's two callables by assignment.

### What was chosen to compare, and why

Nuked-MD runs at about **0.33 emulated frames a second** here (2,000,000 half-MCLK in
3.4 s: 294,000 MCLK a second, against the machine's 53,693,182). One emulated second
costs three minutes. That rules out running a test ROM to its verdict and rules in
three things:

1. **Small purpose-built cartridges** where the question is one timing rule: eight
   probe loops, the three arbiter experiments, the bus grant, the DMA against its word
   count, the FM's BUSY. Each is 20,000-60,000 MCLK of trace after the referee's 68000
   comes out of reset, 2-11 s of wall time.
2. **The first 900,000 MCLK (about one NTSC frame) of each test ROM of milestones
   2-5**, from its reset vector on, which is its whole initialisation -- the VDP set-up,
   the RAM clear, the first DMAs -- and 25,000-29,000 bus cycles. 6-8 s each, run
   five at a time.
3. **Nothing by picture or by verdict**: what the referee is uniquely good for is the
   bus, and the picture already has hardware judges (milestone 3).

**The referee's own clock.** Its 68000 fetches its reset vector at **693,230 MCLK**
(/SRES is released at half-MCLK 10,000 and the FC1004 then holds the CPU); the board's
does at 0. Every comparison therefore puts the board's clock at 693,230 before the
first step, so both machines' VDPs are the same distance into a frame when the program
starts, and aligns the two streams on 24 consecutive matching (address, kind) pairs.

**The two refresh phases are read out of the referee, not fitted.** Both refreshes are
free-running counters whose phase at power-on nothing models. Because the referee
reports each cycle's length, its ticks are *observed*: a +14 on a cartridge access is
the cartridge refresh, a +21 on a work-RAM access is the work RAM's. A grid is fitted
to those observations and the board's counters are set to match. That is the only
thing taken from the referee; the periods and the stretches are the board's own.

### The cartridge refresh: the board's rule is exactly right

Fitting the board's rule -- a fixed 896-MCLK grid, the next bus cycle absorbs the tick,
and it costs 14 MCLK only if that cycle is a cartridge access -- to the referee's own
observed stretches:

| ROM (300,000 MCLK from the referee's reset) | cartridge stretches | phases that reproduce all of them |
| --- | ---: | --- |
| Nemesis's `sr logic analyser noint - H40V28` | 269 | 518-523 |
| CMD's `wtest.bin` | 250 | 518-523 |

The same phase window for two unrelated programs, because both start from the same
power-on. The obvious variant -- "only a cartridge access absorbs the tick", so that a
work-RAM access leaves it standing -- reproduces neither ROM at any phase. Over the ten test ROMs below the board's
rule misplaces **0 of the referee's cartridge ticks** in every one.

### The work-RAM refresh: the period is narrowed, the *rule* does not generalise

The board's rule is "once due, the next work-RAM access waits 21 MCLK, and the next
falls due 805 MCLK after that access started" (milestone 2, from Nuked-MD; the user
accepted it as the basis until a hardware measurement exists).

- **The stretch is confirmed**: 21 MCLK, on every work-RAM stretch seen in four probe
  loops (59, 59, 56 and 20 of them) and on all 100 and 344 in the two test ROMs above.
- **In a dense work-RAM loop the rule is exact**, and the period is narrowed from
  milestone 2's 743-826 to **806-819** (`move.w (a0),d0` x16 with a0 = $FF0000, and the
  same with writes: 799 work-RAM accesses, 59 stretched, every period in 806..819
  reproduces all 59 at some phase and 805 reproduces none). So the board's 805 is one
  MCLK below the window that fits.
- **It does not generalise.** With work-RAM accesses every 112 MCLK instead of every
  62, no period in 600..1400 reproduces the referee at any phase. On CMD's `wtest.bin`
  the exact periods are **694-699**, not 806-819. On Nemesis's capture ROM no period
  in 600..1400 fits at all (the best leaves 269 misplaced). The referee's stretches are
  not raster-locked either (their times are spread evenly modulo a 3,420-MCLK line).

So the re-arm timer is a fit to one access pattern, not the machine's rule, and
**805 was left unchanged**: no single value fits, moving it would re-open milestones
2-5's acceptance runs for no gain, and the question is now sharper than a constant.
This is the single largest source of difference in the ROM comparison below, and every
cartridge-stretch difference that goes with it is a knock-on: each misplaced work-RAM
tick shifts the board's whole timeline by 21 MCLK, after which the (correct) cartridge
grid lands on different accesses.

### The probe loops

`.venv-pypy/bin/python scripts/run_nuked_md.py probes --cycles 400`, 20,000 MCLK of
trace from the referee's reset, 2-3 s each:

| Probe | bus cycles equal | what the rest is |
| --- | ---: | --- |
| NOP loop (ROM fetches only) | **394 / 394** | |
| ROM reads (`move.w (a0),d0`, a0 in ROM) | **394 / 394** | |
| VDP data writes (a control write then eight data writes) | **394 / 394** | |
| work RAM reads | 312 / 394 | the work-RAM refresh (27 ticks misplaced) and its knock-on |
| work RAM writes | 311 / 394 | likewise |
| work RAM read-modify-write (`addq.w #1,(a0)`) | 325 / 394 | likewise |
| VDP status reads | 336 / 394 | 58 reads differ in bit 2 (HBlank) only: the referee's VDP comes out of reset at its own point in the frame (below) |
| VDP HV counter reads, Mode 5 | 232 / 394 | the same, and the counter itself (below) |
| VDP HV counter reads, still in Mode 4 | 223 / 394 | the referee's Mode 4 HV port reads a constant $9F00; the board reads the Mode 5 counter whatever the mode |
| **total** | **2,921 / 3,546** | |

Three named causes come out of this and hold for everything below:

1. **The referee's VDP has its own power-on phase.** At the same master clock the
   board's V counter reads $CA (line 202, which is 693,230 mod 3,420 x 262) and the
   referee's $9F (line 159): its VDP starts about 43 lines and 1,040 MCLK after the
   board's. Shifting the board's clock to make the *first* HV read agree leaves only 1
   of 162 later reads agreeing, so the two also step differently. **The HV counter and
   the status register are not the referee's to judge**: Nemesis's logic-analyser
   captures are hardware and cover exactly this question (milestone 2, 8,762,415 of
   8,847,360 words), and the tier rule puts hardware above a die-derived model. Recorded
   and not pursued.
2. **Mode 4's HV port is not modelled.** At power-on the VDP is in Mode 4 and the
   referee's $C00008 reads a constant; the board reads its Mode 5 counter. No test ROM
   of milestones 2-5 reads HV before setting Mode 5, so nothing here depends on it.
3. **Work RAM powers up with the referee's DRAM contents** ($5E in the even bytes of
   the first words, in these runs) and the board's with zero, so a probe that reads work
   RAM before writing it differs in data. The probes now clear what they read.

### The test ROMs of milestones 2-5

`.venv-pypy/bin/python scripts/run_nuked_md.py roms --window 900000 --cycles 40000
--parallel 5`, 6-8 s each. Every 68000 bus cycle from the reset vector on: byte
address, direction, size, the stall charged to it, and the data on a read. The
comparison stops at the first *stream* difference, because after that the two machines
are running different code.

| ROM (SHA-256, first 16) | bus cycles equal | in the window | what the rest is |
| --- | ---: | ---: | --- |
| `wtest.bin` `160927b5a9f23efc` | **28,921 / 28,921** | 28,927 | |
| `vctest.bin` `085115ec3e82ed4c` | **28,921 / 28,921** | 28,927 | |
| `ym2612csmmode.bin` `3faaae394f6eba5e` | **25,307 / 25,307** | 25,313 | |
| `ym2612detune.bin` `74306d736f99541c` | **25,307 / 25,307** | 25,313 | |
| `ym2612multiplexing.bin` `519d19a023edf623` | **25,307 / 25,307** | 25,313 | |
| `gen_test_z80_bus.md` `ed344fed9de5c985` | 21,828 / 21,832 | 28,096 | one work-RAM refresh tick, then the bus grant (below) |
| `stetest.bin` `0c7729d63bc193ce` | 230 / 233 | 28,135 | one VDP status bit, then the bus grant |
| `vdp_tests.bin` `207cd17db44a2142` | 22,125 / 28,122 | 28,128 | the work-RAM refresh (1,841 ticks) and its knock-on |
| `sr logic analyser noint - H40V28` `fd25dd3094734b6b` | 23,258 / 28,621 | 28,627 | likewise (1,305 ticks) |
| `VDPFIFOTesting.bin` `8b0ed3944893d679` | 23,259 / 28,622 | 28,628 | likewise (1,303 ticks) |
| `SpriteMaskingTestRom.bin` `1331819ef3ed07c6` | -- | 11 | the referee's 68000 makes eleven bus cycles and stops: not compared |

In every one of the ten the board's cartridge-refresh rule misplaces **0** of the
referee's ticks. Every difference is one of four named causes:

- **the work-RAM refresh rule** (above), and the cartridge stretches that move with it;
- **the Z80's bus grant** (below), which ends `gen_test_z80_bus` and `stetest`;
- **the VDP's power-on phase** (a single status read each in `stetest` and
  `vdp_tests`);
- **the I/O chip's hardware version**: the referee reads $A1 at $A10001 (overseas,
  NTSC, no Mega CD, version 1) where the board's `genesis` machine reads $A0 (version
  0). The comparisons give the board the referee's value; without it `stetest` and
  `vdp_tests` branch away after 34 and 36 cycles, which is what version detection is
  for. Nuked-MD is an FC1004, so version 1 is right for it.

`SpriteMaskingTestRom.bin` is recorded rather than explained: the referee's 68000
makes eleven bus cycles from its reset vector and then nothing at all for the rest of
the window. No other ROM behaves that way, and chasing it was out of proportion to the
comparison it would buy.

### Milestone 5's open items, answered

#### Kabuto's DMA start-up cost: the referee says it is real (open item 15)

`.venv-pypy/bin/python scripts/run_nuked_md.py dma --words 1,2,3,4,8,16,32,64,128,256,512
--targets CRAM,VSRAM,VRAM`. Each ROM sets the VDP up (Mode 5, DMA enabled, display off,
H40), starts one 68000-memory DMA from $FF0000, and the cost measured is the gap the
transfer leaves in the 68000's own bus stream beyond the write that starts it -- which
is Kabuto's "on top of what the actual 68k instructions cost", verbatim.

| words | referee, CRAM/VSRAM | board | referee, VRAM | board |
| ---: | ---: | ---: | ---: | ---: |
| 1 | **11.0** | **1.1** | **11.0** | **1.1** |
| 2 | 13.0 | 4.0 | 13.0 | 4.0 |
| 4 | 18.0 | 6.9 | 18.0 | 6.9 |
| 16 | 45.0 | 35.4 | 73.0 | 69.7 |
| 64 | 168.0 | 175.4 | 305.0 | 326.6 |
| 256 | 641.0 | 642.6 | 1227.0 | 1245.4 |
| 512 | 1273.0 | 1254.6 | 2449.0 | 2475.7 |

Fitted over all eleven word counts, in 68000 cycles:

| | slope | intercept |
| --- | ---: | ---: |
| referee, fast (CRAM, VSRAM) | 2.47 | **+8.0** |
| board, fast | 2.46 | +3.2 |
| Kabuto, fast | 2.4 | **+5.6** |
| referee, slow (VRAM) | 4.78 | -0.3 |
| board, slow | 4.86 | -2.6 |
| Kabuto, slow | 4.7 | -6 |

**The start-up cost is real.** A one-word fast transfer costs the referee 11.0 68000
cycles and the board 1.1; Kabuto's formula gives 8.0. The board reproduces both slopes
(within 1 % of the referee, within 3 % of Kabuto) and neither intercept, and the
referee sits on Kabuto's side of the question rather than the board's. That is what
milestone 5's open item 15 asked for: the +5.6 is not an artefact of his fit.

#### The `pop` wait states: the board has the two reads the wrong way round (open item 14)

`scripts/run_nuked_md.py arbiter`, MoD's three experiments run on the referee and on
the board from the *same* image. One change from `scripts/run_bus_arbiter.py`: each
Z80 loop jumps back to $0000 rather than into the middle of itself. With MoD's
`jp $0003` the referee's Z80 never executes the `ld sp,$8002` -- found by tracing: its
`pop` read Z80 RAM at three T-states and /WAIT never moved -- so the experiment
measured nothing. Jumping to $0000 makes the loop self-correcting on both machines.

The referee's wait states are counted exactly, from ZCLK edges: a Z80 memory cycle is
three T-states and the next machine cycle begins the moment it ends, so
`(next cycle's first ZCLK) - (this one's) - 3` is the count.

| Experiment (2,000 board accesses, 134-218 referee accesses) | referee | board | hardware |
| --- | --- | --- | --- |
| 68000 in `stop #$2700`, Z80 reading the bank window | **2-3, mean 2.25** | 2-3, mean 2.19 | MoD: "2-3" |
| 68000 in his `jmp (a0)` loop, Z80 reading | **2-4, mean 2.74** | 2-5, mean 3.55 | MoD: 2-4, "seemed about 2.93"; Kabuto: 2-5, mean 3.3 |
| the same, Z80 `pop`ping | 2-5, mean 3.28 | 2-5, mean 3.19 | MoD: the second read "at least 4 wait states", mean 3.5 over 10 |
| -- the `pop`'s **first** read | mean **2.95** (2-4) | mean **4.08** (2-5) | |
| -- the `pop`'s **second** read | mean **3.61** (2-5) | mean **2.30** (2-3) | MoD: 3.5 |

The first experiment agrees with the referee and with MoD. The second is the board's
worst: it charges 3.55 where the referee charges 2.74 and MoD measured 2.93, and it
produces 5s the referee never does -- Kabuto's `a` (how long the 68000 still needs the
bus) comes out systematically larger on the board than on the die.

**The `pop` is the interesting one, and the referee names the cause.** The two reads
of one `pop` cost the referee 2.95 then 3.61 -- the second is the expensive one, which
is exactly what MoD measured on hardware ("the second read from the POP seems to always
have at least 4 wait states", mean 3.5). The board has it **the wrong way round**:
4.08 then 2.30. The totals happen to be close (3.19 against 3.28), which is why
milestone 5's averages looked reasonable; the split shows what is really happening.
The cause is the one docs/polish.md already records: z80-python reports only a step's
total, so the board places both of a `pop`'s reads at the step's start, the first one
absorbs the whole bus hold and the second finds it already paid. This is a measured
confirmation of that entry, not a new problem, and it is the strongest argument yet for
proposing the Z80 equivalent of m68000-python's `step_clocks`.

#### The bus grant: the board grants about 7 68000 cycles too early

`scripts/run_nuked_md.py busack`: the 68000 asks for the Z80's bus, holds it for 20
NOPs, gives it back, waits 20 NOPs and asks again, for ever, while the Z80 runs its
bank-reading loop. 37 grants in 60,000 MCLK.

| | delay from the $A11100 write to /BUSAK | in 68000 cycles |
| --- | ---: | ---: |
| referee, /ZBR to /BUSAK | 50-100 MCLK | 7.14-14.29, mean 9.64 |
| referee, the $A11100 write to /BUSAK | 68-118 MCLK | **9.71-16.86, mean 12.22** |
| board (`System.z80_busack`) | 10-44 MCLK | **1.43-6.29, mean 4.86** |
| hardware | nothing in the pinned material measures this direction | |

The board models only Zilog UM0080's rule -- BUSREQ "is sampled by the CPU with the
rising edge of the last clock period of any machine cycle" -- and nothing of the
arbiter's own propagation, so it grants about 7.4 68000 cycles early. Milestone 5
accepted the rule as unjudged because nothing could judge it; **this is the judge the
tier rule allows**, and it says the rule is right in shape and short by a constant.

It is not academic. In the ROM comparison above, both `gen_test_z80_bus` and
`stetest` end at a read of $A11100 where the referee returns $0067 and the board
$0066: bit 0 is the grant, the programs are polling for it, and the board answers one
poll sooner. Those are the only two ROMs that poll it, and it ends both comparisons.

### The sound chips' wiring, which milestone 4's referees could not see

`scripts/run_nuked_md.py sound`. A cartridge releases the Z80's reset (the FM shares
that line), takes the Z80's bus, writes an FM register eight times and after each write
polls $A04000 until BUSY clears, counting the polls; then it reads all four ports. The
whole thing is on the 68000's bus, so the trace answers it.

- **BUSY: the same 6 polls on both machines, all eight times.** The board's program
  takes the same path as the referee's through every poll loop (there is no *stream*
  difference anywhere in the run), so the board's BUSY window and the referee's are the
  same to the poll. That is the board's wiring of the chip, not the chip's arithmetic,
  and it is the part Nuked-OPN2 and YM2608-LLE could not check.
- **Ports 1-3 read $00 on the referee**, as they do on the board (milestone 4's
  decision 6 took Genesis Plus GX's 0 for want of anything better). One more source,
  and a die-derived one.
- **One real difference, and it is the board's: every 68000 access to $A0xxxx costs
  one 68000 clock more on the referee.** 68 of the run's accesses to the Z80's space
  are held 7 MCLK by the referee and 0 by the board (and 93 later cartridge fetches
  move with them): 434 of 636 bus cycles agree, and every difference is that one clock
  or a knock-on of it. This is **MAME's `before_delay(1)`** -- `megadriv.cpp`,
  "required by pacman2 intro", "TODO: unverified" -- which milestone 1 recorded as a
  named class of difference between the board and MAME, 41,274 steps of Sonic alone.
  Nuked-MD verifies it. Nothing hardware covers the question, so the tier rule lets the
  die-derived model judge, and it agrees with MAME against the board.

  It was **not** changed this session: adding a clock to every $A0xxxx access moves
  milestone 1's lockstep (it would remove a whole named-difference class, which is an
  improvement, but the lockstep has to be re-recorded from MAME to show it), and it
  moves the arbiter's `subcycle` and the Overdrive 2 sample rate. It is the one
  actionable board fix this milestone found, and it is left as a question for the user
  with the numbers above.

Run as `genesis2` (the ASIC FM the referee actually carries) the figures are identical
to `genesis`: nothing in this probe distinguishes the two FM variants.

### What is *not* claimed

- Nothing about the **picture**: the referee's video output was not compared.
- Nothing about the **HV counter or the status register's** values: Nemesis's hardware
  captures cover them and outrank the referee (above).
- Nothing about **Overdrive 2's four wrong frames**: out of scope this session, and
  nothing here happened to localise them.
- The referee's own FM is the **CMOS YM3438**; milestone 4's sample-for-sample work
  against Nuked-OPN2 and YM2608-LLE is unaffected and was not re-run.
- **Mode 4** (the Power Base Converter) is not modelled by the board at all and the
  referee's Mode 4 HV port differs; milestone 10 is where that belongs.

### Milestones 1 to 5, unchanged

**Nothing in `src/` was modified this session.** Every difference the referee found is
a decision that re-opens an earlier milestone's acceptance run -- the lockstep needs
re-recording from MAME, the arbiter's numbers move, Overdrive 2's realised sample rate
moves -- so each is a `QUESTION:` below and a row in docs/polish.md's new "waiting on a
decision the user has to make" table, not a change made at the end of a session that
could not re-run what it moved.

`.venv-pypy/bin/python -m pytest -q` at commit `10255f9`: **46 passed, 1 deselected**
in 139 s, against milestone 5's 42 passed, 1 deselected -- the four new ones are
milestone 6's (three probe loops reproduced cycle for cycle, and the cartridge refresh
grid). `ruff check` and `ruff format --check` clean over the whole tree.

### Open questions for the user

QUESTION: **Should the board charge one 68000 clock for every access to $A00000-$A0FFFF?**
The referee does (68 of 68 such accesses in the sound probe, 7 MCLK each) and so does
MAME, whose own comment calls it unverified; nothing hardware covers it. Doing it would
delete a named-difference class from milestone 1's lockstep but needs that lockstep
re-recorded from MAME, and it moves the arbiter's `subcycle` and Overdrive 2's realised
sample rate. Change it, or leave it named?

QUESTION: **The bus grant is 7.4 68000 cycles too early.** The referee measures the
$A11100 write to /BUSAK at 9.71-16.86 68000 cycles (mean 12.22) against the board's
1.43-6.29 (mean 4.86), and two test ROMs poll $A11100 and see the difference. Milestone
5 kept UM0080's rule because nothing could judge it; now something can. Add the
arbiter's propagation (a constant, fitted to the referee), or leave the specification's
rule alone and name the difference?

QUESTION: **The work-RAM refresh rule does not generalise.** The stretch is confirmed
at 21 MCLK, and the re-arm period is exact at 806-819 in a dense work-RAM loop -- but
694-699 in `wtest.bin` and nothing at all in Nemesis's capture ROM. It is the largest
remaining source of difference (1,300-1,900 misplaced ticks per test ROM). Leave 805
and the named difference, spend a session finding the referee's real mechanism, or drop
the work-RAM refresh until there is a rule that holds?

QUESTION: **Kabuto's DMA start-up cost is confirmed by the referee** (+8.0 cycles
fitted, 11.0 for a one-word transfer, against the board's +3.2 and 1.1). Milestone 5's
decision 2 left it open until milestone 8. Model it now from the referee -- which would
be fitting the board to a die-derived model where Kabuto's hardware figure agrees in
sign and size -- or keep it for milestone 8 as decided?

QUESTION: **The board charges 3.55 Z80 wait states where the referee charges 2.74 and
MoD measured 2.93** for a bank read with the 68000 constantly on the bus, and it
produces 5s neither sees. Kabuto's `a` is too large on the board. Worth running down
against the referee's own bus log (which shows exactly where each 68000 cycle was when
the request arrived), or accept a 0.6-wait-state bias?

QUESTION: **`SpriteMaskingTestRom.bin` makes eleven bus cycles on the referee and then
stops.** Worth a look (it may be a referee limitation, in which case it is a named
cause, or a real difference in what the ROM does on an FC1004), or leave it?

### Commits

| | |
| --- | --- |
| `b95f0d5` | Nuked-MD headless, with a bus-level trace (`validation/nuked_md_headless.c`, `validation/nuked_md_sdl.h`, `validation/build_nuked_md.sh`) |
| `10255f9` | Six comparisons against the referee (`scripts/nuked_md.py`, `scripts/run_nuked_md.py`, `tests/test_milestone6.py`) |
| `0ae7a6a` | Milestone 6's record: the worklog, the certification record, and the open items in `undocumented-behavior.md`, `timing.md`, `polish.md` and `README.md` |

**End of milestone 6.** Stopping here, as the working agreement says.

## Decisions (the user, 2026-09-23), answering milestone 6's questions

A focused **timing session** takes these together, before milestone 7 and before
milestone 8's sweep: the sweep compares thousands of frames against MAME, and
every timing change moves its results, so the sweep runs once, on corrected
timing.

1. **Adopt the one extra 68000 clock on $A00000-$A0FFFF accesses.** Nuked-MD
   (die-derived) and MAME agree, which is as close to a judge as this has; it
   also deletes a named lockstep difference class. Re-record the MAME traces and
   re-run the milestone-1 replay.
2. **Adopt the arbiter's propagation on the bus grant** (the referee's ~7.4
   68000 cycles), keeping UM0080's machine-cycle rule underneath it: the
   specification says *when* the Z80 lets go, the referee says how long the
   arbiter takes to say so. Label it die-derived.
3. **The work-RAM refresh rule is investigated, with a limit.** It is fitted
   machinery that does not generalise (806-819 in a dense loop, 694-699 in
   `wtest`, nothing in 600-1400 for the capture ROM). Try to find the real
   mechanism from the referee; if a rule cannot be found that fits the referee
   *and* keeps milestone 2's capture agreement, keep 805 with the difference
   named, and say what was tried. Do not spend the session on it.
4. **Adopt the DMA start-up cost** from the referee, checked against Kabuto's
   +5.6 and re-run against VDPFIFOTesting and `run_dma_cost.py`.
5. **Chase the Z80 wait-state bias** (board 3.55, referee 2.74, MoD's hardware
   2.93) in the same pass: it is the same arbiter arithmetic as 2.
6. **Leave `SpriteMaskingTestRom.bin` stalling the referee**: it looks like the
   referee's own limitation, and the ROM is judged by Nemesis's photographs.

## Session 6 (2026-09-23): the timing session

### How the brief was read

Not a milestone: the user's six decisions on milestone 6's questions, taken in one
pass before milestone 7 and before milestone 8's sweep, because the sweep compares
thousands of frames against MAME and every timing change moves its results. Four
things to adopt (the extra clock on $A00000-$A0FFFF, the arbiter's propagation on
the bus grant, the DMA's start-up cost, the Z80 wait-state bias), one to investigate
with a limit (the work-RAM refresh) and one to leave alone
(`SpriteMaskingTestRom.bin`). Then re-run every acceptance run they touch and record
the before and after, explaining anything that moves.

The judging rule the user set for this session, which decided two of the four:
**where the referee and a hardware-measured source disagree in size, the hardware
source is the judge and the referee is the corroboration**; and do not fit a constant
to make one test pass at the cost of another.

### Environment

Same as the previous sessions: `.venv` CPython 3.14.4, `.venv-pypy` PyPy 7.3.23
(Python 3.11.15), m68000-python pinned at 51ee1a5 (unchanged: docs/polish.md),
z80-python 0.4.0, MAME 0.285, Nuked-MD at `594ca2a`, gcc 15.2.0, the 13th-generation
Core i9-13900K. The machine was shared throughout, which is why the frame-rate
measurement below is a paired run of the two trees rather than a table of absolutes.

### 1. One extra 68000 clock on every $A00000-$A0FFFF access

Milestone 6 found the referee holding all 68 of the FM probe's accesses to the Z80's
space 7 MCLK where the board held none, which is MAME's `before_delay(1)`
(`megadriv.cpp`, "required by pacman2 intro", "TODO: unverified"). Two independent
models against the board and nothing hardware on the question, so the tier rule lets
them judge.

`Board._z80_space_cost` charges it in the four bus entry points, **after** the access,
so the access itself still lands at the clock its bus cycle began at (the referee
stretches the cycle; it does not delay its start). It is also counted apart in
`Board.z80_space_stall`, because the milestone-1 lockstep compares the board's step
with MAME's own clock count and MAME charges the same clock: `scripts/lockstep_mame.py`
adds that counter to the core's clocks, and the line that used to add MAME's own
`before_delay` to the E-clock phase of an interrupt entry is gone with it.

**The milestone-1 replay, re-recorded and re-run.** `python scripts/mame_trace.py
genesis GAME.zip --seconds 30` for the six games (MAME 0.285, two runs each, six in
parallel), then `.venv-pypy/bin/python scripts/lockstep_mame.py
tests/mame_traces/genesis-NAME`. The traces (3.5 GB) were deleted afterwards.

| Game | 68000 instr. | Z80 instr. | unexplained | `before_delay(1)` steps | wall |
| --- | ---: | ---: | ---: | ---: | ---: |
| Sonic The Hedgehog (USA, Europe) | 23,286,815 | 12,728,465 | 0 | **0** (was 41,274) | 397 s |
| Streets of Rage 2 (USA) | 21,274,844 | 11,269,553 | 0 | **0** | 372 s |
| Gunstar Heroes (USA) | 20,854,523 | 15,425,681 | 0 | **0** | 397 s |
| Castlevania - Bloodlines (USA) | 11,343,385 | 8,460,818 | 0 | **0** | 355 s |
| Sonic The Hedgehog 3 (USA) | 21,741,798 | 11,837,738 | 0 | **0** | 403 s |
| Phantasy Star IV (USA) | 21,254,417 | 13,280,703 | 0 | **0** | 400 s |
| **Total** | **119,755,782** | **73,002,958** | **0** | | |

Every instruction count is milestone 1's to the instruction. To be sure that nothing
*else* moved, the same Sonic trace was also replayed against a worktree at the
session's starting commit `86fa7e1`: its 41,274 `before_delay(1)` steps move into
"agree" exactly (23,240,769 → 23,282,043) and every other class is identical to the
count -- 3,165 $A11100 non-grant bits, 1,477 VDP reads replayed, 463 YM2612 status
replayed, 48 Z80 RAM reads sorted to MAME's side, 26 Z80 instructions replayed in the
other order, 3,276 MAME DMA stalls, 1,495 interrupt entries. Summed over the six:
107,762 YM2612 status replayed, 47,067 $A11100 non-grant bits, 10,879 VDP reads, 516
Z80 RAM reads, 47 Z80 instructions, 1 I/O latch bit -- the last five identical to
milestone 1's totals.

(The YM2612 status count is the one number that differs from milestone 1's 90,707,
and it did not move this session: the comparison at `86fa7e1` gives Sonic's 463 too.
It moved when milestone 4 put a real FM on the board and was never re-measured
across all six games.)

### 2. The arbiter's propagation on the bus grant

The board modelled Zilog UM0080's rule -- the Z80 lets go at the end of the machine
cycle in progress -- and nothing of the board around it. `run_nuked_md.py busack
--window 200000` over **122 grants**: the referee takes 68-118 MCLK (9.71-16.86 68000
cycles, mean **12.34**) from the 68000's write to $A11100 to /BUSAK where the
specification's rule alone takes 10-37 MCLK (mean 4.15). `BUSACK_PROPAGATION_MCLK` is
the difference of the two means, 8.19 cycles, rounded to **57 MCLK** and labelled
die-derived and fitted; `busack_grant` now returns the machine-cycle end plus it.
(The constant was fitted twice: once at 50 MCLK against the release rule of the time,
and again at 57 after section 5 corrected that rule and moved the board's unaided
figure from 5.19 cycles to 4.15. 57 is the one in the tree.)

After: the board's grants are 67-94 MCLK, **mean 12.29 68000 cycles against the
referee's 12.34**. The board's spread is narrower (9.57-13.43 against 9.71-16.86)
because `System.z80_probe`'s machine-cycle estimate is coarse; the constant fixes the
mean, not the shape, and that is said rather than hidden.

MoD's `gen_test_z80_bus`: all eight checks still PASS, the Z80's writes to 68000 work
RAM still read back $4243 and its reads $FFFF, and its two delay counters now print
**0001** instead of 0000. MoD's own hardware values for those two counters are still
not in the pinned material, so this is a change, not a pass.

**What it did not fix, and the measurement that says why.** `run_nuked_md.py roms` is
unchanged in every figure, including the two ROMs that poll $A11100 --
`gen_test_z80_bus` (21,828 / 21,832) and `stetest` (230 / 233) -- which still end
where the referee reads $67 and the board $66. Tracing that poll on both machines:

```text
referee: $A11100 write at 1,382,688;  /BUSAK at 1,382,923  (235 MCLK, 33.6 cycles)
referee: polls at 1,382,884 -> $6700 (busy), 1,383,038 -> $6600 (granted)
board:   $A11100 write at ~1,382,807; grant at 1,382,864  (57 MCLK)
board:   poll at 1,383,003 -> $6600 (granted)
```

The Z80 is **held in reset** there, and the referee's arbiter takes 235 MCLK in that
state against the 68-118 it takes with the Z80 running (the board takes 57). The propagation was made to
apply with the Z80 held as well (it is the arbiter's, not the Z80's), but a second
constant fitted to a single observation is exactly what the user's rule forbids, so
the board keeps 57 and the remaining ~178 MCLK is a named, measured difference rather
than an unmodelled one. QUESTION below.

### 3. The work-RAM refresh: what was tried, and why 805 stayed

Time-boxed, as the decision said. The referee reports every bus cycle's /AS-low
length, so its work-RAM stretches are *observed*; eight traces were taken (three
dense probe loops of 300,000 MCLK and five test ROMs of 900,000) and candidate rules
replayed over the referee's own cycle times.

**Three new facts.**

1. **The stretch is not always 21 MCLK.** It is 7, 21 or 35 -- one, three or five
   68000 clocks. Nemesis's capture ROM: 301 of its 401 stretches are 21 and 100 are
   7. r57shell's `vdp_tests`: 820 of 1,020 are 21, 18 are 7 and 182 are 35. The board
   models only the 21.
2. **The rate varies by two and a half between programs, and rises as work-RAM
   traffic thins.** In 900,000 MCLK `wtest.bin` shows 1,032 stretches (one per 872
   MCLK) and the capture ROM 401 (one per 2,244). The apparent period is 845 MCLK
   where 44 % of bus cycles are work RAM, 871 at 29 %, 880 at 40 %, 896 at 20 %.
3. **No free-running grid fits.** For a fixed grid of period *G*, every stretch's
   time must fall in one narrow band of residues mod *G*. Over *G* = 200..2,000 the
   tightest band any *G* gives is 70-165 MCLK, 8-18 % of the period, and no *G* is
   shared by two programs. The 4,546 stretches of all eight runs together -- which
   share one power-on phase, since the referee always starts from the same reset --
   band no better at any period (best arc 196 of 203).

**Five candidate rules, each free to choose its own period and phase**, scored by how
many of the referee's stretches it misplaces (fewest misses @ the period that gave
them):

| rule | dense probe (354) | `wtest` (1,032) | capture ROM (401) | `vdp_tests` (1,020) |
| --- | ---: | ---: | ---: | ---: |
| **A** re-arm *P* after the stretched RAM access (the board's) | 556 @ 946 | **0** @ 760 | 1,055 @ 946 | 1,664 @ 834 |
| **B** fixed grid, any bus cycle absorbs the tick, only RAM pays | 233 @ 845 | 863 @ 871 | 300 @ 896 | 638 @ 880 |
| **C** fixed grid, the tick waits for a RAM access | 175 @ 845 | **0** @ 871 | 604 @ 896 | 183 @ 880 |
| **D** every *N*th work-RAM access (a row counter, not a timer) | 353 @ 44 | **0** @ 8 | 300 @ 57 | **0** @ 11 |
| **E** any bus cycle absorbs the tick and re-arms the timer | 361 @ 785 | 1,032 @ 834 | 401 @ 876 | 1,072 @ 778 |

Rules C and D are each exact on two programs and hopeless on the other two, and the
period or count each needs differs by a factor of five between programs (C: 845 to
896; D: 8 to 57 accesses). At the board's own 805 MCLK, C is no better than A overall
(663 against 709 on the dense probe, but 1,656 against **0** on `wtest`), so even the
shape change does not pay.

**Decision: 805 MCLK and rule A stay, the difference stays named.** No rule was found
that fits the referee, and changing the constant or the shape would move milestone
2's capture agreement for no gain. What the session did buy is a sharper question:
the interval depends on the access pattern, the stretch has three sizes, and the
mechanism is not a grid, not a re-armed timer and not an access counter. Written up
as open item 5 of docs/undocumented-behavior.md, with the table above.

Not tried, and the obvious next step: reading the referee's refresh counter out of
its own state instead of inferring it from stretches. That needs a change to
`validation/nuked_md_headless.c` to expose FC1004 internals, which is more than a
time-boxed investigation.

### 4. The DMA's start-up cost

Kabuto (hardware-measured research) gives a fast transfer as `words * 2.4 + 5.6`
68000 cycles, so a one-word transfer costs eight; milestone 5 reproduced his slope
within 2 % and had no mechanism for the +5.6, charging about one cycle; milestone 6
measured the referee at 11.0 cycles for one word and +8.0 fitted over 1 to 512.

The two judges agree the start-up is real and differ by 2.4 cycles in its size, so
**the hardware source judges**: `VDP.DMA_STARTUP_MCLK` is Kabuto's own 5.6 cycles,
39 MCLK (5.571, the rounding to whole master clocks), charged before the transfer's
first word is read. Taking his constant as the mechanism it is named as, rather than
a delta fitted to make some measurement land, is the whole reason for the choice --
and it matters, because the board's own two harnesses fit intercepts seven cycles
apart on the same board (`run_dma_cost.py` fits over 1..512 by the VDP's own `hold`,
`run_nuked_md.py dma` over 1..512 by the gap in a real 68000 bus stream).

`.venv-pypy/bin/python scripts/run_dma_cost.py`, H40, display off, fast transfer:

| words | 1 | 2 | 4 | 8 | 16 | 64 | 512 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| board, before | 0.6 | 4.0 | 7.4 | 16.0 | 36.0 | 145.7 | 1216.0 |
| board, after | **7.4** | **8.6** | **12.7** | **22.1** | **40.6** | 150.3 | 1217.4 |
| Kabuto | 8.0 | 10.4 | 15.2 | 24.8 | 44.0 | 159.2 | 1234.4 |

Fitted: 2.37 x words + **1.7** (was −3.5) fast, 4.78 x words − 9.7 (was −14.3) slow,
against Kabuto's 2.4 + 5.6 and 4.7 − 6. In the referee's own harness the board is now
2.46 x words + **9.0** with one word at **6.9**, against the referee's 2.47 + 8.0 and
11.0. So the board sits between Kabuto and the referee on the one-word figure and just
past the referee on the fit: the residual 3.6 cycles to the die are named, not fitted
away.

The Manual's DMA transfer capacity table still comes out: **16.1, 167.1, 18.1, 205.1**
bytes a raster line against its 16, 167, 18, 205. VDPFIFOTesting is **122 of 122** on
every machine but genesis3 (114 of 122, its VSRAM tests), its seven DMA Busy Flag
tests, DMA Transfer Bus Locking, FIFO Full Before DMA Transfer and DMA Transfer using
FIFO included.

### 5. The Z80 wait-state bias: one number, found in the referee's bus log

The board charged **3.55** wait states for a bank read with the 68000 in Kabuto's
`jmp (a0)` loop where the referee charges 2.74 and MoD's logic analyser measured 2.93,
and it produced 5s neither of them sees. The user's hint was to run it down against
the referee's own bus log, which shows where each 68000 cycle was when the request
arrived. That is exactly what it shows.

**A 68000 bus cycle is four clocks, but the 68000 holds the bus for only 18 of its 28
MCLK.** The bus is a requester's once /AS and /DTACK are negated (68000 UM, "Bus
Arbitration"). In the referee's trace of that loop, **1,706 of its 1,766 68000 bus
cycles hold /AS low for exactly 18 MCLK**, with a 10-MCLK gap to the next; the other
60 hold it 32, which is 18 plus the cartridge refresh's 14 and is Kabuto's "up to 6
cycles if the last access is delayed by the refresh logic". The board charged the
whole 28 MCLK, which made the 68000 look continuously on the bus in a tight loop, and
then also looked two cycles ahead to the next access, so Kabuto's `a` came out 5 and
6 where the die never goes past 4.

Replaying his formula over the referee's own 134 held Z80 reads with three candidate
`a`s:

| how ``a`` is taken | wait-state distribution | mean | the referee's own |
| --- | --- | ---: | --- |
| the /AS-low remainder (18 MCLK) | 2: 52, 3: 65, 4: 17 | **2.74** | 2: 51, 3: 67, 4: 16, mean **2.74** |
| the whole four-clock cycle | 2: 1, 3: 67, 4: 66 | 3.49 | |
| the whole cycle plus the 2-cycle look-ahead (the board's) | 3: 16, 4: 66, 5: 52 | 4.27 | |

So `Board.m68k_accesses` now holds `[start, span]` -- where /AS fell and how long it
stays down, lengthened by whatever stretches that cycle -- and `_m68k_release` counts
only a cycle actually under way. Kabuto's sentence is not contradicted: a cycle is
four clocks and the next does begin within two; the bus is simply free for the 10
MCLK in between, which his own footnote could not have distinguished.

On the board (`run_nuked_md.py arbiter`, 2,000 accesses; `run_bus_arbiter.py`, 4,000):

| experiment | before | after | referee | hardware |
| --- | --- | --- | --- | --- |
| 68000 in `stop #$2700`, Z80 reading | 2-3, 2.19 | 2-3, 2.19 | 2-3, 2.25 | MoD: "2-3" |
| 68000 in `jmp (a0)`, Z80 reading | 2-5, **3.55** | 2-4, **2.86** | 2-4, 2.74 | MoD: 2-4, 2.93 |
| the same, `run_bus_arbiter.py` | 3-4, **3.36** | 2-4, **2.91** | | MoD: 2.93 |
| the same, Z80 `pop`ping | 2-5, 3.19 | 2-4, 2.50 | 2-5, 3.27 | MoD: 3.5 |
| -- the `pop`'s **first** read | 2-5, 4.07 | 2-4, **2.97** | 2-4, 2.95 | |
| -- the `pop`'s **second** read | 2-3, 2.30 | 2-4, 2.04 | 2-5, 3.60 | MoD: 3.5 |

The board's `jmp (a0)` figure is now 2.91 against MoD's hardware 2.93 and 2.86
against the referee's 2.74, in his range 2-4, with the 5s gone.

**One thing got worse, and it is the right kind of worse.** The `pop` pair's mean
falls from 3.19 to 2.50 against the referee's 3.27. The inflated `a` had been
compensating for the board's inability to place the *second* of a `pop`'s two reads
at its own T-state: the first read now lands on the referee's own figure (2.97
against 2.95) and the second is plainly short (2.04 against 3.60). That is the
z80-python limitation docs/polish.md records, unmasked instead of cancelled out, and
it is the strongest argument yet for the Z80 equivalent of `step_clocks`.

Kabuto's whole-model check is unmoved: **481.0** of a raster line's 488.6 clocks left
for the 68000's own instructions, against his "roughly 480".

### 6. `SpriteMaskingTestRom.bin`: left alone, as decided

The referee's 68000 makes eleven bus cycles from its reset vector and stops. Nothing
was done, and Nemesis's MD1600 photographs go on judging that ROM: `0 of 18 result
cells differ`.

### Everything else these changes touch, re-run

Every figure is in docs/validation.md's certification record, "The timing session".
The short version: the HV/status captures are **8,762,415 of 8,847,360 words**,
unchanged to the word; VDPFIFOTesting is unchanged on all nine machines; the sprite
masking ROM is unchanged; the PSG is identical to Nuked-PSG on **121,279,196 input
clocks** over ten streams and the YM2612 identical to Nuked-OPN2 on **110,549,790
internal cycles** in each of two variants over eighteen streams, so **sound is still
cycle-exact**; `run_nuked_md.py probes` is 2,921 of 3,546 and `roms` is unchanged in
every one of its ten figures. The one referee comparison that improved is the FM
probe: **568 of 636 bus cycles** against milestone 6's 434, the 68 $A0xxxx stall
differences gone.

### Frames per second: the changes make the board slightly faster

Timing changes can cost speed, so this was measured -- and it does the opposite, for
a reason worth stating: a frame is a fixed number of master clocks, so charging the
68000 more stall clocks means it executes *fewer* instructions per emulated frame.

The machine was shared all session and identical runs of the same build vary by up to
10 %, so this is a **paired** measurement: the session's starting commit `86fa7e1` in
a git worktree and the current tree, 900 frames each, `--no-sound`, alternating, two
runs each, the better of the two, by **user CPU time**.

| Game | before (s) | after (s) | before fps | after fps | change |
| --- | ---: | ---: | ---: | ---: | ---: |
| Sonic The Hedgehog (USA, Europe) | 19.00 | 18.26 | 47.4 | 49.3 | +4.1 % |
| Streets of Rage 2 (USA) | 20.32 | 19.58 | 44.3 | 46.0 | +3.8 % |
| Gunstar Heroes (USA) | 24.33 | 21.58 | 37.0 | 41.7 | +12.7 % |
| Castlevania - Bloodlines (USA) | 18.54 | 17.58 | 48.5 | 51.2 | +5.5 % |
| Sonic The Hedgehog 3 (USA) | 22.16 | 21.39 | 40.6 | 42.1 | +3.6 % |
| Phantasy Star IV (USA) | 20.96 | 20.84 | 42.9 | 43.2 | +0.6 % |

(The absolute figures are well below milestone 5's 50-64 because of the load; only
the paired difference means anything here.)

### Overdrive 2's realised DAC rate, which these changes do move

Milestone 5's clearest single piece of evidence was that the arbiter's delays bring
Kabuto's own sound driver from 27.0 kHz to 26.3 kHz -- "26 kHz sample playback", the
number he designed for. 600 PAL frames, counting the driver's writes to the YM2612's
register $2A:

| | milestone 5 | now |
| --- | ---: | ---: |
| arbiter on | 26,313 Hz | **25,858 Hz** |
| arbiter off | 27,028 Hz | 26,318 Hz |
| Kabuto | "26 kHz" | "26 kHz" |

Both figures moved and the two halves say which change did what. With the arbiter off
the only changes in play are the $A0 clock and the DMA start-up, and they cost 2.6 %
(27,028 → 26,318). The arbiter itself now costs 1.8 % rather than 2.6 %, which is the
corrected wait states (2.86 rather than 3.55 per bank access). The result is 0.6 %
*below* 26 kHz where it was 1.2 % above; both round to his figure, and the demo is
still being driven at the rate its author says it should be.

### The picture, unmoved

The nine milestone-3 test ROMs against MAME's own frame after 3 emulated seconds
(`scripts/compare_mame_frames.py`, a detector): 100.00 % for `wtest`, `wtest_i1`,
`stetest` and r57shell's `vdp_tests`, 99.84 % `wbug`, 96.93 % `vctest`, 96.25 %
`wtest_i2`, 69.64 % `SHLTEST`, 41.97 % `STETEST2` -- **every figure identical** to
milestones 3 and 5.

### The suite

`.venv-pypy/bin/python -m pytest -q`: **49 passed, 1 deselected** in 163 s;
`.venv/bin/python -m pytest -q` (CPython 3.14.4): 49 passed in 395 s. (While the MAME
trace directories were still present six were deselected instead of one: the extra
five are `slow` replays that are only collected when their logs exist.)
Three tests are new (the Z80-space clock, the /AS-low release rule, the DMA
start-up) and one was rewritten for the grant's propagation. The six deselected are
the `slow` marker's MAME-log replays, collected this time because the trace
directories were present; they were run directly instead, above, and the logs
(3.5 GB) were deleted afterwards. `ruff check` and `ruff format --check` clean.

### Open questions for the user

QUESTION: **The bus grant with the Z80 held in reset.** The propagation constant (50
MCLK) is fitted to 122 grants with the Z80 *running*, and the board now matches the
referee's mean there to 0.01 of a cycle. With the Z80 held in reset the referee takes
**235 MCLK**, and that is one observation, on one ROM, so nothing was fitted to it --
which leaves `gen_test_z80_bus` and `stetest` ending their referee comparison at the
same $A11100 poll as before. Is it worth a purpose-built probe (request the bus
repeatedly with the Z80 held, over many grants) to see whether 235 is a constant, or
does the named difference do?

QUESTION: **Which DMA start-up figure.** The board took Kabuto's 5.6 cycles because
the session's rule makes the hardware source the judge; that leaves a one-word fast
transfer at 7.4 cycles against his 8.0 and the referee's 11.0, and the fit +1.7 in one
harness and +9.0 in the other against the referee's +8.0. Keep the hardware number, or
is the referee's 8.0 the better choice now that both agree the effect is real?

QUESTION: **The work-RAM refresh, next step.** 805 stayed and the question is sharper
(three stretch sizes, a rate that varies two and a half times between programs, no
grid that fits). The obvious next move is to stop inferring the refresh from stretches
and read the FC1004's refresh counter out of the referee's own state, which means
adding to `validation/nuked_md_headless.c`. Worth a session of its own, or leave it
named until milestone 8's sweep says whether it costs anything visible?

QUESTION: **The `pop` split is now the board's largest arbiter difference** (2.50
against the referee's 3.27, entirely because both of a `pop`'s reads land at the Z80
step's start). The fix is the Z80 equivalent of m68000-python's `step_clocks`, which
docs/polish.md has been carrying since milestone 6. Is it time to propose that PR to
z80-python, given that it is now the only thing between the board and the referee on
this experiment?

QUESTION: **The referee stretches a work-RAM access by 7 or 35 MCLK as well as 21**
(100 of 401 stretches in Nemesis's capture ROM are 7; 182 of 1,020 in `vdp_tests` are
35). The board models only the 21. Since the rule that places them is not known, the
sizes cannot be modelled either -- but should the *observation* be part of what
milestone 8's triage looks for?

### Commits

| | |
| --- | --- |
| `8085361` | Two clocks the referee found: the Z80's space, and the grant |
| `9ca2ec6` | The DMA's start-up cost, taken from the hardware source |
| `7558425` | How long the 68000 really holds the bus |
| `3ae1498` | The grant's propagation applies with the Z80 held too |
| `647c283` | The timing session's record (worklog, certification record, open items, polish, timing, README) |
| `23837e5` | The rest of the record: speed, Overdrive 2, the picture, and the questions |
| `ccdb63b` | Re-fit the grant's propagation on the corrected release rule |

The last of those is worth reading as a lesson rather than a change: the
propagation constant had been fitted while the board still charged the whole
four-clock bus cycle for Kabuto's `a`, and correcting that moved the quantity it
was fitted to. It was caught by re-running every acceptance script **at the final
commit** rather than trusting the number each change was measured with, which is
worth doing whenever a session changes two things that touch the same clock.
Re-run there: the 288 HV/status capture dumps (8,762,415 of 8,847,360 words, to
the word), the sprite masking ROM (0 of 18), VDPFIFOTesting on all nine machines
(122/122, 114/122 on genesis3), `gen_test_z80_bus`, `run_bus_arbiter.py`,
`run_nuked_md.py` `arbiter`, `busack` and `roms`, Overdrive 2's DAC rate and the
suite.

**End of the timing session.** Milestone 7 next, then milestone 8's sweep on this
timing.

## Decisions (the user, 2026-09-23), answering the timing session's questions

1. **The grant with the Z80 held in reset stays a named difference** (referee 235
   MCLK against the board's 57, one observation on one ROM). The post-sweep
   debugging session can probe it; it is measured now, which was the point.
2. **Kabuto's 5.6-cycle DMA start-up stays**, as the session chose: the
   hardware-measured source judges, the die-derived referee corroborates.
3. **The work-RAM refresh keeps 805 MCLK with the difference named.** Five rules
   were scored and none fits more than two programs; milestone 8's sweep says
   whether it costs anything visible before anyone spends a session on the
   FC1004's refresh counter. Unneeded machinery is latency.
4. **The z80-python accessor is proposed as a PR there** by the reviewing session
   (the same shape as m68000-python's `step_clocks`), so the `pop` pair's second
   read can land at its own T-state. The board adopts it in the polish pass, not
   before: `docs/polish.md` carries the row.
5. **Milestone 8's triage watches for the 7- and 35-MCLK work-RAM stretches**,
   which the board does not model, as a possible cause of a frame difference.

## Session 7 (2026-09-23): milestone 7, cartridges and peripherals

### How the brief was read

Scope is milestone 7 only: battery SRAM, the EEPROM games, the SSF2 mapper, the 3- and
6-button pads with Terakawa's timing, TMSS with the local No-Intro boot ROM, and the
region and version register. Each is judged by its research document **and by a game
that uses it doing the right thing** -- saving and reloading, a mapper's banks
appearing, a region check passing or failing as the hardware would -- with MAME as a
detector where nothing better exists. Plus the two things the brief hands over
explicitly: run r57shell's VDP tests once the 6-button pad works and judge them against
the MD2 report in forum t=1585, and give the SSF2 mapper (which milestone 5 already
had as `rom_titan`) its own judge, Super Street Fighter II.

MD Soft Checker v0.30 is not available (a dangling symlink in the local romset) and was
not used.

### Environment

Same as the previous sessions: `.venv` CPython 3.14.4, `.venv-pypy` PyPy 7.3.23
(Python 3.11.15), m68000-python pinned at 51ee1a5 (unchanged: docs/polish.md),
z80-python 0.4.0, MAME 0.285, the 13th-generation Core i9-13900K, shared throughout.

### What was built

| File | Lines | What it is |
| --- | ---: | --- |
| `src/megadrive_python/eeprom.py` | 241 | the serial (I2C) EEPROM, modes 1, 2 and 3, from Eke-Eke's `gen_eeprom.pdf` |
| `src/megadrive_python/io.py` | +212 | `Pad6`, and TH edges timed: every access that can move TH now tells the port when |
| `src/megadrive_python/cartridge.py` | +392 | the EEPROM boards, the J-Cart ports, the battery's save and load, `BootROM`, the SSF2 mapper's documentation |
| `src/megadrive_python/board.py` | +72 | the TMSS boot ROM and $A14101, and the I/O chip's clock |
| `src/megadrive_python/render.py` | +13 | register $00 bit 0: no display generation |
| `src/megadrive_python/{system,__main__}.py` | +37 | `pads=`, `tmss_rom=`, `--pad`, `--pad2`, `--save`, `--tmss-rom` |
| `scripts/run_pads.py` | 299 | the pads against CMD's table, r57shell's `joy_speed`, and which games handshake |
| `scripts/run_battery.py` | 168 | battery SRAM and EEPROM against the games and against MAME's own nvram |
| `scripts/run_mapper.py` | 130 | the SSF2 mapper against `ssf2tnc.txt` and the game |
| `scripts/run_tmss.py` | 197 | the boot ROM, the hand-off, the header check and the VDP lock |
| `scripts/run_region.py` | 211 | the version register against the Manual, and three games that check it |
| `scripts/run_r57shell_tests.py` | 154 | the eight VDP-test screens, now that they can be walked |
| `tests/test_milestone7.py` | 379 | 24 tests that need nothing fetched |

### The pads

#### Decisions and their sources

- **Two sources describe the 6-button pad and they agree on every value it returns.**
  Terakawa's page (`reference/web/terakawa-6button.html`, hardware-corrected: he timed
  real pads) gives the sequence as rows of Sel and the six pins; CMD's `gen-hw.txt` 3.2
  ("checked on the real thing") gives the same rows as `?1CBRLDU` / `?0SA00DU` /
  `?1CBRLDU` / `?0SA0000` / `?1CBMXYZ` / `?0SA1111`. Reading the two together settles
  two things Terakawa's table alone does not: **D4 and D5 still carry B and C in the
  extra-buttons phase** (his "HI HI" is those two released), and **the counter stops**
  after the fourth rising edge -- "From this point on, the standard 3-button pad values
  will be returned if any further TH transitions are done" -- rather than starting
  round again, which is what MAME does (it wraps modulo 4, `bus/sms_ctrl/md6bt.cpp`).
- **The timeout is a field, not a constant, and nothing is claimed for it.** The brief
  said not to fit one number and call it hardware, and the sources make that easy to
  obey: they do not measure the same quantity. CMD times out "about 8192 (probably less
  than that) 68000 CPU cycles" after TH was last *modified*; Terakawa gives 1.1 ms for
  the second up-edge, 1.6 ms for the data to stay readable and 1.8 ms before the
  sequence may restart, all from the *first* up-edge; MAME uses 1.5 ms from the last
  rising edge. `Pad6.timeout_mclk` takes CMD's shape and figure (57,344 MCLK) because
  his is the only one that says what the clock runs from, and the class docstring
  carries all four. Nothing observable turns on the choice: a poll takes a few
  microseconds and a game polls once a frame, so every one of the four resets the pad
  between frames and none of them resets it inside one.
- **TH's edges now have a time.** A 6-button pad counts edges, not levels, so
  `IOChip.read` and `IOChip.write` take a master clock and `Port.set_th` tells the
  device whenever a write to the data *or* the control register could have moved TH (a
  control write can make TH an output or let it float back up, which is an edge too).
  The board passes `m68k_access_clock()` for a 68000 access and `step_start` for a Z80
  one. A 3-button pad ignores all of it and behaves exactly as before.
- **The pad's own MODE switch** is `Pad6.mode_switch`: "the MODE button can be held
  down when powering-up the console, and the 6-button gamepad will respond like a
  3-button one" (CMD 3.2). Held False the pad never advances its counter.

#### What the pads were judged by (2026-09-23)

**CMD's table, every button combination** (`scripts/run_pads.py`, 21 s). A small 68000
program makes TH an output and walks it high, low, high, ... eight times, reading
$A10003 after each change into work RAM, so every access goes through the board's own
bus. The eight bytes are compared with CMD's rows written out as bytes, for **all
4,096 combinations of the twelve buttons on the 6-button pad and all 256 on the
3-button one**: **34,816 of 34,816 bytes match**.

**r57shell's `joy_speed_v1.02.bin`** (hardware-corrected; his own tool, forum t=2308;
SHA-256 `3c49a278f737…`). The ROM's own reads of $A10003 were watched on the port and
grouped into bursts, 400 frames each:

| pad | reads in 400 frames | the commonest burst |
| --- | ---: | --- |
| 3-button | 972 in 198 bursts | `$7F $33 $7F $33 $7F $33 $7F $33` (56 x) |
| 6-button | 1,552 in 197 bursts | `$7F $33 $7F $30 $7F $3F $7F $33` (56 x) |

which is CMD's table read off a real test ROM: `?1CBRLDU` = $7F, `?0SA00DU` = $33,
then `?0SA0000` = $30 and `?1CBMXYZ` = $3F where the 6-button pad's counter has
reached two and three. The ROM's own screen shows the same eight bytes.

**What `joy_speed` does *not* do is name the pad**: its heading reads "Port 1: 3
Buttons Pad No Reset" for both pads. MAME 0.285 with `-ctrl1 md6button` prints exactly
the same heading, and its own code says why -- its pad reads its MODE switch in
`device_reset` with the comment "TODO: doesn't work - reset happens before host inputs
are read for the first time", so MAME's 6-button pad is stuck in 3-button mode. So
MAME cannot detect the difference here and the board agrees with it; the evidence for
the pad is the byte sequence above, not that heading.

**The games** (`scripts/run_pads.py --games`). A 6-button game drives TH high three or
more times in a burst; a 3-button game never does:

| game | frames | the counter reached 2 | 3 | 4 |
| --- | ---: | ---: | ---: | ---: |
| Super Street Fighter II (USA) | 2,600 | 2,368 | 2,368 | 2,368 |
| Comix Zone (USA) | 600 | 399 | 399 | 399 |
| Sonic The Hedgehog (USA, Europe) | 600 | **0** | **0** | **0** |
| Streets of Rage 2 (USA) | 600 | **0** | **0** | **0** |

So the two games sold with the 6-button pad in mind ask for the extra buttons about
once a frame and the two 3-button games never do, which is the pad working in a game
rather than in a harness.

### The battery: SRAM, FRAM and the serial EEPROMs

#### Decisions and their sources

- **The EEPROM is written from Eke-Eke's `gen_eeprom.pdf`** (in
  `reference/docs/md_tech.zip`), the research document the brief names:
  `src/megadrive_python/eeprom.py` is the two-wire protocol -- START and STOP, eight
  clocks and an acknowledge, one, two or three address words by mode, a write wrapping
  inside the page and a read wrapping at the end of the chip. His game table gives
  which game is on which board and MAME's software list gives which board each image
  is, so `cartridge.EEPROM_BOARDS` is keyed by MAME's slot name with Eke's numbers in
  it. CMD's `gen-eeprom.txt` names the actual chips found on several boards (a Xicor
  X24C01 in Monster World 3 and Mega Man - The Wily Wars, an X24C02 in NBA Jam), which
  agrees with Eke's modes.
- **Eke's NBA Jam entry is wrong and the game says so.** His table gives the Acclaim
  type 1 board `SDA_IN` bit 0, `SDA_OUT` bit 1 and `SCL` bit 1 of $200001 -- the data
  out and the clock on one bit, which cannot be a board. MAME's NBA Jam device reads
  /SDA at bit 1 and writes /SDA at bit 0 and /SCL at bit 1. **Measured, with the game
  as the judge**: with bit 1 the board writes NBA Jam's "Snake 1993" header once and,
  restarted with that EEPROM loaded, reads it back and writes *nothing*; with bit 0 the
  game cannot read its own save and rewrites all 256 bytes on every boot. The board
  takes bit 1 (docs/undocumented-behavior.md, "Cartridges" item 1).
- **MAME's `sram` data area is an address range, not a chip size.** `md_slot.cpp` sets
  `m_nvram_end = m_nvram_start + length - 1` and allocates that many bytes as 16-bit
  words, one per address the chip answers at. The chip is eight bits wide on the odd
  byte (CMD 4.3, of Phantasy Star 4: "16k of battery backed RAM mapped to
  $200001-$203FFF. (odd addresses used)"), so it holds half as many bytes as the range
  is wide, and the board now halves the list's figure. Before this the board mapped
  twice the address range MAME does; no game in the lockstep ever read the extra half,
  which is why milestone 1 never saw it.
- **A battery is saved and loaded as the chip's own bytes**, packed, which is half
  MAME's file; `Cartridge.save_bytes` and `load_bytes`, and `--save FILE` on the
  command line, which reads the file at power-on if it exists and writes it at the end.

#### The runs (2026-09-23)

`.venv-pypy/bin/python scripts/run_battery.py`. Each game runs 40 emulated seconds
from a blank battery, the battery is saved, a fresh machine is built with it loaded
and the game runs again; MAME 0.285 runs the same image for the same time with
`-nvram_directory`. 90-125 s a game.

| game | board's battery | after a reload | MAME's file | equal |
| --- | --- | --- | --- | --- |
| Sonic The Hedgehog 3 (USA), `rom_fram` | 512 bytes, **272 written** | unchanged | 1,024 bytes, both halves of every word equal | **512 of 512** |
| NBA Jam (USA, Europe) (Rev 1), `rom_nbajam_alt` | 256 bytes, **48 written** ("Snake 1993") | unchanged, **0 EEPROM bytes written on the warm start** | 256 bytes | **256 of 256** |
| Phantasy Star IV (USA), `rom_sram` | 8,192 bytes, none written in 40 s | unchanged | 16,384 bytes, halves equal | **8,192 of 8,192** |
| Wonder Boy in Monster World (USA, Europe), `rom_eeprom_mode1` | 128 bytes, none written | unchanged, 0 written | 128 bytes | **128 of 128** |
| Evander Holyfield's 'Real Deal' Boxing (World), `rom_eeprom_mode1` | 128 bytes, none written | unchanged, 0 written | 128 bytes | **128 of 128** |

The two games that write during their own start-up are the evidence: **Sonic The
Hedgehog 3 lays out its FRAM and the board's 512 bytes are MAME's 512 bytes exactly**,
and **NBA Jam writes its EEPROM header and the board's 256 bytes are MAME's exactly**,
then reads it back on the next boot and leaves it alone. The other three write nothing
without being played, so for them the comparison says only that both machines leave a
blank chip blank; that is stated rather than counted as a pass.

### The Sega (SSF2) mapper

The mapper itself is milestone 5's: `rom_titan` was added then so that Overdrive 2
would run. What milestone 7 adds is its documentation, its own slot name, and the
judge the brief names -- **Super Street Fighter II**.

Bart Trzynadlowski's `ssf2tnc.txt` (second edition, 2000, in `md_tech.zip`) gives the
mechanism ("views the addressable 4 megabytes of ROM as 8 512KB regions. The first
area, 0x000000-0x07FFFF is fixed and cannot be remapped because that is where the
vector table resides"), the seven registers and their windows, the 6-bit page number
("bits 7 and 6 are always 0"), and -- which is what makes it a judge -- the game's own
code, quoted from the US dump:

```text
0x003BEC:       move.b  #$08, ($A130FD)
0x003BF4:       move.b  #$09, ($A130FF)
0x003BFC:       lea     ($300000), a0
```

"The bankswitching hardware must be properly emulated before the game starts working.
If it is not, the checksum will fail and the game will hang with a red screen."

`.venv-pypy/bin/python scripts/run_mapper.py --titan`, 2,400 frames each:

| | |
| --- | --- |
| Super Street Fighter II (USA), 5,242,880 bytes, `rom_ssf2` | **267 writes to $A130xx**, the first two of them `$A130FD = $08` and `$A130FF = $09` -- the checksum pair, at the value `ssf2tnc.txt` quotes -- then `$A130F1 = $02` and the seven windows set back to 1..7 |
| the picture | the intro artwork at frame 1,200 and the **"SUPER STREET FIGHTER II - The New Challengers" title screen** at frame 2,400; no red screen |
| Overdrive 2 (World) (Revision 2017), 8,386,056 bytes, `rom_titan` | the same registers, ending with pages 10 and 11 in the two top windows (`banks [0, 1, 2, 3, 4, 5, 10, 11]`), which is the demo reaching past the 4 MB the 68000 can see |

So the game pages in its top megabyte at the address the document says, checksums it,
and goes on to its title screen: the mapper is judged by the cartridge it was made for.

### TMSS

#### Decisions and their sources

- **The boot ROM is something a caller fits.** `System(..., tmss_rom=...)` (and
  `--tmss-rom` on the command line) puts a `cartridge.BootROM` on the bus in the
  cartridge's place and makes bit 0 of $A14101 choose between them; without one the
  cartridge is on the bus from power-on and $A14101 does nothing. That is exactly what
  every run of milestones 1 to 6 did -- VDPFIFOTesting is scored on `genesis-tmss`
  among nine machines -- and making the boot ROM automatic would move all of them for
  no gain. The VDP lock is *not* optional: every TMSS machine has it.
- **The two are swapped, not tested for.** `Board.install_tmss_rom` puts the boot ROM
  where `self.cartridge` was and `_tmss_select` swaps them back, so a board with no
  boot ROM pays nothing on the hottest path in the emulator.
- **The behaviour is read out of the boot ROM's own disassembly**
  (`reference/src/TMSS-Disassembly`, which rebuilds bit for bit): `bset #0,(a3)` with
  a3 = $A14101 to look at the cartridge, `cmp.l (ROM_Header).w,d7` against 'SEGA' and
  then ' SEG' with 'A' after it, the licence message for 60 iterations of a delay loop,
  `move.l #0,(a2)` with a2 = $A14000 to lock the VDP again, and `bset #0,(a3)` for
  good before jumping to the cartridge's start vector. CMD's 4.4 says the same in
  three sentences.
- **The ROM is 16 KB.** The No-Intro dump is the chip's 2,048 bytes; MAME's is those
  same bytes and 14 KB of zeros, which the disassembly's README calls "hardware
  padding", and the board pads the same way. Above 16 KB it reads $FFFF, as MAME does.

#### The runs (2026-09-23)

`.venv-pypy/bin/python scripts/run_tmss.py --png ...`, 14 s, with the local No-Intro
boot ROM (2,048 bytes, SHA-1 `3f50b76b0529db7f79c396b5e808cc0786ffc311`):

1. **Sonic The Hedgehog (USA, Europe) on `genesis-tmss`**: the licence message
   "PRODUCED BY OR UNDER LICENSE FROM SEGA ENTERPRISES LTD." is on screen by frame 2,
   the cartridge takes the bus at **frame 189 (3.15 s)**, and by frame 420 the picture
   is Sonic's own SEGA screen. That is the brief's acceptance: **TMSS boots the local
   No-Intro boot ROM to a licensed cartridge.**
2. **MAME 0.285 `genesis_tmss` on the same cartridge at 7 emulated seconds**
   (a detector): **71,680 of 71,680 pixels equal, 100.00 %**.
3. **A cartridge whose header is not 'SEGA'**: the licence message never appears, the
   cartridge never takes the bus, and $A14000 reads $00000000 -- the boot ROM switched
   itself back in, locked the VDP and stopped in `FailLoop`, which is what the
   disassembly's `.fail` path does.
4. **The VDP lock, with no boot ROM fitted**: a cartridge that writes to $C00004
   without having written "SEGA" raises the board's `Lockup` at that access.

### The region and version register

`.venv-pypy/bin/python scripts/run_region.py`, 126 s. The Manual's own page 72 (PDF
page 84) gives $A10001 as MODE, VMOD, DISK, RSV, VER3-0, and all nine machines decode
to what their fields say:

| machine | $A10001 | | machine | $A10001 |
| --- | --- | --- | --- | --- |
| genesis | $A0 overseas NTSC ver 0 | | megadrive | $E0 overseas PAL ver 0 |
| genesis-tmss | $A1 | | megadrive-tmss | $E1 |
| genesis2 | $A1 | | megadrive2 | $E1 |
| genesis3 | $A1 | | megadrive-japan | $20 domestic NTSC ver 0 |
| | | | megadrive2-japan | $21 |

A 68000 reading $A10001, $A10000 and $A10000 as a word gets the byte, the same byte and
the byte in both halves on every machine, which is CMD's 3.0 rule.

**The games.** Three that check it, each run on all nine machines for 6 emulated
seconds; every one splits the nine into exactly the four overseas NTSC machines and the
five that are PAL or domestic:

| game | boots on | refuses on |
| --- | --- | --- |
| Mortal Kombat 3 (USA) | genesis, genesis-tmss, genesis2, genesis3 | megadrive, megadrive-tmss, megadrive2, megadrive-japan, megadrive2-japan |
| Comix Zone (USA) | the same four | the same five |
| Streets of Rage 3 (USA) | the same four | the same five |

The refusal is the screen "DEVELOPED FOR USE ONLY WITH NTSC GENESIS SYSTEMS". Against
MAME's three drivers as a detector: `megadriv` and `megadrij` **100.00 %** of pixels for
all three games, and `genesis` 100.00 % for Mortal Kombat 3, 97.28 % for Streets of Rage
3 and 91.23 % for Comix Zone -- the three machines where the game is actually running
and its animation is a frame or two out of phase, the same kind of difference milestone
3 recorded for the lockstep games.

### r57shell's VDP tests, which the pad was supposed to unblock

Milestones 3 and 5 recorded that "the ROM polls the pad seven times a frame -- the
6-button handshake -- so neither the board nor MAME leaves its first screen". The
observation was right and the conclusion was wrong: the polling *is* the handshake, but
the ROM is navigated with the **D-pad**, and r57shell says so in the thread ("even if
you don't see screen, you can change tests by arrows keys on pad", forum t=1585).
RIGHT walks its eight screens with a 3-button pad just as well as with a 6-button one.
So the blocker was a misreading, and it is recorded here as one.

**What can judge them, and it is very little.** r57shell never ran the ROM on hardware
("I didn't because I can't do that") and asked for results. The one report in the
thread is MrTamk1s's, on an MD2 VA1.8 with a Sega 315-5708-01 (he photographed the
chip: `reference/images/t1585-18.jpg`) plus a 32X, captured to a VCR and watched on two
CRTs, and it is one sentence about one test: "VDP Redraw Test #4 acted differently on my
smaller CRT and on the Big CRT. On the smaller, the screen completely rolled over, while
on the larger, only the first few rows were flickering/rolling." His video is on YouTube
and is not pinned; of his three pinned photographs two are of the console and the VCR
and one is a CRT too dark and too small to read a test off. So **the MD2 report judges
test 4 and nothing else**, and what it says is about a display losing and regaining
sync. For the rest the only statement is the ROM's own printed expectation, which its
author wrote from what he believed and wanted confirmed.

`.venv-pypy/bin/python scripts/run_r57shell_tests.py`, 10 s, 12 frames measured per
screen:

| screen | the ROM says | the board |
| --- | --- | --- |
| 1 VDP Redraw Time Test 1 | MUST SEE right column of spheres | the display alternates 256x224 and 320x224 frame by frame; the right 40 columns carry **1,446 lit pixels on the wide frames and 0 on the narrow ones**, so the column is drawn, flickering at half the frame rate |
| 2 VDP Redraw Time Test 2 | MUST NOT SEE right column of spheres | 256x224 throughout, **0 lit pixels** in the right 40 columns, nothing changing |
| 3 VDP Redraw Time Test 3 | MUST NOT SEE glitches | 256x448 (interlace mode 2), a regular field of spheres, **identical on all 12 frames** |
| 4 VDP Redraw Time Test 4 | MUST NOT SEE blinking of whole screen, only first rows | 320x224, **nothing changes at all**: neither the whole screen nor the first rows. The MD2 report's "only the first few rows were flickering/rolling" is a CRT losing sync, which a frame buffer cannot show. **Not reproduced**, and recorded as an open item |
| 5 VDP Blink Test (Reg 0 bit 0) | MUST SEE blinking of whole screen | **every one of the 11 frame pairs differs**: the whole screen alternates between the text and black |
| 6 VDP Colors Test 1 | MUST SEE only same colors within solid block | a static screen with the shadow and highlight blocks |
| 7-8 VDP Colors Test 2 and 3 | (interactive) | static screens of the shadow/highlight priority matrix |

**Test 5 needed a change to the VDP, and it is the one thing this milestone found that
was a real gap.** Register $00 bit 0 "actually turns off all display generation, as
opposed to the screen blanking feature which simply shows the backdrop color" (CMD
`genvdp.txt` 17); the board did not model it, so the blink test showed a still picture.
It now draws such a line black whatever CRAM holds (`render.py`, per row, because there
is no CRAM entry that means "nothing"). r57shell's own note from 2013 -- "blink test
works only in MESS" -- says how few emulators did it then.

Tests 6 to 8 are the shadow/highlight priority matrix by eye, which milestone 3 already
judged against Kabuto, CMD and Genesis Plus GX; nothing here adds to that.

### Everything these changes touch, re-run (2026-09-23)

Five things in `src/` moved: the pads and the I/O chip's clock, the cartridge's
EEPROM and battery handling and the halved battery size, TMSS's bank register, the
renderer's register $00 bit 0, and the two new constructor arguments. The battery
size is the one that could move an earlier run -- Sonic The Hedgehog 3 and Phantasy
Star IV are the battery games in the milestone-1 lockstep -- so their MAME traces
were re-recorded and replayed; the rest are checked because they are cheap and
because the working agreement says to.

| Run | Result |
| --- | --- |
| milestone 1's replay, **Sonic The Hedgehog 3 (USA)**, trace re-recorded from MAME 0.285, 30 emulated seconds | **21,741,798 68000 and 11,837,738 Z80 instructions identical, 0 unexplained**, 251 s -- milestone 1's counts to the instruction |
| milestone 1's replay, **Phantasy Star IV (USA)**, likewise | **21,254,417 and 13,280,703, 0 unexplained**, 251 s -- likewise |
| Nemesis's HV/status captures, 288 dumps | **8,762,415 of 8,847,360 words**, unchanged to the word, 96 s |
| VDPFIFOTesting, all nine machines | **122/122** on eight and **114/122** on genesis3 (its eight VSRAM tests), unchanged |
| Nemesis's sprite masking ROM against his MD1600 | **0 of 18 result cells differ**, unchanged |
| MoD's `gen_test_z80_bus` | 8 checks PASS, $4243 and $FFFF, delay counters 0001/0001, unchanged |
| `scripts/run_bus_arbiter.py`, 4,000 accesses | 2.27 / **2.91** / 2.44 wait states and 481.0 clocks a raster line, unchanged |
| `scripts/run_dma_cost.py` | one word 7.4, fit 2.37 x words + 1.7, the Manual's table 16.1/167.1/18.1/205.1, unchanged |
| the nine milestone-3 test ROMs against MAME's frames, 3 emulated seconds each | 100.00 (`wtest`), 100.00 (`wtest_i1`), 100.00 (`stetest`), 100.00 (`vdp_tests`), 99.84 (`wbug`), 96.93 (`vctest`), 96.25 (`wtest_i2`), 69.64 (`SHLTEST`), 41.97 % (`STETEST2`) -- **every figure identical** |
| the PSG against Nuked-PSG, ten streams | **0 of 121,279,139 input clocks differ** |
| the YM2612 against Nuked-OPN2, eighteen streams, both variants | **0 of 110,502,778 internal cycles x 3 values x 2 variants not identical** -- 663,016,668 values, none different, so **sound is still cycle-exact** |
| `scripts/run_nuked_md.py roms`, ten test ROMs against the die-derived referee | **every one of its ten figures unchanged** (28,921/28,921 twice, 25,307/25,307 three times, 21,828/21,832, 230/233, 22,125/28,122, 23,258/28,621, 23,259/28,622) |
| `pytest -q`, PyPy 7.3.23 | **73 passed, 1 deselected** in 154 s, against the timing session's 49 passed: the 24 new ones are milestone 7's |

**Two numbers needed running down, and both are the records' and not the board's.**
The PSG total is 121,279,139 where the timing session recorded 121,279,196, and the
YM2612's 110,502,778 where it recorded 110,549,790. Both were checked against a git
worktree at this session's starting commit `fa22dcc`, run with the same interpreter
and the same referee binaries: the PSG's ten streams come out **stream for stream
identical** to this tree's, and `run_fm_streams.py --games`'s twelve streams are
**identical line for line before and after**, register writes and cycle counts alike
("BEFORE AND AFTER IDENTICAL", 12 streams, 0 comparisons not identical). So nothing
this session changed moved either stream; the earlier figures were recorded from a
slightly different state and are superseded by the ones here.

### Open questions for the user

QUESTION: **The 6-button pad's timeout is a field with CMD's figure in it.** Four
published numbers measure three different quantities and their author says the
parameter varies from pad to pad. The board takes CMD's "about 8192 68000 cycles from
the last change of TH"; nothing observable turns on it, because a poll is microseconds
and a frame is 16 ms. Keep that, or carry Terakawa's three figures as the default and
CMD's as the alternative? (The brief said not to fit one number and call it hardware,
which is why neither is called one.)

QUESTION: **After the fourth rising edge the board's pad stops** ("From this point on,
the standard 3-button pad values will be returned if any further TH transitions are
done", CMD 3.2), where MAME's wraps its counter modulo 4 and starts the sequence again.
Nothing pinned settles it and no game seen here does more than four edges in a burst.
Accept CMD's, or is this worth a probe?

QUESTION: **Eke's NBA Jam bits are corrected from MAME's code, with the game as the
judge.** With his /SDA in on bit 0 the game rewrites its whole EEPROM on every boot;
with MAME's bit 1 it reads its save back and writes nothing. That is as good a judge as
exists here, but it is one game on one board. Accept, or should the other Acclaim
boards (NBA Jam TE, NFL Quarterback Club, College Slam) be driven far enough into a
game to save, which needs a scripted input sequence?

QUESTION: **Three EEPROM boards are modelled from sources that disagree and no game
was run on them**: the Ninja Burai Densetsu prototype (Eke's mode 1 against MAME's
mode-2 chip) and Brian Lara Cricket and Cricket 96 (Eke's Codemasters addresses against
MAME's $380000 pair, and his X24C01 against MAME's 24C65). All three are in the local
romset but none writes its battery without being played. Leave them as they are with
the disagreement named, or drop the two Brian Lara boards until something can judge
them?

QUESTION: **r57shell's VDP Redraw Time Test 4 is not reproduced and probably cannot
be.** The only hardware statement is that a CRT rolled -- completely on a small one,
only the first rows on a big one -- which is a display losing sync, not a picture.
The board's frame does not change at all there. Accept that as out of reach, or is a
"the VDP's sync went away for N lines" flag on the frame worth having for a frontend?

QUESTION: **Register $00 bit 0 draws a black line.** CMD says it "turns off all display
generation, as opposed to the screen blanking feature which simply shows the backdrop
color", which says what it is *not* and not what a console puts out. Black is the
obvious reading of "no display generation" and it makes r57shell's blink test behave as
he says; nothing pinned confirms the colour. Accept?

QUESTION: **The J-Cart's two extra ports answer as empty sockets.** The four J-Cart
boards in the local romset are modelled far enough to run (the port reads all pins
high); putting pads on them is a frontend's job, and milestone 9 is the frontend. Leave
it, or wire them to `System(pads=...)` now?

QUESTION: **MD Soft Checker v0.30 is still not available** (the local romset's symlink
is dangling and the only link is a mega.nz page), so Sega's own diagnostic has never
been run against the board. Worth fetching by hand before milestone 8's sweep?

### What is *not* claimed

- **The 6-button pad's timeout.** The four published figures do not agree and their
  author says the parameter varies from pad to pad; the board carries one of them as a
  field and nothing here measures it.
- **Test 4 of r57shell's VDP tests**, which is the only one the MD2 report covers, and
  which is a CRT losing sync rather than a picture.
- **The three EEPROM boards no game was run on** (the Ninja Burai prototype and the two
  Brian Lara boards): they are modelled from sources that disagree and the
  disagreements are named, not settled.
- **What a console puts out with register $00 bit 0 set**: the board draws black
  because "no display generation" is not the backdrop, which is all CMD says.
- **The J-Cart's extra ports** answer as empty sockets; no pad can be put on them yet.
- **Nothing about the picture of Overdrive 2's four wrong frames**, which are still the
  user's post-sweep session, and nothing about the sound path, which this milestone
  does not touch.

### Frames a second, paired against the session's starting commit

Two things were added to the cartridge's read path -- the battery and the EEPROM's
lines -- and a 68000 fetch is the hottest thing in the emulator, so it was measured
rather than assumed. Measured first, it cost about 2 %; the two checks were then
folded into one attribute (`Cartridge.special`, true only when there is a battery or
something decoded), which puts a plain cartridge back where it was. 900 frames after
120 of warm-up, `--no-sound`, by **user CPU time**, the session's starting commit
`fa22dcc` in a git worktree and the current tree run alternately, twice each, on a
machine shared throughout:

| Game | before (s) | after (s) |
| --- | --- | --- |
| Sonic The Hedgehog (USA, Europe), before the fold | 20.87, 21.39 | 21.25, 21.41 |
| Phantasy Star IV (USA), before the fold | 19.53, 19.03 | 19.93, 19.50 |
| Sonic The Hedgehog (USA, Europe), after the fold | 19.19, 17.63 | 17.48, 18.06 |

and an EEPROM cartridge costs nothing measurable against a plain one: NBA Jam 46.2
frames a second against Sonic's 46.4 in the same conditions.

### What is left for milestone 8

The compatibility sweep is next and it inherits three things from here: cartridges now
identify themselves (`Cartridge.unsupported` names a board the sweep cannot run), the
pads can be either kind, and `--save` means a sweep can be run twice over a game that
saves. The boards still not modelled are the unlicensed mappers, the SVP, the Game
Genie and the Sega Pico family; the sweep will say how many images they are.

### Commits

| | |
| --- | --- |
| `851b9e7` | The cartridges: serial EEPROMs, the J-Cart's ports, and a battery that saves (`eeprom.py`, `cartridge.py`) |
| `e94a4f3` | The 3- and 6-button pads, with TH's edges in time (`io.py`) |
| `0229a70` | TMSS: the boot ROM on the bus, and the clock the I/O chip now needs (`board.py`) |
| `efb7b45` | Register $00 bit 0: no display generation at all (`render.py`) |
| `e16e3d7` | The system and the command line: a pad, a battery file and a boot ROM |
| `9a9643a` | Six runs that judge milestone 7 (`scripts/run_{pads,battery,mapper,tmss,region,r57shell_tests}.py`) |
| `69d836c` | Milestone 7's tests: 24 that need nothing fetched |
| `a0d299e` | `run_mapper.py`: do not try to write a PNG of a frame not yet drawn |
| `d6a8659` | Say exactly what a read of an EEPROM board's register gives |
| `ef04b86` | `start-here.md`: two things it got wrong about the pads and TMSS |
| `e755a3b` | Milestone 7's record: the worklog, the certification record, the open items, the findings and the README, and the fold in `cartridge.py` that keeps a plain cartridge as fast as it was |
| `58cbc47` | the final line counts |

**End of milestone 7.** Stopping here, as the working agreement says; milestone 8's
sweep is next.


## Decisions (the user, 2026-09-23), answering milestone 7's questions

1. **The 6-button pad keeps CMD's timeout** (8192 68000 cycles since TH last
   changed) as the field's default. Terakawa's three figures measure different
   quantities and he says they vary per pad, so no single number is claimed as
   hardware; the field stays a field.
2. **The pad stops after the fourth rising edge** (CMD's rule), not MAME's
   modulo-4 wrap: a research document plus `joy_speed`'s byte sequence beat a
   detector.
3. **The NBA Jam correction is accepted.** One game is thin, but a game behaving
   correctly beats a document that cannot be right (Eke's `gen_eeprom.pdf` puts
   /SDA-out and /SCL on one bit). Do not script the other Acclaim boards for now.
4. **The three unrun EEPROM boards stay, named**: dropping them would stop those
   games booting at all, and their disagreements are already recorded.
5. **Redraw Test 4 is out of reach** and stays that way: a frame buffer cannot
   show a CRT losing sync. No "sync went away" flag; unneeded machinery.
6. **Register $00 bit 0 draws black**, labelled: nothing pinned says the colour.
7. **Wire the J-Cart's two extra ports to `System(pads=...)` now**, while the
   cartridge code is fresh, rather than leaving them to the frontend.
8. **MD Soft Checker v0.30 is available after all** (the user asked the right
   question): the local mirror has it as
   `/data/emu/source/myrient.erista.me/files/No-Intro/Sega - Mega Drive - Genesis/MD Soft Checker (USA) (Proto) (Unl).zip`
   (zip SHA-256 `688debcf0635e48ef501c84fd8348cdd40d18b2a5efa1e4391be1fd5cbf2996a`,
   inner `.md` 262,144 bytes). No-Intro dropped the `(v0.30)` from the name, which
   is why the romset's symlink dangles. It runs on the board: at 600 frames it
   shows its pad screen with both ports reading a 3-button pad. It is Sega's own
   factory diagnostic, so it is a hardware-corrected oracle: use it.

## Session 8 (2026-09-23): milestone 8, the compatibility sweep and its triage

### How the brief was read

Scope is milestone 8 only: boot every cartridge in `/data/emu/roms/megadrive` for N
emulated seconds against MAME's frame at the same time, then triage every difference
into a named cause, as sms-python did. Nothing is changed to match MAME without a
higher-tier source; MAME is a detector here and never a judge, and a difference that
is MAME's own -- its `insta_68k_to_*_dma` hack, its `rand()` on $A11100, its
interrupt acceptance, its lack of an arbiter, its 6-button pad that its own source
says does not work -- is counted apart from the board's.

Three things the user asked for by name: run Sega's own MD Soft Checker, now that
the local mirror is known to have it, and record what it reports as its own
acceptance (a judge, not a detector); watch for the 7- and 35-MCLK work-RAM refresh
stretches the board does not model as a possible cause of a frame difference (the
timing session's decision 5); and say whether a real game's difference points at
Kabuto's DMA start-up cost (milestone 5's open item, left for here). Overdrive 2's
four wrong frames are the user's own next session and were not touched.

### Environment

Same as the previous sessions: `.venv` CPython 3.14.4, `.venv-pypy` PyPy 7.3.23
(Python 3.11.15), m68000-python pinned at 51ee1a5 (unchanged: docs/polish.md),
z80-python 0.4.0, MAME 0.285, the 13th-generation Core i9-13900K, shared throughout
(load average 25-30 of 32 threads for most of the session, which the wall times
below reflect).

### What was built

| File | Lines | What it is |
| --- | ---: | --- |
| `scripts/mame_media.py` | 207 | which board MAME puts an image on, and how to hand it one so that both machines run the same cartridge |
| `scripts/compat_sweep.py` | 290 | the sweep: every cartridge on the board and on MAME, the frame compared |
| `scripts/triage_diffs.py` | 210 | the triage: the lockstep's verdict for each difference, and which of the board's own timing models moves it |
| `scripts/run_soft_checker.py` | 123 | Sega's MD Soft Checker, its verdicts read off the screen |
| `tests/test_milestone8.py` | 90 | 7 tests that need nothing fetched |

### Sega's MD Soft Checker, which is a judge

`MD Soft Checker (USA) (Proto) (Unl)` from the local No-Intro mirror (zip SHA-256
`688debcf0635e48ef501c84fd8348cdd40d18b2a5efa1e4391be1fd5cbf2996a`, the image
262,144 bytes, SHA-1 `bd716dbbbe3fd50169fa5bad7e203ce9356fdfc0`, header
"MD Soft Checker     Version 0.30"), pinned in `scripts/pinned_files.json` by a
`file://` URL that names exactly where it came from, fetched into
`tests/test_roms/sega/` and not committed. It is not in MAME's software list.

It is Sega's own factory diagnostic, so under the tier rule it is a **judge**, not a
detector: it was written by the people who made the machine, and what it reports is
about hardware. `scripts/run_soft_checker.py` runs it and reads its verdicts off the
rendered picture -- each result line's last two glyphs as a block of pixels, because
the ROM is proprietary and publishes no result table -- and the glyphs themselves
were read once by eye from the PNG.

`.venv-pypy/bin/python scripts/run_soft_checker.py` (96 s for all nine machines):
**17 result lines on every machine, all seventeen with the same verdict, and that
verdict reads OK.** The report, read off the picture at frame 240:

```text
OVERSEA mode  NTSC  VER.0
68K Register R/W OK          DMA ROM  ==> VRAM OK
68K All Instruction Set OK   DMA ROM  ==> Color RAM OK
WORK RAM R/W OK              DMA ROM  ==> VSRAM OK
Z80 BUS Request OK           DMA WRAM ==> VRAM OK
Z80 RAM R/W OK               DMA WRAM ==> Color RAM OK
VRAM R/W OK                  DMA WRAM ==> VSRAM OK
Color RAM R/W OK             DMA VRAM FILL OK
VSRAM R/W OK                 DMA VRAM COPY OK
I/O Port Register R/W OK
```

and its header is the version register read back, which is milestone 7's work
judged by Sega's own program: `OVERSEA mode NTSC VER.0` on `genesis`,
`OVERSEA mode PAL VER.1` on `megadrive2`, `JAPAN mode NTSC VER.0` on
`megadrive-japan`. The Genesis 3, whose VSRAM is 0x80 bytes and which fails
Nemesis's VSRAM tests, passes the checker's VSRAM test -- the checker writes and
reads back, which a larger VSRAM does not break.

At 600 frames it has moved on to its pad screen, which reads `JOY1 PAD3 1P` and
`JOY2 PAD3 1P` with `U L R D ST A B C` -- a 3-button pad found on both ports, which
is what the board presents.

**What this does not cover**: the checker tests that each memory answers reads and
writes and that each kind of DMA moves bytes; it says nothing about timing, about
the picture, or about sound. It is a floor, and the board is over it.

### The sweep: what was chosen, and why

Four choices had to be made before running anything, and each is a number.

**Sound off.** `YM2612.enabled = False` keeps the chip's bus, register file, timers
and BUSY and skips only the generator, so nothing a game can observe changes
(milestone 4). It is the difference between 2.9 seconds of wall time per emulated
second and 0.3: with the FM running, the whole sweep would be ten times the cost for
a picture that does not depend on it.

**Ten emulated seconds**, as sms-python's sweep used. Measured on this machine, a
cartridge costs about 100 s of CPU at that length -- 600 frames of a board that runs
38 to 51 frames a second without sound, plus MAME (0.8 s), the snapshot and the
comparisons -- so the whole romset is about four hours on 16 processes. Five seconds
would halve it; twenty would double it and lose comparisons rather than gain them
(below). Ten is also the length milestone 3's own MAME frame table used.

**Sixteen processes**, the cap the working agreement sets on this shared machine,
under `nice`. The machine was carrying other work throughout.

**MAME's `genesis` for every image**, not a driver chosen per region. Both machines
then run the same console, which is what makes the comparison a comparison: a
Japanese or European cartridge that refuses to run on an overseas NTSC machine
refuses on both and shows the same screen (milestone 7 measured exactly that against
MAME's three drivers).

### Two traps of this board's own, both measured

**MAME's file loader gives almost every cartridge battery RAM it does not have.**
sms-python's trap 8 -- MAME guesses a board from the image -- has a worse form here.
`md_slot.cpp`'s `get_cart_type` matches a table of code signatures by exact file
length and, when nothing matches, returns `SEGA_SRAM_FALLBACK`: 64 KB of battery RAM
at $200000, "in case this is a broken header", where the board mirrors its ROM (Eke's
mirroring rule). Counted over the local romset: of the 1,799 present images MAME's
`megadriv` software list names, **1,523 would run on a different board in MAME if
handed to it as a file** -- 1,347 of them exactly this way, and the rest as
`rom_sram`, `rom_eeprom`, `rom_codemast` and so on. So `scripts/mame_media.py` hands
MAME a **software-list name** for every listed image, from a scratch rompath holding
that one image under the name the list expects, which is the same board
`Cartridge.from_image` reads from the same list. The 874 unlisted images have to go
as files, and `mame_guess` -- a port of `get_cart_type`'s table -- says which board
MAME then puts each on, so that a differing frame can be attributed to that rather
than to the board. (Checked against the list itself: for every listed image the
board's own slot equals the list's, 1,799 of 1,799.)

**The board's picture is a few frames behind MAME's at the same emulated time.**
MAME's exit snapshot is the frame it completes after `-seconds_to_run`; the board's
is the first frame it completes at or after the same master clock. Those are not the
same field of an animation. Fitted over the 35 cartridges of a 207-cartridge sample
whose picture is identical at *some* offset and not at all of them, at three lengths:

| length | the offset at which most of the 35 are identical | how many |
| --- | --- | --- |
| 5 emulated seconds | **+4** | 29 of 35 (0: 22) |
| 10 | **+3** | 24 of 35 (0: 10) |
| 20 | +4, +5 | 19 of 35 (0: 11) |

The offset does **not** grow with the length of the run, which is what says it is a
one-off shift and not a rate difference: the two machines run the frame at the same
rate, and the board's game simply starts its animation two to five frames later.
Why it would: the board charges the 68000 stalls MAME does not model at all -- both
refreshes, the arbiter's bank-access delays, the DMA's start-up -- so a game's
CPU-bound initialisation (a checksum over a 2 MB ROM is a second of 68000 time) ends
a little later on it. That is measured below rather than argued.

`compat_sweep.FRAME_OFFSET` is therefore **3**, one figure for the whole sweep, and
the frames within `--window` of it are scored as well, so that a cartridge whose
picture is identical a frame or two away is reported as `phase` rather than as a
difference in what the machine drew. A search per cartridge was not used: it picks a
wrong frame on a mostly uniform picture, which is the trap sms-python's sweep found.

### What a row of the sweep means

`tests/compat/megadrive.csv` (git-ignored, as sms-python's is) has a row per entry:
the cartridge, the board `Cartridge.from_image` puts it on, the board MAME runs it on
when that differs, the status, the differing pixels, and every offset in the window
at which the two pictures are identical.

| status | what it means |
| --- | --- |
| `same` | the frame compared is identical to MAME's, pixel for pixel |
| `phase` | it is not, but a frame within 12 either side of it is: the same picture, a different field of an animation |
| `differs` | no frame in that window is identical |
| `size` | the two pictures are not even the same size (H32 against H40, or interlace) |
| `unsupported` | `Cartridge.unsupported`: a board the emulator does not model (the unlicensed mappers, the SVP, the Game Genie) |
| `mame-board` | the frame differs *and* MAME ran the image on another board (an unlisted image) |
| `lockup` | the board raised `Lockup` -- CMD's rule that some accesses stop the machine -- where MAME went on |
| `missing` | a broken symlink in the local romset |
| `error: …` | MAME would not run it, or the board raised something else |

### The triage, and why it is two passes

sms-python's triage was one pass: run the MAME lockstep on every differing
cartridge and read the verdict off it. That still works here and is pass 1
(`--lockstep`): MAME's two traces recorded for the sweep's own emulated time, the
board stepped against them instruction for instruction, and the verdict taken from
its report -- `cpu` for a divergence or an unexplained read, else the first non-empty
class of *differing device read* (`vdp-reads`, `fm-status`, `z80-order`,
`mame-rand`), and `identical` when nothing differed at all, which says the difference
is in the picture and not in what either program did.

But the lockstep has a blind spot on this board that it did not have on the Master
System, and it is the one the user's decision 5 points at. `lockstep_mame.py` turns
the refresh and the arbiter **off**, because MAME models neither and with them on the
two machines interleave the CPUs differently and the replay is no longer
instruction-exact. So the lockstep cannot see a difference the board's own bus timing
causes -- which is exactly where the work-RAM refresh's unmodelled 7- and 35-MCLK
stretches would show.

Pass 2 (`--sensitivity`) is for that, and it needs no MAME trace: run the sweep's own
comparison again with one model turned off at a time -- the work-RAM refresh, the
cartridge refresh, the arbiter, and the cartridge put on MAME's fallback SRAM board
-- and see which of them moves the frame. A cartridge whose picture is the same
whether the work-RAM refresh runs or not is one where that model cannot be the cause;
one whose picture moves is a candidate, and it is named as one. None of it needs a
change in `src/`: both refreshes are due at a master clock the board keeps in a
field, so a clock they never reach turns them off.

### What is *not* claimed by this milestone

- **Nothing about sound.** The sweep runs with the generator off; the FM and PSG are
  judged cycle for cycle by milestone 4's die-derived referees and nothing here
  touches them.
- **Nothing about timing** beyond what the sensitivity pass measures: MAME models no
  refresh, no arbiter and no DMA start-up, so it cannot judge any of them, and the
  lockstep turns them off to compare at all.
- **Nothing about a cartridge whose board the emulator does not model**: those are
  counted and named, not run.
- **Nothing about Overdrive 2's four wrong frames**, which are the user's own next
  session.
- **Nothing hardware.** MAME is a detector throughout. The only judge in this
  session is Sega's MD Soft Checker, and it judges that the memories and the DMA
  work, not how they are timed.


### Why the board's picture is behind MAME's, measured

The alignment above is a fitted constant, so what it is fitting matters. The claim
is that the board charges the 68000 stalls MAME models nothing of -- both refreshes
and the arbiter -- so a game's CPU-bound initialisation ends later on it and its
whole animation starts a few frames later. That is testable with the board's own
knobs: run the same comparison with all three turned off.
`/tmp/.../lag.py` (`compat_sweep.run_one` with a `disable` that clears both refresh
timers and `arbiter_enabled`), 10 emulated seconds, `--window 12`, the offsets given
relative to the frame the sweep compares (so `FRAME_OFFSET` = 3 is already in them):

| cartridge | as the board ships | with both refreshes and the arbiter off |
| --- | --- | --- |
| Sonic The Hedgehog (USA, Europe) | identical at **+9** | identical at **+1** |
| Phelios (USA) | identical at **0** | identical at **-1** |
| Zero Wing (Japan) | identical at 0, 1, 2, 3 | the same four |
| JuJu Densetsu (Japan) (En) | identical at 0 to 7 | the same eight |

**Sonic's picture moves eight frames closer to MAME's when the stalls the board
charges and MAME does not are taken away**, which is the claim, measured. Phelios
moves one frame. The two whose animation is locked to the vertical blank from the
first frame do not move at all, which is the other half of it: the lag is in how
long a game's *initialisation* takes, not in how fast the frames come.

So `FRAME_OFFSET` is not a fudge of unknown origin: it is the accumulated cost of
milestone 5's and the timing session's own work, which MAME cannot see. It is also
why it cannot be one exact number -- how much CPU-bound work a game does before its
first drawn frame differs from game to game.

### The sweep: the run, and what it found (2026-09-23)

`.venv-pypy/bin/python scripts/compat_sweep.py --seconds 10 --jobs 16 --window 12
--frames --offsets`, 14,423 s (4 h 0 m) on 16 processes under `nice`.

| status | count | what it means |
| --- | ---: | --- |
| `same` | 1,609 | identical to MAME's frame, pixel for pixel |
| `phase` | 461 | identical at another frame within 12 either side |
| `missing` | 841 | a broken symlink in the local romset |
| `differs` | 261 | no frame in the window is identical |
| `mame-board` | 229 | the frame differs *and* MAME ran the image on another board |
| `unsupported` | 65 | a board the emulator does not model, across 40 distinct slots |
| `size` | 32 | the two pictures are not the same size |
| `error:` | 11 | MAME would not run it, or the board raised |
| `lockup` | 5 | the board raised `Lockup` where MAME went on |

**2,070 of the 2,673 present cartridges -- 77.4% -- are identical to MAME's picture
or identical a frame or two away.** That figure is a detector's, not a judge's, and
it is deliberately not called a pass rate: the sections below say what the
remainder is, and a good part of it is MAME's.

### The triage, pass 1: which of the board's own models moves the picture

`scripts/triage_diffs.py --sensitivity --jobs 16 --seconds 10 --window 12 --sample 60`,
4,057 s. Each of 60 differing cartridges was run again with one model turned off at
a time.

| what moves the picture | cartridges of 60 |
| --- | ---: |
| MAME's fallback SRAM board | 31 |
| the work-RAM refresh | 26 |
| the cartridge refresh | 26 |
| the arbiter | 15 |
| the DMA's start-up cost | 8 |
| **nothing: unmoved by every model** | **14** |

The largest single cause is **MAME's**, not the board's: 31 of 60 move when the
cartridge is put on the `SEGA_SRAM_FALLBACK` board MAME's file loader would have
chosen. Fourteen move for none of the five, which says the board's timing models are
not implicated in those at all. A model "moving" a picture implicates it; it does
not convict it, and nothing here was changed on the strength of it.

### The triage, pass 2: the lockstep's verdict

`scripts/triage_diffs.py --lockstep --jobs 16 --seconds 10 --status differs,size,lockup`,
9,902 s (2 h 45 m) over 298 cartridges.

| verdict | count | what it means |
| --- | ---: | --- |
| `vdp-reads` | 181 | the two programs read different values from the VDP |
| `cpu` | 112 | the two programs diverged, or made an unexplained read |
| `identical` | 4 | the programs ran identically and no read differed |
| `fm-status` | 1 | the FM's status register read differently |

`vdp-reads` dominating is expected and is not a finding about the board: the
lockstep runs with the refreshes and the arbiter **off** (MAME models none of them),
so the two machines reach the VDP's counters and status at different times by
construction.

### The four cartridges that ran identically, which are the cleanest result here

`Breach (USA) (Proto)`, `Joshua & the Battle of Jericho (USA) (Unl)`,
`Zany Golf (USA) (Unl)` and `Exodus - Journey to the Promised Land (USA)` each ran
**5.9 to 6.8 million 68000 instructions with not one read differing from MAME's**,
and their pictures are still not the same: the board draws 256x224 where MAME draws
320x224.

Identical execution and identical device reads leave nothing for the difference to
be except the picture itself, so this is the sharpest thing the sweep produced: it
is about the width the renderer settles on, not about timing, the bus or the CPUs.
`Renderer.frame` takes `width = max(self.widths[:height], ...)`, so the two machines
disagree about H32 against H40 for a frame whose program neither disputes. Ten more
cartridges show the same 256-against-320 shape without an identical lockstep behind
them. Not chased in this milestone; it is a QUESTION below.

### The `size` rows are three different things, and should never be one number

Of the 32:

- **17 are the partial-frame capture** (320x1, 320x4, 320x13, 320x48, 320x55,
  320x56, 320x101, 320x223, 256x4): `System.run_frame` returned part way into the
  *next* frame. Diagnosed in full under "The partial-frame capture" above; it is a
  limitation of the harness and of `Renderer`, not a rendering difference.
- **15 are a width or mode difference** (256x224 against 320x224 and four the other
  way round), the four identical ones among them.
- **Overdrive 2 is neither**: 320x240 against 320x224, because the board runs it on
  a PAL machine, as the demo demands, and MAME's snapshot is 224 lines.

### Two things MAME could not do, both attributable to MAME

**MAME 0.285 cannot run the Demons of Asteborg family.** All nine `error: MAME` rows
are one family -- `Astebros (World) (PixelHeart)` and eight `Demons of Asteborg`
releases -- and every one exits with glibc's `corrupted size vs. prev_size`, a heap
error inside MAME rather than a refusal to load. Deterministic: every member fails
and nothing else does. MAME's own software list marks the entry `supported="no"`.
The detector is unavailable for those nine; they say nothing about the board.

**`Phantasy Star Fukkokuban (Japan)` is a bad dump.** Its initial PC is `00CB187F`,
which is odd, and the header at $100 is not a console string. It is not an
interleaved dump: neither a whole-file de-interleave nor an SMD 16 KB-block
de-interleave yields a valid header or an even PC. MAME runs it for ten seconds and
snapshots 256x224 with every pixel 0 (the PNG was decoded here: one distinct byte
value). The board raises `GroupZero (3, 13310079, False, True)` out of
`System.__init__`, at the initial PC's prefetch. Both machines agree that nothing
runs; a real 68000 takes an address error *during* reset processing, which is a
double bus fault, so it halts with a black screen -- MAME's behaviour. The board's
verdict is right and its presentation is not. A `src/` change, so it is in
[polish.md](polish.md), not made here.

### An image larger than the cartridge window is silently truncated

Found while triaging the `error:` rows and larger than the rows that led to it.
`Cartridge.from_image`'s fallback returns `cls(image)` with no size check, so an
image larger than `ROM_WINDOW` ($000000-$3FFFFF) loads with the rest unreachable.
**36 romset images exceed the window; 29 of them are unlisted and get `rom` or
`rom_sram` with `unsupported = None`.** The mapper machinery is not at fault: the
*listed* Super Street Fighter II gets `rom_ssf2` and runs, while its unlisted
Virtual Console re-release of the same 5 MB game gets plain `rom`.

It distorts this sweep's own numbers: all nine `Earthion` images score `differs`,
counted against the board where the cause is ours.

These carts are **not on the SSF2 mapper**. They are on Krikzz's Extended SSF, a
different device, and its designer documents it: `extended_ssf-v2.txt`, krikzz.com,
22.09.2014, now pinned in `scripts/pinned_files.json` (specification tier).

| | the Sega/SSF2 mapper the board implements | Krikzz Extended SSF v2 |
| --- | --- | --- |
| bank field | 6 bits (`ssf2tnc.txt`) | **5 bits**, 32 x 512 KB = 16 MB |
| bank registers | odd $A130F3-$A130FF, windows 1-7 | even $A130F2-$A130FE, windows 1-7 |
| $A130F0/F1 | SRAM enable and write protect | **mapper control**: protect, 32x, ROM write-enable, LED, and **the first bank** |
| first bank | fixed | **switchable** |
| extras | none | hardware multiply and divide at $A130D0-$A130DA, SD and USB IO |
| activation | MAME's software list | the header at $100 reads `"SEGA SSF"` |

The documented header rule covers 14 romset images -- 3 Demons of Asteborg, all 9
Earthion, 2 Kunio no Nekketsu -- **and Overdrive 2**, which declares `"SEGA SSF"`
and currently runs on `rom_titan`. It does not cover the other 6 Demons of Asteborg,
which use a game-specific `"SEGA DOA"`, nor `Astebros`, which is 14.75 MB under a
plain `"SEGA MEGA DRIVE"` header, so a size check is still needed behind it.

One conflict is left open rather than guessed: MAME's `megadrive_hb_ssf_device::time_f0_w`
implements $A130F0 as the Sega mapper's SRAM control with the comment "same as Sega
mapper (Demons of Asteborg wants it this way)", which contradicts krikzz's own
document. The document is specification tier and MAME is emulator-derived, so the
document judges -- but MAME's comment claims a real game disagrees, and that is
evidence about its own cartridge. It is in [polish.md](polish.md) to be resolved by
observation before any of this is implemented.

### Two fixes taken from the triage, and the numbers they correct (2026-09-24)

The user took the recommendation to make the oversize guard first, "because it is
cheap and makes the 77.4% honest". It was, and it did -- and validating it turned up
a second bug that was not on any list.

**1. An image larger than the window is no longer silently truncated.**
`Cartridge.__init__` sets `unsupported` when the image exceeds `ROM_WINDOW` and no
mapper slot was chosen. Checked against the romset: **29 images are flagged, and the
seven that must not change did not** -- Overdrive 2 (`rom_titan`) and the three
listed Super Street Fighter IIs (`rom_ssf2`) keep their mappers, and the three Pier
Solars keep their own, more specific `rom_stm95`.

The guard does not guess a mapper from a size. That is precisely what MAME's
`SEGA_SRAM_FALLBACK` does and what this milestone counted against it; the Krikzz
Extended SSF work stays queued in [polish.md](polish.md).

**It costs more `same` rows than it gains, which is the point**: eight cartridges
lose a `same` they had not earned, because they matched MAME on their first 4 MB and
nothing else. Nine `Earthion` images stop being counted as the board's failures.

**2. `Sonic & Knuckles (World)` was being loaded as its lock-on adapter.** Chasing
the last `error:` rows left two `GroupZero`s: the known-bad Phantasy Star dump, and a
flagship retail cartridge, which is not a thing to leave as "a bad dump". The archive
holds two image members:

```text
    262144  Sonic & Knuckles (World) (Lock-on).bin
   2097152  Sonic & Knuckles (World).md
```

`load_image` took the zip's first image member and both suffixes are image suffixes,
so the board was handed the **256 KB lock-on adapter ROM** instead of the 2 MB game.
Its initial PC is odd, so `m68k.reset()`'s prefetch raised out of `System.__init__`
-- the same signature as the genuinely bad dump, from an entirely different cause.

`_pick_image` now takes the member named after the archive, falling back to the
largest. The blast radius was measured before the change: **exactly one archive in
the romset's 3,514 entries holds more than one image member.** Sonic & Knuckles now
loads the real image (`SEGA GENESIS` header, even PC) and scores `same`, 0 of 71,680
pixels differing, at offset 0.

### The sweep's numbers, corrected

| status | as first run | after the two fixes |
| --- | ---: | ---: |
| `same` | 1,609 | **1,602** |
| `phase` | 461 | 461 |
| `differs` | 261 | **252** |
| `mame-board` | 229 | **226** |
| `unsupported` | 65 | **94** |
| `size` | 32 | 32 |
| `lockup` | 5 | 5 |
| `error:` | 11 | **1** |
| `missing` | 841 | 841 |

**2,063 of the 2,673 present cartridges: 77.2%**, against 77.4% before. The figure
goes *down* because eight cartridges stopped passing on a truncated image, which is
the fix working. Counted against the cartridges whose board the emulator actually
models, it goes up: **2,063 of 2,579, 80.0%**, from 79.4%.

The single remaining `error:` row is `Phantasy Star Fukkokuban (Japan)`, the bad
dump, whose presentation is still queued in [polish.md](polish.md).

Six tests were added (`tests/test_milestone8.py`): the guard fires, a mapper board
may exceed the window, an unmodelled board keeps its own reason, and the three
`load_image` cases. `pytest`: **86 passed, 1 deselected** on both interpreters.

### The renderer's two buffers, and the full re-sweep that checked them (2026-09-24)

The partial-frame capture diagnosed in milestone 8 is fixed. `Renderer.start_frame`
now hands the finished frame over by **swapping** five row arrays -- pixels, widths,
blanked, palettes and dots -- with a second set, so the next frame is drawn into what
the *previous* finished picture used and cannot overwrite the one a caller is about
to read. It costs five reference exchanges a frame and about 150 KB; nothing is
copied. `frame()` and `frame_rgb()` both take their source from one `_finished()`
helper, so they can never disagree about which picture they are showing.

Before any frame has finished -- only at power-on -- `_finished` still returns what
has been drawn, so a caller always gets a picture.

**The whole romset was re-swept rather than the `size` rows the polish list asked
for.** The change can move any cartridge whose `run_frame` overshoots a frame
boundary, and which those are is exactly what was not known; 3 h 37 m of machine time
is cheaper than a number nobody can defend.

**17 rows moved, and all 17 are the partial-frame captures. Nothing else in the
3,514 changed.**

| what the 17 became | count |
| --- | ---: |
| `same` -- the picture was right all along | 6 |
| `phase` | 1 |
| `differs` -- a full frame, and a real difference the truncated one hid | 10 |

Seven were being scored wrong purely because the wrong frame was handed out. Ten had
a genuine difference that a 320x1 picture could not show. One of the 17 was not a
`size` row at all: `Pac-Man (World) (Aftermarket) (Unl)` was `mame-board` with a
`320x3` picture -- the same bug wearing a different label -- and it now matches
exactly.

**No partial-frame row is left.** All 16 surviving `size` rows are genuine width or
mode differences (256x224 against 320x224 and four the other way), plus Overdrive 2's
PAL 240-line picture against MAME's 224.

| status | before | after |
| --- | ---: | ---: |
| `same` | 1,602 | **1,608** |
| `phase` | 461 | **462** |
| `differs` | 252 | 262 |
| `size` | 32 | **16** |
| `mame-board` | 226 | 225 |

**2,070 of the 2,673 present cartridges: 77.4%**, and 2,070 of the 2,579 whose board
the emulator models: **80.3%**.

### Krikzz's Extended SSF, implemented (2026-09-24)

With the $A130F0 conflict settled by observation the day before, the mapper itself
was small. The board already had a Sega-mapper-shaped implementation; what these
cartridges lacked was a **board**, because almost none of them is in MAME's software
list and an unlisted image fell through to plain ROM.

**Detection is the cartridge's own statement, not a guess.** krikzz: "Mapper will be
activated in case if ROM header contains "SEGA SSF" string". Twenty-two of the
romset's images say so at $100 -- seventeen `SEGA SSF` and five `SEGA DOA`, Demons of
Asteborg's own variant of the same announcement, which item 20 showed behaves
identically. That is the difference between this and MAME's `SEGA_SRAM_FALLBACK`,
which invents a board from a file's length.

**The backup-RAM header picks which of the two boards it is.** Twenty declare `RA`
over $200000-$20FFFF, which is exactly the range MAME's `ssf_sram` device answers
with a 32 KB chip on the odd byte -- and exactly the 32 KB krikzz reserves ("First
32Kbyte of this bank will be copied to SD card"). Overdrive 2 and the Joyzz test
program declare none and are the plain `ssf` board.

**Two of the twenty-two are under the 4 MB window**, so the oversize guard had never
touched them and nothing else would have found them: `Castle Cat 3 - The Vegas
Connection` (3.88 MB) and `Joyzz Controller Test Program` (64 KB). A size rule would
have missed both.

The bank field is five bits on these boards (`BANK_MASK`) and stays six on the Sega
mapper, and $A130F0/$A130F1 stays SRAM control, both on the evidence of item 20.
Overdrive 2 is listed, so `identify` still puts it on `rom_titan` and nothing about
it moved.

#### The board is now more right than the detector

| cartridge | the board draws | MAME draws |
| --- | --- | --- |
| Demons of Asteborg | its language-select screen, six flags, "Press A to confirm" | nothing: MAME 0.285 exits with a glibc heap error on this family |
| Earthion | its title screen: the logo, "PRESS BUTTON", the credits line | **noise** -- MAME puts it on `rom_sram`, a flat 7 MB ROM with no mapper |
| Kunio no Nekketsu | its disclaimer screen, legible | the same, and the two agree pixel for pixel |

MAME's file loader has no rule for the `SEGA SSF` header; only its software list
names the board, and these images are not in it. So for Earthion and Castle Cat 3 it
runs the very bug this change fixed, and its picture is garbage while the board's is
the game. The 70,000-odd differing pixels on those rows are the detector's, and the
rows are counted `mame-board` for exactly that reason.

#### What it moved

| status | before | after |
| --- | ---: | ---: |
| `same` | 1,608 | **1,610** |
| `unsupported` | 94 | **75** |
| `mame-board` | 225 | 243 |
| `differs` | 262 | 261 |

**2,072 of 2,673 present cartridges, 77.5%.** Counted against the boards the emulator
models the figure *falls*, 80.3% to 79.8%, and that is not a regression: nineteen
cartridges left `unsupported`, so the denominator grew by nineteen while two became
`same`. The other seventeen are cartridges the board now runs and MAME cannot, which
no comparison against MAME can score.

### BlastEm's event log, read (2026-09-24)

The user asked for BlastEm as a second detector, which matters more since the mapper
landed: seventeen cartridges sit in `mame-board` not because the board is wrong but
because **MAME cannot run them**. It crashes on the whole Demons of Asteborg family
and puts Earthion on `rom_sram` with no mapper, where it draws noise.

Getting a *picture* out of BlastEm headless turned out to be closed on every route:

- its **debugger** spawns a terminal emulator (`terminal.c`: `execlp(term, term,
  "-title", "BlastEm Debugger", "-e", termhelper)`), so piped stdin drives nothing;
- **`-g`** does not avoid OpenGL and SDL's dummy video driver cannot supply it;
- **`USE_FBDEV=1` does not build** at the pinned revision: `render.h` types
  `render_thread` as `SDL_Thread *` and only `render_sdl.c` implements
  `render_create_thread`, so the 2013 framebuffer backend no longer compiles. It is
  about twenty lines of pthread shim to fix, and was **not** fixed: patching a pinned
  oracle to build would make it "our BlastEm" rather than an independent one.

Its **event log** needs no display at all. `scripts/blastem_events.py` reads it, and
`tests/test_blastem_events.py` checks the decode on logs built in the test.

**What it settles.** BlastEm runs Earthion properly -- 380,043 VRAM byte writes,
13,840 CRAM and VSRAM writes, 9,062 register writes, PSG and YM activity, sane
registers -- where MAME draws noise. Two independent emulators run this cartridge and
the one that cannot is the one without the mapper, which is as close to corroboration
of milestone 8's mapper work as a detector can give.

**What it opens.** The board and BlastEm do *not* agree on Earthion. The board runs
the game's whole VDP initialisation **twice**: frames 25 and 26 repeat frames 10 and
11 byte for byte, the full register set `0:04 1:74 2:38 3:34 ...` and then the same
DMA burst. BlastEm runs it once. Best alignment over 600 frames matches only 118 of
them, so this is a structural difference and not a phase one. Whether the board is
re-initialising wrongly or the game reaches that path at a different moment is **not
settled here**, and needs the divergence traced to its first differing write rather
than its first differing frame.

**Two traps, both of which produced wrong readings before they were found**, and both
now recorded in the module's own docstring so the next reader does not repeat them:

1. **`EVENT_FLUSH` is not a frame marker.** It is an output-buffer flush. Its count
   lands near the frame count -- 599 for a 600-frame run -- which is exactly what
   makes it look like one. Its gaps cluster: over that run the mean gap is one frame
   and the **median is two**. Grouping by it produced an apparent "BlastEm runs at
   half the rate" that does not exist. Frames come from the cycle instead, and a
   cycle is **half a master clock** (~26.85 MHz), not a 68000 clock.
2. **Comparing instantaneous VDP registers says nothing here.** The board's and
   BlastEm's differ at registers 0, 10, 12 and 13 after 600 frames -- which are
   exactly the ones Earthion rewrites per raster band. That is sampling phase, not
   disagreement. The stream is the instrument; a snapshot is not.

The parser's own correctness check is that it consumes the body exactly: 887,746 of
887,746 bytes on Overdrive 2's log and 1,226,184 of 1,226,184 on Earthion's. One
wrong payload width desynchronises and either raises or stops short, so landing on
the last byte is the proof.

### Item 21 chased, and closed the other way (2026-09-24)

The user asked for the Earthion difference to be chased. It turned out there was no
divergence to chase, and then the rate difference that was left resolved against the
detector rather than against the board.

**There is no divergence.** The first 9,062 VDP register writes are identical, in
order, between the board and BlastEm; the board's stream is a strict superset. The
"the board runs the initialisation twice" reading was an artefact of comparing by
frame number: BlastEm reaches the same code later. That claim is withdrawn.

**What the loop actually is.** Between two DMA triggers the game reads $C00004 three
or four times and nothing else -- `0A0A`, `0A0A`, `0A0A`, `0A08`. Bit 1 is the
DMA-busy bit. The game triggers a 31-word 68000-to-VRAM transfer, polls until busy
clears, and triggers the next, so the loop's rate *is* the transfer's duration.

**The board's duration is the Manual's, to the digit.** `scripts/run_dma_cost.py`
still reproduces the whole DMA transfer capacity table -- 16.1, 167.1, 18.1, 205.1
against the Manual's 16, 167, 18, 205 -- and Earthion's own transfers take a median
of **1,036 MCLK where the table predicts 1,034**, a ratio of 1.00. The blanking
column is the right one: the display is disabled on all 34 of the board's triggers,
and with the display disabled the blanking figure applies on every line.

BlastEm takes about 2.6 times longer for the same transfers. The Manual is
specification tier and outranks both emulators, so the board is right here and
nothing was changed.

**The contradiction with MAME was never one.** Running *behind* MAME is the
accumulated cost of stalls MAME models nothing of; running *ahead* of BlastEm is one
DMA rate the Manual settles. Two different measurements that were only ever in
tension because both were described as "speed".

**What this cost, and what it bought.** Two readings had to be thrown away -- the
double initialisation, and `EVENT_FLUSH` as a frame marker -- both from comparing
before checking the instrument. What survived is: an independent emulator
corroborating the Extended SSF mapper, a second detector for the cartridges MAME
cannot run, and one more of the board's timing rules confirmed against the
specification rather than against an emulator.

### The event reader's 32-bit counter, and a heuristic thrown away (2026-09-24)

Redoing Overdrive 2 on the double-buffered renderer needed a way to find a broken
frame among 12,000. Two instruments were tried and one of them was already committed
and wrong.

**The distinct-row count does not work, and is inverted.** It was right about the
*old* symptom -- a torn frame collapses to 37 rows of 224 -- and carrying it over to
corruption was a mistake. Scored by how far a frame's self-difference falls at some
small shift, the four frames confirmed broken by eye score 0.226, 0.395, 0.552 and
0.620, scattered across the whole range, while frame 10,100 -- a correct grey cube on
black -- scores the highest of all at 0.892. A mostly-black correct scene is hugely
self-similar; a dense plaid has every row distinct. Pixel statistics do not separate
these two, and the approach was dropped rather than tuned.

**`scripts/blastem_events.py` silently corrupted any log longer than about eighty
seconds.** BlastEm's cycle counter is 32 bits, and `EVENT_ADJUST` deducts a large
amount from every component at once to keep it from overflowing. Its own reader does
that subtraction in `uint32` arithmetic; the parser did it in Python's unbounded
integers, so the counter ran off. The 12,000-frame Overdrive 2 log carries exactly
one adjust -- 4,278,391,830, taken when the raw counter had reached 4,286,955,510 --
and read without the mask the log **appears to stop at frame 4,008** instead of the
12,002 it holds.

The fix separates the two clocks, which is what the format actually has:

- `Reader.raw` is the counter as BlastEm holds it: 32 bits, wrapping, deducted from
  by an adjust.
- `Event.cycle` is absolute time, which only ever advances by an event's delta. An
  adjust is bookkeeping against an overflow, not a moment when time moved, so it does
  not touch it.

Three tests cover it, including a wrap that accumulates past 2^32 with real 24-bit
deltas. The reader still consumes the whole body: 76,821,806 of 76,821,806 bytes on
the Overdrive 2 log, now over 12,002 frames.

Milestone 8's Earthion work is unaffected -- 600 frames is about twelve seconds, well
before the first adjust -- so item 21 stands as measured.

**The reconstruction was validated before anything was built on it.** The risk was
that BlastEm logs the register writes that *trigger* a DMA but not the bytes the
transfer moves, in which case a VRAM rebuilt from the log would quietly drift from
the real one. Earthion settles it, because its loading is almost entirely DMA: the
board's **380,340 VRAM byte writes are identical to BlastEm's, address and value, in
order**, with no address transform (`^ 1` mismatches at the very first write;
straight matches throughout). So the log carries the transferred bytes, the address
convention is the one `_vram_write_byte` takes, and a replay of the log reconstructs
VRAM faithfully.

It is also a much deeper corroboration than the register streams gave. 9,062 matching
register writes was suggestive; 380,340 matching VRAM writes between two independent
emulators is another kind of evidence.

## Session 9 (2026-09-24): the Overdrive 2 diff, and the instrument corrected first

### How the handoff was read

The handoff put the Overdrive 2 diff first and said to validate every instrument
before drawing a conclusion from it. Doing that turned up three faults in the
BlastEm instrument, one of which had hidden the whole target: the reference log the
diff was to be built on **never reached a broken frame**.

Environment: PyPy 3.11.15 (`.venv-pypy`), BlastEm at `reference/src/blastem` @
542164d, `Overdrive 2 (World) (Revision 2017) (Aftermarket) (Unl).md` (SHA-256
`f722fc34...5611`, unpacked alone into a scratch directory), `MACHINES["megadrive"]`.

### The BlastEm clock is one master clock, and `-b N` runs N/2 frames

`scripts/blastem_events.py` took a logged cycle for half a master clock. It is one:
BlastEm's VDP clock, which stamps every VDP event, advances `MCLKS_LINE` = 3,420 a
line (`vdp.c` 3815) and 16 an H40 slot. The data says the same without the source:
Overdrive 2's once-a-line VSRAM writes land 3,420 apart in the log, one line, where
the old reading put them two lines apart.

The half-clock reading had been fitted to a second fault: **`blastem -b N` runs N/2
frames**, at least headless at this revision. The frame counter `-b` reads is bumped
in two places (`vdp.c` 2254 when a threaded renderer exists, 3109). So the handoff's
reference log, `-b 12000`, holds 6,423,581,234 master clocks, **6,001 PAL frames**,
and its last register write and its last VRAM write are both the board's in frame
6,001. Frames 7,200, 8,400, 9,000 and 10,800 were never in it.

`-b 24000` gives the whole demo: 65,160,157 events over **12,001 frames** (by the
clock, not the argument), decoding to byte 211,063,218 of 211,063,218. The old log
regenerated byte-identical first (20,545,852 events, 76,821,806 bytes), so BlastEm is
deterministic here -- though not hermetic: it loads and saves a battery file under
`~/.local/share/blastem/<game>/save.sram`, which is why an Earthion log regenerated
this session came out 1,227,750 bytes against the 1,226,184 recorded.

### Item 21 withdrawn: the factor of two was the unit

Item 21's table -- the board 300 frames and a 1,710 MCLK gap between DMA set-ups,
BlastEm 599 frames and 3,608 -- is exactly this error: 599 half-frames are 300 frames
and 3,608 is 1,804 doubled. Re-measured on `Earthion (World) (En) (Aftermarket)
(Unl)`, `genesis`, 300 frames: both emulators run the same 31-transfer burst every
other frame, one frame apart, and the median gap between set-ups (register 23
writes, below 20,000 MCLK) is **1,712 MCLK on the board and 1,804 on BlastEm**, a
ratio of 1.05. The board's transfers being the Manual's (ratio 1.00) stands; it never
rested on BlastEm. "BlastEm takes 2.6 times longer" and "BlastEm is the outlier" are
withdrawn, in `docs/undocumented-behavior.md` item 21 and in the handoff.

### The third fault: `VRAM_BYTE_AUTO` steps by register 15

The reader stepped `VRAM_BYTE_AUTO` by a fixed 2 ("the value every log seen here
uses"). BlastEm's writer and reader both step by the live `regs[REG_AUTOINC]`
(`vdp.c` 1192, 6908-6910), from the zero `calloc` gives. The reader now tracks
register 15 from the log's own writes, with a test. Overdrive 2's log has no
`VRAM_BYTE_AUTO` event at all, so nothing measured moved; it would have mattered on
the next game that does.

### Two things the log cannot carry, found by reading before building

- **A VRAM copy DMA is not logged** (`external_slot`, `vdp.c` 1263-1266:
  `write_vram_byte` with no `event_vram_byte`). The board's copy bytes are flagged and
  left out of the comparison. Overdrive 2 copies 131,072 bytes, all in frames
  11,086-11,149, after every frame examined here.
- **In 128 KB mode BlastEm stores a write to an odd address**, where the board, after
  Kabuto, stores nothing. The instrument flags it on both sides so it is counted, not
  hidden. None occurred.

### The instrument: `scripts/vdp_streams.py`

Records the board's four VDP write streams (register, VRAM byte, CRAM, VSRAM) in one
canonical form, rebuilds the same four from a BlastEm log, and reports where each
pair first parts; `replay` rebuilds the state a stream leaves at the end of any
frame. The board and BlastEm store VRAM in the same byte-swapped layout (byte address
`a` at index `a ^ 1`, and Kabuto's 128 KB rearrangement, which is also BlastEm's
`write_vram_word`), so one index serves both.

Validated before use:

- **The board side rebuilds exactly.** A 7,201-frame run saved the VDP's real final
  state; VRAM, CRAM and VSRAM rebuilt from its streams equal it. The only registers
  that differ are 19 and 21, the DMA length and source counters `_dma_step` updates
  directly -- which BlastEm's log does not carry either, so the comparison is like
  for like.
- **The board run is deterministic**: the 7,201-frame run's streams are an identical
  prefix of the 12,000-frame run's, values and positions.
- **`fm.enabled = False` changes nothing the VDP sees**: 1,499 frames with the FM
  generator on and off give identical streams in all four kinds, values and
  positions.
- **Alignment holds** once the clock is right: every divergence below sits at the
  same line within a frame or two on both sides.

Commands (scratch directory `S` holding only the image):

```text
blastem -b 24000 -e $S/od2-24k.bel "$S/Overdrive 2 ... .md"                           # 45 s
.venv-pypy/bin/python scripts/vdp_streams.py blastem $S/od2-24k.bel --out $S/blastem   # 10 s
.venv-pypy/bin/python scripts/vdp_streams.py board "$S/Overdrive 2 ... .md" --frames 12000 --out $S/board  # 344 s
.venv-pypy/bin/python scripts/vdp_streams.py diff $S/board $S/blastem
```

### What the streams say

| stream | board | BlastEm | agree from the start | then |
| --- | ---: | ---: | ---: | --- |
| registers | 1,033,488 | 1,033,780 | 395,312 | **one episode**: the board makes two extra writes at frame 7,047 (`r15=04, r15=02`), reaching a transition a frame late; every later write matches to the end |
| VSRAM | 761,855 | 761,857 | 107,740 | two small episodes at frames 5,660-5,661 (a raster effect starting one line later on the board); identical to the end after them |
| VRAM | 52,642,830 | 52,665,188 | 15,165,203 | from frame 6,032, one FIFO entry at each 128 KB-mode boundary (below), then the frame-7,047 lag |
| CRAM | 351,858 | 351,857 | 1,971 | from frame 548, 271,414 of the values differ while count and slots start out the same (below) |

**The 128 KB-mode boundary.** Each time the demo leaves 128 KB mode the board commits
the last FIFO entry as a normal-mode word where BlastEm commits it in 128 KB mode
(frames 6,032-6,256); each time it enters, the board commits the second byte of the
last normal-mode word in 128 KB mode where BlastEm commits both in normal mode (from
6,270). Both emulators apply a register write at once, not through the FIFO
(BlastEm `genesis.c` 1071-1074 runs the VDP to the 68000's cycle, then applies it),
so this is a slot or so of 68000-against-VDP phase, one entry per boundary. Two
detectors cannot say which is right.

**The CRAM values.** From frame 548 to 11,083 the two write the same number of CRAM
words but mostly different values -- green levels in steps of `0x20`, which looks like
a random-number effect seeded or fed differently on the two. The register and VRAM
streams stay identical for thousands of frames after 548, so these values do not feed
back into anything else the VDP holds. Their source is **not settled**.

**Overdrive 2 does use 128 KB mode**, contrary to milestone 5's note above: the board
writes register 1 with bit 7 set 12,460 times over frames 4,123-6,520, 11,124 of them
as `$F4`, **with the display enabled**. What 128 KB mode does to the picture is not
modelled (`vdp.py`, `_vram_write_128k`), so that section is suspect too. It is not
one of the four sampled frames.

### The four broken frames: the data is right

Rebuilt at the end of each broken frame and set against BlastEm's frames a few either
side:

| board frame | VRAM bytes differing | CRAM | VSRAM | registers | BlastEm frame |
| --- | ---: | ---: | ---: | ---: | --- |
| 7,200 | **0** | 0 | 0 | 0 | 7,198 |
| 8,400 | **0** | 0 | 0 | 0 | 8,398 |
| 9,000 | **0** | 0 | 0 | 0 | 8,998 |
| 10,800 | **0** | 0 | 0 | 0 | 10,797 |

At every broken frame the VDP holds, byte for byte, what BlastEm's VDP holds. The
plaid is **not** wrong data in VRAM, CRAM or VSRAM. What is left, in the order to
look:

1. **The debug register ($C0001C)**, which neither stream carries: milestone 5 found
   Overdrive 2 forcing planes through it in windows around all four frames.
2. **Mid-frame timing**: raster writes landing on other lines than on BlastEm.
3. **The renderer**, drawing the right data wrongly.

### The debug register was not it: three of the four frames were never broken

The debug register was the first suspect for the four frames, since BlastEm's log
cannot carry it. Recording every write Overdrive 2 makes to it on the board (25 over
11,200 frames) settled that quickly: **frame 7,200 is drawn with it at 0** -- cleared
at frame 7,064 and not set again until 8,128 -- while 8,400, 9,000 and 10,800 are in
the demo's "B-masked" windows (bits 8-7-6 = 110). Every write lands in the vertical
blank or at the frame's edge, 56-84 MCLK behind the 68000, so the missing sync on
`$C0001C` does not matter here.

"Broken by eye" had no reference behind it, so one was made. BlastEm's framebuffer
backend does not build and its debugger will not run headless, but the pinned tree
builds unchanged as a **libretro core with its own `Makefile.libretro`**, and a
libretro core hands every finished frame to its frontend. A frontend of our own
(ctypes, about 100 lines: environment calls answered, SRAM kept in a scratch
directory rather than `~/.local`) drew BlastEm's picture of the same 31 frames the
board was asked for, aligned to within a frame or two (the Mega Drive console scene
appears at 7,598-7,700 on both).

- **8,400, 9,000 and 10,800 are not broken.** Their "plaid" is the B-masked effect the
  demo means, and the board draws it as BlastEm does. Three of the four "wrong frames"
  carried since milestone 5 were a judgement made without a reference.
- **7,200 was broken, and it is not alone**: frames 7,066-7,598 are one scene, a
  fractal zoom into nested squares that changes colour as it goes. BlastEm draws it
  clean; the board drew the same squares chopped vertically, with bands of vertical
  bars at a few lines.

### The vertical scroll latch

The zoom sets full-screen vertical scroll on every line (two VSRAM words a line from
an H interrupt handler, values that nearly cancel the line number, so each plane row
is repeated for several lines -- a vertical magnification), and on the board those
writes commit 2-66 pixels into the visible line (448 commits in frame 7,200: 177 at
pixel 2, 215 at 18, 47 at 34, 9 at 66). The board read VSRAM as it stood whenever it
drew, so each such line was drawn half from one value and half from the next.

When the chip takes the value is not in any hardware-captured source. BlastEm latches
once a line at H40 slot 165 and says it is untested (`vdp.c` 3611); Nemesis
"believe[d]" it is re-read at every mapping slot and had not tested it (forum
t=1291); Eke was "pretty sure it is fetched close to HSCROLL". By the tier rule the
die-derived referee judges, so it was measured on Nuked-MD:

- `validation/nuked_md_headless.c` gained two instruments of our own: `NMD_PLOT`, the
  VDP's RGB pins sampled on every clock edge and framed by its own `/HSYNC` and
  `/VSYNC` (with the V counter of each row), and `NMD_VDPSIG`, a log of every change
  of the two VSRAM-to-scroll latches `l184` (plane A) and `l185` (plane B), of VSRAM
  words 0 and 1, and of `/HSYNC`, each with the die's H and V counters (`l106`,
  `l105`).
- A test cartridge (`scripts/run_vscroll_latch.py`, hand-assembled, each encoding in
  its comment): plane red over its top 128 rows and green below, full-screen scroll;
  on each line the 68000 waits (V & 63) `dbra` loops, sweeping its write across the
  whole line over 64 lines, then writes `(V & 1) << 7`. The value is fixed by V: a
  first version toggled once a line, and the board's H40 result came out almost
  exactly inverted because its set-up ended a line from the referee's -- a phase
  artefact of the instrument, caught and removed before anything was concluded.

Result, planes A and B, H40 and H32:

| | plane A latch | plane B latch | split lines | line v shows the latch of v - 1 |
| --- | --- | --- | ---: | ---: |
| H40 | $149 (640 of 640) | $143 (640 of 640) | 0 | 224 / 224 |
| H32 | $109 (640 of 640) | $103 (640 of 640) | 0 | 224 / 224 |

(against 37-39 of 224 for "the latch of line v"). So the scroll is latched **once a
line, in the last pixels before the V counter increments**, and a VSRAM write made
during a line -- including in the horizontal blank before its display, where an H
interrupt handler writes -- reaches the picture only on the line after.

The board now does that: `VDP.vscroll_latch` holds each plane's value, set at
`_next_line` from VSRAM as it stood at the plane's latch point (`_vsram_store` keeps
the old word when a write lands after it), and the renderer draws full-screen scroll
from it. The 2-cell column mode, interlace mode 2 and display-off lines are **not**
measured and not changed. The rule is `docs/undocumented-behavior.md` VDP item 23.

Against the referee on the same cartridges, the board now splits no line and matches
**222/224 (plane A, H40), 221/224 (A, H32), 206/224 (B, H40) and 201/224 (B, H32)**
lines. The rest is item 20, already open: the board commits a slot's write about 4
pixels (H40) or 1 pixel (H32) before the die does (measured here per line, board H
minus referee H: -4 on 135 of 187 H40 lines, -1 on 125 of 188 H32), and plane B's
latch at $143 sits just after the slots at $140/$142, so a write the die places after
it the board places before.

**Overdrive 2's fractal zoom now matches BlastEm's picture**: the chopped rows and the
bands are gone, frames 7,150-7,500 show the same squares, and the other scenes did not
move. Frame 10,000 still differs from BlastEm in one blue shape at the top: open.

### What it moved, and why

Checks at the latch commit: both suites (109 passed on each interpreter, before the
four new tests), the sprite masking ROM (0 of 18 cells differ from the MD1600), and
r57shell's eight VDP tests (identical output before and after).

The whole romset was re-swept (`scripts/compat_sweep.py --seconds 10 --jobs 16
--window 12 --offsets --out tests/compat/megadrive-latch.csv`, 13,478 s, PyPy 3.11.15)
and diffed row by row against the sweep before it. **10 of 3,514 rows moved**:

| cartridge | before | after |
| --- | --- | --- |
| Batman (Europe), (Japan) (En), - The Video Game (USA) | differs 7,041 | **phase** |
| Mega Turrican (USA) | differs 5,487 | **phase** |
| Panorama Cotton (Japan) | differs 64,351 | **phase** |
| ZPF (World) (Demo) (Aftermarket) (Unl) | differs 6,903 | **phase** |
| Vectorman (USA, Europe), (Beta 1), (Beta 2) | phase 2,159 | differs 2,836 |
| McDonald's Treasure Land Adventure (USA) | differs 10,647 | differs 10,682 |

**Every move is the latch and nothing else**: a new sensitivity knob,
`disable="vscroll_latch"` (`scripts/compat_sweep.py`, also in `triage_diffs.py`'s
pass 2), hands the renderer live VSRAM again, and with it all ten rows return to
their old status and pixel count exactly.

The four that moved away from MAME are the pattern the referee measured. Both games
write plane B's full-screen word once a line during the display -- Vectorman at H $12
(about pixel 18) on 209 of 224 lines, McDonald's at H $62/$82 (pixels 98-130, the
middle of the line) -- so the board used to split those lines, and now draws each
from the latch of the line before, as the die does. MAME draws a line at one moment of
its own choosing and agrees with neither. MAME is a detector; nothing is changed to
match it.

**One of them was put to the judge directly.** Nuked-MD ran `Vectorman (USA,
Europe)` for 600 frames (38 minutes; the picture taken with `NMD_PLOT`), and the
board's frames 594-605 were compared with it line by line, at the best horizontal
offset and colour mapping. With the latch, board frame 601 equals the referee's frame
**on 224 of 224 lines**; with the live read (`disable="vscroll_latch"`), the best is
75 of 224, on the same frame and offset. So the row the sweep counts as having moved
away from MAME moved onto the die: Vectorman's `differs` is MAME's.

Checks after the latch: both suites **113 passed, 1 deselected** (PyPy 172 s, CPython
449 s); ruff check and format clean.

| status | before | after |
| --- | ---: | ---: |
| `same` | 1,610 | 1,610 |
| `phase` | 462 | **465** |
| `differs` | 261 | **258** |

**2,075 of 2,673 present cartridges, 77.6%** (from 77.5%), and **2,075 of the 2,598 on
boards the emulator models, 79.9%** (from 79.8%).

## Session 10 (2026-09-25): where a mid-line write shows (item 20)

### The question

Item 20 had been open since milestone 3: the board lets a mid-line change take effect
from the pixel the H counter names when it lands, and Kabuto places H40's second
mid-tiles slot "at the pixel with X=-3 (display area coordinates)" for palette writes,
where the board put it at 18 -- a colour output running about 21 pixels behind the
counter. It was also the one residual of the vertical scroll latch measurement. The
referee instruments of Session 9 made it measurable.

### The measurement

`scripts/run_output_delay.py`: two cartridges with both planes transparent, so the
display is the backdrop; on each line the 68000 waits (V & 63) `dbra` loops, sweeping
across the line, then changes the backdrop's colour by V's parity -- by writing CRAM
entry 0 (`cram`), or register 7, the backdrop's index (`reg7`). The referee's picture
(`NMD_PLOT`; display pixel 0 is sample 154 in H40 and 130 in H32, two samples a pixel,
read off the latch test's plots) gives the pixel where each line's colour changes, and
`NMD_VDPSIG` now also logs CRAM entry 0 (`C0`) with the die's H counter.

- **A CRAM write committed at die H counter h shows from output pixel h - 24**, on 153
  of 154 lines in H40 and 155 of 158 in H32.
- The die numbers H40's slots three above the board (the board's $042 is its $045,
  the same "-4" seen in the latch work, which was therefore mostly labelling: the two
  commit within about a pixel of each other). So in the board's terms the output runs
  **21 pixels** behind the counter in H40 and **24** in H32, which puts H40's slot $012
  at X = -3 and $002 at -19, in the blank -- exactly Kabuto's two statements.
- **Register 7 behaves the same way**, shown by timing the write on the board (whose
  68000 milestone 6 matched to the referee's): the change sits 21-25 pixels left of the
  write's pixel in H40, spread a few pixels by the 68000's polling phase. And the
  board had a second gap here: it read the backdrop index once, at `begin_line`, so a
  mid-line write never showed on its own line; the referee changes colour part way
  along 166 of 224.

### The change

`render.OUTPUT_DELAY = {"H40": 21, "H32": 24}`. `Renderer.catch_up` takes `output=`:
a change acting where the colour is looked up is drawn from that many pixels further
left. `VDP._write_register` passes it for register 7, and `VDP._slot` when the slot
commits to CRAM (a FIFO write or a fill). The renderer now reads register 7 as it
stands when it draws. Changes that act where tiles are fetched -- VRAM, the plane and
scroll registers -- are **not** delayed: the fetch runs ahead of the output by an
amount not measured here, and they stay at the H counter's pixel. The rule is
`docs/undocumented-behavior.md` VDP item 20, rewritten.

After it, referee minus board per line (`scripts/run_output_delay.py`):

| | CRAM | register 7 |
| --- | --- | --- |
| H40 | 0 on 102 of 151 lines, the rest whole slots (+-16, +-32) | centred on 0 (mode -2), spread -7..+7 by polling phase |
| H32 | 0 on 114 of 152, the rest whole slots | spread -7..+16, centred near 0 |

The whole-slot spread is the 68000 reaching the write a slot earlier or later on some
lines in one run than in the other (the board's frame 12 against the referee's 11th);
before the change the CRAM mode was -21 and -24.

One test pinned the old rule: `test_a_cram_write_in_the_display_leaves_a_dot` expected
the dot at its slot's pixel (118-135 for a write at pixel 120); it now expects it 21
further left (97-114).

### What it moved

Checks: the sprite masking ROM (0 of 18 cells differ), r57shell's eight tests
(identical output), both suites (below).

Re-swept (`--seconds 10 --jobs 16 --window 12 --offsets --out
tests/compat/megadrive-delay.csv`, 14,444 s) and diffed against the post-latch sweep:
**2 of 3,514 rows moved, and no status changed** -- `George Foreman's KO Boxing
(Europe)` differs 44 -> 48 pixels, `Satyr in Hell, A (World) (Digital) (Aftermarket)
(Unl)` 302 -> 320. Both are the delay and nothing else: a new knob,
`disable="output_delay"` (`compat_sweep.py`, and in `triage_diffs.py`'s pass 2), puts
the delay back to 0, and both return to 44 and 302. Their extra pixels are CRAM changes
now shown where the die shows them; MAME is a detector and nothing is changed to match
it. The totals are the post-latch sweep's: 2,075 of 2,673, 77.6%; 79.9% on the boards
modelled.

Overdrive 2: frames 9,000 and 10,000 are unchanged pixel for pixel (so 10,000's one
blue shape against BlastEm is something else, still open); frame 9,500, the sphere,
changes in 180 pixels, all in its left quarter, where its mid-line CRAM dots now land.

Both suites after it: **113 passed, 1 deselected** (PyPy 183 s, CPython 450 s); ruff
check and format clean.

### The 2-cell column latch

Item 23 had left 2-cell column scroll unmeasured, and the board still read each
column's word as it stood whenever it drew -- so it split columns the way it had split
full-screen lines. It is also where tile fetching's lead over the output (the half of
item 20 left open) shows first.

`scripts/run_vscroll_columns.py`: a cartridge like the latch one, with 2-cell column
scroll, every VSRAM word 0, and each line's write aimed at **one** column's word.
Because only that word ever changes, the die's column-scroll latch `l185` takes 128
exactly when it latches that column, so the log names the moment, and the picture
says which value each line used. `NMD_VSRAM_WATCH` makes the log watch any one VSRAM
word.

| plane, mode | die H of column k's latch | columns measured | line v shows counter line v's latch |
| --- | --- | --- | ---: |
| A, H40 | 16k + 1 | 0, 10, 19 | 224 / 224 each |
| B, H40 | 16k + 9 | 0, 10 | 224 / 224 each |
| A, H32 | 16k + 1 | 0, 8, 15 | 224 / 224 each |
| B, H32 | 16k + 9 | 8 | 224 / 224 |

So each column is latched during its own line, 16 pixels after the one before, and
the referee never splits a column. A first check of the rule from write positions
missed the same lines for every column; it was the check that was wrong (the most
common write position over three frames, and H values compared without the counter's
wrap), and reading the latched value straight from `l185` line by line gave 224/224.

The board now keeps, word by word, what a column's word held when a write landed
after that column's latch in the current line (`VDP._column_before`, cleared at
`_next_line`), and draws 2-cell columns from `VDP.column_scroll`. The latch points,
in the board's counter (the die's H40 counts three above it), are
`VSCROLL_COLUMN_H`: plane A 16k - 2 and plane B 16k + 6 in H40, 16k + 1 and 16k + 9 in
H32.

That alone left plane B splitting (7 lines in H40, 20 in H32): its latch falls 6 or 9
pixels *into* the column, after the board has begun drawing it, while the die outputs
the column about 21 pixels later (item 20), after the latch. So a slot committing a
2-cell VSRAM write before its column's latch now holds the drawing at that column's
first pixel (`VDP._column_limit`, `catch_up(limit=)`), and the whole column takes the
value the latch will.

| board against referee, lines matching / split | before | after |
| --- | --- | --- |
| A H40 column 0 | 207 / 0 | 222 / 0 |
| A H40 column 10 | 216 / 7 | 223 / 0 |
| A H40 column 19 | 219 / 0 | 219 / 0 |
| B H40 column 10 | 216 / 7 | 223 / 0 |
| A H32 column 8 | 202 / 20 | 222 / 0 |
| B H32 column 8 | 201 / 20 | 220 / 0 |

(The remaining lines are the 68000's polling phase, a slot either way, as before.)

Checks: sprite masking 0 of 18, r57shell identical, `scripts/run_vscroll_columns.py`
passing on all nine cases (board 218-224 of 224 lines, no split), both suites **116
passed, 1 deselected** (the three new column tests; PyPy 178 s, CPython 460 s).

**The re-sweep moved nothing**: `--seconds 10 --jobs 16 --window 12 --offsets --out
tests/compat/megadrive-2cell.csv`, 14,719 s, 0 of 3,514 rows different from the sweep
before it, in status or in pixel count. The romset's 2-cell games do not, at the frame
the sweep compares, write a column's word in the window the two rules disagree on. The
triage knob is there for when one does: `disable="column_latch"` reads each column's
word as it stands again and never holds the drawing back.

### Frame 10,000, and where a tile write shows

Overdrive 2's frame 10,000 still differs from BlastEm in one place: the darker region
at the top right of the upper shape. The VDP data is not it -- VRAM, CRAM, VSRAM and
the registers at the end of board frames 9,998-10,004 equal BlastEm's two frames
earlier (`vdp_streams.py` replay) -- and nor is frame phase: BlastEm's 10,000 is the
board's 10,003-10,004 in geometry, and none of the board's frames 9,996-10,004 has the
region. (Pixel counts against BlastEm are no guide here: its libretro frame is 320x240
with borders, five lines above the board's, and its colour levels are not the board's.)

Two leads, one ruled out:

- The scene rewrites tile 0's first row about 870 times a frame **during the
  display** (bytes $02-$05, every line), so which columns see which row depends on
  where each column's tiles are fetched relative to the write -- the half of item 20
  left open. `scripts/run_output_delay.py` now measures it (`vram`: plane A one tile
  everywhere, each line rewriting the row it shows): in **H40 the board already lands
  on the referee's pixel** (60 of 79 lines, the rest a slot either way) and in **H32
  it is 8 pixels late** (53 of 77). The scene is H40, so this is not the cause. The
  H32 offset is recorded in item 20, open; name-table writes are not measured.
- The scene has **shadow/highlight on** (register 12 = $89). The missing region is
  darker, as a shadow would be. That is the lead left: how the board decides shadow
  there, not what it is given. BlastEm is a detector; the judge would be Nuked-MD
  running the demo to frame 10,000, about eight hours at 0.33 frames a second. Open.

## Session 11 (2026-09-26): where tiles are fetched -- measured as far as slots allow

### The die run

Overdrive 2 is running on Nuked-MD to frame 10,006 (PAL, about eight and a half hours),
to judge the region frame 10,000 lacks against BlastEm. Nuked-MD handles the Sega
mapper up to 8 MB (`cartridge.c` 8, 36-46), which the demo's 8,386,056 bytes fit.
`NMD_PLOT_FRAMES` now plots that many consecutive frames from `NMD_PLOT_FRAME`, one
file each (`<NMD_PLOT>.<frame>`), so a frame or two of phase between the board and
the referee costs nothing; it is plotting 9,995-10,006. Checked first on the latch
cartridge (frames 11 and 12, full line structure in both).

### Name-table writes

Session 10 left the H32 pattern-row offset (-8) open because names are fetched at a
different point from patterns. The name test: plane A tile 1 everywhere; each line
rewrites **one cell's name** (column c of the cell row the line shows) to tile 1 or 2
by V's parity, at the sweeping delay; per line, did the cell show the name written on
that line. Writes land only in access slots, so the result is a switch point between
two slots, per column; columns 16-23 in H40 and 12-19 in H32 were run to move the
column across fixed slots.

**In every column, in both modes, the die stops taking the new name one slot earlier
than the board does** -- names are fetched well before the board draws them, even in
H40, where pattern writes land on the board's pixel. So no single offset per mode is
right for all VRAM writes: names lead, patterns lead by nothing in H40 and trail by 8
pixels in H32.

What slots cannot give is the exact fetch point: the windows they leave (up to 32
pixels wide across the refresh gap between $0A2 and $0C2) do not line up into one
fetch time per 2-cell group, which means the inference is too coarse, not that the
hardware is irregular. **Not modelled, and not fitted**: a lead read off these
windows would be a fit. The way to an exact answer is to log the die's VRAM reads --
address and H counter -- which `vram.c` does not expose; our driver would rebuild them
from the VDP's VRAM pins (`o_vram_ras`, `_cas`, `_oe1`, `_ad`, and the serial port's
`_sc`/`_se0`), and the fix it points to is a renderer that fetches a column's tiles at
their fetch time and outputs them 21 (or 24) pixels behind the counter, instead of
doing both at the one moment it draws.

### Where tiles are fetched (the user chose to do both steps)

**Step 1: the die's own VRAM accesses.** `NMD_VRAMSIG` (our driver) rebuilds every
VRAM access from the VDP's DRAM pins by the ordinary protocol -- the row on /RAS
falling, the column on /CAS falling, a random read where /OE is low outside a transfer,
a transfer cycle where /OE is low as /RAS falls (its start column arrives with /CAS),
serial shifts on SC rising, and each write's bank (/WE0 or /WE1) and data -- and logs
the raw chip address with the die's H and V. The plane fetches go through the **serial
port**: one transfer cycle, then four shifts, per pattern row and per name pair.

The chip address maps to VRAM as `(a & 3) | ((a >> 6) & 0x3FC) | ((a & 0xFC) << 8)`,
and that was checked, not assumed: on the name cartridge every access of line 100 lands
where its layout says -- plane A's name row 12 at $C600 and on, plane B's at $E600,
tile 1's row 4 at $0030, tile 0's at $0010, the sprite table at $F000, the scroll table
at $FC00, and plane A's and B's column -1 at $C67C and $E67C.

The schedule, identical in H40 and H32 in the die's counter (group g, 16 pixels):

| fetch | die H |
| --- | --- |
| plane A name pair | 16g + 3 |
| plane A pattern rows | 16g + 7, 16g + 9 |
| plane B name pair | 16g + 11 |
| plane B pattern rows | 16g + 15, 16g + 17 |
| group -1 (fine scroll) | in the blank: $1F3, $1F7/$1F9, $1FB, $1FF/$001 |
| horizontal scroll entry | $1E9 |

**Two wrong turns on the way, both caught by the instrument:**

- **Byte order.** The name test had left a slot's disagreement in some columns, and
  a word's two bytes going in over two slots looked like the cause; the die's writes
  (`$CDA0` then `$CDA1`) seemed to say the board wrote them in the wrong order, and
  the board was changed. That made the other columns wrong. Logging each write's bank
  and data showed the die writes the tile index (1 or 2) **first**: its chip index is
  the board's store index with bit 0 flipped, and the board's order was right. The
  change was reverted.
- **Pattern writes read as name writes.** After the renderer change, the tile-row
  test got worse (-16 in both modes). A trace of one line showed pattern writes
  flagged `names=True`: the name-table check counted the window's table, and the
  cartridge never sets register 3, so a window base of $0000 made the patterns at the
  bottom of VRAM look like names. The window's table now counts only on a line that
  shows the window.

**Step 2: a renderer in two stages.** `render.FETCH_AT` holds the schedule in the
board's pixel time (the die's less three in H40): plane A (0, 4, 6) and plane B
(8, 12, 14) from each group's start in H40, three later in H32. Each line keeps a fetch
pointer per plane; `catch_up(names=)` first fetches every cell whose fetch time has
come (with `names`, every cell whose name has), then mixes to the H counter's pixel
less `OUTPUT_DELAY`, for *every* change -- so the special `output` flag is gone, and
with it a latent gap: an earlier catch-up could mix past where a CRAM change should
still have shown. Group g sits at screen pixel 16g plus the plane's fine scroll.
`VDP._in_name_table` decides `names` for a VRAM write; register writes pass it for
the plane registers (2, 3, 4, 11, 12, 16, 17, 18).

**What it measures now**, against the die:

- Name writes, eight columns across both modes: the board's picture differs from the
  die's on 3-8 lines a column, and **every such line is one where the 68000's write
  reached a different access slot** on the two (the same slot on 116-145 lines a
  column, with no disagreement among them). What is left is the 68000 and FIFO's
  timing, not the fetch.
- A tile-pattern row written mid-line: the board lands on the die's pixel on 60 of 79
  lines in H40 and **53 of 77 in H32, which was 8 pixels late**; the rest whole slots,
  as above.
- CRAM, register 7, both latch measurements and the column latches: unchanged.

A first attempt to check the die against itself -- predicting its picture from its
own log -- agreed on only 150-158 of 198 lines, because the log and the picture are
framed by different counters and cannot be matched line for line; it is not a result
either way, and the board-against-die comparison does not depend on it.

Checks: sprite masking 0 of 18, r57shell identical, both suites **120 passed, 1
deselected** (the four new fetch-schedule tests; PyPy 165 s, CPython 461 s), ruff
clean. `scripts/run_fetch_schedule.py` passes: no access of line 100 off the layout in
either mode, the schedule above, and in all eight columns every board-die difference a
commit-slot difference. Its first run failed its own layout check on three accesses,
which were transfers of tile 2's row for the one cell the cartridge renames -- the
check, not the log, was wrong, and was widened to tile 2.

Also seen and not explained: once a group the die reads a plane A name through the
**random** port, three counts after that name's serial transfer (16g + 6). It is not
the fetch the picture is drawn from: were it, writes committed at the board's slot
16g + 2 would show the new name on the die and the old on the board, and none of the
disagreements above is of that kind.

**The re-sweep moved nothing**: `--seconds 10 --jobs 15 --window 12 --offsets --out
tests/compat/megadrive-fetch.csv` (15 jobs, the die run holding the 16th process),
13,357 s, 0 of 3,514 rows different from the sweep before it. The knob for when one
does: `disable="fetch_schedule"` fetches every cell as the screen reaches it again.

### The Overdrive 2 die run: the demo does not get past its first frame on Nuked-MD

The run to frame 10,006 (PAL, 11 h 8 m) finished, and every sample of all twelve
captured frames is black. The capture is not at fault: the latch cartridge under `-pal`
draws its red and green through the same code. A boot check (frames 298-300) is black
too, where the board shows the demo's opening Pong scene.

Nuked-MD's own 68000 bus trace says why: from frame 2 to at least frame 10 the demo sits
in

    $2F021C  btst  #3,$C00005   ; the VDP status's VBlank flag
    $2F0224  bne.s $2F021C      ; loop while it is set

and the flag never clears there. The board reaches the same loop in frame 1, in the
vertical blank (V 230) with the display on (register 1 = $54), and leaves it when the
blank ends (V 511). The die's picture also has an odd V count (293 rows under 224), so
its VDP is not in the state the demo set up: something earlier in the demo's start-up
went another way on the die -- something it detects, a register write that did not take,
or a gap in the model or in this build's configuration (`-DTMSS_ENABLE=0`, a pre-TMSS
board the README says no retail console was). **Not chased**: that is debugging the
referee's run of the demo, not the board, and it was stopped here to ask.

Two unit traps met on the way, the second new: the driver's `frame N` progress line
counts 1,789,772 half-master-clocks (`md.c` 70, 309-311) -- an NTSC-sized unit even
under `-pal` -- so "frame 10,478" was PAL frame 8,759, and the run's real pace was 0.24
PAL frames a second, not the 0.33 first estimated.

So frame 10,000's missing region (the scene has shadow/highlight on) stays open, with
the die unable to judge it until the demo runs on it.

## Session 12 (2026-09-27): milestone 9, the desktop frontend

### What the brief asks, and how it was read

"A desktop frontend, after sms-python's (audio-paced, vsync, pads)", with pygame-ce as
an optional extra and sms-python's design copied, not imported. There is no acceptance
number; full speed on PyPy "is a goal to measure, not a promise".

### What was built

`src/megadrive_python/frontend.py`, from sms-python's `frontend.py` (same author, MIT):
pure conversions tested without pygame; a SCALED window presented at the display's
refresh; SDL's audio thread draining a queue that paces the console a frame at a time;
keyboard and game controllers through SDL; Tab fast-forward, F3 pause, F5 power cycle,
F11 fullscreen, F12 screenshot. What is the Mega Drive's own:

- **Pads**: the 3- and 6-button pads' bit masks, the keyboard's Z X C / A S D rows
  for A B C / X Y Z and Q for Mode, and a controller's face buttons laid out as the
  Mega Drive's own row (A B C as X A B, X Y Z on the shoulders and Y).
- **The machine** from the header's "Country in which the product can be released"
  at $1F0, "Japan: J, USA: U, Europe: E" (the Manual, PDF page 130): the NTSC Genesis
  if it names the USA, the PAL Mega Drive if Europe only, the Japanese one if Japan
  only, the Genesis otherwise; `--machine` overrides it.
- **Sound** is the board's own mix (`System.take_audio`, FM and PSG through the audio
  path at 44,100 Hz), so the frontend only packs it for SDL.
- **Battery RAM** (`--save`) read at power-on and written at exit and at F5, and
  **TMSS** (`--tmss-rom`), as the command line has them.
- The first frame after power-on can be empty -- the renderer hands out finished
  frames only -- so a black one stands in until one has finished.

Not built: the console's reset button. The Mega Drive's resets the 68000 without a
power cycle, and the board has no path for it yet; F5 power-cycles.

### Speed, measured

PyPy 3.11.15, a shared machine (load average 4-8 of 32 cores during these), so lower
bounds:

| cartridge | FM on | FM off | FM off, one frame in 3 drawn |
| --- | ---: | ---: | ---: |
| Sonic The Hedgehog (USA, Europe) | 16.7-17.3 | 39.8-41.4 | 56.4 |
| Streets of Rage 2 (USA) | 21.3 | 55.5 | -- |
| Gunstar Heroes (USA) | 16.4-19.3 | 37.3-39.8 | 53.6 |

The YM2612 is about two-thirds of a frame's time with it on; skipping frames does
nothing for that (17-21 frames a second whatever is drawn). So the frontend degrades
rather than pretends: paced by the audio queue while the board keeps up, flat out when
it does not, with the console's speed as a percentage in the title bar; `--no-fm`
turns the FM generator off (its registers, timers and BUSY still run, so a game
behaves the same) and `--skip N` draws one frame in N + 1. Real time with sound needs
the FM generator faster -- milestone 4 left flattening its seventeen stages for a
session that can re-run all eighteen of its referee streams afterwards.

The README had said the board runs "138 to 205 [frames a second] without" sound; that
was milestone 4's "silent" column, a board with no sound chips at all, before the
per-slot renderer and the timing work. It now gives today's measurement and its basis.

### Checks

`tests/test_frontend.py`: the conversions, the sound buffer, the header's country
codes, a key for every pad button, and the real window loop run headless under SDL's
dummy drivers on a cartridge that loops (skipped where pygame-ce is not installed, as
in CI). A run on Sonic under the dummy drivers, three ways: 90 frames drawn in 4.5 s
with the FM, 2.1 s without, and 180 console frames in 4.4 s drawing one in two;
screenshots written. Both suites **126 passed, 1 deselected** (PyPy 189 s, CPython
456 s), ruff clean. Two warnings in the headless test: SDL's dummy driver has "no fast
renderer", and pygame-ce 2.5 deprecates `Window.from_display_module`, which sizes the
window here as it does in sms-python; its replacement is the whole `Window` API, which
asks for vsync differently, and that move is left for both frontends together.

### The reset button

Asked after milestone 9: is the 68000 core missing a lifecycle event? No.
m68000-python's `reset()` is the reset exception (UM 6.3.1), callable between any two
steps, and since `1f43e0b` (after this board's pin, `51ee1a5`) a fault during it halts
the processor as a double bus fault -- which is also the answer to `polish.md`'s last
decision row, the odd initial PC, when the pin moves. The missing piece was the board's.

What the button does, from hardware (forum t=1262, pinned): Charles MacDonald measured
the reset signals -- VRES# "to 68000 and cartridge connector", SRES# (the VDP, the I/O
chip, the 315-5364) "only asserted on a cold boot" -- and Eke, against the 68000's
clock, that "when RESET button is pressed, !ZRES is held low at the same time as !VRES
and !HALT on 68k side... but remains low after that until software enables it again.
68k RESET pulse is asserted for 128 VCLK".

`System.press_reset()`: ZRES asserted at once through the same path an $A11200 write
takes (now `Board.set_z80_reset`), and at the 68000's next step 128 of its clocks held
in reset with the rest of the machine running, then the reset exception; the VDP, the
I/O chip and the cartridge are left alone. The frontend's Backspace presses it.
`tests/test_reset_button.py`: the 68000 boots again and work RAM keeps its count; ZRES
is held from the press until the game's own $A11200 write; the VDP keeps its registers;
the pulse costs 128 + 42 clocks; a halted 68000 comes back. On Sonic, a press at frame
400 goes back to the SEGA logo, and the game releases the Z80 itself.

Both suites **131 passed, 1 deselected** (PyPy 186 s, CPython 456 s), ruff clean.

## Session 13 (2026-09-27): the rain

Aubrey, watching Overdrive 2 through the new frontend: the first rain scene draws its
city and mountains correctly and **no rain at all**, and older emulators showed it
the same way.

### What the rain is

BlastEm's libretro core (the Session 9 reference, unmodified) draws the scene at
frames 4,150-5,100 with rain; the board does not. A colour census of frame 4,600
says what kind of thing is missing: BlastEm's frame has 70 colours, the board's 26,
and mapped level by level onto the board's picture BlastEm's extra pixels are the
board's own colours **shadowed and highlighted** -- 1,223 and 1,359 of a sample of
8,025, streaked diagonally over the whole screen. Highlight needs a sprite, so the
rain is sprites.

The table (base $0000, H40, shadow/highlight on) holds exactly that: a 10 x 7 grid of
32x32 sprites in palette 3 (colours 14 and 15 of palette 3 are the highlight and
shadow operators), 70 of them, 40 tiles a line -- H40's whole budget. The board's
sprite cache ended its chain at sprite 0 (Y 0, link 0), so no sprite was ever
examined.

### The first guess was wrong, and was tested before anything was built on it

The scene uses 128 KB mode in the vertical blank for byte-wide uploads of the rain's
pattern columns, and Kabuto warns about 128 KB mode and the sprite cache ("even though
the high byte is discarded it'll still get written to the cache"). The board's 128 KB
path fed the cache only the low byte at the rearranged address, where BlastEm checks
the address as written and stores both bytes. Patched in BlastEm's way, from a
snapshot and then from power-on: **still no rain**. Kept as a question, not a fix.

### What the demo does

Logging every write to the table's first 2 KB from frame 4,000: at 4,117 the demo
writes the chain properly (sprite 0 at Y 128 linking to 1, and so on); from 4,123,
every frame, in the vertical blank and in **64 KB mode**, it writes zeros over the Y
and size/link words of sprites 0, 4, 8, ... 76. On the board that broke the cache's
chain at sprite 0.

BlastEm's cache rebuilt from its event log broke the same way -- yet BlastEm draws the
rain. The instrument was the suspect, so BlastEm's real cache was read instead: its
libretro core's `retro_serialize` writes `sat_cache` right after VRAM, CRAM and VSRAM
(`vdp_serialize`, `vdp.c` 6527-6534), and the 320 bytes were found by their contents
and cross-checked against the registers that follow them. Frame by frame, 4,110-4,130:
VRAM's sprite 0 goes to zeros at 4,124 and **the cache keeps Y 128, link 1 throughout**.

A throwaway instrumented copy of the libretro core, in the scratch directory and never
used as a reference, printed every cache check and every VRAM byte write to the first
16 bytes: after 4,117 BlastEm never checks the cache for those writes, because they
are addressed to **$10000-$10003**. The log had shown them at $0000 because
`event_vram_byte` takes a `uint16_t` address (`event_log.c` 424) -- a third thing the
log cannot carry, now in `scripts/vdp_streams.py`'s docstring.

### The rule

Kabuto, "Precise details of sprite rendering": the cache check "is sensitive to the
entire 128 KB address range even in 64 KB mode, so when setting bit 16 for the sprite
table address but not for the Write/DMA target address (or the other way round) the
sprite table in memory will get updated but the cache won't". The demo is his; the
rain is that sentence used on purpose. Nuked-MD has the register the sentence needs:
`reg_addr` takes the second control word's `io_data & 7` into bits 16-14 in mode 5
(`vdp.c` 947-951) and increments `& 0x1ffff` (926-927); the first word leaves bits
16-14 alone. BlastEm keeps the same three bits. Exodus keeps 16, and the board's port
logic came from Exodus, so the board kept 16: bit 2 of the second word was dropped
and the increment wrapped at 64 KB.

`vdp.py`: `ADDRESS_MASK = 0x1FFFF` for the command address, its increments, the FIFO
entries and fills; the second control word takes bits 2-0 in mode 5 (still 1-0 in
mode 4, which the die clears and the board does not model further); VRAM is still
indexed modulo 64 KB, and `sprite_cache_write` already compared 17 bits. Nothing
else changed. `tests/test_sprite_cache.py`: a write through $10000 changes VRAM and
not the cache, and the increment carries into bit 16 and wraps at 128 KB -- both fail
on the code before.

### Result

Against BlastEm, best of its frames n-4..n+4, its levels mapped onto the board's ramp
(its 27/49/71/103/119/146/157/174/190 are the board's 29/52/70/101/116/144/158/172/187):

| frames (every 25th) | before | after |
| --- | --- | --- |
| 4,150-5,075, the rain | 25,057-67,095 of 71,680 pixels | **71,680 of 71,680, every frame** (BlastEm frame n - 1) |
| 4,100-4,125 and 5,100-6,500 | not scored | identical except 1-4 pixels in 5,675, 5,825, 5,950, 6,525 |
| 6,550-6,600, the logo over rings | not scored | 53,033-53,226: the board has no blue rings |

The last two rows are unchanged from the code before (same seven frames rendered from
`HEAD`, identical pixel for pixel) and are not this fault. The 128 KB question makes
no difference to the demo: the two cache rules give identical pictures in all 101
sampled frames, 4,100-6,600, so the board keeps its rule and the question is written
up as unsettled (docs/undocumented-behavior.md, VDP item 24).

### Checks

Triage knob `disable="address_16"` (`scripts/compat_sweep.py`, and in
`triage_diffs.py`'s list): bit 16 dropped as the command word is taken and the
increments wrapped at 64 KB. Checked faithful before use: under it both new tests fail
as they did on the code before, and Overdrive 2's frame 4,600 is identical pixel for
pixel to the old code's (no rain).

Both suites **133 passed, 1 deselected** (PyPy 197 s, CPython 461 s), ruff clean. The
romset re-sweep is running at this commit and is reported below when it finishes.

### The re-sweep for the address

`scripts/compat_sweep.py --seconds 10 --jobs 16 --window 12 --offsets`, 17,115 s:
**0 of 3,514 rows moved** against the sweep before it. The 17-bit address changes no
other cartridge's picture at ten seconds.

### The whole demo against BlastEm

Every 25th frame of 12,000, each against the best of BlastEm's frames n-4..n+4
(`score.py`, its levels mapped onto the board's ramp), then a frame at a time where a
scene was off. What was found, in the order it was found; each fix is a rule of its
own with a triage knob, checked to put the old pictures back exactly.

**The rings (6,540-7,060).** The scene forces the sprite plane on through the debug
register (bits 8-7 = 01), and the board drew black where BlastEm drew blue and magenta
rings: palette entry 0 of palettes 1 and 2. Both emulators' CRAM and debug register
were the same (BlastEm's read from a save state), so the difference was the sprite
line buffer: Kabuto's holds colour, palette and priority and lets a transparent pixel
leave its palette, the board's kept opaque pixels only. That took the scene from
73-75% to 99.7%; the rest was one line, the last. The demo points register 5 at
another table in the blank before line 223, after the hardware has read that line's
sprites (phase 2, "1 raster line before"); the board read them as the line began,
from the moved table (X 383, garbage names), and drew none. Phases 1 and 2 now run
when the line before begins. VDP item 25; knobs `sprite_transparent`, `sprite_scan`.

**The shadow scene (9,750-10,125; Aubrey's "gray box missing a kind of gray").**
Wherever the two differed, the board had a colour at its shadow level and BlastEm at
normal: plane B showing through transparent pixels of high-priority plane A tiles, and
the board dropped a transparent pixel's priority, so both planes looked low. Kabuto:
shadow "depends on plane tile priority, even when all plane tile pixels are
transparent". `_tile_row` now keeps palette and priority on every pixel; all 16
sampled frames went from 0-95% to identical. VDP item 26; knob `plane_transparent`.

**Left alone: 9,225-9,725, 150-190 pixels a frame.** One CRAM write every fourth line
(3, 7, 11, ...), exactly the rows that differ, about 3 pixels a row: where a mid-line
colour change starts. The board's place for that was measured on Nuked-MD (item 20);
by the tier rule the die outranks BlastEm, so the board keeps it.

**The arcade (12,154-12,960; Aubrey saw it stop advancing).** Not a hang: the board
fell behind from 12,200, by 330 frames at 13,300, and drew black bands and missing
monitors. A profile of one frame put 87% of the 68000's time in `tst.w $FF079A / beq`
at $39FBE4: the main loop waits for a flag the last of a chain of H interrupt handlers
(vector $FF069A, each loading the next and the next register 10 value) sets. The
board's own register log gave the reason: $7C (V30) at line 207 and $74 (V28) at 225,
every frame, which is Kabuto's border trick. The board latched V28/V30 at the blanking
line, so the border started anyway and the H interrupt counter was reloaded through
it; the chain fell out of step and the loop ran every 1.4 frames. The border is now
tested against the live mode (VDP item 27). The demo keeps pace (2-6 frames of
BlastEm, as everywhere else) and all 32 sampled frames of the scene, 240 lines each,
equal BlastEm's (rows 0-231 against its 8-239: its libretro frame keeps an 8-line top
border even with the border open).

Two wrong turns. Making V28/V30 live everywhere, the counter's jump included, and the
HV captures test failed in 26 dumps -- but the cause was `run_hv_captures.py`, which
places the VDP at a fitted line and did not set the new `border`; with that set, they
are unchanged to the word. The counter's sequence was put back on the latched mode
anyway, since only the border has a source, and a sentence claiming the captures ruled
the live jump out was caught before it was committed: the live jump was never run
with the harness fixed. And `pkill -f` / a `grep` over process command lines killed
their own shell twice (the trap memory already names): kill by the process's own PID.

### Aubrey's other questions

- **"Crashed around 4:11"**: the board runs to 13,500 without an exception, FM off,
  and FM on gives the same frames through the arcade intro (11,500-12,400). What was
  seen was the arcade's lag.
- **The very first second**: frames 3-14 flash grey and black, six times, on both
  emulators -- the demo's own. The board's run is one frame behind BlastEm's there
  (grey on 4, 6, ... 14 against 3, 5, ... 13), the same frame of offset as through the
  whole demo; every frame from 15 on is identical after that offset.
- **The pong screen's static**: seeded from memory nothing initialised. Filling work
  RAM and Z80 RAM with $FF or random bytes changes exactly the static frames (550-575,
  785-810, 1,135-1,160) and nothing else in the first 1,200. Both emulators zero RAM
  and still differ, so something else nothing initialised feeds it too (the CPU
  registers at reset are one candidate); on a console it differs at every power-on,
  so there is no picture to match. Session 9's "CRAM values differ from frame 548"
  (to 11,083) is probably the same source, but only the first 1,200 frames were
  tested here; the pictures after them match, so those values do not show.

### The frontend's frame counter

The title bar now carries `frame N (m:ss)`: the VDP's count since power-on (a reset
leaves it), which is the number every tool here calls a frame, and the emulated time
at the machine's rate. `tests/test_frontend.py` checks the format.

### Checks

`tests/test_sprite_line_buffer.py`: five tests, four of which fail on the code before
(the fifth, V28's border at line 224, holds on both). Both suites **140 passed, 1
deselected** (PyPy 211 s, CPython 481 s), ruff clean; the HV captures equal their
baseline in every dump.

### The tools, for anyone chasing the rest

The method of this session is now in the repository, each checked to reproduce the
scratch work it came from (same pictures to the pixel, same scores):

```text
sh validation/build_blastem_libretro.sh                  # the pinned BlastEm, unchanged, as a libretro core
python scripts/blastem_frames.py reference/build-blastem-libretro/blastem_libretro.so ROM OUT_B --range 7600:9200:25 [--states]
python scripts/board_frames.py ROM OUT_M --range 7600:9200:25
python scripts/compare_frames.py OUT_M OUT_B --pairs     # best of BlastEm's n-6..n+6, levels and border put right
```

`--pairs` says what kind of fault a frame has: k against 2k is a shade fault
(shadow against normal), matched pairs both ways a picture partly a frame out of step.
For BlastEm's frames around each board frame, ask for n-6..n+6 (`--frames`). BlastEm's
own VDP state at a frame comes from `--states` (`vdp_serialize`: VRAM, CRAM, VSRAM,
sprite cache, registers); its event log cannot carry bit 16 of a VRAM address or a copy
DMA (`scripts/vdp_streams.py`).

What is open in Overdrive 2 at the end of this session, every 25th frame to 13,500
(477 of 540 identical):

| frames | what | where it stands |
| --- | --- | --- |
| 550-1,150 (4) | the pong screen's static | uninitialised memory; nothing to match |
| 5,675-6,525 (4) | 1-4 pixels each | not looked at |
| 7,700-8,075 (7) | parts of the picture a frame out of step (grid scroll against objects) | pacing, not drawing (below) |
| 8,150-9,125 (25) | 16-517 pixels, the B-masked debug-register scenes | not started |
| 9,175-9,725 (23) | where a mid-line CRAM change starts | kept: die-measured (item 20) |

### 7,650-7,760: the 3D console is paced differently, not drawn differently

Every frame of 7,650-8,100 was scored on both (`compare_frames.py`): the frames that
differ come about every fourth frame (7,666, 7,670, 7,674, ...), about 6,500 pixels
each, with the frames between identical. In a differing frame the rows above and
below the rotating 3D Mega Drive are BlastEm's frame n - 2, as everywhere in this
stretch, and the object's rows match none of BlastEm's frames: the grid comes from
one frame and the object from another.

The object is rendered in software by the 68000 and swapped in when an image is done,
a new image every 3-5 frames. Labelling each frame by the object's own pixels (the
grey ones) on both:

| images | 1-3 | 4-10 | 11-20 | 21-25 | 26-27 |
| --- | --- | --- | --- | --- | --- |
| board's first frame after BlastEm's | 2 | 3 | 4 | 3 | 2 |

against a constant 2 for the grid, and both emulators take 106 frames for images 0 to
26. So the board renders the object at BlastEm's average rate and only the phase of
the image swaps drifts, by up to two frames and back: an image that finishes close
to a frame's end lands on the other side of it. That is a sub-frame difference in how
much 68000 time a frame gives (bus stalls: the work-RAM refresh rule is the open item
most likely to move it), and there is no judge for it here -- BlastEm's 68000 timing
is an approximation, and Nuked-MD cannot run Overdrive 2 to frame 7,650 (it does not
get past its first frame, and at 0.24 fps would take nine hours). Left open; not
worth changing the board to follow a detector.

**7,930-8,090, the same, bigger.** Labelled the same way (the grey pixels of the
console and the TITAN sign), 50 of the board's 51 object images are BlastEm's images
exactly (the 51st is one transitional frame at 8,088), but the board falls behind
BlastEm by 4, 3, 5, 8 and 4 frames at images 1, 10, 20, 30 and 40: images 19-30 take
it 48 frames against BlastEm's 45, some 6% less 68000 work a frame in this stretch,
and it catches up at the scene's end. Still pacing, not drawing, but a throughput
difference rather than a phase: what takes the 68000's time here (refresh stalls, DMA
contention) is modelled differently on the two, and BlastEm is not a judge of it. Open,
with the same lack of a judge; a cycle profile of one image on each side would be the
next step if it is taken up.

### Overdrive 2 on Nuked-MD: a reset after the first run

Session 11 found the demo stops on the referee after its first frame. Nuked-MD's own
notes say why: "The Overdrive 2 demo has an issue loading Z80 microcode and requires
pressing Reset after the first run (behavior is similar on real hardware)". The driver
gained `NMD_RESET=<t0>:<t1>`, which holds the console's reset button as Nuked-MD's
frontend does (`md.wres` and the arbiter's `ext_wres` low, `controller.c` F12).

Traced (68000 bus cycles, PAL): with no reset the 68000 waits at $2F0240 until about
frame 15, moves on, and by frame 15-19 makes no bus cycle at all (the last are reads
of a table at $398A34 and the VDP status) -- the first run's freeze. A press at frames
4-9, before it, changes nothing. A press at frames 25-30, after it, is a real reset
(the reset vector is read at frame 25), and the demo then starts again and keeps
running: the 68000 is still busy at frame 100, and the captured pictures from frame 60
show the opening -- the ship fading in through its green, cyan, yellow and grey tints
-- as BlastEm draws it at frames 30-67. So Overdrive 2 is now a referee run, at about
25 frames of offset for the reboot. Measured speed on the shared machine: 100 frames
in 13 minutes (0.13 frames a second, with the romset sweep running beside it).

The board does not freeze on the first run, and the note says real hardware does;
what the board does differently in the Z80 start-up is a question for later.

### One referee run for everything

Runs of the die model are expensive (0.27 frames a second with the machine free), so
the whole demo is being run once, recording what any later question could need. The
driver gained, each checked on a 60-frame run:

- `NMD_PICS=<dir>`: every frame's raw capture, zlib-compressed and indexed (frame,
  offset, bytes, half-MCLK); about 11 KB a frame.
- `NMD_AUDIO=<file>`: FM left, FM right and PSG summed over each FM sample period.
- `NMD_SAVE_EVERY=<n>`: a save state every n frames, taken where md.c takes its own,
  with the driver's counters and part-drawn frame beside it; `NMD_LOAD=<file>` starts
  from one. Nuked-MD's save holds six pointers FC1004_Init aims into the chip, stale in
  another process, so the build renames savestate.c's `load_state` and the driver's
  wrapper re-aims them. A run loaded from frame 40 draws frames 40-59 bit for bit as
  the run that saved it did, at the same half-MCLK. (Its sound could not be checked
  there: the demo is silent until later.)
- `NMD_PLOT_LIST=<spec>`: plot a list of frames.

The run (`reference/nuked-od2/launch.sh`): to die frame 20,400, reset held at frames
25-30 (after the first run's freeze), every frame's picture, the sound, a save every
250 frames, and a 68000 bus trace over die frames 12,230-12,260 (the arcade's V
interrupt). Die frames are the board's frames plus about 25 for the reboot; the board
can press its own reset at frame 25 to line the two up.

**The board lined up with the referee.** `scripts/board_frames.py --reset-mclk
26761500` presses the board's reset button at the master clock the referee's was
pressed at (PAL frame 25). After it, board frame n is the referee's frame n - 1, pixel
for pixel: 62/61, 70/69, 78/77, 86/85 all 71,680 of 71,680 through the opening fade,
where each frame differs from its neighbours (the one frame is where the two count
their first). The aligned board run (`reference/od2-board-aligned`, every frame to
20,400, FM on, the chips' own samples in `fm.bin` and `psg.bin`, a snapshot every
1,000 frames) is the board half of the whole-demo comparison.

### The one-frame slip at 7,047, and the Z80 from the reset

Against the referee (frames 1-9,897 compared, `scripts/compare_referee.py`), 7,155
frames are identical at the aligned offset and 8,686 within two frames. From frame
7,051 the board is one frame late. Traced cycle by cycle from the referee's save at
frame 7,000:

1. Both write the same VDP stream until a pass at frame 7,051 where the demo waits for
   the V counter to read $DF. The board enters the wait at line $DD; the die after
   $DF has passed, so it waits through the blank for line $1DF, 57 lines late.
2. The die's 68000 is late because over ~33 lines before it the board ran the same
   decryption loop (move/eor/move, ROM to RAM) ~17% faster: the die's Z80 made 332 bank
   reads in the stretch (each costing the 68000 ~11 cycles, which the board's arbiter
   charges too: 10.57), the board's Z80 only 54, from another place in the ROM. The
   two Z80 sound drivers were at different points of their playback; the FM recordings
   of the whole demo correlate at only 0.4.
3. From the reset, the two Z80s load the same 7,440-byte program at the same times and
   then run identically for 187 reads, until a loop that polls the YM2612 for timer
   A's overflow. The board's flag rose 617 MCLK before the die's.
4. The cause: the FM's sample phase after a reset. Nuked-MD's FM resets its prescaler
   and slot counter only for 12 FM clocks after /IC is asserted (`FM_Prescaler`'s
   `ic_check`) and then runs on under /IC; its slot-23 marker keeps its 1,008-MCLK
   period through a reset and moves by 2 cycles when /IC is pulsed. The board froze
   its counter under /IC and restarted it at release. It now restarts it at the
   assertion, holds it 2 cycles, and lets it run (`ym2612.IC_FSM_HOLD`); release
   resets nothing. Timer A's first flag after the reset is now 113 MCLK from the die's
   (was 617), and the Z80s run identically for 398 reads (was 187).

What looked left there -- the board's timer-A flag "jittering" by up to 3 cycles -- was
the probe's own artifact, not the board's: `YM2612.run` advances its clock to the
catch-up target before running the cycles, so a probe reading `mclk` inside `cycle`
stamps every cycle of a catch-up with the same time. Timed cycle by cycle the board's
flag keeps the die's 4,032-MCLK period. The next section is what the difference was.

Tools: the driver's `NMD_FMSIG` logs the FM's BUSY latch, timer flags, slot-23 marker,
timer clock and /IC; `tests/test_sprite_line_buffer.py` holds the counter rule. Both
suites pass (140 before this test).

### The access-timing table, and what the slip turned into

Aubrey's decision: adopt z80-python's access-timing table. What that and the chase
behind it found, in order.

1. **The table.** z80-python `b62891b` (PR #19, merged, not yet released; pinned by
   revision in `pyproject.toml`) gives, for an instruction's bytes, each memory access
   with its T-state (`z80_python.timing.instruction_timing`). The system looks the Z80's
   next instruction up before stepping it (`System._z80_offsets`, cached by its first
   four bytes, read with `Board.z80_peek`, which has no side effects), and the board
   places each FM, VDP and bank-window device access at the step's start plus that
   offset plus what the Z80 has already been held (`Board._z80_access_time`). Where the
   Z80 takes an interrupt that step the table does not apply and the access stays at
   the step's start; an interrupt merely requested, with IFF1 clear or in EI's shadow,
   does not count (z80-python's own acceptance test, `iff1` and `_ei_delay`). Identical
   Z80 reads against the die from the reset: 398 to 1,150.
2. **Not the arbiter.** The bank-access delay (`Board._bank_delay`) still places the
   access at the step's start. The arbiter reads the 68000's bus cycles only as far as
   the Z80 has been caught up to; an access later in the instruction lands past them on
   a bus that looks idle, and MoD's spinning-68000 experiment then costs the Z80
   exactly what a stopped 68000 does (682 wait states in 300 tries both ways;
   `tests/test_milestone5.py` caught it). Placing the arbiter's accesses too needs the
   Z80's catch-up to stop short of an instruction whose accesses would cross the
   68000's clock; that, and with it undocumented-behavior open item 14, is next.
3. **The 1,150 rested on two errors cancelling.** Before the Z80 starts, the 68000
   copies its sound driver into Z80 RAM a byte at a time (7,357 writes over 1.2M
   MCLK), and on the board it released the bus 252 MCLK before the die did. The die's
   write-to-write spacings are 161, 168 or 175 MCLK where the board's are 161 or 175:
   the cartridge refresh falling on a write to Z80 space holds it one 68000 clock, not
   the cartridge's two and not the board's none. Of the 519 refreshes that fell on
   those writes, 392 due a clock or more before /AS held it 7 MCLK and 127 due within
   the last clock held it not at all, though they were used up (the next refresh came
   1,785 MCLK later) -- every one. `board.Z80_REFRESH_STALL`, `Z80_REFRESH_LATE`;
   undocumented-behavior "The bus" item 7. The bus is now released 7 MCLK from the
   die's.
4. **The FM's /IC: a reset or a pause, by where in its slot it lands.** With the Z80
   now on time, the timer flag was early by 239 MCLK. Nuked-MD's fm.c, clocked the way
   `fc1004.c` clocks it (scratch harness, not kept: the die's own code driven through
   every position), resets the slot counter only if the assertion is sampled 1-3 FM
   clocks into a slot; at 0, 4 or 5 clocks the counter keeps its count and resumes,
   after the 12-clock stop, at the slot under way two clocks after the assertion. The
   Overdrive 2 run's three assertions agree: the console reset (2.6 clocks in) reset
   it, the first run's release-and-reassert (5.1 clocks in) paused it 11 clocks, the
   final one (0.1 clocks in, sampled at the next) paused it 12. `ym2612.
   IC_PAUSE_POSITIONS`; the rule the board had, always a reset, is item 4 of the
   section above.
5. **Why the reads still part.** The FM's phase at the Z80's final /IC depends on the
   last resetting assertion, the console reset, and on everything the 68000 does
   between them. The die's reset button reaches VRES and ZRES 80,496 MCLK after the
   driver presses it (the board's `press_reset` is immediate), and before the reset the
   two runs differ outright (the die freezes on its first run), so the board cannot be
   lined up with the die to 7 MCLK from outside. Forcing the board's slot counter at
   the final /IC through all 24 slots and seven grid offsets finds the die's phase
   between two of the board's cycles: slot 18 with the grid 12-17 MCLK late gives
   3,507 identical reads, where the best whole-cycle phase gives 1,150.
6. **And what stops those at 3,507**: the 68000's boot runs out of step with the VDP.
   From its precise vblank polls after the Z80 starts (every 210 MCLK), the die's VDP
   runs 240,277 MCLK after the board's; the Z80 release, 235,880. The difference comes
   from a start-up loop that reads CRAM back 64 words at a time in H40 with the
   display on, re-checking vblank every ~20,500 MCLK: the board runs each pass 300-900
   MCLK fast, so it leaves on a different pass. Per line the die stalls two reads, one
   served at the hblank slot near $1CE and one near $002; the board stalls the same
   pair in most lines, but after a read at 3,316 MCLK into a line it still catches an
   end-of-line slot ($140/$142), where the die, from any read at 3,300-3,334, never
   does (about 50 lines, three exceptions). That is a read-prefetch slot rule in H40
   active lines, not yet settled: whether the die's end-of-line slots sit earlier, or
   its prefetch needs more lead time, the data here does not say. The next thing to
   settle.

Also seen: after a reset release with a bus request already pending, the die's Z80
fetches its first opcode before granting the bus; the board's grants first.

Checks: both suites pass with z80-python `b62891b` installed (142 each, CPython 3.14 and
PyPy 3.11). The milestone-1 replay, re-recorded (`scripts/mame_trace.py genesis GAME.zip
--seconds 30`, MAME 0.285) and re-run on PyPy: all six games 0 unexplained, with the same
instruction counts as before (Sonic 12,728,465 Z80 instructions, Streets of Rage 2
11,269,553, Gunstar Heroes 15,425,681, Castlevania 8,460,818, Sonic 3 11,837,738 with
1,688 interrupts, Phantasy Star IV 13,280,703). New tests: the Z80-space refresh stretch
(`tests/test_board.py`), the /IC reset and pause (`tests/test_sprite_line_buffer.py`).

### The Z80 after a hold, and a read-ahead rule that the hardware test refused

Chasing the reads further, with the FM phase still forced (item 5 above) so that each
change is judged on the Z80 reads it matches, from 3,507:

1. **When the Z80 takes up its bus again** (Nuked-MD, all 83 bus releases and both
   reset releases after the power-on reset). Its clock runs on through a hold: every
   access falls on one phase of the 15-MCLK clock, 6 MCLK past a multiple of 105 on
   both machines. After the write that releases the bus, its next access comes on the
   first such edge 42 MCLK or more after the write began (42-56 measured); after a
   reset release, 57 or more. A reset released with the bus already requested still
   makes its first opcode fetch before letting go. And an instruction the request
   falls inside keeps the machine cycle under way and resumes with its later accesses
   moved, where the board used to rerun the whole instruction from the start.
   `system.Z80_CLOCK_PHASE`, `Z80_RESUME_MCLK`, `Z80_RESET_RESUME_MCLK`,
   `System._z80_resume`, `Board.z80_hold`. 3,507 to 6,305.
2. **The Z80's VDP accesses are arbitrated.** Its 3,095 reads of the VDP ($7F08, the
   V counter) in the run are held 60, 75 or 90 MCLK, the bank window's three lengths;
   its RAM and FM reads take 30. They go over the 68000's bus, so they take the bank
   window's delay (`board.Z80_VDP_ARBITRATED`). 6,305 to 6,970.
3. **The arbiter sees the access where it falls.** With the delay at the access's own
   time (item 2 of the section above undone), the Z80 is no longer stepped into an
   instruction whose accesses would fall past the 68000's clock: it waits for the
   next catch-up (`System.catch_up_z80`). MoD's experiment is back to its shape
   (stopped 2-3 wait states, spinning mostly 3 with 2-4, the 68000's share 10-12),
   and the reads against the die reach 7,435.

**The read-ahead.** `scripts/run_read_slots.py` (new) sweeps bursts and pairs of CRAM
reads across H40 active lines on Nuked-MD and the board. What the die showed: a read
that waits for the buffer ends 42-48 MCLK later than the board's, at every slot; a
fill needs the read that emptied the buffer to have begun about 21 MCLK before the
slot; and the slot at H $142 fills no read at all. VSRAM reads behave the same; VRAM
reads miss $140 too. Modelled as three rules, they reproduced the die's map exactly --
and failed **VDPFIFOTesting's "FIFO Wait States"**, which passes on hardware
(Nemesis) and, run here on Nuked-MD from the same ROM, on the die too. Each rule
removed alone still failed; the test passes only with the release delay and the $142
rule both gone. A hardware-verified test outranks a model fitted to die traces, and
the die's own pass says the three rules are not how it works, only what it looks like
from outside. **Not adopted**; the measurements and the script stay. What the die's
read-ahead really is, is the next thing to find there: its VDP's read path, read from
`vdp.c` itself rather than fitted from bus traces.

Also tried, not adopted: the die's longer DMA start-up (item 15 of the open items);
with 100 MCLK the best forced phase reached 7,835 reads, but the hardware source
(Kabuto's 5.6 cycles) still judges that question.

Checks: both suites pass (143, a test for the resume rule added); the six-game MAME
lockstep still 0 unexplained. The mini sweep against `megadrive-branch.csv`: 3 better
(Arkagis Revolution - Input Diagnostics from a lock-up to `same`, Dynamite Headdy and
Ristar from `phase` to `same`), 3 worse (After Burner II 2,531 to 2,841 differing
pixels, Ultraman 4,673 to 4,801, Windjammers 2,243 to 2,244), all still to triage.

### Triage of the three worse carts

Each of this round's three rules taken out in turn (scratch copies of `src/`, the
sweep run against each):

| cart | all rules | no VDP arbitration | no arbiter horizon | no resume | none of them | branch sweep |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| After Burner II (USA, Europe) | 2,841 | 2,527 | 2,841 | 2,841 | 2,527 | 2,531 |
| Ultraman (Japan) | 4,801 | 4,801 | 4,733 | 4,770 | 4,814 | 4,673 |
| Windjammers (Proto) | 2,244 | 2,243 | 2,243 | 2,243 | 2,243 | 2,243 |

After Burner II moves with the Z80's VDP arbitration alone. Its title screen scatters
star balls at random positions over a fixed logo, and blinks "PUSH START BUTTON": the
logo and text match MAME's in every variant, the balls and the blink phase do not.
Ultraman's title draws its logo with a random pixel dissolve; every differing pixel is
inside the logo, and the count moves under every variant, the old rules included. Both
are random effects whose seed follows timing, so a count moving is not a fault in the
picture; Windjammers moves by one pixel. None is a regression. MAME can't judge them
and a die run of 600 frames from power-on (about 40 minutes) would not start in step
with the board either (the first-run freeze, Session 13). `tests/mini_sweep.txt`
notes them so a later move is read the same way.

### What "FIFO Wait States" can and cannot judge

The read-ahead rules above were set aside because they failed VDPFIFOTesting's "FIFO
Wait States". Traced on both machines from the ROM itself (die: Nuked-MD, frames
56-170 with Start pressed at 55, as the harness does), sub-test 8 -- six data writes
fill the FIFO, the long write `$00008F01` then sets up a VRAM read, and 16 status reads
look for a full FIFO -- loops until it has seen full or 2,048 times:

| machine | iterations | iterations with a full sample |
| --- | ---: | ---: |
| Nuked-MD | 812 | 1 (the last; the sixth write held 74 MCLK) |
| board, no read-ahead rules | 1 | 1 |
| board, with the rules | 2,049 (the limit) | 0 |

Both machines spread the iterations over the whole line (34-35 bins of 100 MCLK), so
neither is phase-locked. At the commonest timing (413 MCLK from the sixth write's end
to the first status read) the die reads full in 0 of 281 iterations and the board
without the rules read full at once. So the board's pass of this test was partly luck,
and on the die the test passes about once per 800 iterations: it cannot discriminate
between read-ahead models, and Nemesis's own note that data-port tests during
transfers "fail occasionally" on hardware fits. The rules stay out anyway, on the
other count: they did not move the Overdrive 2 comparison (3,507 identical reads with
and without them). Scripts for this: the scratch `fifo8.py`/`vft8.py` harnesses were
not kept; `scripts/run_read_slots.py` holds the measurement that matters.

### The arbiter, measured

The per-read Z80 wait mismatches (15 MCLK, now and then, on its reads of the VDP) came
from Kabuto's footnote-1 arithmetic, which the board had used since milestone 5. Tested
straight against the die -- every one of the 24,343 bank-window and VDP accesses in the
Overdrive 2 trace, predicted from the die's own 68000 bus cycles around it -- it gives
the die's wait for 76.8 % of them. What the die does, read off its bus (the Z80's own
cycle on the 68000's bus is in the trace, marked as not driven by the 68000):

1. A 68000 cycle that begins up to 21 MCLK after the Z80's /MREQ still goes first; none
   that begins later does (every access in the run).
2. The Z80's own cycle begins 17 MCLK after the last of those ends, and no sooner than
   29 MCLK after /MREQ, on a 68000 clock edge. The grants 7 or 14 MCLK later than
   17, which had looked unexplained, are this floor and edge.
3. It ends on the first Z80 clock edge at /MREQ + 37 + 15w that leaves it at least 30
   MCLK: 67, 82 or 97 MCLK after /MREQ for 2, 3 or 4 wait states, every access.
4. The 68000's next cycle waits for its first clock edge 25 MCLK after that.
5. A ROM read through the bank window that meets the cartridge refresh takes it: its
   own cycle is 14 MCLK longer (Kabuto's ``b``). With the 68000 stopped a quarter of
   the Z80's reads do, one per refresh period (882-889 MCLK apart).

Rules 1-4 give the die's wait for 94.3 % of the accesses; in 97.8 % the grant is right
and the rest are rule 5, which the board takes from its own refresh grid (the window
in which a refresh lands on the Z80's read rather than passing on an idle bus is not
pinned: the board takes every owed tick, and in MoD's stopped case gives 3 wait states
to half the reads where the die gives it to a quarter). `arbiter.z80_access_wait`;
Kabuto's arithmetic (`bank_access_delay`) and `Board._m68k_release` are gone. The
board also moves the 68000's later cycles in its step by the delay each access costs
it, so the cycle the 68000 resumes with is on the bus when the Z80's next access comes.

Against MoD (`scripts/run_nuked_md.py arbiter`, hardware-captured, with the die beside
it): the 68000 stopped, 2-3 wait states ("2-3"; the die 2.25, the board 2.49); in
`jmp (a0)`, 2-3 with mean 2.86 (MoD 2-4, "about 2.93"; die 2.74); the `pop`'s second
read, mean about 2.2 where MoD and the die have 3.5-3.6 -- no better than Kabuto's
arithmetic was (2.04), and still open.

On Overdrive 2, every Z80 read of the VDP now waits what the die's does, through the
whole comparison window. The best forced-phase run stops at 4,738 reads rather than
7,435, and the reason is not the arbiter: after the Z80 starts, the board's 68000
makes its periodic bus request about 4,800 MCLK earlier than the die's (the boot
offset between the two CPUs, Session 13), so the request lands on another Z80
instruction; with the old arbiter the two holds cancelled exactly (-150/+150), with
this one they miss by 15 MCLK and the timer-A poll flips. That offset is the next
thing.

Checks: both suites pass (142; Kabuto's formula test and `_m68k_release`'s test
replaced by one for `z80_access_wait`); the six-game MAME lockstep 0 unexplained (it
runs with the arbiter off); the mini sweep against the previous one: one move, Sansan
(Japan) 2,370 to 3,134 pixels at the exact frame, still `phase` -- an identical frame
remains within the window.

## Session 14 (2026-09-29): the boot offset

Merged into main (d8fdf1c); the 7,435-read state is tagged `od2-z80-7435` (6b07497)
as the regression baseline, reproducible with the forced FM phase. Aubrey's brief: find
the first point where the board's 68000 gets ahead of the referee's, reporting for each
experiment the boot offset, the first divergence, the matching Z80 reads and whether
the visible frame slip moves; no new forced alignment.

**The measure.** The boot offset is the Z80's start (the 68000's second release of the
bus after the final sound reset) measured against the start of the next vblank line on
each machine: 183,964 MCLK on the referee (its line starts taken from `NMD_VDPSIG`'s
/HSYNC log, `reference/boot-frame`, and the H counter it logs), 179,782 on the board.
**Board minus referee: +4,182 MCLK**, the board's 68000 late against its own VDP.

**Where it comes from.** `scripts/od2_boot_drift.py` (new) lines up the two 68000s'
bus cycles by address and kind and prints where the time difference steps. From the
last vblank edge before the Z80's start through the start itself the two stay within
about 100 MCLK -- but that edge is found by a loop that polls vblank only once per
pass of a CRAM readback (every ~20,500 MCLK), so where each machine leaves it depends
on how long its passes take, and the board's run 300-900 MCLK short (the read-ahead,
Session 13's "The access-timing table" item 6). After the Z80's start the next vblank
wait then ends 4,074 MCLK early on the board: that is the offset, surfacing.

| experiment | boot offset | matching Z80 reads, unforced | first divergence | frames 6,981-7,100 at the aligned offset |
| --- | ---: | ---: | --- | ---: |
| main (d8fdf1c) | +4,154 | 197 | timer-A poll (FM phase) | 22 / 120; late from 6,981 |
| + the three read-ahead rules | **-57** | **4,738** | 15-30 MCLK per bus hold | **111 / 120; late from 7,095** |
| + cartridge refresh restarted at the reset button | -130 | 1,150 (best of 25 reset clocks) | -- | not run |

All unforced, the reset button pressed with ZRES at the die-aligned clock (26,601,687,
found by stepping 7 MCLK at a time; 26,601,680 follows from the VDP offset 240,316).
With the read-ahead rules the unforced run reaches what the forced one reached with the
same arbiter: the boot no longer needs the FM phase forced. What remains at 4,738: at
each of the 68000's bus requests the board holds the Z80 15-30 MCLK longer, and the
drift tool shows why: the board takes work-RAM and cartridge refreshes on cycles where
the die takes none, so the two refresh schedules are out of phase after the reset. The
third row tried the one explanation the die suggested -- its refresh ticks sit where
its reset pulse began, modulo 896 -- and it made every clock worse; not adopted.

**The read-ahead rules and the hardware gate.** They are the same three rules set
aside in Session 13 (the read waits 45 MCLK after the slot that fills it, a fill needs
the read that emptied the buffer begun 21 MCLK before the slot, H40's $142 fills no
read), and with them VDPFIFOTesting's "FIFO Wait States" fails on the board every time.
On the die that test passes about once per 800 iterations ("What FIFO Wait States can
and cannot judge"). Adopting them means moving that test to the intermittent list the
harness already keeps for Nemesis's "fail occasionally" tests, which is Aubrey's call.
**Aubrey adopted them.** `vdp.READ_RELEASE_MCLK`, `READ_FILL_LEAD_MCLK` and the $142 rule
are in `src/`; "FIFO Wait States" is in `run_vdpfifotesting.INTERMITTENT` with the die
evidence, and the test exempts the intermittent ones; a new test holds the board's
read slots to the die's measured windows (`tests/test_milestone5.py`); VDP item 28.
Checks: both suites pass (143); MAME lockstep six games 0 unexplained; the mini sweep
against the previous one: Gauntlet IV (USA, Europe) (En,Ja) from `differs` to `phase`,
nothing worse.
