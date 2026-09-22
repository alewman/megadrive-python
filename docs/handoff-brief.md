# Handoff: build `megadrive-python`, the Mega Drive / Genesis board

## Context you are inheriting

This is the family's second console and its first 16-bit one. It is a board on
two finished cores:

- **m68000-python** (`/data/emu/m68000-python`, private GitHub
  alewman/m68000-python): the 68000, 317,500/317,500 on the SingleStepTests
  corpus, 28.2 million instructions of Genesis Altered Beast identical to MAME,
  a claim boundary in its `docs/claims.md`. About 20 million instructions a
  second on PyPy; the Mega Drive needs about one million.
- **z80-python** 0.4.x (PyPI): the Z80, as sms-python embeds it.

and it inherits a finished sibling board, **sms-python**
(`/data/emu/sms-python`): the Sega PSG, a Mode 4 renderer, the MAME lockstep,
compatibility sweep and triage tools, and a desktop frontend. sms-python's
`docs/validation.md` shows what "done" looks like for a board: every
difference from MAME traced to a MAME artefact or a documented open question.

Read, in order, before writing code:

1. `README.md`: scope and what is fetched.
2. `docs/start-here.md`: the machine, memory maps, the VDP, sound, I/O.
3. `docs/timing.md`: the clock tree, the counters, access slots, DMA, the bus.
4. `docs/undocumented-behavior.md`: revisions, quirks, known-wrong sources, open items.
5. `docs/validation.md`: every oracle with its tier and pin.
6. `docs/mame-oracle.md`: the two-run trace recipe and its traps.
7. m68000-python's `README.md`, `docs/claims.md` and `src/m68000_python/cpu.py`;
   sms-python's `src/sms_python/system.py` and `board.py` (the pattern to follow).

Then run `python scripts/fetch_references.py --verify` (or without `--verify`
on a fresh clone; add `--large` for the recordings when you reach sound).

## Prerequisites and decisions already made

- **Depend on m68000-python by git revision** (it is not on PyPI) and on
  z80-python ≥ 0.4.0 from PyPI, through their callables contracts. Pin the
  m68000-python revision in `pyproject.toml`, not a local path.
- **Python floor 3.11**, CPython and PyPy both, in CI (the newest Python PyPy
  supports). **PyPy is the platform**: sms-python runs its Master System at
  about 200 frames a second on PyPy and 22 on CPython.
- **The core stays dependency-free.** A frontend is an optional extra, as in
  sms-python (pygame-ce, which has PyPy wheels for Linux, Windows and macOS).
- **Do not modify m68000-python or z80-python from here.** Propose changes as
  PRs there. One is foreseeable: the board needs to know *where in an
  instruction* each bus access falls, to stall the 68000 for a full VDP FIFO or
  an arbiter delay at the right clock. m68000-python already counts clocks as
  it goes (`_cycles`, 4 per access, in `_core.py`); a read-only public accessor
  for "clocks spent so far in this step", readable from inside a bus callback,
  is the small change to propose. Until it lands, reading the private
  attribute behind one function in the board is acceptable, labelled.
- **Reuse sms-python by copying its design, not by importing it.** The PSG, the
  sweep and triage tools, the frontend: copy and adapt (same author, MIT), and
  say where each came from. A dependency on another board would couple two
  boards' release cycles for a few hundred lines.
- **Die-derived models are judges where nothing hardware covers the question**
  ([validation.md](validation.md), "The tier rule"). This is a decision the
  user may revisit; do not overstate it in claims.

## Your task

Build the board as `src/megadrive_python/`. The requirements:

- **Machines as data.** Model 1 (with and without TMSS; discrete YM2612),
  Model 2 (ASIC FM), Genesis 3, PAL and Japanese variants: a VDP variant, an
  FM variant, a region and version register, TMSS on or off, the audio-path
  model. The quirks in `docs/undocumented-behavior.md` are fields with a
  comment naming their source and tier.
- **One scheduler in master clocks.** The 68000 steps (`clocks = cpu.step()`,
  7 MCLK each), the Z80 interleaves (15 MCLK each), the VDP, PSG and YM2612
  catch up to the current MCLK when touched and at line boundaries. Stalls (a
  full FIFO, a 68000-memory DMA, the arbiter's delays, the refresh steal) add
  clocks to the CPU that suffers them. Choose, and write down why.
- **Cartridges from MAME's software list** (`megadriv.xml`: SRAM, EEPROM,
  mappers) read at runtime, as sms-python reads its lists.
- **Tooling at the family bar**: whole-machine state save and restore, a
  debugger with board watchpoints (VDP registers and memories, the Z80 bus, the
  YM2612), traces in the cores' schemas plus device events, `py.typed`, and
  `python -m megadrive_python CART --frames N --png out.png --wav out.wav`.

### Milestones, each with its acceptance test, in oracle-tier order

1. **The board and both CPUs.** The 68000 and Z80 memory maps, the bus
   request and reset registers, the bank register and window, the I/O chip's
   registers and version register, work RAM, cartridge ROM, open-bus reads
   (the prefetched opcode), the byte/word rules of each device; the VDP and FM
   as register stubs. **Acceptance:** a two-CPU lockstep against
   `scripts/mame_trace.py` runs (both logs merged by master clock), device
   reads replayed from the logs and every write checked, on at least five games
   for at least 30 seconds each, N stated; the first divergence diagnosed to a
   source before the run is extended.
2. **The VDP's ports, FIFO, DMA, counters and interrupts** (no pixels yet).
   First decode Nemesis's raw logic-analyser dumps
   (`sr logic analyser results.zip`, with the capture ROMs' source) into
   per-mode tables of H counter, V counter and status against time; that gives
   the H counter's MCLK positions `timing.md` leaves open. **Acceptance:** the
   board's HV counter and status register equal the captures sample for sample
   in every mode captured (hardware-captured); **VDPFIFOTesting** passes every
   test Nemesis reports passing on hardware, the intermittent ones are listed
   with the result the board gives, and the Genesis 3 variant fails the VSRAM
   tests it fails on hardware. Read its results from VRAM the way sms-python's
   `scripts/run_vdptest.py` reads FluBBa's.
3. **The VDP's picture.** Planes A and B with every scroll mode, the window,
   sprites (the cache, the four phases, the per-line and per-pixel limits,
   masking, the X = 0 rule), priority, shadow/highlight, H32/H40, V28/V30,
   both interlace modes, 128 KB mode. **Acceptance:** Nemesis's sprite masking
   ROM gives the MD1600 results in his chart (hardware-corrected); r57shell's
   VDP tests match the MD2 report in t=1585; CMD's window and colour tests match
   his capture photos; `gen_test_z80_bus` shows the results MoD reports; MAME's
   exit frames compared on the lockstep games as a detector only.
4. **Sound: the PSG and the YM2612.** The PSG after sms-python's (judged by
   Nuked-PSG). The YM2612: all six channels, channel 3's special mode, CSM,
   SSG-EG, the LFO, both timers, BUSY (Eke's measurements), the DAC, and the
   ladder effect as a variant field. **Acceptance:** for register streams from
   Nemesis's test ROMs and from five games, the FM output equals Nuked-OPN2's
   (YM3438 variant) and YM2608-LLE's YM2612 path sample for sample at the
   chip's native rate, or every difference is explained; Nemesis's SSG-EG test
   against his MD1600 recording by measurement; the MDFourier signal from the
   240p Suite against the recordings as a detector of the analogue model.
   Record the YM2612's cost per emulated second on PyPy: it may dominate.
5. **Bus timing.** The arbiter's delays for Z80 bank accesses (Kabuto; MoD's
   logic analyser), the refresh steal, VDP stalls and DMA speed by the slot
   table. **Acceptance:** VDPFIFOTesting's timing tests; Kabuto's DMA-cost
   formulas reproduced within their stated form; Overdrive 2 runs to its end
   with its effects intact and its music at the right speed (a stress case:
   compare with Kabuto's notes, not pass/fail).
6. **Nuked-MD as a referee** (strongly recommended). Build it locally from
   `reference/src/Nuked-MD` (GPL-2.0: never copy into this repository), headless,
   and teach it to write a bus-level trace. **Acceptance:** for the test ROMs of
   milestones 2–5, the board's bus and device timing agrees with Nuked-MD's, or
   each difference is sorted into a named cause. Mind that Nuked-MD models the
   FC1004 with NMOS CPUs, a combination closest to a Model 1 VA7.
7. **Cartridges and peripherals.** Battery SRAM, the EEPROM games, the SSF2
   mapper, the 3- and 6-button pads (Terakawa's timing), TMSS with the boot
   ROM, the region and version register. **Acceptance:** each against its
   research document and a game that uses it, and TMSS booting the local
   No-Intro boot ROM to a licensed cartridge.
8. **Compatibility sweep (detector).** Every cartridge in
   `/data/emu/roms/megadrive` against MAME's frame after N seconds, then every
   difference triaged in lockstep, as sms-python did (copy its
   `compat_sweep.py` and `triage_diffs.py`). Nothing is changed to match MAME
   without a higher-tier source.
9. **A desktop frontend**, after sms-python's (audio-paced, vsync, pads).
10. **Optional**: Mode 4 for the Power Base Converter (after sms-python's
    renderer and Kabuto's and MoD's Mode 4 differences); the sound-driver
    stream against vgmrips logs (the user's `alewman/vgmllm` has a VGM parser,
    YM2612 register decoding and a scan of which tracks use CH3 special mode,
    SSG-EG, the LFO and the DAC: use it to pick test tracks).

Do not skip a rung. The early ones make the later ones cheap to diagnose.

## Working agreement for the first build session

- **Scope: milestones 1 and 2, then stop.** Report at the end of milestone 1
  (in the work log) and carry on; stop after milestone 2's acceptance and write
  the summary for review. Do not start milestone 3.
- **Keep `docs/worklog.md`**: what was run, when, with what command and result;
  every decision with its reason; every open question for the user, marked
  `QUESTION:`. The reviewing session reads it first.
- **The environment.** PyPy 3.11 is `~/.local/bin/pypy3.11`; make `.venv-pypy`
  and a CPython `.venv` in this repository. m68000-python is a private
  repository: for development install it from the local clone
  (`/data/emu/m68000-python`, at the revision you pin), and pin the git URL and
  revision in `pyproject.toml` for CI. MAME is `/usr/games/mame` (0.285); the
  romset is `/data/emu/roms/megadrive` (some entries are broken symlinks); the
  MAME ROM path is in `scripts/mame_trace.py`.
- **A shared machine.** Run long jobs under `nice`, at most 16 parallel
  processes, and delete MAME traces you no longer need (they are hundreds of
  MB); never fill `/tmp`.
- **The GitHub repository exists** (private, alewman/megadrive-python). Commit
  locally; do not push. The reviewing session reviews and pushes.
- **When in doubt about a source's meaning, quote it in the work log** and say
  how you read it. The documents here were written from the sources; if one of
  them misreads a source, fix the document in its own commit and say so.

## Constraints

- **Oracle tiers are not negotiable.** MAME, Genesis Plus GX, BlastEm,
  Exodus, jgenesis and clownmdemu are detectors; the die-derived models judge
  only where nothing hardware covers the question.
- **Never commit ROMs, BIOS images, traces, recordings, fetched documents or
  test programs.** `reference/`, `tests/test_roms/` and `tests/mame_traces/`
  are ignored. Commercial cartridges are read in place from `/data/emu/roms`.
- **Read GPL, AGPL, LGPL and non-commercial sources; never copy them.** Cite
  by file and line and write your own.
- **Pins in every claim**: core revisions, MAME version, test ROM SHA-256,
  Python versions, wall time, instruction and frame counts.
- **Do not touch other directories under `/data/emu`.** Propose core changes as
  PRs there.
- Commit in reviewable units with `git -c user.name=alewman -c
  user.email=alewman@gmail.com commit` and the `Co-Authored-By:` line for the
  model you are running as. Do not push.

## Size and speed, honestly

- **Code**: the VDP is the bulk again, larger than the SMS's (two planes, the
  window, a sprite pipeline with a cache, S/H, interlace, DMA and the FIFO):
  expect 4,000–6,000 lines. The YM2612 is 1,500–2,500. The board, arbiter,
  I/O and cartridges about 2,000; tooling as much again.
- **Speed**: per emulated second the machine runs about one million 68000
  instructions, half a million Z80 instructions, 4.3 million pixel decisions
  (320 × 224 × 60) and 53,267 FM samples of 24 operators each (1.3 million
  operator updates). The CPUs are cheap on PyPy. The VDP and the YM2612 are
  not: measure frames per second on PyPy at every milestone from 3 on, render
  per line, and keep the FM's inner loop free of per-sample allocation. Full
  speed on PyPy is a goal to measure, not a promise; on CPython it is out of
  reach and not a milestone.

## What done looks like

`README.md` states which milestones pass, with counts and pins;
`docs/validation.md` turns into the certification record; CI runs milestones
1–2 on every push with the freely licensed test ROMs; the MAME lockstep and
sweep runs are recorded locally with their command lines; every difference from
MAME in the sweep is sorted as sms-python sorted its own.

Before starting, tell the user in a few sentences how you read this brief and
what you will do first.
