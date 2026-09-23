# Timing

Everything on the board is a fraction of one master clock (MCLK, 53,693,175
Hz NTSC, 53,203,424 Hz PAL; [start-here.md](start-here.md), "Clocks"). The
68000 ticks every 7 MCLK, the Z80 and PSG every 15, the YM2612 every 7 (one
output sample per 144 of its clocks), the VDP every 8 or 10 depending on the
horizontal mode. A board that schedules in MCLK can place every event exactly
and convert at the edges; one that schedules in 68000 cycles has to carry
fractions for the Z80 (15/7) and the VDP (a line is 488 4/7 cycles).

Sources and their tiers are in [validation.md](validation.md). The hardware
tables here come from Nemesis's 2010 logic-analyser captures (forum t=768; the
raw dumps are `tests/test_roms/nemesis/sr logic analyser results.zip`) as
tabulated by Kabuto ("H/V counter values"), and from Kabuto's own
measurements. Where only an emulator gives a number, it says so.

## The line and the frame

A line is 3,420 MCLK in both horizontal modes (Jorge Nuno's oscilloscope
measurements, forum t=519: an NTSC H40 line of 420 pixels is 3,420 MCLK).
H32 is 342 pixels at MCLK / 10. H40 is 420 pixels at MCLK / 8, with the
pixel clock slowed for part of the horizontal sync to make up the difference
("the VDP slows down its clock for a number of cycles during HBlank (32 pixel
clocks according to my oscilloscope tests) - starting 24 pixels after the
right edge", Kabuto, "Picture size").

Lines per frame, from Kabuto ("Picture size"), V28 (224 active lines):

| | Top border | Picture | Bottom border | Sync | Blank | Total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| NTSC | 11 | 224 | 8 | 3+3+3 | 10 | 262 |
| PAL | 38 | 224 | 32 | 3+3+3 | 10 | 313 |

V30 (240 lines) takes 8 lines from each border. On NTSC it leaves no border
and no sync ("0x000-0x1FF" in the V counter table below): the picture rolls,
as it does on the SMS in its 240-line mode. Horizontally there are 13 left and
14 right border pixels (Kabuto).

Interlace modes alternate fields; on odd frames the NTSC field has an extra
line in both interlace modes (Nemesis, acknowledged in forum t=768).

## The H and V counters

Read at $C00008 (V in the high byte, H in the low byte; "VC0 is replaced with
VC8 when in interlace mode 2", CMD `genvdp.txt` citing the Manual). From Nemesis's captures
(Kabuto's table, "H/V counter values"):

| | H32 | H40 |
| --- | --- | --- |
| H counter sequence | $00–$93, $E9–$FF | $00–$B6, $E4–$FF |
| V counter increments when H goes | $84 → $85 | $A4 → $A5 |
| HBlank flag set when H goes | $92 → $93 | $B2 → $B3 |
| HBlank flag cleared when H goes | $04 → $05 | $05 → $06 |

| | NTSC V28 | PAL V28 | NTSC V30 | PAL V30 |
| --- | --- | --- | --- | --- |
| V counter sequence | $000–$0EA, $1E5–$1FF | $000–$102, $1CA–$1FF | $000–$1FF | $000–$10A, $1D2–$1FF |
| VBlank set when V goes | $DF → $E0 | $DF → $E0 | $EF → $F0 | $EF → $F0 |
| VBlank cleared when V goes | $1FE → $1FF | same | same | same |
| F (vertical interrupt) flag set | H $00 → $01 on V $E0 | same | H $00 → $01 on V $F0 | same |

The H counter counts every other pixel (it is the internal 9-bit counter
shifted; Eke confirms the internal sequence in forum t=768). CMD's `m5hvc.txt`
has the same ranges but calls itself "probably off by one or two"; do not use it
for positions. Where each value falls in MCLK, read out of the raw captures, is
the next section.

## What the captures say, sample by sample (milestone 2)

`scripts/run_hv_captures.py` runs each of Nemesis's 288 dumps (32 captures: "HV"
and "SR" ROMs, PAL Model 1 and NTSC-J Model 1, H32/H40 x V28/V30, no interlace
and the three interlace settings; each ROM stores 15,360 pairs of reads in 30,720
words) on the board and compares every word. What the board cannot know is
fitted per dump: where in its line the VDP is, the 9-bit V and ODD flag, whether
the H mode written before the loop has latched, and the phases of the two refresh
stalls. The fit is a search over every phase pair (7-MCLK steps) and every line
position the first sample allows, scored on the H counter alone; the survivors are
scored on H and V; the board then runs the loop from each best placement. Nothing
else is fitted per dump. Result at the commit that adds this section (PyPy 7.3.23 /
Python 3.11.15, 78 s on 16 processes):

- **8,762,415 of 8,847,360 words equal (99.04 %).** Of the 84,945 others,
  61,440 are two dumps the search could not place at all, 11,103 one dump
  (below), 9,869 reads at a sampling phase where a counter steps (next
  paragraphs), and 2,533 the differences listed under "Not explained".
- Per capture: the non-interlaced HV and SR captures agree on 99.74-99.99 % of
  words, with at most a few differences away from the stepping phases each; the
  interlaced ones on 99.5-99.99 % except the four with the dumps below.
  Per-capture counts come from the script (`--verbose` gives every dump) and are
  kept in `tests/hv_captures_baseline.json`, which the tests hold the board to.

**Line timing.** Measured from the V counter's increment point (internal H $10A
in H32, $14A in H40), a pixel lasts:

| Line | Pixels (internal H) and MCLK each | Total |
| --- | --- | ---: |
| H32 | $10A-$127, $1D2-$1FF, $000-$109: 10 | 3,420 |
| H40 | $14A-$16C, $1C9-$1CC: 8; $1CD-$1D3: 10; $1D4-$1D5: 9; $1D6-$1DC: 10; $1DD: 8; $1DE-$1E4: 10; $1E5-$1E6: 9; $1E7-$1ED: 10; $1EE-$1FF, $000-$149: 8 | 3,420 |
| H40, VSYNC lines | as H40 but $1DE-$1ED: 8, and $09F-$0A5: 10, $0A6-$0A7: 9, $0A8-$0AE: 10 | 3,420 |
| H40, interlaced odd field, first VSYNC line | as H40 plus the moved run at $09F-$0AE | 3,450 |
| H40, interlaced odd field, last VSYNC line | as H40 but $1DE-$1ED: 8 | 3,390 |

The H32 and ordinary H40 lines are Exodus's tables (Nemesis's), confirmed. The
VSYNC lines are new: on the nine lines ending on V = jump + 5 (PAL V28 $100-$102
and $1CA-$1CF, PAL V30 $108-$10A and $1D2-$1D7, NTSC V28 $0E8-$0EA and
$1E5-$1EA; none in NTSC V30, which has no VSYNC) H40's second run of long pixels
is at $09F-$0AE instead of $1DE-$1ED (7,681 of 7,724 PAL V28 samples on those
lines agree, 4,043 with the ordinary line). In an interlaced odd field the lines
with the moved run end one line earlier than those with the short $1DE-$1ED, so
the first is 30 MCLK long and the last 30 short. The V sequences, the HBlank and
VBlank flags and F are as in the tables above, at internal H $126/$166 (HBlank
set), $00A/$00B (cleared) and $001 (F); in interlace the port shows V bits 7-1
and 8 (doubled: 6-0 and 7), and an odd field adds line jump - 1.

**Sampling.** A read of the HV counter or the status register sees the counters
at the first pixel-clock edge at or after 3 MCLK into its bus cycle. At three
phases the read lands as a counter steps: the pixel before that edge began 1
MCLK (*edge*) or 2 MCLK (*late*) before the sampling point, or the sampled pixel
begins exactly at it (*next*). What a read there shows is analogue: the two
consoles differ, and one console's reads at the same phase vary between runs and
even within one. The board copies each console's majority (`vdp.HV_UNITS`):

| | PAL Model 1 | NTSC-J Model 1 |
| --- | --- | --- |
| H32, edge | old AND new if the step changes bit 2 or above, else new | the same from bit 3 |
| H32, next | clean | the three low bits read clear if the coming step carries past them |
| H32 jump $127 → $1D2, edge | $1C2 (54 of 67 reads; $1E2 otherwise) | $1C2 |
| H40 8-MCLK pixels, edge | new | old AND new from bit 2; jump $16C → $1C9 reads $148 |
| H40 long pixels, edge | old | old |
| H40 long pixels, late | new | old AND new from bit 2 |
| V (H32), step carrying into bit 1 | old (13 of 22; old OR new otherwise) | old OR new |

A step that carries changes its low bits first: bits that clear have cleared and
the bit that sets has not (a ripple counter). 9,869 reads at these phases differ
from the board anyway (outside the one dump below); they are counted apart.

**Not explained, and not claimed:**
- Two dumps are not placed: "HV noint intdub NTSCJ H40V28" 1, whose H low nibble
  reads 3 in every sample and whose status shows DMA busy (not the counters at
  all, as far as the board can tell), and "HV noint intinv PAL H40V30" 1.
- "HV noint intinv PAL H40V28" 6: 11,103 differences, H reads one step behind
  the board's in every second sample; the other eight dumps of that capture
  agree.
- "HV noint intnorm PAL H32V30" 1 reads F set from its first sample; the first
  run of every other capture reads it clear until its first VBlank.
- The first runs of "intnorm NTSCJ H32V28" and "H40V28" read VBlank set on the
  lines before the first VBlank (668 samples; 37 more VBlank reads elsewhere).
- The HBlank flag differs on 228 status reads over all captures, a pixel from its
  edges; the sprite overflow flag (bit 6) on 10 (no sprites are drawn).
- 302 H or V reads away from the stepping phases, most of them in the interlaced
  NTSC V30 H32 captures.

The HV counter can be latched (mode register 1 bit 1; used by light guns via
the external interrupt). Jorge Nuno's 2022 captures with a "fake 68000" give
the status register's trip points in H40 (mddc.dev thread, snapshotted); his
raw data is on a pastebin the snapshot links to.

## Interrupts

- **Vertical (68000 level 6)**: the F flag is set at H $00 → $01 on the first
  line of vertical blanking (table above); the 68000's IPL follows when mode
  register 2 enables it. The exact delay from the flag to the IPL change, and
  to the 68000 taking it, is the kind of number VDPTEST
  (`md_test.zip` → `VDP_TEST.zip`) and BlastEm's `gen_test_hv.s68` probe;
  neither publishes hardware values in the pinned material. Settle it from the
  logic-analyser threads (MoD, forum t=1291, gives an HSYNC-to-ODD-flag and a
  HINT-to-HSYNC count) or leave it open.
- **Horizontal (level 4)**: register $0A is a down-counter decremented on each
  line of the active display and reloaded outside it; the interrupt fires when
  it underflows (Manual; Plutiedev `vdp-registers`). With the vertical border
  opened (below), it "runs continuously and is not reset before the start of
  each frame" (Kabuto).
- **External (level 2)**: the TH pin of a port set to interrupt (light guns).
- **Z80**: an IRQ once a frame at the start of vertical blanking (CMD
  `gen-hw.txt` 2.4), missed if the Z80 has interrupts disabled. Its length is
  open ([undocumented-behavior.md](undocumented-behavior.md)).

## VRAM access slots

The VDP makes one 32-bit VRAM access every two pixel clocks: 210 per line in
H40, 171 in H32 (Kabuto, "Memory access pattern", confirmed by Nemesis's and
MoD's logic-analyser work, forum t=851 and t=1291). The order within a line
(H = H scroll, A/B = plane name fetches, a/b = plane pattern data, S/s =
sprite, ~ = an external slot for the CPU or DMA, r = refresh):

```text
H40: Hssss AsaaBsbb ((A~aaBSbb)*3 AraaBSbb)*5 ~~ s*23 ~ s*11
H32: Hssss AsaaBsbb ((A~aaBSbb)*3 AraaBSbb)*4 ~~ s*13 ~ s*13 ~
```

So H40 has 18 external slots a line in the active display, H32 16 (MoD, forum
t=1291: 171 slots, 4 refresh, 16 CPU). With the display disabled, and during
vertical blanking, every slot except the refresh slots is external (Kabuto:
"When display is disabled the VDP still does those refresh cycles and uses all
other cycles as access slots"). Where the external slots fall matters for the
FIFO: a 68000 write that finds the FIFO full waits for the next external slot.

**How many slots an idle line has** (settled in milestone 5). Exodus's
`GetPixelClockTicksUntilNextAccessSlot` skips *two* slots per refresh, which
leaves 161 in H32 and 198 in H40. Two hardware sources say one: Kabuto's
pattern above has a single `r` per group, and the Manual's DMA transfer
capacity table (below) gives 167 and 205 bytes a line in blanking, which is
171 − 4 and 210 − 5. The board takes one slot per refresh, at the H counter
values Exodus's own tables name as the refresh slots — five in H32 and six in
H40, "one more than there is during an active line" (Exodus), the extra one
falling in the horizontal blank. That gives **166 and 204**, and reproduces
every figure of the Manual's table to one byte
(`scripts/run_dma_cost.py`).

**Where the display's pixels are.** The active display is external H $00–$9F
in H40 and $00–$7F in H32 — internal H $000 onwards, 320 or 256 pixels (CMD
`genvdp.txt`; the V counter increments after it, at external $A5/$85). Kabuto's
slot groups fall into place around that: his 15 "mid-tiles" slots are the ones
inside $000–$13F, his two "between-tiles-and-sprites" ones are $140 and $142
just past it, and his single "mid-sprite" one is $1CE, in the blanking. What
the board does **not** model is his statement that the second mid-tiles slot
lands "at the pixel with X=-3 (display area coordinates)", which would put the
output about 21 pixels behind the H counter; a mid-line write lands at the pixel
its access slot's own H counter names (milestone 5). CRAM writes during the
display (the "CRAM dots") appear there too.

## DMA

Kabuto ("DMA speed"), in 68000 cycles beyond the instructions' own:

- **Fast** (to CRAM, to VSRAM, to VRAM in 128 KB mode): words × 2.4 + 5.6.
  Limited by how fast the VDP fetches: one word per internal cycle, except two
  skipped for each of five refresh cycles per line.
- **Slow** (to VRAM in 64 KB mode): max(words × 2.4 + 5.6, words × 4.7 − 6).
  Limited by VRAM writes once the FIFO is full.

During a 68000-memory DMA the 68000 is off the bus (Nemesis's
`TestDMATransferBusLock` in VDPFIFOTesting). Fill and copy run inside the VDP
and do not stop the 68000. The Manual's "DMA transfer capacity" table (PDF
page 44) agrees with the slot counts:

| Bytes per line | H32, display | H32, blanking | H40, display | H40, blanking |
| --- | ---: | ---: | ---: | ---: |
| 68000 memory to VRAM | 16 | 167 | 18 | 205 |
| VRAM fill | 15 | 166 | 17 | 204 |
| VRAM copy | 8 | 83 | 9 | 102 |

(The copy figures are partly illegible in the text-layer scan; check them in the
183-page scan.) With the display disabled, the blanking figure applies on
every line.

**What the board gives** (milestone 5, `scripts/run_dma_cost.py`, display off,
68000 memory to VRAM): 16.1, 167.4, 18.1 and 205.9 bytes a line, against the
Manual's 16, 167, 18 and 205. Per word, fitted over 64 to 512 words in H40:
**2.38 68000 cycles** for a fast transfer (CRAM, VSRAM, VRAM in 128 KB mode)
against Kabuto's 2.4, and **4.78** for a slow one (VRAM in 64 KB mode) against
his 4.7. His intercepts are not reproduced: he measures +5.6 cycles of start-up
on a fast transfer where the board charges −3.5, so a one-word transfer costs
him 8 cycles and the board about 1. The board holds the 68000 from the end of
the control write that starts the transfer until the last word is *read*, which
is where his "the DMA ends while there is still data in the FIFO to be
processed, this is why there are seemingly negative costs" comes from; the
start-up he measured has no mechanism here and is left open.

## Bus sharing

- **Z80 access to the 68000's bus** (the bank window): "delayed by 2 to 5
  cycles ... The average Z80 delay is around 3.3 Z80 cycles, whereas the
  average 68k delay is around 11 68k cycles" (Kabuto, "The bus system"). MoD
  measured it with a logic analyser on a Genesis 2 (forum t=1661): with the
  68000 stopped, 2–3 wait states and 2 cycles from /BR to /BG; with it running,
  4 wait states; an average just under 3 over 16 accesses.
  Modelled in milestone 5 from Kabuto's footnote 1, which gives the arithmetic
  behind his averages (`src/megadrive_python/arbiter.py`); the board's figures
  are in [worklog.md](worklog.md) and `scripts/run_bus_arbiter.py` measures them
  by running MoD's two experiments.
- **The bus grant to the 68000** ($A11100 to BUSACK): the Z80 samples BUSREQ
  "with the rising edge of the last clock period of any machine cycle" and lets
  go after it (Zilog UM0080), so the grant falls at the end of the machine cycle
  in progress and a 68000 access to the Z80's space before then is held by the
  arbiter. No hardware measurement of the Mega Drive's own delay is published
  ([undocumented-behavior.md](undocumented-behavior.md), open item 8).
- **A refresh steals 2 of every 128 68000 cycles**: "every time 128 cycles
  have passed the next bus access is slowed down by 2 cycles" (Kabuto), except
  when the 68000 is writing to the VDP. After all slow-downs "the 68K seems to be
  running at roughly 480 cycles per raster line instead of the theoretical
  488+4/7". Work RAM accesses have their own further slow-down he did not
  measure. The board's model (`board.py`), made exact against Nuked-MD
  (die-derived; a detector) and judged by the captures, whose sampling loop it
  reproduces to the MCLK: a fixed 896-MCLK grid; the first 68000 bus cycle to
  start at or after each tick absorbs it, and takes 14 MCLK longer only if it is
  a cartridge access. Separately, the next work-RAM access once one is due waits
  21 MCLK, and the next falls due 805 MCLK after that access started (Nuked-MD
  allows 743-826 for the capture loop; which is right is open). In the capture
  loop that gives 143.588 MCLK per `MOVE.L (A0),(A1)+`, as the hardware.
  With every stall the board charges, a 68000 in a `jmp (a0)` loop with the Z80
  reading ROM keeps **481.0** of a raster line's 488.6 clocks for its own
  instructions, which is Kabuto's "roughly 480 cycles per raster line instead of
  the theoretical 488+4/7 ones" (`scripts/run_bus_arbiter.py --cartridge`;
  the six lockstep games, which also pay VDP stalls, run at 468 to 475).
- **VDP stalls**: a 68000 write to a full FIFO, or a read of VRAM/CRAM/VSRAM,
  holds the 68000 until the VDP's next external slot; a 68000-memory DMA holds
  it for the transfer. A Z80 bank access during either waits too (Kabuto).

m68000-python's `step()` returns an instruction's total clocks, and the board
decides where in the instruction each bus access falls. sms-python solved the
same problem for the Z80 with a per-opcode table of access T-states
(`IO_ACCESS_T`). The 68000 has many more cases; m68000-python's
`function_codes=True` passes each access through the callables in order, which
is enough for a board to count cycles between them if the core also reports
the clock at each access. Decide this early (milestone 1): it is what makes the
VDP stalls and the arbiter's delays land at the right cycle.

## The YM2612

- A register write must wait for the chip's shift register to come round:
  "maximum write speed ... 33.6 Z80 cycles" between writes (Kabuto, "The
  YM2612").
- The BUSY flag lasts 32 internal clocks (32 × 6 68000 cycles) after a data
  write, the same on the discrete YM2612 and the ASIC, and only data writes set
  it (Eke's 2017 measurements, forum t=386 start=810). Games that poll it (and
  Hellfire's music tempo on the Model 2) depend on this.
- The DAC (channel 6 PCM) is sampled once per chip cycle, at a point that
  differs between the Model 1 and Model 2 (Kabuto: "unfortunately I can't
  give any precise values here").

Nuked-OPN2 (YM3438) and YM2608-LLE (YM2612) are cycle-accurate die-derived
models: run the same register stream through them to get the judge's output
([validation.md](validation.md)).

**The chip's own clock grid** (milestone 4, from those models). The YM2612 runs at
MCLK / 7 and divides that by 6, so one *internal cycle* is 42 MCLK and serves one of
the chip's 24 operator slots; 24 cycles -- 1,008 MCLK -- are one output sample, which
is 53,267 Hz on NTSC and 52,781 Hz on PAL. Slots 0-5 are the six channels' operator
1, 6-11 operator 3, 12-17 operator 2, 18-23 operator 4. Every stage of the chip is a
fixed number of cycles behind the slot being served (the phase generator 19 and 20,
the envelope 22 and 23, the modulation 6 and 18), which is why a register written
between two cycles reaches one operator this sample and the next operator only next
sample, and why `src/megadrive_python/ym2612.py` runs the pipeline rather than a
per-sample loop.

BUSY's 32 internal cycles are 32 x 6 = 192 68000 clocks, which is Eke's measurement
read the other way round. Timer A counts one per output sample (1,008 MCLK); timer B
one per 16 of them.

## The PSG

The PSG inside the VDP is clocked at MCLK / 15, the Z80's clock. It divides that by
4 into two internal phases and serves one of its four channels per phase pair, so one
full rotation -- one output sample -- is 16 input clocks: 240 MCLK, 223,721 Hz on
NTSC. Five FM samples take exactly 21 PSG samples (1,008 x 5 = 240 x 21), which is
how `src/megadrive_python/audio.py` box-filters one onto the other without drift.

The *period* a counter is compared against comes from a one-hot selector that runs
one place behind the counter index, so counter 0 is the noise channel's, counter 1
tone 1's, counter 2 tone 2's and counter 3 tone 3's (Nuked-PSG; milestone 4).

## Frame pacing for a frontend

NTSC runs 53,693,175 / (3,420 × 262) = 59.92 frames a second, PAL
53,203,424 / (3,420 × 313) = 49.70. The YM2612's native rate is 53,267 Hz
(NTSC); a frontend resamples to the host's rate. sms-python's frontend lets
the audio queue pace the frames; the same design fits here.
