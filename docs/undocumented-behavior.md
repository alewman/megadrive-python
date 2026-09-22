# Hardware quirks, revision differences, and open questions

What the Manual does not say, or says wrongly, with the source for each and its
tier ([validation.md](validation.md)). A board should make the revision
differences fields of its machine description, each with a comment naming its
source, the way sms-python does.

## Models and revisions

| Difference | Where it shows | Source |
| --- | --- | --- |
| VDP 315-5313 versus 315-5313A (a manufacturing change) | "around 30% of Model I and 5% of Model II systems experience issues" with Overdrive 2 | Overdrive 2's readme (inside `titan-overdrive2.zip`); Kabuto, "Plane masking instability": "VA4 and prior use this chip, while VA6 and higher use the 5313A or newer", unreliably |
| YM2612 (NMOS, discrete) versus the ASIC-integrated FM (YM3438-like) | the DAC's crossover distortion ("ladder effect"); BUSY readable from port 0 only on the discrete chip; DAC output held versus pulsed | HardWareMan's oscilloscope traces and Aly James's `MD1_LADDER.wav`; Eke, forum t=386 start=810; Kabuto, "The YM2612" |
| Audio circuit | Model 1: first-order filter; Model 2: second-order, more distortion, "noisy, muffled and heavily distorted" | Kabuto's measurements of several MD1s and MD2s; MDFourier's 21-board recordings |
| TMSS | a nonzero hardware version (from Model 1 VA6) has the boot ROM and the $A14000 lock | Manual PDF page 94; TmEE's dumps of several chips agree (forum t=3197) |
| Genesis 3 VSRAM | a different VSRAM size makes it fail several of Nemesis's VSRAM tests | Nemesis, forum t=1291 start=30 |
| Version register | bits 7–6 region and video standard; bits 3–0 the hardware version | Manual PDF page 84 |

## The bus and memory map

1. **Open bus returns the next prefetched opcode word**, for unmapped reads, the
   unused VDP addresses ($C00018–$C0001E), and the top six bits of the status
   register; "every now and then, probably when there are bus delays /
   z80->ROM accesses, a value of $FFFF is read" (Kabuto, "The VDP ports";
   CMD `gen-hw.txt` section 1). Through the Z80's bank those bits read as 1 (CMD).
2. **Lock-ups**: accessing $800000–$9FFFFF without a 32X, a VDP address outside
   its mirror pattern, the HV counter by writing, the PSG by reading, or
   $7F20–$7FFF from the Z80 locks the machine (CMD). A board may treat these as
   halts and report them.
3. **The Z80 reading 68000 RAM through the bank fails; writing works** (MoD's
   `gen_test_z80_bus`, "writes always worked and reads always failed", tested
   on several machines; CMD saw $FF). Steve Snake told CMD reads work on some
   models: no evidence here supports that.
4. **The Z80 reading $A00000–$A0FFFF through the bank locks up** (CMD; MoD's
   test).
5. **68000 access to Z80 space while the Z80 holds its bus or is in reset**
   reads the prefetched opcode and ignores writes; the I/O area stays
   accessible (CMD 2.2).
6. **TAS**: the Genesis bus drops the write half of `TAS` (m68000-python's
   `docs/undocumented-behavior.md`; it exposes a `tas_write` hook for this).
7. **Refresh slow-down**: 2 of every 128 68000 cycles, not while writing to the
   VDP; work RAM has an extra slow-down Kabuto did not measure ([timing.md](timing.md)).
8. **The bank register** is written a bit at a time; the result of a partial
   sequence is unknown (CMD could not find it).

## The VDP

1. **The sprite cache.** The Y position and size/link fields of each sprite are
   cached inside the VDP and updated only by VRAM writes that land in the
   current sprite table; moving the table does not refresh the cache, and the
   check spans 128 KB even in 64 KB mode (Kabuto, "Precise details of sprite
   rendering"). Games and demos that move the table show stale sprites on
   hardware.
2. **Sprite rendering phases** (Kabuto): visibility is decided two lines ahead
   from the cache, X and pattern fetched one line ahead, pixels drawn during the
   preceding horizontal blank into a 320-pixel buffer. A sprite with X = 0
   following one with X ≠ 0 masks the rest of the line; the masking has other
   conditions Nemesis's sprite masking ROM checks on an MD1600.
3. **Shadow/highlight**: Kabuto's rules ("S/H") correct the Manual. Sprite
   colours 62 and 63 (palette 3, colours 14 and 15) highlight and shadow what is
   beneath instead of being drawn; a sprite over low-priority planes is darkened
   unless it is colour 14, 30 or 46 (colour 14 of palettes 0–2), which he reads
   as a leftover of a design change. The DAC levels in S/H are
   not exact halves and doubles (Plutiedev `vdp-color-ramp`; CMD's `ste.txt`
   is idealised).
4. **V28/V30 border trick**: switching to V30 during the active display and back
   before line 240 makes the VDP forget the bottom border, giving up to 243
   stable lines on NTSC (Kabuto, "V28/V30 tricks").
5. **CRAM dots**: a CRAM write during the display shows the written colour for
   a pixel at the access slot's position. BlastEm emulates it. Kabuto's
   "Access timing" says which slots fall in visible pixels.
6. **The debug register** $C0001C: layers can be forced, disabled or mixed, and
   the PSG can be made louder; writes can corrupt VRAM, sprites and reads.
   Overdrive 2 depends on parts of it (Kabuto, "The mighty debug register";
   CMD `newreg.txt`).
7. **The control port is not latched**: a register write triggered by the top
   two bits of the first word; a DMA triggered by bit 7 of the second; a read
   set-up fetches immediately, and an immediate control write after a VRAM
   read can AND the register number and value with the address (Kabuto, "The
   VDP ports").
8. **Register $17 with bit 7 clear** sets a flag that crashes the machine if a
   VRAM address is then set before a DMA runs (Kabuto).
9. **A CRAM DMA wrapping a 128 KB boundary does not abort** ("definitely
   wrong", Kabuto, of a claim elsewhere).
10. **Mode 4 on the Mega Drive VDP** differs from the SMS's: no sprite doubling,
    no 224/240-line modes, 4 bytes fetched per slot of which 2 are used, all
    64 sprites scanned, the background starting 12 slots later, different
    interrupt timing, a broken plane B and a broken H40 in Mode 4 (Kabuto,
    "Mode 4"; MoD, forum t=2557; SMS Power's `VDPRegisters` page).
11. **Plane masking instability**: some layer-masking behaviour differs between
    consoles and power-ons (Kabuto, "Plane masking instability").
12. **The window plane's column-fetch bug** at its left edge (CMD's capture
    photos in `md_tech.zip`, `mdpic.htm`; `wbug.bin` in `md_test.zip`).
13. **H40's long pixels move around VSYNC** (found in Nemesis's captures,
    milestone 2): on the nine lines ending on V = jump + 5 the second run of
    long sync pixels ($1DE-$1ED) lasts 8 MCLK and $09F-$0AE take the long
    lengths instead; in an interlaced odd field the two sets of lines are
    offset by one, making one line 30 MCLK longer and one 30 MCLK shorter.
    No emulator table has it ([timing.md](timing.md), "What the captures say").
14. **A read that lands as a counter steps** sees a mid-change value, and the
    two captured consoles differ there (a ripple counter caught part-way; the
    PAL Model 1 and the NTSC-J Model 1 at slightly different phases, and each
    varying between runs). Details and the board's choice in
    [timing.md](timing.md).
15. **In mode 4 a register write clears the F flag**: every capture's first run
    after power-on (the VDP still in mode 4) reads F clear until its first
    VBlank, every later run reads it set; Nuked-MD shows the mechanism (any
    register written in mode 4 clears F; written in mode 5, not).
16. **Interlace mode 2 addresses patterns by the name's low ten bits times 64**, not
    by "bit zero of the name is ignored" as CMD's `genvdp.txt` says: Exodus
    (`CalculatePatternDataRowAddress`), Genesis Plus GX (`GET_LSB_TILE_IM2`) and MAME
    (`get_nametable`, `m_imode == 3`) agree against him, and CMD's own `wtest_i2.bin`
    renders correctly only the other way (milestone 3).
17. **The window's column-fetch bug hits the partial column** the fine horizontal
    scroll puts before plane A's first whole 2-cell column, so that column repeats the
    two cells of the column after it (CMD's diagram; Genesis Plus GX reads it the same
    way, MAME differently -- the two disagree by 112 pixels of `wbug.bin`).
18. **In shadow/highlight mode the backdrop is darkened too**, wherever both planes are
    low priority: Kabuto ("If both A+B are low prio, the visible color becomes
    shadowed", with G in his mixer), CMD's `ste.htm` and Genesis Plus GX all say so;
    MAME's backdrop stays at normal intensity, which is most of what the board and MAME
    differ by on the S/H test ROMs (milestone 3).
19. **A VSRAM read past its 40th word** returns the last word the renderer read
    from VSRAM, which follows the H counter (the column's entry, or the first two
    in full-screen vertical scroll; entries $26 AND $27 past the end): Exodus's
    model, which VDPFIFOTesting's DMA Fill to VSRAM tests judge. A Genesis 3,
    with 64 words, returns the words themselves and fails those tests.

## The YM2612

1. **BUSY**: set only by data-port writes, for 32 internal clocks (32 × 6 68000
   cycles), the same on both chip types; readable from any port on the ASIC but
   from port 0 only on the discrete YM2612, which is why Hellfire's music runs
   slow on a Model 2 (Eke, forum t=386 start=810). "The YM2612's busy flag is
   useless as already documented elsewhere" (Kabuto) is about relying on it,
   not about its timing.
2. **Write speed**: registers $30–$FF live in a rotating shift register; wait
   about 33.6 Z80 cycles between writes (Kabuto). A Z80 word write of address
   and data is too fast for the chip.
3. **The DAC**: 9 bits, with a visible jump between $7F and $80 on the Model 1,
   mostly gone on the Model 2; the "loud PCM" debug bit amplifies about 5×
   (Model 2) or 30× (Model 1) (Kabuto).
4. **The ladder effect** (crossover distortion) is a YM2612 property the
   YM3438 lacks. Nuked-OPN2 models the YM3438 and has a YM2612 mode flag
   ("MD1, MD2 VA2"); YM2608-LLE models the YM2612 from its die.
5. **Test register**, CSM, SSG-EG: Nemesis's test ROMs and recordings
   ([validation.md](validation.md)); the early pages of the YM2612 thread were
   corrected later in the same thread (phase generator at start=165, envelope
   generator at start=405): cite the corrections.
6. **The two die-derived models disagree about where in a channel's four cycles the
   sample is driven** (found in milestone 4). The six channels are multiplexed onto
   MOL/MOR four cycles each. YM2608-LLE -- a transcription of the *YM2612's* die --
   drives the channel's value on the first of the four and the bare sign (+1 or -1)
   on the other three (`fmopna_impl.c` lines 5824-5853). Nuked-OPN2 is a
   transcription of the *YM3438's* die with a YM2612 output mode its own comment
   calls "YM2612 DAC emulation(not verified)" (`ym3438.c` line 966); that mode drives
   the sign on three cycles and the value on the **last**. The 24 values of an output
   sample are the same multiset either way, so the sample at the chip's native rate
   is unaffected and nothing audible turns on it; the board follows Nuked-OPN2, and
   the difference is measured and recorded in [worklog.md](worklog.md).
7. **What a discrete YM2612's ports 1-3 read is not known.** Eke measured that the
   status answers on port 0 only on the discrete chip and on every port on the ASIC
   (forum t=386 start=810), but not what the other three ports give. Genesis Plus GX
   returns 0 (`core/sound/sound.c` line 152), which the board copies, labelled
   emulator-derived. A hardware read would settle it.
8. **The ladder effect is a property of the DAC's output stage, not of a filter.**
   The discrete chip adds 1 to every non-negative sample and drives +1/-1 in the gaps
   between channels, so the console's low-pass sees a step at the zero crossing
   (HardWareMan's oscilloscope traces; Aly James's `MD1_LADDER.wav`). The board
   carries it as `FMVariant.ladder` and it is what `ym2612ladder` differences in the
   referee comparison come from.

## The PSG and the analogue path

1. **The attenuation ladder is not 2 dB a step.** Nuked-PSG's measured curve
   (`ympsg.c` line 7) gives 1.0, 0.772, 0.622, 0.485 ... where the ideal would give
   1.0, 0.794, 0.631, 0.501. The board uses the measured one
   (`psg.VOLUME_LEVELS`); sms-python uses the ideal one, which is why the two
   boards' PSG levels differ slightly.
2. **The PSG's counter is an up-counter compared with the period**, reset to 0 and
   then incremented, not a down-counter reloaded at zero (Nuked-PSG
   `YMPSG_ClockInternal1`). The two differ for one tick after a period is written and
   for the periods 0 and 1.
3. **A PSG register write reaches the register on the clock after the write, or on
   the same clock** when that clock happens to be the second of the two internal
   phases, which updates the registers a second time (`ympsg.c` lines 208 and 285).
   Worth 19 samples in 102,569 on the first stream the board was compared on.
4. **No distortion model.** Kabuto measures the Model 2's amplifier as introducing
   "severe nonlinearities ... Distortion is quite high too, very well audible" and
   the Model 1's filter as first-order against the Model 2's second-order, but
   publishes no transfer curve and no corner frequency. The board low-passes (one
   pole for `model1`, two for `model2`) at a corner fitted to the MDFourier
   recordings and models no distortion at all, so its Model 2 output is cleaner than
   the hardware's. `src/megadrive_python/audio.py` says so where the constants are.
5. **The mixing ratio between the PSG and the FM** has no pinned number either; the
   board's is measured from the MDFourier recordings ([worklog.md](worklog.md),
   milestone 4).

## Known-wrong or superseded sources

- **CMD `m5hvc.txt`**: "probably off by one or two". Superseded by Nemesis's
  captures.
- **CMD `genvdp.txt`**: "very preliminary"; timing superseded by Nemesis,
  MoD and Kabuto; its S/H colour model is idealised.
- **CMD `segasix.txt`** (Rosenberg, 1996): three select states and no timeout.
  Superseded by Terakawa's measurements and Plutiedev `controllers`.
- **Rick McTeague's `genhw.txt`** (1993): early and incomplete.
- **The Genesis Software Manual**: corrected by the Technical Bulletins; the two
  scans differ in page count (183 and 150).
- **"CRAM DMA across a 128 KB page aborts"**: wrong (Kabuto).
- **The YM2612 thread's early pages**: corrected later in the thread.

## Open items (no pinned source settles them)

1. **The Z80's interrupt length.** CMD says the IRQ comes at line $E0 and is
   missed if disabled; Genesis Plus GX holds it to the end of that line. No
   hardware measurement is pinned. A test that enables interrupts at a
   measured delay after the line starts would settle it. The board holds it for
   one line (Genesis Plus GX's choice, labelled): the only support for it is that
   Sonic The Hedgehog 3's 1,688 Z80 interrupts in 30 seconds all fall at the
   instruction MAME takes them at (milestone 1's lockstep).
2. ~~The H counter's position in MCLK~~: decoded from Nemesis's captures in
   milestone 2 ([timing.md](timing.md), "What the captures say"). Left open
   there: a few hundred status reads near the HBlank edges, four dumps.
3. **The delay from the F flag to the 68000's interrupt**: VDPTEST probes it
   without published hardware values.
4. **TMSS on Japanese consoles**: Nicole Express (2026) reports that a Japanese
   Model 2 enforces only the header check and a header-modified Hellfire failed
   on it and on a Genesis 3 while running on a VA3; one tester, not in Sega
   Retro. A lead to confirm, not a rule.
5. **The work-RAM slow-down** Kabuto saw but did not measure. The board's model
   (21 MCLK on the next work-RAM access once due, due again about 805 MCLK after)
   is Nuked-MD's, and reproduces the capture loop's rate; the interval is fixed
   only to 743-826 MCLK by that loop, and Nuked-MD varies the stretch (7/21/35
   MCLK) in other loops, which the board does not model.
6. **VDPFIFOTesting's intermittent tests**: data-port writes during a DMA fill,
   and the fill's FIFO byte order, vary on real hardware (Nemesis). The board
   passes all four (milestone 2).
7. **The YM2612's DAC sampling point** within its cycle, which differs between
   the Model 1 and Model 2 and which Kabuto did not publish. Related: "On MD1 it
   just outputs a short pulse for each voice ... on MD2 it holds the value for quite
   a while", which the board does not model -- it sums a sample's 24 multiplexed
   values, the same on both.
8. **The delay from a bus request to BUSACK.** MoD's `gen_test_z80_bus` measures it
   twice (with the Z80 running 4-T-state and 6-T-state machine cycles) but its
   hardware values are not published in the pinned material; his logic-analyser
   measurement of 2 68000 cycles from /BR to /BG (forum t=1661) is of the other
   direction, the Z80 reaching the 68000's bus. The board grants at once, as MAME
   does, so both counts read 0000. A model of the real delay (the Z80 finishing its
   machine cycle, the 68000 held by the arbiter until BUSACK) was written and tried in
   milestone 3: it moves the Z80/68000 interleaving off MAME's, which the milestone-1
   lockstep replays instruction for instruction, so it waits for milestone 5's
   arbiter work ([worklog.md](worklog.md)).
9. **What a VRAM write in 128 KB mode does to the picture.** The board models the
   write path from Kabuto's formulas (one byte per word, the address bits rearranged)
   but keeps reading VRAM as in 64 KB mode, where the hardware shows garbage. Nothing
   pinned judges either half; Overdrive 2 is the case.
10. **Mid-line VDP writes.** The board renders a line at the point its active display
   begins, so a VRAM, CRAM or register write made *along* a line lands at the next
   line. r57shell's redraw-timing tests depend on the hardware's behaviour here and do
   not pass; CRAM dots (milestone 5) are the same machinery.
11. **MD Soft Checker v0.30** (Sega's own diagnostic) is not available: the local
   romset's symlink is broken, and the forum link is a mega.nz page.
12. **The analogue path's corner frequency and the PSG/FM mixing ratio** are fitted
   to the MDFourier recordings, not read from a document; no source gives either.
13. **What a discrete YM2612's ports 1-3 read** (above, "The YM2612" item 7).
