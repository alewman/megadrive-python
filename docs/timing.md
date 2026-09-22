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
shifted; Eke confirms the internal sequence in forum t=768). Its relation to
MCLK within the line, and where the H40 clock slow-down falls in it, have to
be read out of the raw captures: this is milestone 2's first job
([handoff-brief.md](handoff-brief.md)). CMD's `m5hvc.txt` has the same ranges
but calls itself "probably off by one or two"; do not use it for positions.

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
vertical blanking, every slot except the refresh slots is external (Kabuto).
Where the external slots fall matters for the FIFO: a 68000 write that finds
the FIFO full waits for the next external slot. Kabuto lists which slots land
in visible pixels ("Access timing"), which is what CRAM writes during the
display (the "CRAM dots") depend on.

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

## Bus sharing

- **Z80 access to the 68000's bus** (the bank window): "delayed by 2 to 5
  cycles ... The average Z80 delay is around 3.3 Z80 cycles, whereas the
  average 68k delay is around 11 68k cycles" (Kabuto, "The bus system"). MoD
  measured it with a logic analyser on a Genesis 2 (forum t=1661): with the
  68000 stopped, 2–3 wait states and 2 cycles from /BR to /BG; with it running,
  4 wait states; an average just under 3 over 16 accesses.
- **A refresh steals 2 of every 128 68000 cycles**: "every time 128 cycles
  have passed the next bus access is slowed down by 2 cycles" (Kabuto), except
  when the 68000 is writing to the VDP. After all slow-downs "the 68K seems to be
  running at roughly 480 cycles per raster line instead of the theoretical
  488+4/7". Work RAM accesses have their own further slow-down he did not
  measure.
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

## Frame pacing for a frontend

NTSC runs 53,693,175 / (3,420 × 262) = 59.92 frames a second, PAL
53,203,424 / (3,420 × 313) = 49.70. The YM2612's native rate is 53,267 Hz
(NTSC); a frontend resamples to the host's rate. sms-python's frontend lets
the audio queue pace the frames; the same design fits here.
