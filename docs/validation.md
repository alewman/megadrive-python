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
| BlastEm | GPL-3.0 | `reference/src/blastem` @ 542164d (libretro's git mirror) | first to run Overdrive 2 correctly; passes VDPFIFOTesting and Direct Color DMA; CRAM dots; its author's own logic-analyser work |
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
- **MD Soft Checker v0.30** (Sega's diagnostic): the local symlink is broken and
  the only link is a mega.nz page.
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
