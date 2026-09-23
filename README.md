# megadrive-python

A readable, pure-Python emulator of the **Sega Mega Drive / Genesis**, to be
built as a board on the family's cores:
[m68000-python](https://github.com/alewman/m68000-python) for the main CPU and
[z80-python](https://github.com/alewman/z80-python) for the sound CPU. The
cores own their instruction sets; this repository will own everything around
them: the memory maps and the bus arbiter between the two CPUs, the VDP (Sega
315-5313 / Yamaha YM7101), the YM2612 FM chip and the PSG, the I/O chip and
pads, cartridges and their mappers, TMSS, and the clock that ties them together.

**Status: milestones 1 to 7 of ten** ([docs/validation.md](docs/validation.md),
"Certification record"): both CPUs run in lockstep with MAME over six games
(119.8 M 68000 and 73.0 M Z80 instructions, every difference named); the VDP's
ports, FIFO, DMA, counters and interrupts pass VDPFIFOTesting (122/122; a Genesis 3
fails VSRAM tests as the hardware does) and reproduce 99.04 % of the words of
Nemesis's 288 HV/status capture dumps; and the picture -- both planes, the window,
sprites, priority, shadow/highlight, H32/H40, V28/V30 and both interlace modes --
gives Nemesis's sprite masking ROM the same nine results his MD1600 photographs show,
in both widths, and matches MAME's frame pixel for pixel on every static screen of the
six lockstep games. Sound is there too: the PSG and the YM2612 -- all six channels,
channel 3's special mode, CSM, SSG-EG, the LFO, both timers, BUSY, the DAC and the
ladder effect -- run the chips' own pipelines and are compared against the die-derived
models cycle by cycle: 524 million values of the YM2612's two DAC pins and its status
byte over eighteen register streams, and 80 million input clocks of the PSG's, none
different. `--wav` writes what a console would have played; the FM costs about three
seconds of wall time per emulated second on PyPy, which is most of why the board runs
at 17 to 21 frames a second with sound and 138 to 205 without. Bus timing is in as
well: the arbiter charges a Z80 bank access the 2 to 5 cycles Kabuto's footnote 1
gives and the 68000 about 11, which reproduces MoD's logic-analyser range exactly with
the 68000 off the bus and lands on his mean, 2.91 against his 2.93, with it on the bus;
the bus grant waits for the end of the Z80's machine cycle and then for the arbiter to
report it; the DMA costs 2.37 and 4.78 68000 cycles a word against Kabuto's measured
2.4 and 4.7 with the 5.6-cycle start-up he measured, and moves the Manual's 16, 167, 18
and 205 bytes a raster line to within one byte; and after every stall the 68000 keeps
481 of a line's 488.6 clocks, which is Kabuto's "roughly 480". The picture is now drawn a piece
at a time, so a write part way along a line lands there, CRAM dots and all. And all of
that has now been held against **Nuked-MD**, the whole chipset modelled from decapped
die photographs, built here headless with a bus tracer of our own: on one NTSC frame of
each test ROM of milestones 2 to 5, five of the ten agree with it on **every** one of
their 25,307 to 28,921 68000 bus cycles -- address, direction, size, the stall charged
to each, and the data read -- and every difference elsewhere is sorted into a named
cause. It settled four things nothing else could, and a
timing session then adopted them: the DMA start-up cost Kabuto measured is real, the
Z80's bus grant came about seven 68000 cycles early, a 68000 access to the Z80's space
costs one clock more than the board charged -- which removed the largest named class of
clock difference from the milestone-1 lockstep, now 119,755,782 68000 and 73,002,958
Z80 instructions with nothing unexplained and nothing named against MAME's own clock --
and the 68000 holds the bus for only 18 of a bus cycle's 28 master clocks, which had
been making every Z80 bank access cost half a wait state too much. It also showed that
the board has the two halves of a Z80 `pop` the wrong way round, which needs a change
to z80-python to fix. Cartridges and peripherals are in as well: battery SRAM
and FRAM, the serial EEPROMs from Eke-Eke's protocol document, the Sega (SSF2) mapper,
the 3- and 6-button pads, TMSS with the real boot ROM, and the region and version
register -- each judged by a game doing the right thing. **Sonic The Hedgehog 3 lays out
its FRAM and NBA Jam writes its EEPROM's header, and every byte of both equals what
MAME writes**; a save reloaded into a fresh machine is read back and left alone.
**Super Street Fighter II pages in its top megabyte with the two writes its technical
document quotes and reaches its title screen. The TMSS boot ROM draws its licence
message and hands off to the cartridge at frame 189, and MAME's own TMSS driver agrees
on every one of 71,680 pixels.** The two pads reproduce CMD's published table for all
4,352 combinations of their buttons, the games that want six buttons ask for them about
once a frame and the ones that do not never ask, and r57shell's VDP tests -- blocked
since milestone 3 on what turned out to be a misreading -- are walked and measured. Around the
groundwork it was built from, in the shape that built
[sms-python](https://github.com/alewman/sms-python):

- a primer on the machine;
- the clock tree, the counters, the VRAM access slots, DMA and bus timing, with
  where each number came from;
- the hardware quirks, the differences between models and revisions, the
  sources known to be wrong, and the open questions;
- every oracle found, ranked by tier and pinned;
- a fetch script that rebuilds the reference library from those pins;
- a working, verified recipe for MAME traces of both CPUs and their bus traffic;
- a handoff brief with ten milestones.

See [docs/README.md](docs/README.md).

## The machine

| | |
| --- | --- |
| Main CPU | Motorola 68000, 7.67 MHz (7.60 PAL) |
| Sound CPU | Zilog Z80 (NMOS), 3.58 MHz, 8 KB RAM, a 32 KB window onto the 68000's space |
| Video | 315-5313 / YM7101: two planes, a window, 80 sprites, 64 KB VRAM, 9-bit colour from 64 CRAM entries, DMA, H32/H40, V28/V30, interlace; Mode 4 for the Master System |
| Sound | YM2612 (OPN2): six 4-operator FM channels, one of them a DAC, and a YM3438-like ASIC on later models; the Sega PSG inside the VDP |
| Memory | 64 KB work RAM; cartridge ROM up to 4 MB (more with the SSF2 mapper), battery SRAM or EEPROM |
| I/O | three ports (two pads and an expansion port), the version register, TMSS on later models |
| Models | Model 1 (VA0–VA7), Model 2, Genesis 3, Nomad; NTSC, PAL, Japanese |

## What is here

| Path | What it is | Committed |
| --- | --- | --- |
| `docs/` | The documents listed in `docs/README.md` | yes |
| `scripts/fetch_references.py` | Rebuilds `reference/` and `tests/test_roms/` from the pins | yes |
| `scripts/pinned_files.json` | 94 files: URL, SHA-256, size, kind, tier, why (458 MB; 3 over 50 MB fetched only with `--large`) | yes |
| `scripts/pinned_repos.json` | 14 git repositories at pinned revisions (sparse where large) | yes |
| `scripts/mame_trace.py` | The MAME 0.285 two-run trace recipe for both CPUs and the device buses, verified | yes |
| `src/megadrive_python/` | The board: machines, cartridge, I/O, both memory maps and the bus, the VDP and its renderer, the YM2612 and the PSG, the analogue path, the system clock | yes |
| `scripts/lockstep_mame.py` | Milestone 1: both CPUs against MAME's logs | yes |
| `scripts/run_hv_captures.py` | Milestone 2: Nemesis's HV/status captures, word for word | yes |
| `scripts/run_vdpfifotesting.py` | Milestone 2: VDPFIFOTesting, read back from VRAM | yes |
| `scripts/run_sprite_masking.py` | Milestone 3: the sprite masking ROM against Nemesis's MD1600 photographs | yes |
| `scripts/run_z80_bus_test.py` | Milestone 3: MoD's `gen_test_z80_bus`, read off the screen | yes |
| `scripts/compare_mame_frames.py` | Milestone 3: the board's picture against MAME's frame (a detector) | yes |
| `validation/` | The three audio referees' harnesses and their build script (Nuked-OPN2, Nuked-PSG, YM2608-LLE) | yes |
| `scripts/run_fm_streams.py` | Milestone 4: the YM2612 against Nuked-OPN2 and YM2608-LLE, cycle by cycle | yes |
| `scripts/run_psg_compare.py` | Milestone 4: the PSG against Nuked-PSG, input clock by input clock | yes |
| `scripts/run_ssg_eg.py` | Milestone 4: Nemesis's SSG-EG test against his MD1600 recording, by measurement | yes |
| `scripts/run_mdfourier.py` | Milestone 4: the MDFourier signal against the hardware recordings (a detector) | yes |
| `scripts/run_bus_arbiter.py` | Milestone 5: MoD's two logic-analyser experiments, and the 68000's rate | yes |
| `scripts/run_dma_cost.py` | Milestone 5: DMA cost per word and per raster line, against Kabuto and the Manual | yes |
| `scripts/run_nuked_md.py` | Milestone 6: six comparisons against Nuked-MD, the die-derived referee | yes |
| `scripts/run_pads.py` | Milestone 7: the 3- and 6-button pads against CMD's table, r57shell's `joy_speed`, and the games | yes |
| `scripts/run_battery.py` | Milestone 7: battery SRAM and the EEPROMs, against the games and MAME's nvram | yes |
| `scripts/run_mapper.py` | Milestone 7: the Sega (SSF2) mapper against `ssf2tnc.txt` and Super Street Fighter II | yes |
| `scripts/run_tmss.py` | Milestone 7: the boot ROM, the hand-off, the header check and the VDP lock | yes |
| `scripts/run_region.py` | Milestone 7: the version register against the Manual, and three games that check it | yes |
| `scripts/run_r57shell_tests.py` | Milestone 7: r57shell's eight VDP-test screens | yes |
| `scripts/run_soft_checker.py` | Milestone 8: Sega's own MD Soft Checker, read off the screen | yes |
| `scripts/mame_media.py` | Milestone 8: which board MAME puts an image on, and how to hand it one | yes |
| `scripts/compat_sweep.py` | Milestone 8: every cartridge of the romset against MAME's frame (a detector) | yes |
| `scripts/triage_diffs.py` | Milestone 8: every difference sorted, by the lockstep and by the board's own timing | yes |
| `tests/` | pytest: the board's tables, milestone 2's judges (fetched), milestone 1's replays (`-m slow`, local) | yes |
| `reference/` | Fetched manuals, research documents, forum threads, source trees and die-derived models | no |
| `tests/test_roms/` | Fetched test ROMs, logic-analyser captures and hardware audio recordings | no |
| `tests/mame_traces/` | MAME runs | no |

    python -m megadrive_python CART --frames 600 --png frame.png --wav sound.wav
    python -m megadrive_python CART --pad 6button --save game.srm --tmss-rom BOOT.zip
    sh validation/build_referees.sh             # build milestone 4's three referees
    sh validation/build_nuked_md.sh             # build milestone 6's referee, Nuked-MD
    python scripts/fetch_references.py          # fetch everything missing
    python scripts/fetch_references.py --large  # also the large recordings
    python scripts/fetch_references.py --verify # check the pins, fetch nothing

## Licence

MIT for what this repository contains. The fetched material keeps its own terms
(recorded per entry in `scripts/pinned_files.json` and `scripts/pinned_repos.json`,
and in `docs/validation.md`) and is never committed. Several of the reference
emulators and die-derived models are GPL, AGPL or LGPL, and Genesis Plus GX's
licence forbids commercial use: read them, cite them, do not copy from them.
