# Start here: the Mega Drive / Genesis as a board

This is a primer for the session that builds the board. It says what the
machine is, where each fact comes from, and which facts are soft. Numbers
with no source here are not to be trusted; the sources are pinned in
[validation.md](validation.md) and fetched into `reference/` and
`tests/test_roms/` by `scripts/fetch_references.py`.

Short names used below:

- **Manual**: Sega's *Genesis Software Manual*
  (`reference/manuals/GenesisSoftwareManual-150p.pdf`, the scan with a text
  layer; PDF page numbers). The 183-page scan is the fuller copy; the
  *Technical Bulletins* correct the manual where they differ.
- **CMD**: Charles MacDonald's documents (`reference/docs/cmd/`): `gen-hw.txt`
  (the board, "checked on the real thing"), `genvdp.txt` (the VDP, "very
  preliminary"). Research tier, 2000–2001.
- **Kabuto**: Kabuto's *Mega Drive hardware notes* v1.5
  (`reference/docs/kabuto-hardware-notes.html`), written from four years of his
  own test programs, oscilloscope and audio measurements for Overdrive 2.
  Hardware-measured research: the values are strong, the explanations are his.
- **Nemesis**, **MoD** (Mask of Destiny, BlastEm's author), **Eke** (Genesis
  Plus GX's author): SpritesMind threads, snapshotted in `reference/forum/`.
- **Plutiedev**: Sik's programmer's pages (`reference/web/plutiedev-*.html`).

## The machine in one paragraph

A Motorola 68000 at 7.67 MHz (7.60 MHz PAL) runs the game from a cartridge of
up to 4 MB, with 64 KB of work RAM. A Zilog Z80 at 3.58 MHz runs the sound
driver from its own 8 KB of RAM, drives the Yamaha YM2612 FM chip, and can
reach the 68000's address space through a 32 KB banked window. The VDP (Sega
315-5313, made by Yamaha as the YM7101) draws two scrolling tile planes, a
window plane and 80 sprites from its own 64 KB of VRAM, contains the Sega
PSG (an SN76489 descendant), and keeps the Master System's Mode 4 for backward
compatibility. An I/O chip serves three ports (two pads and an expansion
port) and a version register. The 68000 owns the main bus; the Z80, the VDP's
DMA and the 68000 share it through an arbiter. Every one of these has
behaviour the manual does not describe, which is what the rest of the
documents are about.

## Clocks

One master crystal feeds everything (MAME `sega/megadriv.cpp`,
`MASTER_CLOCK_NTSC` and `MASTER_CLOCK_PAL`; the Manual's version register
gives the CPU clocks):

| | NTSC | PAL |
| --- | --- | --- |
| Master clock (MCLK) | 53,693,175 Hz | 53,203,424 Hz |
| 68000 = MCLK / 7 | 7.670 MHz | 7.600 MHz |
| Z80 = MCLK / 15 | 3.580 MHz | 3.547 MHz |
| YM2612 = MCLK / 7, one sample per 144 clocks | 53,267 Hz | 52,781 Hz |
| PSG = MCLK / 15 (the Z80's clock) | 3.580 MHz | 3.547 MHz |
| VDP: a line is 3,420 MCLK | 488 4/7 68000 cycles | same |
| | 228 Z80 cycles | same |
| Lines per frame | 262 (59.92 Hz) | 313 (49.70 Hz) |

The VDP's pixel clock is MCLK / 10 in H32 mode (342 pixels per line) and
MCLK / 8 in H40 mode (420 pixels per line), except that in H40 it slows down
for part of the horizontal sync ("32 pixel clocks according to my
oscilloscope tests", Kabuto, "Picture size"; Jorge Nuno measured the H40 line
as 3,420 MCLK with an oscilloscope, forum t=519). So both modes keep the
same 3,420-MCLK line. [timing.md](timing.md) has the details.

## 68000 memory map

From CMD `gen-hw.txt` section 1 and the Manual (PDF pages 12–14):

| Range | What |
| --- | --- |
| $000000–$3FFFFF | Cartridge ROM (4 MB; larger ROMs use a bank mapper, see "Cartridges") |
| $400000–$7FFFFF | Expansion (Sega CD); unused reads return the prefetched opcode |
| $800000–$9FFFFF | 32X; any access without one locks the machine (CMD) |
| $A00000–$A0FFFF | The Z80's space: its RAM, the YM2612, the bank register; the 68000 must hold the Z80's bus |
| $A10000–$A1001F | I/O chip: version register, three ports' data, control and serial registers |
| $A11000 | Memory mode (DRAM development cartridges) |
| $A11100 | Z80 bus request (write bit 8 = request; read bit 8 = 0 when granted) |
| $A11200 | Z80 reset (write bit 8 = 0 to hold the Z80 and YM2612 in reset) |
| $A14000 | TMSS: write "SEGA" to unlock the VDP on TMSS consoles |
| $A130F1–$A130FF | Cartridge registers (SRAM enable, the SSF2 bank mapper) |
| $C00000–$C0001F | VDP: data port, control port, HV counter, PSG, debug register; mirrored across $C00000–$DFFFFF by a bit pattern (CMD gives it) |
| $E00000–$FFFFFF | 64 KB work RAM, mirrored; games use $FF0000–$FFFFFF |

What the unmapped areas return matters: reads of open 68000 bus return the
**next prefetched opcode word** (CMD, "Reads return the MSB of the next
instruction", and Kabuto on $C00018/$C0001C). m68000-python exposes its
prefetch queue, so the board can reproduce this.

Byte and word access rules differ per device, and games depend on them (CMD
section 1.2): a byte write to a VDP port is a word write with the byte
duplicated; a word write to Z80 RAM stores only the high byte; a word read of
Z80 RAM duplicates the byte.

## The Z80 side

From CMD `gen-hw.txt` sections 2.1–2.4 and the Manual (PDF pages 12, 89–90,
106):

| Z80 range | What |
| --- | --- |
| $0000–$1FFF | 8 KB sound RAM (mirrored at $2000–$3FFF) |
| $4000–$4003 | YM2612 (address/data for parts I and II), mirrored to $5FFF |
| $6000 | Bank register: nine writes of bit 0 build 68000 address bits 15–23 |
| $7F00–$7F1F | VDP (only the HV counter and PSG are useful; beyond $7F1F locks up) |
| $8000–$FFFF | The banked 32 KB window onto the 68000's space |

- The Z80 runs in interrupt mode 1. It gets an IRQ from the VDP at the start of
  vertical blanking every frame, whatever the 68000's interrupt enables say;
  if the Z80 has interrupts disabled then, the IRQ is missed (CMD 2.4). How
  long the line stays asserted is not in a hardware source here: Genesis Plus
  GX holds it to the end of that line (`core/system.c`), an emulator's
  choice ([undocumented-behavior.md](undocumented-behavior.md)).
- The Z80 reading **68000 RAM** through the bank fails and writing works
  ("writes always worked and reads always failed", MoD's `gen_test_z80_bus`,
  tested on several machines, forum t=3021; CMD saw reads return $FF). Reading
  the bank at $A00000–$A0FFFF (the Z80's own space) locks the machine.
- Each Z80 access to the 68000's bus pauses the 68000 through the arbiter: "the
  average Z80 delay is around 3.3 Z80 cycles, whereas the average 68k delay is
  around 11 68k cycles" (Kabuto, "The bus system"; MoD measured 2–5 wait
  states with a logic analyser, forum t=1661). Games' sample players depend on
  it ("My music player relies on the Z80 getting delayed for ROM accesses",
  Kabuto).
- "On power-up, the Z80 will be reset and will have the bus" (CMD 2.2), so the
  68000 must request the bus before loading the sound driver; CMD gives the
  start-up sequence games use.

## The VDP (315-5313 / YM7101)

The Manual's VDP chapter is the specification; Plutiedev's `vdp-registers`
and `vdp-commands` pages are the practical summary; CMD `genvdp.txt` fills
gaps but is preliminary; Kabuto and Nemesis are the measured corrections.

**Memories.** VRAM 64 KB (128 KB mode exists and is abused by Overdrive 2:
Kabuto, "128 KB mode abuse"); CRAM 64 colours of 9 bits (3 bits per
component); VSRAM 40 entries of 10 bits (the Genesis 3's VSRAM is a
different size, which is why it fails some of Nemesis's tests: see
[undocumented-behavior.md](undocumented-behavior.md)).

**Ports.** $C00000 data, $C00004 control (register writes, address set-up,
DMA start; reading it returns the status register), $C00008 the HV counter,
$C00011 the PSG, $C0001C the debug register (Kabuto, "The mighty debug
register"). Writes go through a **4-entry FIFO** (Nemesis's VDPFIFOTesting measures its
size); a write to a full FIFO
stalls the 68000, a read waits for the VDP's next access slot. Nemesis's
VDPFIFOTesting is the hardware-checked specification of this behaviour.

**Registers** $00–$17: two mode registers (display, interrupts, DMA enable,
V28/V30), the plane A, window, plane B, sprite table and H scroll table
bases, the backdrop colour, the H interrupt counter (reg $0A), mode registers 3
and 4 (scroll modes, external interrupt, H32/H40, shadow/highlight,
interlace), the auto-increment, plane size, window position, DMA length and
source (Manual PDF pages 19–33; Plutiedev `vdp-registers`).

**Access codes** in the control word select VRAM, CRAM or VSRAM, read or
write, and whether a DMA starts (Manual; Plutiedev `vdp-commands`).

**DMA** has three kinds: 68000 memory to VDP memory (the 68000 is taken off
the bus), VRAM fill, and VRAM copy (both internal; the 68000 keeps running).
Speeds are in [timing.md](timing.md).

**Display.** H32 (256 pixels) or H40 (320) wide, V28 (224 lines) or V30 (240,
PAL only in practice), progressive or two interlace modes (the second doubles
vertical resolution with 8×16 tiles). Planes A and B scroll per screen, per
8-line cell or per line horizontally, per screen or per 2-cell column
vertically; the window replaces plane A over a rectangle; 80 sprites (64 in
H32) with a linked list, 20 per line (16 in H32) and a per-line pixel budget;
per-tile priority; shadow/highlight mode darkens or brightens by priority and
by two special sprite colours (Kabuto, "S/H", corrects the manual's
description).

**Interrupts** to the 68000 (Manual, PDF page 19): level 6 vertical
interrupt, level 4 horizontal interrupt (every *n*+1 lines, reg $0A), level 2
external interrupt (the light gun's TH line). The 68000 autovectors.

**Mode 4.** The VDP still runs the Master System's Mode 4 for the Power Base
Converter, with differences (sprites are never doubled; no 224/240-line modes;
Kabuto, "Mode 4"; SMS Power's pages). sms-python implements Mode 4 on the
SMS VDPs; the Mega Drive's variant is a late, optional milestone.

## Sound

- **YM2612 (OPN2)**: six 4-operator FM channels, channel 3's special mode
  (per-operator frequencies, used for CSM speech), SSG-EG envelopes, an LFO,
  two timers, and channel 6's 8-bit DAC mode used for samples. Model 1 and
  early Model 2 consoles have the NMOS YM2612; later ones integrate a
  YM3438-like core in an ASIC. The two differ audibly: the YM2612's DAC has
  a crossover distortion (the "ladder effect", named by HardWareMan from
  oscilloscope traces), and the BUSY flag is readable from port 0 only on the
  discrete chip (Eke's 2017 measurements, forum t=386 start=810). No Yamaha
  YM2612 manual is available; the YM2608 (OPNA) manual (Nemesis's translation
  pinned) is the nearest specification, and the YM3438 application manual is
  inside Nemesis's `FMDocs.7z`.
- **PSG**: the Sega PSG inside the VDP, the same design sms-python
  implements (three tones, one noise, 16-bit LFSR tapped at bits 0 and 3),
  written at $C00011 by the 68000 or $7F11 by the Z80.
- The two are mixed in analogue circuits that differ per board revision
  (Kabuto: the Model 1 has a first-order filter, the Model 2 a second-order one
  and more distortion). The MDFourier recordings capture 21 boards' output.

## I/O, pads, cartridges, TMSS

- **Version register** $A10001 (Manual PDF page 84): bit 7 overseas (1) or domestic,
  bit 6 PAL (1) or NTSC, bit 5 clear when an expansion unit ("FDD unit" in the
  Manual; the Sega CD in practice) is attached, bits 3–0 the hardware version
  (0 on consoles without TMSS; the Manual's security code tests it).
- **Ports** A, B and EXT each have a data, a control (direction) and serial
  registers (CMD 3.0). A 3-button pad multiplexes its buttons on the TH line;
  the 6-button pad adds a counter that advances on TH's rising edges and times
  out. CMD 3.2 and Terakawa's page (`reference/web/terakawa-6button.html`) give
  the same six rows of pin values; the timeout they give is not the same
  quantity -- CMD "about 8192 (probably less than that) 68000 CPU cycles" since
  TH last changed, Terakawa 1.1, 1.6 and 1.8 ms from the *first* rising edge --
  and it depends on a capacitor, so it "differ[s] one by one controller" (his
  words). Do not fit one number to it.
- **Cartridges**: plain ROM up to 4 MB; battery SRAM at $200000 (switched in
  and out by $A130F1); I²C EEPROMs in a few games (Eke's `gen_eeprom.pdf` in
  `md_tech.zip`); the Super Street Fighter II bank mapper at $A130F3–$A130FF
  for ROMs over 4 MB (`ssf2tnc.txt` in `md_tech.zip`; Plutiedev `beyond-4mb`).
  MAME's software list (`/usr/share/games/mame/hash/megadriv.xml`) names the
  board for each known dump, as in sms-python.
- **TMSS** (Trademark Security System): consoles with a nonzero version number
  have a boot ROM that shows "PRODUCED BY OR UNDER LICENSE FROM SEGA
  ENTERPRISES LTD." and requires the cartridge to write "SEGA" to $A14000
  before using the VDP (Manual PDF page 94 gives the "U.S. security software"
  games must include). It powers up in the cartridge's place, and **bit 0 of
  $A14101 chooses between the two** -- 1 the cartridge, 0 the boot ROM (CMD
  `gen-hw.txt` 4.4; the boot program does it with BSET and BCLR while running
  from work RAM). The chip holds 2,048 bytes and the hardware pads them to 16 KB
  with zeros; the ROM is locally available as a No-Intro dump
  (`/data/emu/roms/megadrive/[BIOS] Sega Mega Drive - Genesis - Boot ROM
  (World).zip`), with a disassembly that rebuilds it bit for bit in
  `reference/src/TMSS-Disassembly`. A 2026 write-up claims Japanese consoles check
  only the header ([undocumented-behavior.md](undocumented-behavior.md)).

## The models

Model 1 (VA0–VA7), Model 2 (VA0–VA4), Model 3 (Genesis 3, Majesco) and the
Nomad and CDX differ in the VDP revision (315-5313 versus 315-5313A: a
manufacturing change that breaks about 30 % of Model 1s on Overdrive 2), the
YM2612 or its ASIC successor, TMSS, the audio circuit, and the Genesis 3's
VSRAM. A board model should make these fields of a machine description, as
sms-python does for the SMS1/SMS2/Game Gear.

## Where the cores fit

- **m68000-python** ([GitHub](https://github.com/alewman/m68000-python), public) is the
  68000: `M68000CPU(read_byte, read_word, write_byte, write_word)`, `step()`
  returns clocks, `set_ipl(level)`, an `acknowledge` hook for autovectors, and
  a `tas_write` hook (the Genesis bus drops TAS's write: its
  `docs/undocumented-behavior.md`). About 20 million instructions a second on
  PyPy; the Mega Drive needs about one million.
- **z80-python** 0.4.0 (PyPI) is the Z80, through the same callables contract
  sms-python uses. The Mega Drive's Z80 is an NMOS part.
- **sms-python** (the author's Master System board, not public) has the Sega
  PSG, a Mode 4 renderer, the MAME sweep and triage tooling, and a desktop frontend, all of which this
  board can reuse or copy the design of (same author, MIT).
