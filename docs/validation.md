# Validation: the oracles found, ranked, and pinned

## The tier rule

Same rule as every core in this family: **hardware-captured > hardware-corrected
> emulator-derived**. A lower-tier oracle is a *detector* (it finds where to
look); it is never the *judge*. When a detector and a judge disagree, the judge
wins, and the disagreement goes in [undocumented-behavior.md](undocumented-behavior.md).

For a board, "hardware-captured" means output recorded from a real console
(logic-analyser samples, an audio recording, a photograph of a test screen), and
"hardware-corrected" a test program whose expected values its author established
or checked on real hardware. The **specification** tier (Sega's and Yamaha's
manuals) defines the intent; hardware overrides it where they differ.
**Research documents** (MacDonald, Kabuto, Plutiedev) are only as strong as the
measurements behind each statement: Kabuto's notes report measurements, and
this repository cites them as such, but his explanations are his.

**Die-derived models** are a class the SMS work only touched (Nuked-OPLL): a
model transcribed from the chip's silicon (Nuked-MD, Nuked-OPN2, YM2608-LLE,
Nuked-PSG, and the SEGAChips netlists behind them). They are stronger than any
behavioural emulator, because nobody fitted them to games or tests, but a
transcription can err and none of them publishes test results. This repository
ranks them **between hardware-corrected and emulator-derived**: where a hardware
capture or a hardware-corrected test exists, it judges; where none does (the
YM2612's arithmetic, the arbiter's cycle-level timing), the die-derived model
judges, and the claim says so.

Everything below was downloaded and hashed on 2026-09-22
(`scripts/pinned_files.json`, `scripts/pinned_repos.json`); every hash the
survey recorded matched an independent download. `scripts/fetch_references.py`
rebuilds `reference/` and `tests/test_roms/` (both ignored); `--large` adds the
entries over 50 MB (recordings and the FM document bundle).

## The oracles

### VDP: hardware-captured

| Oracle | What it covers | Why it is hardware | Pinned as |
| --- | --- | --- | --- |
| Nemesis, "sr logic analyser" results (2010) | the H and V counters and the status register sampled across whole frames: H32/H40 × V28/V30 × no interlace, interlace, double interlace | a logic analyser on a PAL Model 1 and an NTSC-J Model 1 (forum t=768); 20 raw dumps, with the ROMs and source that produced them | `tests/test_roms/nemesis/sr logic analyser {results,roms,source}.zip` |
| Nemesis, VRAM access slots (from 2010) | the order of plane, sprite, external and refresh fetches in a line | a logic analyser on the VRAM bus of a modified console, corroborated by HardWareMan's oscilloscope (t=851) | `reference/forum/spritesmind-t851.html` |
| MoD et al., "VDP internals" (from 2012) | VRAM address mapping; 171 slots in H32 (4 refresh, 16 CPU); FIFO and DMA; HSYNC-to-ODD and HINT-to-HSYNC counts | an Open Bench Logic Sniffer on a Genesis 2 VA1 (t=1291) | `reference/forum/spritesmind-t1291.html` |
| MoD, Mode 4 VRAM timing (2017) | Mode 4 fetches on the MD VDP | VRAM traffic observation; instrument not stated (t=2557) | `reference/forum/spritesmind-t2557.html` |
| Jorge Nuno, video timings (2009) | the NTSC H40 line (3,420 MCLK), the H sync's clock mix | a four-channel oscilloscope (t=519) | `reference/forum/spritesmind-t519.html` |
| Jorge Nuno, MD VDP research (2022) | HV and status trip points in H40 | captures with a "fake 68000"; raw data on a linked pastebin, not fetched | `reference/forum/mddc-md-vdp-research-thread.html` |
| Kabuto, hardware notes v1.5 (2017–2018) | the bus, the YM2612's DAC, the ports, picture size, HV values, sprite phases, access timing, DMA speed, S/H, the debug register | his own test programs on several Model 1/2/3 boards, an oscilloscope and an audio analysis tool; values without raw data | `reference/docs/kabuto-hardware-notes.html` (a live page, snapshotted) |
| CMD's capture photos (2000–01) | mid-frame H32/H40 switches, Mode 4 and TMS modes on the MD VDP, the window bug, S/H | frames from a real Genesis through a capture card | `mdpic.htm` inside `reference/docs/md_tech.zip` |

### VDP: hardware-corrected test programs

| Oracle | What it covers | Why it is hardware | Pinned as |
| --- | --- | --- | --- |
| **Nemesis, VDPFIFOTesting** (2013) | over 100 self-checking tests of the ports: FIFO size and waits, the address and code registers, DMA of every kind into every memory, the busy flag, wrapping, the bus lock, the HV latch, byte swapping, the VSRAM read cache, the Mode 4 register mask | "all pass on the hardware, with a few exceptions" (t=1291 start=30): a Genesis 3 fails the VSRAM tests; the DMA-fill data-port and FIFO byte-order tests are intermittent on hardware. Exodus scored 121/122 | `tests/test_roms/nemesis/VDPFIFOTesting.zip` (ROM and source) |
| **Nemesis, sprite masking and overflow test** (2009) | nine tests of sprite limits, the dot overflow and masking, in H32 and H40 | results checked on an MD1600, with a chart comparing emulators (t=541 start=30); his two photographs of that machine are the judge and are pinned beside the ROM | `SpriteMaskingTestRom.{bin,zip}`; `reference/images/MD1600SpriteMask{H32,H40}.png` |
| r57shell, VDP tests (2013) | mode changes, sprite links, S/H priority, colour and layer interactions | run by eye on an MD2 VA1.8 on two CRTs (t=1585); test 4 depends on the monitor | `tests/test_roms/r57shell/vdp_tests.bin` |
| TmEE's colour and S/H tests; CMD's window, V counter and colour tests; Paul Lee's demos; VDPTEST | visual checks | written on and for hardware; authorship of CMD's and VDPTEST inferred from headers (Eke's bundle has no readme) | `tests/test_roms/eke/md_test.zip` |
| 240p Test Suite v1.30 | video patterns; it also generates the MDFourier signal | "developed and tested on real hardware" (the author); a calibration tool, not a timing judge | local romset; source `reference/src/240pTestSuite` |
| **Titan, Overdrive 2** (2017) | the hardest known demo: the debug register, FIFO/DMA stall timing, mid-line tricks, 128 KB mode, the SSF2 mapper | developed on hardware; its correct output depends on the VDP revision (readme); a stress case, not pass/fail | `tests/test_roms/titan/titan-overdrive2.zip` (identical to the local romset's) |

### Board: hardware-captured and hardware-corrected

| Oracle | What it covers | Why it is hardware | Pinned as |
| --- | --- | --- | --- |
| MoD, Z80 bank-access wait states (2014) | Z80 accesses to the 68000's space: waits with the 68000 stopped and running, /BR to /BG | a logic analyser on a Genesis 2 (t=1661) | `reference/forum/spritesmind-t1661.html` |
| **MoD, `gen_test_z80_bus`** (2019) | Z80 access to 68000 RAM (writes work, reads fail), $A0xxxx/$A1xxxx from the Z80, the I/O mirrors, the /TIME range | "tested across multiple machines" (t=3021) | `tests/test_roms/mask-of-destiny/gen_test_z80_bus.{md,s68}` |
| Terakawa, 6-button pad protocol | 1.1 ms to detect two edges, the 1.8 ms timeout, 1.6 ms data validity | timed on real pads; "parameters differ one by one controller" | `reference/web/terakawa-6button.html` (mirror; the original page no longer answers) |
| r57shell, `joy_speed` | pad re-read latency | a hardware tool (t=2308) | `tests/test_roms/r57shell/joy_speed_v1.02.bin` |
| TMSS boot ROM | the licence screen and the lock | dumps of several chips agree (TmEE, t=3197) | local No-Intro `[BIOS] Sega Mega Drive - Genesis - Boot ROM (World).zip` (SHA-1 `3f50b76b…`); a disassembly in `reference/src/TMSS-Disassembly` |
| **MD Soft Checker v0.30** | Sega's own factory diagnostic: the 68000's registers and whole instruction set, work RAM, the Z80's bus request and RAM, VRAM, CRAM, VSRAM, the I/O registers, every kind of DMA, the VRAM fill and copy, and which pad is on each port | Sega wrote it to test the machine; it prints its own verdict per check | `tests/test_roms/sega/MD Soft Checker (USA) (Proto) (Unl).zip` (zip SHA-256 `688debcf…`, image SHA-1 `bd716dbb…`), from the local No-Intro mirror |

### Sound: hardware-captured and hardware-corrected

| Oracle | What it covers | Why it is hardware | Pinned as |
| --- | --- | --- | --- |
| **MDFourier recordings** (2020) | the MDFourier signal (from the 240p Suite) through 21 Mega Drive boards' analogue paths, plus the Sega CD | recordings of real consoles; they judge mixing and filtering per revision, not FM arithmetic | `MDFourier-Genesis-SegaCD-Recordings-14062020.zip` (large); analyser `reference/src/MDFourier` |
| Nemesis, SSG-EG recordings (2008–09) | the SSG-EG and attack/release test ROMs on an MD1600 | recorded from hardware | `ssg-eg md1600*.rar` (the second is large) |
| Nemesis, CSM-mode recording | the CSM test ROM | beside the ROM; confirm in t=386 that it is from hardware before judging with it | `ym2612csmmode.wav` |
| Eke, BUSY flag (2017) | 32 internal clocks; port 0 only on the discrete chip | a counting program on a VA4 Model 1 and a VA0 Model 2 (t=386 start=810) | forum snapshot |
| Aly James, ladder effect | the YM2612's crossover distortion | a recording; which board is not stated | `tests/test_roms/aly-james/MD1_LADDER.wav` |
| Nemesis, YM2612 test ROMs | CSM, detune, frequency write order, SSG-EG, attack and release, DAC multiplexing | written against hardware; not self-checking: compared by ear or against the recordings | `tests/test_roms/nemesis/ym2612*.bin` |
| Ghost, 1 kHz sweep ROMs | DAC and test-register sweeps | capture tools (the thread shows scope pictures from an EU Model 1) | `tests/test_roms/ghost/sweep_1000.rar` |

### Die-derived models (judges where nothing above covers the question)

| Model | From | Licence | Pin |
| --- | --- | --- | --- |
| **Nuked-MD** | the whole chipset as the integrated FC1004: YM7101 VDP with its PSG, YM6045 arbiter, YM6046 I/O, TMSS, FM, NMOS 68000 and Z80; "Model 1 VA7 would be closest" of retail boards | GPL-2.0 | `reference/src/Nuked-MD` @ 594ca2a |
| SEGAChips | the gate-level reverse engineering behind it (netlists and notes; photographs left out) | CC0-1.0 | `reference/src/SEGAChips` @ 1b378b4 |
| **Nuked-OPN2** | the YM3438 from its die, with a YM2612 mode | LGPL-2.1 | `reference/src/Nuked-OPN2` @ 335747d |
| **YM2608-LLE** | low-level models including the NMOS YM2612 | GPL-2.0 | `reference/src/YM2608-LLE` @ 7a2aca7 |
| **Nuked-PSG** | the PSG inside the YM7101 | GPL-2.0 | `reference/src/Nuked-PSG` @ d15a168 |

All are run, read and compared against, never copied into this repository.

### Specification

| Document | Pinned as |
| --- | --- |
| Genesis Software Manual, 183-page scan | `reference/manuals/GenesisSoftwareManual-183p.pdf` |
| Genesis Software Manual, 150-page scan with a text layer | `reference/manuals/GenesisSoftwareManual-150p.pdf` |
| Genesis Technical Bulletins (they correct the Manual) | `reference/manuals/GenesisTechnicalBulletins.pdf` |
| Genesis Technical Overview v1.00 | `reference/manuals/GenesisTechnicalOverview.pdf` |
| Sega Genesis Reference Sheets (1992) | `reference/manuals/SegaGenesisReferenceSheets_1992-11-05.pdf` |
| YM2608 (OPNA) application manual, Japanese and Nemesis's translation | `reference/manuals/YM2608J*.pdf` |
| YM3015 DAC datasheet | `reference/manuals/YM3015.pdf` |
| Nemesis's FM bundle, including the YM3438 application manual | `reference/manuals/FMDocs.7z` (large) |

No 315-5313 datasheet and no YM2612 manual are known to exist.

### Research documents

CMD's `genvdp.txt`, `gen-hw.txt`, `m5hvc.txt`, `newreg.txt`, `vdppin.txt`,
`vdpint.txt`, `gen-eeprom.txt`, `segasix.txt` and Rick McTeague's `genhw.txt`
(`reference/docs/cmd/`); Eke's bundle `md_tech.zip` (CMD's documents and
capture photos, `ste.txt`, `ssf2tnc.txt`, EEPROM and light-gun documents);
Plutiedev's pages; the md.railgun.works VDP page; the SpritesMind threads
listed in `pinned_files.json` (the YM2612 reference thread in nine pages, the
debug register, colour levels, TMSS, reset, the two index threads); Nicole
Express on TMSS (2026, one tester). What each says is wrong or superseded is in
[undocumented-behavior.md](undocumented-behavior.md).

### Emulator-derived (detectors)

| Emulator | Licence | Pin | Its evidence |
| --- | --- | --- | --- |
| MAME 0.285 (`genesis`, `megadriv`, `megadrij`, `genesis_tmss`) | BSD-3/GPL | installed binary; source `reference/src/mame` @ f379cd0 | the trace recipe in [mame-oracle.md](mame-oracle.md); the weakest of these for VDP timing |
| BlastEm | GPL-3.0 | `reference/src/blastem` @ 542164d (libretro's git mirror) | first to run Overdrive 2 correctly; passes VDPFIFOTesting and Direct Color DMA; CRAM dots; its author's own logic-analyser work. **Built and used here since 2026-09-24**: its hardware event log (`blastem -e`) is read by `scripts/blastem_events.py`, which is the only way to compare against the cartridges MAME cannot run. Stock at the pinned revision -- its framebuffer backend no longer compiles and was deliberately not patched |
| Exodus | MIT | `reference/src/Exodus` @ 08f388f | built by Nemesis beside his captures; 121/122 on his own test (2013); dormant |
| Genesis Plus GX | non-commercial | `reference/src/Genesis-Plus-GX` @ fdab4a4 | the widest compatibility and many hardware-checked details (BUSY, EEPROM, mappers); no FIFO-test or Overdrive 2 claim found |
| jgenesis | GPL-3.0 | `reference/src/jgenesis` @ d19ee94 | claims VDPFIFOTesting, Direct Color DMA and MD Soft Checker in its release notes |
| clownmdemu | AGPL-3.0 | `reference/src/clownmdemu-core` @ 5f0d4ea | explicitly incomplete |

There is no independent cross-emulator scoreboard: each project's claims are its
own.

## What is *not* available

- **No hardware-captured per-instruction or bus-cycle corpus for the board.**
  The CPUs have theirs (in their own repositories); the board has Nemesis's HV
  and status dumps, logic-analyser findings written up as prose and diagrams,
  and audio. Nuked-MD could *generate* a gate-level bus trace of real software,
  which would be the strongest detector available, not a capture.
- **No YM2612 datasheet**, and no self-checking FM test ROM: the FM judges are
  the recordings (by ear or by measurement) and the die-derived models.
- ~~**MD Soft Checker v0.30**~~ (Sega's diagnostic): **found** (milestone 8). The
  local No-Intro mirror has it as `MD Soft Checker (USA) (Proto) (Unl)`; the
  romset's symlink dangles because No-Intro dropped the `(v0.30)` from the name.
  Pinned in `scripts/pinned_files.json` and run in milestone 8.
- **Dead links**: TmEE's sine recordings, blargg's Genesis recordings, a
  Reptilia Design recording, Terakawa's original page, CMD's documents on the
  SpritesMind mirror.
- **Not pinned by choice**: jsgroth's YM2612 blog (it answers automated fetches
  with an anti-scraping page); the Overdrive 2 hardware video capture
  (YouTube); Jorge Nuno's pastebin data; Eke's Mega Mouse test; the Chilly
  Willy Direct Color DMA ROM (no download link found).

## Local romsets (not fetched, not committed)

`/data/emu/roms/megadrive`: 3,548 entries (No-Intro), including the 240p Test
Suite, Overdrive and Overdrive 2, and the TMSS boot ROM. As with the SMS sets,
some entries are broken symlinks into the local mirror (MD Soft Checker, the
Teradrive TMSS BIOS among them); a sweep reports them as missing.

## Certification record

Each milestone's acceptance as run, with its pins. Code at the commit named;
m68000-python 51ee1a5 (pinned in `pyproject.toml`), z80-python 0.4.0; PyPy
7.3.23 (Python 3.11.15) unless said.

### Milestone 1: the two-CPU lockstep against MAME (detector) — passes

MAME 0.285 `genesis`, six games, 30 emulated seconds each: 119,755,782 68000 and
73,002,958 Z80 instructions with every register equal before every logged
instruction and every device access matched; 0 unexplained differences, every
named one listed in [worklog.md](worklog.md) (commit 80e2850). Rerun with the
milestone-2 VDP (commit f2055a5) for Sonic The Hedgehog (USA, Europe) and Gunstar
Heroes (USA): the same instruction counts, 0 unexplained. Local only (MAME,
commercial images); `pytest -m slow` replays recorded logs.

### Milestone 2: the VDP's ports, FIFO, DMA, counters and interrupts

**Nemesis's HV/status captures** (hardware-captured; `sr logic analyser results.zip`
SHA-256 in `pinned_files.json`), `python scripts/run_hv_captures.py`, commit
4743f9f, 78 s on 16 processes: all 288 dumps of the 32 captures run on the board;
**8,762,415 of 8,847,360 words equal (99.04 %)**. Of the 84,945 others: 61,440
are two dumps the fit cannot place; 11,103 are one dump whose H reads trail the
board's by a step in every second sample; 9,869 are reads at a sampling phase
where a counter steps (analogue: the two consoles differ there and each varies
between runs); 1,288 are the F flag in one dump that starts with it set; 705 the
VBlank flag (668 of them in two first runs); 228 the HBlank flag at its edges; 302
H or V reads, mostly in the interlaced NTSC V30 H32 captures; 10 the sprite
overflow flag (no sprites are drawn yet). Details in [timing.md](timing.md). **Not passed as
the brief states it**: "equal sample for sample in every mode captured" does not
hold, for the reasons above; what the board reproduces exactly is every counter
sequence, every line's pixel timing (H40's VSYNC lines newly found), the flags'
positions, and the sampling rule; per-capture numbers are the script's output and
`tests/hv_captures_baseline.json`.

**VDPFIFOTesting** (hardware-corrected; `VDPFIFOTesting.zip`, ROM SHA-256
8b0ed394…), `python scripts/run_vdpfifotesting.py --machine M`, commit 4743f9f,
3-4 s each: **122 of 122 on all nine machines** except genesis3; the four tests
Nemesis reports intermittent on hardware (DMA Fill FIFO Usage; DP Writes During
DMA Fill VRAM, CRAM, VSRAM) pass. **genesis3** (0x80-byte VSRAM): 114 of 122,
the eight failures the DMA Fill to VSRAM tests, as a Genesis 3 fails VSRAM tests
on hardware (Nemesis gives no list, so which ones is not checked).


### Milestone 3: the VDP's picture

**Nemesis's sprite masking ROM against his MD1600** (hardware-captured; the two
photographs pinned in `scripts/pinned_files.json`, neither of which holds a red pixel,
so all nine tests pass on his machine): `python scripts/run_sprite_masking.py`, commit
4890f03, 2 s: **0 of 18 result cells differ**, in H32 and in H40.

**MoD's `gen_test_z80_bus`** (hardware-corrected): `python scripts/run_z80_bus_test.py`,
2 s: all eight of the ROM's own checks PASS, the Z80's writes to the 68000's work RAM
read back $4243 and its reads give $FFFF, which is what MoD reports from several
machines. Its two BUSACK delay counts read 0000: the board grants the bus at once
(see [undocumented-behavior.md](undocumented-behavior.md), open item 8).

**CMD's window and colour tests, TmEE's, and r57shell's** (hardware-corrected, judged
against MAME's own frames as a detector, `scripts/compare_mame_frames.py --record`,
3 emulated seconds each): `wtest`, `wtest_i1`, `stetest` and r57shell's `vdp_tests`
100.00 % of pixels; `wbug` 99.84 %; `vctest` 96.93 %; `wtest_i2` 96.25 %; `SHLTEST`
69.64 %; `STETEST2` 41.97 %. Every difference is named in
[worklog.md](worklog.md); the large ones are the board darkening the backdrop in
shadow/highlight mode, which Kabuto, CMD and Genesis Plus GX all describe and MAME does
not do. **Not passed**: r57shell's redraw-timing tests, which want VRAM writes to land
inside the line being drawn (the board renders a line at a time).

**MAME's exit frames** (detector), six games at 2, 5, 10 and 20 emulated seconds: every
static screen identical, every lower figure a moving demo a few frames out of phase;
nothing structural in 24 comparisons.

**Speed** (the brief asks for it from milestone 3 on): 147.7 to 293.6 frames a second
on PyPy 7.3.23 for the six lockstep games, 2.5 to 4.9 times real time, the renderer
costing between half and two thirds of the time.

### Milestone 4: sound

The judges here are the **die-derived models**, because nothing above them covers the
question: there is no YM2612 datasheet, no self-checking FM test ROM and no pinned
capture of a Mega Drive PSG, which is exactly the case the tier rule reserves for
them. The **recordings** judge what they do cover -- SSG-EG's shapes and rates, the
ladder's depth -- and MDFourier is used as a detector of the analogue model, since
what it would judge (a filter corner, a mixing ratio) has no pinned number.

The referees are built from the pinned sources by `sh validation/build_referees.sh`
into `reference/build-referees/` (ignored), with gcc 15.2.0; the harnesses are
`validation/*.c` and copy nothing from them.

**The PSG against Nuked-PSG** (die-derived, `Nuked-PSG` @ d15a168):
`python scripts/run_psg_compare.py --games`, commit 383992c, 47 s: ten streams --
five synthetic (two random walks, every tone period, every noise setting, every
attenuation) and five cartridges' own -- with the four attenuations the chip drives
compared at **every input clock**, sixteen times finer than its own sample rate:
**80,300,605 input clocks, 0 different**.

**The YM2612 against Nuked-OPN2** (die-derived, `Nuked-OPN2` @ 335747d, both output
modes against the matching machine variant): `python scripts/run_fm_streams.py --roms
--games`, commit 383992c, 21 min: eighteen streams -- seven synthetic (two random
walks and six aimed at channel 3's special mode and CSM, SSG-EG's eight shapes, the
LFO at every speed against every depth, both timers, the DAC), Nemesis's six test ROMs
and five cartridges -- with **every value of both DAC pins and of the status byte on
every internal cycle**, 24 to a sample at the chip's native rate:
**524,264,898 values compared, 0 different**.

**The YM2612 against YM2608-LLE** (die-derived, `YM2608-LLE` @ 7a2aca7, its
`fmopna_2612.c`): **not equal sample for sample on every stream.** The two models put
a channel's sample in a different place inside the four cycles it owns (the sum over
an output sample cancels it) and the two harnesses drive a write differently (which
can move a write one internal cycle). At the chip's native rate they are identical
over the SSG-EG, timer and DAC streams, differ on 1 sample in 19,898 on the CSM
stream, and on densely written streams differ in transient bursts around writes --
1,200 writes give 146 runs, median 1 sample -- after which they re-converge. Measured
in [worklog.md](worklog.md).

**Nemesis's SSG-EG test against his MD1600 recording** (hardware-captured, the judge):
`python scripts/run_ssg_eg.py --wav "<unpacked>/ssg-eg md1600.wav"`: nine notes on
both, every length equal within one 50 ms block, every envelope repetition period
identical (150 ms, 50 ms, or none, matching shape for shape). The notes start
**1.0-1.2 % further apart** on the MD1600 than on the board, a rate difference left
open (the recorder's sample clock or the console's crystal).

**The ladder against Aly James's `MD1_LADDER.wav`** (hardware-captured):
`python scripts/run_fm_streams.py --ladder tests/test_roms/aly-james/MD1_LADDER.wav`:
the board's discrete variant's level stops falling 36-37 dB below its peak where the
Model 1's stops 33 dB below; its ASIC variant does not plateau, falling another 12 dB.
The ramps are not the same ramp, so the 4 dB is the distance between two differently
made measurements, not an error.

**The MDFourier signal against the recordings** (hardware-captured, used as a
detector): `python scripts/run_mdfourier.py --recording <unpacked>/A-MD1UTVA3-M192.flac`.
Both machines run the signal in the same 58.40 s between its two syncs. The PSG sits
6.70 dB below the FM on the Model 1 VA3 and 6.66 dB below on the board (the board's
`audio.PSG_GAIN` was fitted to it, 1,400 -> 422); the PSG ramp is 0.07 dB out and the
noise channel 1.72 dB quiet, which is left open. Per block, everything is inside
±1.6 dB, so the low-pass corner is in the right region without being a measured value.

**Speed** (the brief asks for the FM's cost per emulated second): see
[worklog.md](worklog.md), milestone 4.

### Milestone 5: bus timing

The judges here are MoD's logic-analyser measurements on a Genesis 2 (forum t=1661;
hardware-captured), Kabuto's own measurements (hardware-measured research), the
Manual's DMA transfer capacity table (specification) and Nemesis's VDPFIFOTesting
(hardware-corrected). Overdrive 2 is a stress case, never a pass/fail judge: its
correct output depends on the VDP revision.

**The arbiter's delays** (`python scripts/run_bus_arbiter.py --accesses 4000`, commit
`21038dd`, seconds): MoD's two experiments, built as small 68000 and Z80 programs and
run on the board.

| Experiment | board, Z80 wait states | hardware | board, 68000 delay |
| --- | --- | --- | --- |
| 68000 in `stop #$2700`, Z80 reading the bank window | 2-3, mean 2.27 | MoD: "2-3" | 10-12, mean 10.54 |
| 68000 in his `jmp (a0)` loop, Z80 reading | 3-4, mean 3.36 | MoD: 2-4, "about 2.93"; Kabuto: 2-5, mean 3.3 | 10-13, mean 10.95 |
| the same, Z80 `pop`ping (his sound drivers' case) | 2-4, mean 2.53 | MoD: "at least 4 wait states" on the second read, mean 3.5 | 10-13, mean 10.43 |

Kabuto's average 68000 delay is "around 11 68k cycles"; the board's is 10.4 to 11.0.
The `pop` case is the one that does not agree, and the reason is measured rather than
argued: the board cannot say where inside a Z80 instruction an access falls, so both
of a `pop`'s reads land at almost the same clock, where MoD saw the 68000 "sneak in a
bus operation" between them ([undocumented-behavior.md](undocumented-behavior.md),
open item 14; a core change would settle it, [polish.md](polish.md)).

**The 68000's own rate**, with every stall the board charges: **480.9** of a raster
line's 488.6 clocks are left for instructions in the `jmp (a0)` harness with the Z80 in
its own RAM -- the refreshes -- against Kabuto's "roughly 480 cycles per raster line
instead of the theoretical 488+4/7 ones"; with the Z80 reading the bank window, 357.3.
(The 481.0 once quoted for the latter left out the Z80's holds, owed to the next
instruction, which the old count summed to nothing; worklog, "The Z80's grant inside an
instruction".)
The six lockstep games, which also pay VDP stalls, run at 468 to 475.

**The bus grant** ($A11100 to BUSACK) now waits for the end of the Z80's machine cycle
(Zilog UM0080). `python scripts/run_z80_bus_test.py`: all eight of MoD's checks still
PASS and its two delay counters still read **0000** -- the delays the model produces on
that ROM are 0 to 9 68000 cycles, shorter than the 8-cycle instruction it measures
with, so the ROM cannot see them. Its hardware values are unpublished, so this is
recorded, not claimed.

**DMA cost** (`python scripts/run_dma_cost.py`, 30 s). Kabuto's formulas, fitted over
64 to 512 words with the display off in H40: the board's **2.38** 68000 cycles a word
for a fast transfer (CRAM, VSRAM, VRAM in 128 KB mode) against his 2.4, and **4.78**
for a slow one (VRAM in 64 KB mode) against his 4.7 -- both inside 2 %. His intercepts
are not reproduced (+5.6 against -3.5 on a fast transfer), so a one-word transfer costs
8 cycles on his hardware and about 1 here; left open. The Manual's DMA transfer
capacity table for 68000 memory to VRAM is reproduced to one byte: **16.1 / 167.4 /
18.1 / 205.9** bytes a raster line against its 16 / 167 / 18 / 205 (H32 and H40, display
on and off). Getting there took one correction to the idle line's access slots
([undocumented-behavior.md](undocumented-behavior.md), "The VDP" item 19).

**VDPFIFOTesting's timing tests**: `python scripts/run_vdpfifotesting.py --machine M`,
7-8 s each, **122 of 122** on genesis, genesis-tmss, genesis2, megadrive,
megadrive-tmss, megadrive2, megadrive-japan and megadrive2-japan, and 114 of 122 on
genesis3 (the eight DMA Fill to VSRAM tests), unchanged from milestone 2. The timing
tests among them -- FIFO Buffer Size, FIFO Wait States, DMA Transfer Bus Locking, FIFO
Full Before DMA Transfer, and the seven DMA Busy Flag tests -- all pass.

**Overdrive 2** (a stress case): see [worklog.md](worklog.md), milestone 5.

**Milestones 1 to 4, unchanged**: the milestone-1 lockstep re-recorded and replayed for
Sonic The Hedgehog (23,286,815 68000 and 12,728,465 Z80 instructions, 0 unexplained);
the 288 HV/status capture dumps at the same 8,762,415 of 8,847,360 words; Nemesis's
sprite masking ROM 0 of 18 cells differing; the nine milestone-3 test ROMs against
MAME's frames to the same percentage as milestone 3; MAME's exit frames for the six
lockstep games at 20 s within the same band.

### Milestone 6: Nuked-MD as a referee

Nuked-MD (@ 594ca2a) is built headless by `sh validation/build_nuked_md.sh` with
`validation/nuked_md_headless.c` (this repository's own back ends and bus tracer)
and `validation/nuked_md_sdl.h`; nothing of it is copied here, and the binary in
`reference/build-nuked-md/` is git-ignored. `scripts/run_nuked_md.py` runs the
six comparisons; `scripts/nuked_md.py` drives the referee and takes the board's
own bus log in the same shape. The referee runs at about 0.33 emulated frames a
second, which is what sizes every window below.

**Its tier, restated for this milestone.** It judges only where no hardware
source covers the question, and it carries one caveat of its own: it models NMOS
CPUs with the integrated FC1004, a combination no retail board shipped (its
README: "Model 1 VA7 would be closest"), and this build disables its TMSS. So the
HV counter and the status register are *not* its to judge -- Nemesis's
logic-analyser captures are hardware and cover them -- and its VDP's power-on
phase, its Mode 4 HV port, its DRAM's power-on contents and its version register
($A1, hardware version 1) are named causes, not board bugs.

**What is compared**: every 68000 bus cycle's byte address, direction and size,
**the stall charged to it** (the referee reports each cycle's /AS-low length; 18
MCLK is a cycle nothing is stretching) and the data on a read. Both refresh
phases are read out of the referee's own trace rather than fitted, since the
power-on phase is the only thing about either refresh that is not the board's.

**The test ROMs of milestones 2-5** (`scripts/run_nuked_md.py roms --window
900000 --cycles 40000`, one NTSC frame from the reset vector, 6-8 s each):

| ROM | bus cycles equal |
| --- | ---: |
| `wtest.bin`, `vctest.bin` | **28,921 / 28,921** each |
| `ym2612csmmode.bin`, `ym2612detune.bin`, `ym2612multiplexing.bin` | **25,307 / 25,307** each |
| `gen_test_z80_bus.md` | 21,828 / 21,832 |
| `stetest.bin` | 230 / 233 |
| `vdp_tests.bin` | 22,125 / 28,122 |
| `sr logic analyser noint - H40V28` | 23,258 / 28,621 |
| `VDPFIFOTesting.bin` | 23,259 / 28,622 |
| `SpriteMaskingTestRom.bin` | not compared: the referee makes 11 bus cycles and stops |

In all ten the board's **cartridge refresh** rule misplaces **0** of the
referee's ticks; fitted against the referee's own observed stretches it is exact
for two unrelated programs at the same phase window (518-523 MCLK), and the
obvious variant of it -- only a cartridge access absorbs the tick -- reproduces
neither at any phase. Every
difference is one of four named causes: the **work-RAM refresh** rule (below) and
the cartridge stretches that move with it, the **Z80 bus grant** (below), the
referee's **VDP power-on phase**, and its **hardware version register**.

**Eight probe loops** (`--cycles 400`): 2,921 of 3,546 bus cycles equal; the NOP
loop, the ROM-read loop and the VDP-data-write loop **394 of 394** each.

**What the referee settled, that nothing else could:**

- **Kabuto's DMA start-up cost is real.** Fitted over 1 to 512 words, a fast
  transfer costs the referee 2.47 cycles a word with a **+8.0** intercept and the
  board 2.46 with +3.2; a one-word transfer costs the referee 11.0 68000 cycles
  and the board 1.1. Kabuto's hardware figure is 2.4 and +5.6. The board
  reproduces both slopes and neither intercept (milestone 5, open item 15).
- **The `pop` pair is the wrong way round on the board.** The referee charges
  2.95 then 3.61 wait states for the two reads of one `pop`; MoD measured the
  *second* read at "at least 4" and 3.5 on average. The board charges 4.08 then
  2.30 -- the same total, the opposite split -- which is the z80-python
  limitation `polish.md` records (milestone 5, open item 14).
- **The bus grant is about 7.4 68000 cycles early.** $A11100 write to /BUSAK:
  the referee 9.71-16.86 cycles (mean 12.22), the board 1.43-6.29 (mean 4.86).
  Two test ROMs poll $A11100 and see it.
- **A 68000 access to $A00000-$A0FFFF costs one clock more on the referee**, 68
  of 68 in the FM probe. That is MAME's `before_delay(1)` ("TODO: unverified"),
  which milestone 1 recorded as a named difference; the referee verifies it and
  the board does not model it.
- **The FM's wiring agrees**: the same 6 polls of $A04000 before BUSY cleared,
  all eight times, on both machines, and ports 1-3 read $00 on the referee as
  they do on the board.
- **The work-RAM refresh rule does not generalise**: its 21-MCLK stretch is
  confirmed everywhere, its re-arm period is exactly 806-819 MCLK in a dense
  work-RAM loop (narrowing milestone 2's 743-826 and excluding the board's 805),
  694-699 in `wtest.bin`, and no value in 600-1400 fits Nemesis's capture ROM.

**Not passed as the brief states it**: "the board's bus and device timing agrees
with Nuked-MD's" holds outright for five of the ten test ROMs and for three of
the eight probes; everywhere else each difference is sorted into one of the named
causes above, which is the brief's alternative. The numbers and the reasoning are
in [worklog.md](worklog.md), milestone 6.

### The timing session (2026-09-23): what four adopted rules moved

Not a milestone. The user's six decisions on milestone 6's questions, taken in one
pass before milestone 7 and before milestone 8's sweep, because the sweep compares
thousands of frames against MAME and every timing change moves its results. Code at
commit `3ae1498`; m68000-python 51ee1a5, z80-python 0.4.0, MAME 0.285, Nuked-MD
@ 594ca2a, PyPy 7.3.23 (Python 3.11.15).

**What was adopted**, each with the judge that settled it:

| What | Where | Judge | Before | After |
| --- | --- | --- | --- | --- |
| one extra 68000 clock on every $A00000-$A0FFFF access | `board.Z80_SPACE_STALL` | Nuked-MD and MAME agree; nothing hardware | 0 MCLK | 7 MCLK |
| the arbiter's propagation on the bus grant | `arbiter.BUSACK_PROPAGATION_MCLK` = 57 MCLK | Nuked-MD (nothing hardware measures this direction) | mean 4.15 cycles | mean **12.29** against the referee's 12.34 |
| the DMA's start-up | `vdp.DMA_STARTUP_MCLK` | Kabuto (hardware) +5.6, corroborated by Nuked-MD's +8.0 | one word 0.6 cycles | one word **7.4** against Kabuto's 8.0 |
| how long the 68000 holds the bus (Kabuto's `a`) | `arbiter.AS_LOW_MCLK`, `Board._m68k_release` | the 68000 UM's bus-arbitration rule, measured on Nuked-MD | 28 MCLK plus a 2-cycle look-ahead | **18 MCLK**, no look-ahead |

The work-RAM refresh was investigated and **kept at 805 MCLK** with the difference
named; five candidate rules and their scores are in
[undocumented-behavior.md](undocumented-behavior.md), open item 5.
`SpriteMaskingTestRom.bin` was left alone, as decided (the referee's own limitation;
Nemesis's photographs judge that ROM).

**Every acceptance run these touch, re-run:**

| Run | Before | After |
| --- | --- | --- |
| milestone 1's two-CPU replay, six games, 30 s each, MAME traces **re-recorded** | 119,755,782 68000 + 73,002,958 Z80 instructions, 0 unexplained, **41,274 steps of Sonic alone in the class "MAME's `before_delay(1)`"** | the same instruction counts, 0 unexplained, **that class empty**: on Sonic its 41,274 steps move into "agree" exactly (23,240,769 → 23,282,043), every other named class unchanged to the count |
| Nemesis's HV/status captures, 288 dumps | 8,762,415 / 8,847,360 words | **8,762,415 / 8,847,360**, unchanged to the word |
| VDPFIFOTesting, nine machines | 122/122 on eight, 114/122 on genesis3 | **unchanged** |
| Nemesis's sprite masking ROM against his MD1600 | 0 of 18 cells differ | **unchanged** |
| MoD's `gen_test_z80_bus` | 8 checks PASS; delay counters 0000/0000 | 8 checks PASS; delay counters **0001/0001** (no hardware value published for them) |
| the PSG against Nuked-PSG, ten streams | 0 of 80,300,605 input clocks differ | **0 of 121,279,196** (the game streams are longer since milestone 4's press frames, and the board's own clock moved with the $A0 charge) |
| the YM2612 against Nuked-OPN2, 18 streams, both variants | 0 of 87,377,483 internal cycles x 3 values x 2 variants not identical | **0 of 110,549,790 x 3 x 2** (the streams are longer since milestone 4's press frames) |
| `scripts/run_bus_arbiter.py`, MoD's three experiments | 2.27 / **3.36** / 2.53 wait states | 2.27 / **2.91** / 2.44 (MoD's hardware: "2-3" / 2.93 / 3.5) |
| `scripts/run_dma_cost.py` | one word 0.6, fit 2.37 × words − 3.5; Manual's table 16.1/167.4/18.1/205.9 | one word **7.4**, fit 2.37 × words **+ 1.7**; Manual's table 16.1/**167.1**/18.1/**205.1** against its 16/167/18/205 |
| `run_nuked_md.py busack`, 122 grants | board 10-44 MCLK, mean 4.86 cycles | board 67-94 MCLK, mean **12.29** (referee 12.34) |
| `run_nuked_md.py dma` | board fast 2.46 × words + 3.2, one word 1.1 | board 2.46 × words **+ 9.0**, one word **6.9** (referee 2.47 + 8.0, 11.0) |
| `run_nuked_md.py arbiter`, the `jmp (a0)` loop | 2-5, mean 3.55 | **2-4, mean 2.86** (referee 2-4, 2.74) |
| `run_nuked_md.py arbiter`, the `pop`'s first read | mean 4.07 | **mean 2.97** (referee 2.95) |
| `run_nuked_md.py sound`, the FM probe | 434 of 636 bus cycles agree | **568 of 636**; the 68 $A0xxxx stall differences are gone, 1 remains |
| `run_nuked_md.py probes`, eight loops | 2,921 of 3,546 | **unchanged** |
| `run_nuked_md.py roms`, ten ROMs | as milestone 6's table | **unchanged**, every figure |
| `pytest -q`, PyPy and CPython 3.14.4 | 46 passed, 1 deselected | **49 passed, 1 deselected** on PyPy and on CPython 3.14.4 (three new tests, one rewritten for the grant's propagation) |
| the nine milestone-3 test ROMs against MAME's frames (detector) | 100.00/100.00/100.00/99.84/96.93/96.25/69.64/41.97 % | **every figure identical** |
| frames a second on PyPy, 900 frames, `--no-sound`, paired against commit `86fa7e1` | 37.0-48.5 fps | **41.7-51.2 fps**, +0.6 % to +12.7 %: a frame is a fixed number of master clocks, so more stall clocks means fewer instructions to run |
| Overdrive 2's realised DAC rate (Kabuto's own driver, "26 kHz") | 26,313 Hz with the arbiter on, 27,028 without | **25,858 Hz** with it on, 26,318 without -- 0.6 % below his figure where it was 1.2 % above |

**What did not move that might have been expected to.** The referee's ROM comparison
is unchanged in every figure, including `gen_test_z80_bus` and `stetest`, which still
end at the $A11100 poll where the referee reads $67 and the board $66: with the Z80
*held in reset* the referee takes 235 MCLK from the write to /BUSAK, not the 68-118
it takes with the Z80 running, and one observation is not a constant to fit. The
probe loops are unchanged because none of them touches the Z80's space, the DMA or
the arbiter.

**What got worse, and why it is still the right change.** The `pop` experiment's pair
mean falls from 3.19 to 2.50 against the referee's 3.27: the board's inflated `a` had
been compensating for its inability to place the second of a `pop`'s two reads at its
own T-state. The first read now lands on the referee's figure (2.97 against 2.95) and
the second is plainly short (2.04 against 3.60), which is the z80-python limitation
`polish.md` records, now unmasked rather than cancelled out.

### Milestone 7: cartridges and peripherals

The judges here are the **research documents the brief names** -- Eke-Eke's
`gen_eeprom.pdf` for the EEPROMs, Bart Trzynadlowski's `ssf2tnc.txt` for the Sega
mapper, Terakawa's page and CMD's `gen-hw.txt` 3.2 for the pads, the TMSS boot ROM's
own disassembly and CMD's 4.4 for TMSS, the Manual's page 72 for the version register
-- and, for every one of them, **a game doing the right thing**. MAME 0.285 is a
detector: its own board guesses differ from its software list's for some of these
images, and for the 6-button pad it cannot detect anything at all (below).

**The pads** (`python scripts/run_pads.py`, 21 s; `--games`, 156 s).

- CMD's table, walked on the board's own bus by a 68000 program and compared byte for
  byte for **every one of the 4,096 combinations of the twelve buttons** on the
  6-button pad and 256 on the 3-button one: **34,816 of 34,816 bytes match**.
- **r57shell's `joy_speed_v1.02.bin`** (hardware-corrected, forum t=2308): the ROM's
  own bursts of reads are `$7F $33 $7F $33 ...` with the 3-button pad and
  `$7F $33 $7F $30 $7F $3F $7F $33` with the 6-button one, which is CMD's six rows.
  Its *heading* says "3 Buttons Pad" for both -- and for MAME's own 6-button pad too,
  whose code says why (`md6bt.cpp`: "TODO: doesn't work - reset happens before host
  inputs are read for the first time"), so MAME cannot judge this and the board agrees
  with it.
- **The games**: Super Street Fighter II drives the handshake to its third and fourth
  rising edge 2,368 times in 2,600 frames and Comix Zone 399 times in 600, while Sonic
  The Hedgehog and Streets of Rage 2 never do it once.
- **The timeout is not claimed.** Four published figures, three of them measuring
  different quantities, and Terakawa's own warning that it is a capacitor and
  "parameters differ one by one controller"; the board carries CMD's shape and figure
  as a field ([undocumented-behavior.md](undocumented-behavior.md), "The pads").

**The battery** (`python scripts/run_battery.py`, 90-125 s a game): five games run 40
emulated seconds from a blank battery, saved, reloaded into a fresh machine and run
again, against MAME's own `-nvram_directory` file. **Sonic The Hedgehog 3 writes 272
of its 512 FRAM bytes and every one equals MAME's; NBA Jam writes its EEPROM's
"Snake 1993" header and all 256 bytes equal MAME's, and on the warm start it reads
them back and writes nothing at all.** Phantasy Star IV, Wonder Boy in Monster World
and Evander Holyfield's Boxing write nothing without being played, so for them the
comparison says only that a blank chip stays blank. MAME's battery file is twice the
board's, one 16-bit word per address with the byte in both halves, and the comparison
unpacks it.

**The serial EEPROM** is Eke's protocol written from his document, modes 1, 2 and 3,
with `tests/test_milestone7.py` driving the two wires directly. His NBA Jam entry's
bit assignment is contradicted by the game and corrected
([undocumented-behavior.md](undocumented-behavior.md), "Cartridges" item 1).

**The Sega (SSF2) mapper** (`python scripts/run_mapper.py --titan`, 61 s and 94 s):
Super Street Fighter II makes 267 writes to $A130xx in 2,400 frames, the first two of
them `$A130FD = $08` and `$A130FF = $09` -- the checksum pair `ssf2tnc.txt` quotes from
the US dump at $003BEC -- and reaches its title screen rather than the red screen the
document says a wrong mapper gives. Overdrive 2 on the same mapper ends with pages 10
and 11 in the two top windows.

**TMSS** (`python scripts/run_tmss.py`, 14 s), with the local No-Intro boot ROM (2,048
bytes, SHA-1 `3f50b76b0529db7f79c396b5e808cc0786ffc311`, padded to 16 KB with zeros as
the hardware does): the licence message is on screen by frame 2, **the cartridge takes
the bus at frame 189 and Sonic The Hedgehog runs**, which is the brief's acceptance;
MAME's `genesis_tmss` at 7 emulated seconds agrees on **71,680 of 71,680 pixels
(100.00 %)**; a cartridge whose header is not 'SEGA' never gets the bus and leaves
$A14000 locked; and a cartridge that touches the VDP without writing "SEGA" locks the
machine up. The boot ROM is fitted by the caller (`System(..., tmss_rom=...)`), which
is why milestones 1 to 6 are unaffected.

**The region and version register** (`python scripts/run_region.py`, 126 s): all nine
machines' $A10001 decode to the Manual's MODE/VMOD/DISK/VER fields, and a 68000 reads
the same byte at the odd address, the even address and in both halves of a word (CMD
3.0). Mortal Kombat 3, Comix Zone and Streets of Rage 3 each split the nine machines
into exactly the four overseas NTSC ones, where they run, and the five PAL or domestic
ones, where they print "DEVELOPED FOR USE ONLY WITH NTSC GENESIS SYSTEMS". Against
MAME: 100.00 % of pixels on `megadriv` and `megadrij` for all three, and 100.00 %,
97.28 % and 91.23 % on `genesis`, where the games are running and an animation is a
frame or two out of phase.

**r57shell's VDP tests** (`python scripts/run_r57shell_tests.py`, 10 s): the ROM is
navigated with the D-pad -- which milestones 3 and 5 had wrong -- and all eight screens
are walked and measured. Test 1's column of spheres is drawn, test 2's is not, test 3
is a clean interlaced field, test 5 blinks now that register $00 bit 0 is modelled, and
**test 4 is not reproduced**: the only hardware statement about it is that a CRT rolled,
which a frame buffer cannot show. The MD2 report in forum t=1585 covers that one test
and nothing else.

**Milestones 1 to 6, re-run:**

| Run | Before | After |
| --- | --- | --- |
| the milestone-1 replay for the two battery games (traces re-recorded from MAME 0.285) | Sonic The Hedgehog 3: 21,741,798 68000 and 11,837,738 Z80 instructions, 0 unexplained; Phantasy Star IV: 21,254,417 and 13,280,703, 0 unexplained | (see [worklog.md](worklog.md), milestone 7) |
| Nemesis's HV/status captures, 288 dumps | 8,762,415 / 8,847,360 words | **8,762,415 / 8,847,360**, unchanged to the word |
| VDPFIFOTesting, nine machines | 122/122 on eight, 114/122 on genesis3 | **unchanged** |
| Nemesis's sprite masking ROM | 0 of 18 cells differ | **unchanged** |
| MoD's `gen_test_z80_bus` | 8 checks PASS, delay counters 0001/0001 | **unchanged** |
| `scripts/run_bus_arbiter.py` | 2.27 / 2.91 / 2.44 wait states, 481.0 clocks a line | **unchanged** |
| `scripts/run_dma_cost.py` | one word 7.4, fit 2.37 x words + 1.7; 16.1/167.1/18.1/205.1 bytes a line | **unchanged** |
| the nine milestone-3 test ROMs against MAME's frames | 100.00/100.00/100.00/99.84/96.93/96.25/69.64/41.97 % | **unchanged, every figure** |
| the PSG against Nuked-PSG, ten streams | 0 of 121,279,196 input clocks | **0 of 121,279,139** (the same figure a worktree at the session's starting commit gives, stream for stream) |
| the YM2612 against Nuked-OPN2, 18 streams, both variants | 0 of 110,549,790 x 3 x 2 | **0 of 110,502,778 x 3 x 2** -- 663,016,668 values, none different |
| `run_nuked_md.py roms`, ten ROMs | as milestone 6's table | **unchanged**, every figure |
| `pytest -q`, PyPy and CPython 3.14.4 | 49 passed, 1 deselected | **73 passed, 1 deselected** on both (154 s on PyPy, 404 s on CPython): 24 new tests |

### Milestone 8: the compatibility sweep (detector), and Sega's own diagnostic

**Sega's MD Soft Checker v0.30** (hardware-corrected, and the only *judge* in this
milestone): `.venv-pypy/bin/python scripts/run_soft_checker.py`, 96 s for all nine
machines. The ROM prints one line per check with its own verdict; the script reads
each line's last two glyphs off the rendered picture and the glyphs were read once
by eye. **17 result lines on every one of the nine machines, all seventeen with the
same verdict, and that verdict reads OK** -- the 68000's registers and whole
instruction set, work RAM, the Z80's bus request and RAM, VRAM, CRAM, VSRAM, the I/O
registers, DMA from ROM and from work RAM to each of the three memories, and the
VRAM fill and copy. Its header is the version register read back by Sega's own
program: `OVERSEA mode NTSC VER.0` on `genesis`, `OVERSEA mode PAL VER.1` on
`megadrive2`, `JAPAN mode NTSC VER.0` on `megadrive-japan`. At 600 frames it shows
its pad screen, which finds a 3-button pad on both ports. What it does not cover:
timing, the picture, and sound.

**The compatibility sweep (MAME 0.285, a detector)**: `scripts/compat_sweep.py
--seconds 10 --jobs 16 --window 12 --frames --offsets`, 14,423 s over the local
romset's 3,514 cartridge entries. **2,072 of the 2,673 present cartridges (77.5%)
draw a picture identical to MAME's, or identical a frame or two either side**
(1,610 `same`, 462 `phase`), and 2,072 of the 2,598 whose board the emulator models
(79.8%). The rest: 261 `differs`, 243 `mame-board`, 75 `unsupported`, 16 `size`, 5
`lockup`, 1 `error:`; 841 entries are broken symlinks in the romset and were not run.
Every one of the 16 `size` rows is now a genuine width or mode difference: the
renderer's two buffers (2026-09-24) retired all 17 partial-frame captures, which a
full re-sweep confirmed moved nothing else. The figure against modelled boards falls
from 80.3% the same day because Krikzz's Extended SSF mapper moved 19 cartridges out
of `unsupported`, and 17 of those are cartridges the board now runs and MAME cannot
-- Earthion's title screen against MAME's noise -- so no comparison against MAME can
score them.

These are the figures **after** the two fixes of 2026-09-24 (docs/worklog.md, "Two
fixes taken from the triage"): the oversize guard, which moved 29 images to
`unsupported` and cost eight cartridges a `same` they had matched on a truncated
image, and `load_image` picking the cartridge rather than a zip's first member,
which recovered `Sonic & Knuckles (World)` from `error:` to `same`. As first run the
sweep read 1,609 `same` and 77.4%. This is a detector's figure and is
not a pass rate -- MAME is never the judge, and several of the differences are
MAME's own.

**The triage, pass 1 (sensitivity, no oracle)**: 60 differing cartridges re-run with
one of the board's models disabled at a time. **The largest single cause is MAME's**
-- 31 of 60 move when the cartridge is put on the `SEGA_SRAM_FALLBACK` board MAME's
file loader would choose -- and **14 of 60 move for none of the five models**, so
the board's timing is not implicated in those. Work-RAM refresh 26, cartridge
refresh 26, arbiter 15, DMA start-up 8. Nothing was changed on the strength of it.

**The triage, pass 2 (lockstep against MAME, a detector)**: 298 cartridges, 9,902 s.
181 `vdp-reads`, 112 `cpu`, 4 `identical`, 1 `fm-status`. `vdp-reads` dominating is
by construction: the lockstep runs with both refreshes and the arbiter off, because
MAME models none of them.

**Four cartridges ran identically and still drew a different picture**: `Breach
(USA) (Proto)`, `Joshua & the Battle of Jericho (USA) (Unl)`, `Zany Golf (USA)
(Unl)` and `Exodus - Journey to the Promised Land (USA)` each executed 5.9 to 6.8
million 68000 instructions with **no read differing from MAME's**, yet the board
draws 256x224 where MAME draws 320x224. With execution and every device read
identical, the difference is in the width the renderer settles on and nothing else.
Ten more cartridges show the same shape. Open, and recorded as a QUESTION.

**What this milestone does *not* claim**: nothing about sound (the sweep runs with
the generator off), nothing about timing beyond what the sensitivity pass
implicates, nothing about the 65 cartridges whose board is not modelled, nothing
about the 29 oversized images the loader silently truncates (see
[polish.md](polish.md)), and nothing hardware. The only judge in milestone 8 is
Sega's MD Soft Checker.
