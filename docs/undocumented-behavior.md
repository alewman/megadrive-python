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
   Separately, **every 68000 access to $A00000-$A0FFFF is one 68000 clock longer**:
   nothing hardware says so, but Nuked-MD holds every such bus cycle 7 MCLK and MAME
   charges the same clock (`before_delay(1)`, "TODO: unverified"). The board charges it
   since the timing session (open item 16).
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
   a pixel at the access slot's position. BlastEm emulates it (``vdp.c``
   ``write_cram``, lines 443-451: it puts the newly written colour into the one
   output pixel of the current slot), and is the only implementation pinned here,
   so this is emulator-derived. Kabuto's "Access timing" says which slots fall in
   visible pixels; he describes the dots only in passing ("Writing the debug
   register can also cause a glitched pixel, similar to a CRAM dot") and never
   says what colour one shows. Modelled in milestone 5.
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
19. **An idle line has one access slot fewer per refresh, not two.** Exodus's
    ``GetPixelClockTicksUntilNextAccessSlot`` jumps from a refresh block's start to
    six pixels past it, losing two slots each and leaving 161 (H32) and 198 (H40)
    slots on a line with the display off. Kabuto's access pattern has one ``r`` per
    group, and the Manual's DMA transfer capacity table (PDF page 44) gives 167 and
    205 bytes a line in blanking, which is 171 - 4 and 210 - 5: one slot per
    refresh. The board follows the two hardware-tier sources, which puts its DMA
    rates on Kabuto's measured formulas and reproduces the Manual's whole table
    ([timing.md](timing.md); milestone 5).
20. **Where a mid-line write lands.** The board draws a line in pieces and lets a
    write take effect from the pixel its access slot's H counter names (the active
    display being internal H $000 onwards). Kabuto places the second of a line's
    mid-tiles slots "at the pixel with X=-3 (display area coordinates)", which would
    put the output about 21 pixels behind the H counter -- a pipeline delay the board
    does not model. Nothing pinned measures it directly, and r57shell's redraw-timing
    tests, which would, cannot be reached until the 6-button pad exists (milestone 7).
22. **Register $00 bit 0 turns off display generation altogether**, which is not the
    same as blanking: "Setting bit 0 actually turns off all display generation, as
    opposed to the screen blanking feature which simply shows the backdrop color"
    (CMD ``genvdp.txt`` 17). The board draws such a line black, whatever CRAM holds,
    and models nothing else about it (the sprite and plane fetches still happen).
    r57shell's "VDP Blink Test (Reg 0 bit 0)" is what makes it visible; without it
    that test showed nothing at all, which is why milestone 7 added it. What colour a
    console actually puts out with all generation off is not in any pinned source.
21. **A VSRAM read past its 40th word** returns the last word the renderer read
    from VSRAM, which follows the H counter (the column's entry, or the first two
    in full-screen vertical scroll; entries $26 AND $27 past the end): Exodus's
    model, which VDPFIFOTesting's DMA Fill to VSRAM tests judge. A Genesis 3,
    with 64 words, returns the words themselves and fails those tests.

## The pads and the I/O chip

1. **The 6-button pad's timeout has no one value.** It is a capacitor discharging, and
   Terakawa says so in as many words: "It seems that the time is measured by
   discharging of a capacitor in a 6-Button-Controller. It may mean parameters differ
   one by one controller. Please choose values you think proper for parameters."
   The four published figures do not even measure the same quantity: CMD
   (`gen-hw.txt` 3.2) times out "about 8192 (probably less than that) 68000 CPU
   cycles" (1.07 ms) after TH was last *modified*; Terakawa gives 1.1 ms for the
   second up-edge to arrive, 1.6 ms for the data to stay readable and 1.8 ms before
   the sequence may start again, all from the *first* up-edge; MAME 0.285 uses 1.5 ms
   from the last rising edge (`bus/sms_ctrl/md6bt.cpp`). The board takes CMD's shape
   and figure as a field (`io.Pad6.timeout_mclk`) and claims nothing: a poll takes a
   few microseconds and a game polls once a frame, so all four reset the pad between
   frames and none resets it inside one.
2. **D4 and D5 in the extra-buttons phase carry B and C**, not a fixed high. CMD
   writes the phase as `?1CBMXYZ`; Terakawa's table shows "HI HI" there, which is
   those two buttons released. MAME's pad agrees with CMD (`md6bt.cpp`, `case 3`).
   The board follows CMD, and `scripts/run_pads.py` checks every one of the 4,096
   button combinations through the whole sequence.
3. **After the fourth rising edge the pad is a 3-button pad again**, until the
   timeout: "From this point on, the standard 3-button pad values will be returned if
   any further TH transitions are done" (CMD). MAME instead wraps its counter modulo
   4, so its pad starts the sequence again on the fifth edge. Nothing pinned settles
   which is right; the board follows CMD, who checked his on hardware.
4. **MAME cannot be used as a detector for the 6-button pad.** Its pad reads its own
   MODE switch in `device_reset`, and its comment says that does not work -- "TODO:
   doesn't work - reset happens before host inputs are read for the first time" --
   so `-ctrl1 md6button` leaves the pad in 3-button mode. r57shell's `joy_speed`
   prints "3 Buttons Pad" for it, and for the board it prints the same although the
   board's own read sequence is the 6-button one (docs/worklog.md, milestone 7).

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

## Cartridges

1. **Eke's NBA Jam entry has /SDA in and /SDA out on the wrong bits.** His
   `gen_eeprom.pdf` gives the Acclaim type 1 board `SDA_IN: 0x200001 (bit 0)`,
   `SDA_OUT: 0x200001 (bit 1)`, `SCL: 0x200001 (bit 1)`, which puts the data out and
   the clock on one bit and cannot be a board. MAME's own NBA Jam device reads /SDA at
   bit 1 and writes /SDA at bit 0 and /SCL at bit 1 (`bus/megadrive/eeprom.cpp` lines
   606-628). **The game settles it**: with bit 1 the board writes its "Snake 1993"
   header once and, restarted with that EEPROM, reads it back and writes nothing;
   with bit 0 it cannot read its own save and rewrites all 256 bytes on every boot
   (docs/worklog.md, milestone 7). The board uses bit 1.
2. **MAME's `sram` data area is an address range, not a size.** `md_slot.cpp` sets
   `m_nvram_end = m_nvram_start + length - 1` and allocates that many bytes as 16-bit
   words, one per address the chip answers at, with the byte in *both* halves of each
   word. The chip itself is eight bits wide on the odd byte (CMD `gen-hw.txt` 4.3:
   Phantasy Star 4's "16k of battery backed RAM mapped to $200001-$203FFF. (odd
   addresses used)"), so it holds half as many bytes as the range is wide. The board
   keeps the chip's own bytes and halves the list's figure; `scripts/run_battery.py`
   unpacks MAME's file to compare, and checks that its words' two halves agree.
3. **A word read of battery RAM: what is on D15-D8 is not known.** The board reads
   $FF there (the chip drives only the odd byte and nothing else drives the bus);
   MAME returns the byte in both halves, because that is what it stored. No pinned
   source says. Nothing measured here depends on it: the games that save read bytes.
4. **Two EEPROM boards where Eke and MAME disagree, neither settled.** The Ninja
   Burai Densetsu *prototype* (`rom_eeprom`): Eke lists the game under Sega's mode 1
   (X24C01) and MAME fits a mode-2 24C01; the board follows the research document.
   Brian Lara Cricket (`rom_blara95`): Eke gives it mode 1 with the Codemasters
   addresses, MAME puts all three lines on $380000-$380001 and fits a 24C65; the
   board takes MAME's addresses (they are what its code reads and writes) and Eke's
   chip. Neither game was run: both are in the local romset but neither writes its
   battery without being played.
5. **The J-Cart's two extra ports answer with nothing plugged in.** The board decodes
   $38FFFE and returns all pins high with bit 7 set (MAME `bus/megadrive/jcart.cpp`
   lines 150-160), which is an empty socket. Attaching pads there is a frontend's job
   and is not modelled.

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
   is Nuked-MD's, and reproduces the capture loop's rate. Milestone 6 measured it
   properly against the referee's own reported cycle lengths and the rule **does not
   generalise**: the 21-MCLK stretch is confirmed on every one of the several hundred
   stretches seen, but the re-arm period that reproduces the referee exactly is
   806-819 MCLK in a dense work-RAM loop (which narrows the old 743-826 and excludes
   the board's 805), 694-699 in CMD's `wtest.bin`, and no value in 600-1400 fits
   Nemesis's capture ROM at any phase. The referee's stretches are not raster-locked
   either. So the re-arm timer is a fit to one access pattern rather than the
   machine's rule, and it is the largest single source of difference in milestone 6's
   ROM comparison (1,300-1,900 misplaced ticks per test ROM, each shifting the board's
   timeline by 21 MCLK and so moving the -- otherwise exact -- cartridge grid).
   Nuked-MD also varies the stretch (7/21/35 MCLK) in some loops, which the board does
   not model.

   **The timing session investigated it further, with a limit, and kept 805.** Three
   new facts, all read out of the referee's own reported bus-cycle lengths over eight
   traces (three dense probe loops of 300,000 MCLK and five test ROMs of 900,000):

   - **The stretch is not always 21 MCLK.** A work-RAM cycle can be stretched 7, 21 or
     35 MCLK -- 1, 3 or 5 68000 clocks. In Nemesis's capture ROM 100 of 401 stretches
     are 7; in r57shell's `vdp_tests` 18 of 1,020 are 7 and 182 are 35. The board
     models only the 21.
   - **The rate varies by a factor of two and a half between programs.** In 900,000
     MCLK `wtest.bin` shows 1,032 stretches (one per 872 MCLK) and the capture ROM 401
     (one per 2,244), and the apparent period rises as work-RAM traffic thins: 845 MCLK
     where 44 % of bus cycles are work RAM, 871 at 29 %, 896 at 20 %.
   - **No free-running grid fits.** For a fixed grid of period *G* every stretch's time
     must fall in one narrow band of residues mod *G*; over 200-2,000 MCLK the tightest
     band any *G* gives is 70-165 MCLK wide, 8-18 % of the period, and no *G* is shared
     by two programs. Nor do the 4,546 stretches of all eight runs -- which share one
     power-on phase -- band together at any period.

   Five candidate rules were scored against the referee, each free to choose its own
   period and phase, counting how many of the referee's stretches it misplaces:

   | rule | dense probe (354) | `wtest` (1,032) | capture ROM (401) | `vdp_tests` (1,020) |
   | --- | ---: | ---: | ---: | ---: |
   | **A** re-arm *P* after the stretched RAM access (the board's) | 556 @ 946 | **0** @ 760 | 1,055 @ 946 | 1,664 @ 834 |
   | **B** fixed grid, any bus cycle absorbs the tick, only RAM pays | 233 @ 845 | 863 @ 871 | 300 @ 896 | 638 @ 880 |
   | **C** fixed grid, the tick waits for a RAM access | 175 @ 845 | **0** @ 871 | 604 @ 896 | 183 @ 880 |
   | **D** every *N*th work-RAM access (a row counter, not a timer) | 353 @ 44 | **0** @ 8 | 300 @ 57 | **0** @ 11 |
   | **E** any bus cycle absorbs the tick and re-arms the timer | 361 @ 785 | 1,032 @ 834 | 401 @ 876 | 1,072 @ 778 |

   Two rules are exact on two programs each and hopeless on the others, and the period
   or count each needs differs by a factor of five between programs. At the board's own
   805 MCLK rule C is no better than rule A overall (663 against 709 on the dense
   probe, but 1,656 against **0** on `wtest`). So **805 and rule A stay**, the
   difference is named, and the question is sharper than it was: the mechanism makes
   the interval depend on the access pattern in a way none of these five shapes
   captures. What would settle it is a hardware measurement, or reading the refresh
   counter out of the referee's own state rather than inferring it from stretches.
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
   direction, the Z80 reaching the 68000's bus. **Modelled in milestone 5** from the
   specification alone: BUSREQ "is sampled by the CPU with the rising edge of the last
   clock period of any machine cycle" and the bus is released after it (Zilog UM0080),
   so the grant falls at the end of the machine cycle in progress, and a 68000 access
   to the Z80's space before then is held by the arbiter rather than failing. On MoD's
   ROM the delays that gives are 0 to 9 68000 cycles, all shorter than the 8-cycle
   `move.b (a2),dN` the ROM measures with, so **both its counters still print 0000** --
   the same result as before it was modelled, and still not comparable with hardware.
   MAME grants at once and models no arbiter, so the milestone-1 lockstep turns the
   whole arbiter off (`Board.arbiter_enabled`), as it already does the refresh.
   **Milestone 6 gave it a judge.** Nothing hardware covers the question, so the tier
   rule lets Nuked-MD answer it: from the 68000's write to $A11100 to /BUSAK it takes
   9.71-16.86 68000 cycles (mean 12.34 over 122 grants), where the machine-cycle rule
   alone takes 1.43-5.29 (mean 4.15). The specification's rule is therefore right in
   shape and short by the arbiter's own propagation.
   **The timing session adopted that propagation** as `arbiter.BUSACK_PROPAGATION_MCLK`
   = 57 MCLK -- the difference of the two means, 8.19 68000 cycles, rounded to whole
   master clocks and labelled die-derived and fitted. The board's grants are then
   9.57-13.43 cycles, mean 12.29 against the referee's 12.34; the spread is narrower
   than the referee's because the board's machine-cycle estimate is coarse
   (`System.z80_probe`). MoD's ROM's two delay counters now print **0001** instead of
   0000, which is still not comparable with hardware because his own values are not
   published.
   **What is still open.** With the Z80 *held in reset* the referee takes far longer:
   on `gen_test_z80_bus`, the one pinned ROM that polls $A11100 in that state, /BUSAK
   follows the write by **235 MCLK** (33.6 68000 cycles) against the board's 57. One
   observation is not a constant, so no second figure was fitted, and both
   `gen_test_z80_bus` and `stetest` still end their referee comparison at that poll
   (referee $67, board $66) -- a measured gap of about 178 MCLK rather than an
   unmodelled one ([worklog.md](worklog.md), the timing session).
9. **What a VRAM write in 128 KB mode does to the picture.** The board models the
   write path from Kabuto's formulas (one byte per word, the address bits rearranged)
   but keeps reading VRAM as in 64 KB mode, where the hardware shows garbage. Nothing
   pinned judges either half; Overdrive 2 is the case.
10. ~~**Mid-line VDP writes**~~: modelled in milestone 5. A line is set up where its
    active display begins and then drawn in pieces, each write landing at the pixel of
    its own access slot, and a CRAM write in the display leaves its dot. What is still
    open is the pipeline offset (item 20 of "The VDP") and judging any of it:
    r57shell's redraw-timing tests poll the pad seven times a frame -- the 6-button
    handshake -- so neither the board nor MAME leaves their first screen until
    milestone 7 fits that pad.
11. ~~**MD Soft Checker v0.30**~~ **is available and has been run** (milestone 8).
    The local No-Intro mirror holds it as `MD Soft Checker (USA) (Proto) (Unl)`
    (the romset's own symlink dangles because No-Intro dropped the `(v0.30)` from
    the name); it is pinned in `scripts/pinned_files.json`. Sega's own factory
    diagnostic prints 17 checks and the board gets its OK on every one, on all nine
    machines, with the version register read back correctly in its header
    ([validation.md](validation.md), milestone 8). It tests that each memory answers
    and that each kind of DMA moves bytes; it says nothing about timing, the picture
    or sound, so it is a floor and not a ceiling.
12. **The analogue path's corner frequency and the PSG/FM mixing ratio** are fitted
    to the MDFourier recordings, not read from a document; no source gives either.
13. **What a discrete YM2612's ports 1-3 read** (above, "The YM2612" item 7).
14. **Where a Z80 bus access falls inside its instruction.** z80-python reports only
    an instruction's total, so the arbiter places each bank access at the Z80 step's
    start plus the delays already charged. MoD measured that on a `POP` the 68000
    "manages to sneak in a bus operation in between the first and second Z80 read", so
    the second read "seems to always have at least 4 wait states" and the pair averages
    3.5; the board, placing both reads almost together, charges 2.53 wait states on
    that experiment where it charges 3.36 on his single-read one
    ([worklog.md](worklog.md), milestone 5). **Milestone 6 measured the split** on
    Nuked-MD, which charges the `pop`'s first read 2.95 wait states and its second
    3.61 -- MoD's order and very nearly his number -- where the board charges 4.08 then
    2.30: the same total, the opposite split, exactly as the missing accessor predicts.
    A core-side accessor would settle it ([polish.md](polish.md)). **After the timing
    session** the compensation is gone and the gap is plain: with the release rule
    corrected the `pop`'s first read costs the board 2.97 wait states against the
    referee's 2.95, its second 2.04 against 3.60, and the pair 2.50 against 3.27. This
    is now the board's largest remaining arbiter difference and it is entirely the
    missing accessor.
15. **The DMA's start-up cost**, adopted in the timing session and **not fully
    resolved**. Kabuto's fast-transfer formula has a +5.6-cycle intercept, so a
    one-word transfer costs 8 68000 cycles on his hardware; milestone 6 measured
    Nuked-MD at 11.0 for the same transfer, fitting +8.0 over 1 to 512 words. The two
    judges agree that a start-up exists and disagree about its size by 2.4 cycles, so
    the **hardware source was taken**: `vdp.DMA_STARTUP_MCLK` is his own 5.6 cycles
    (39 MCLK). The board now costs 7.4 cycles for a one-word fast transfer against his
    8.0 and the referee's 11.0, so it is still 3.6 cycles under the die. Why the die
    charges more than the hardware research reports is not explained here; the referee
    is an FC1004 with NMOS CPUs and Kabuto measured a Model 1, which is a candidate and
    nothing more.
16. ~~**What a 68000 access to the Z80's space costs.**~~ **Settled and adopted** in
    the timing session (2026-09-23). MAME adds one 68000 clock to every access to
    $A00000-$A0FFFF (`megadriv.cpp`, `before_delay(1)`, "required by pacman2 intro",
    "TODO: unverified"), which milestone 1 carried as a named class of lockstep
    difference (41,274 steps of Sonic alone); Nuked-MD does the same (all 68 of the FM
    probe's $A0xxxx accesses held 7 MCLK). Nothing hardware covers it, so the
    die-derived model judges and it agrees with MAME against the board. The board now
    charges it (`board.Z80_SPACE_STALL`, after the access, so the access still lands at
    the clock its bus cycle began at). The milestone-1 replay was re-recorded from MAME
    and re-run on all six games: the named class is gone and 0 differences are
    unexplained. What remains open is only the *reason* -- no hardware source says why a
    Z80-space cycle is a clock longer, and MAME's own comment still calls it
    unverified.
18. **The TMSS boot ROM is something a caller fits, not something a TMSS machine
    has.** On the hardware a TMSS console powers up with its own ROM on the bus and
    bit 0 of $A14101 chooses between it and the cartridge; the board does the same
    *when a boot ROM is supplied* (`System(..., tmss_rom=...)`), and otherwise puts
    the cartridge on the bus from power-on and ignores $A14101. That is what every
    run of milestones 1 to 6 did -- VDPFIFOTesting is scored on `genesis-tmss` among
    others -- and making the boot ROM automatic would move all of them. The VDP lock
    ("SEGA" to $A14000) is not optional and is on for every TMSS machine.
19. **r57shell's VDP Redraw Time Test 4 cannot be reproduced as reported.** The one
    hardware report (MrTamk1s, an MD2 VA1.8 with a 315-5708-01, forum t=1585) is
    "On the smaller [CRT], the screen completely rolled over, while on the larger,
    only the first few rows were flickering/rolling" -- a display losing and regaining
    sync, which depends on the monitor and which a frame buffer has no way to show.
    The board's picture for that test does not change at all from frame to frame. The
    rest of that ROM's eight screens are measured in docs/worklog.md, milestone 7.
17. **The VDP in Mode 4.** At power-on the VDP is in Mode 4 and Nuked-MD's $C00008
    reads a constant ($9F00 in the runs made); the board reads its Mode 5 HV counter
    whatever the mode. No test ROM of milestones 2-5 reads HV before setting Mode 5,
    and Mode 4 as a whole is milestone 10.

20. **What an Extended SSF cartridge writes to $A130F0/$A130F1: MAME is right and
    krikzz's own document does not describe these carts.** Krikzz's
    `extended_ssf-v2.txt` (specification tier, the designer's own) gives that
    register as `[PXWL.... ...RRRRR]` -- a protection bit, 32x mode, ROM
    write-enable, an LED, and **the first bank** in the low five bits -- and puts no
    SRAM control anywhere in the mapper. MAME's
    `megadrive_hb_ssf_device::time_f0_w` implements it as the *Sega* mapper's SRAM
    control instead (bit 1 disables the SRAM view, bit 0 write-protects it), with the
    comment "same as Sega mapper (Demons of Asteborg wants it this way)".

    The document outranks MAME on the tier rule, so the conflict was settled by
    looking at what the games themselves write, which is evidence about their own
    cartridges. `Board._system_write` was wrapped (the board's own dispatch drops the
    high byte, so the cartridge is the wrong place to watch from) and four carts run
    for 900 frames each on a forced `rom_ssf2` board:

    | cartridge | header | what it writes to $A130F1 |
    | --- | --- | --- |
    | Demons of Asteborg (v1.1) | `SEGA DOA` | `$01` x6, `$03` x3 |
    | Earthion (v3.3.0) | `SEGA SSF` | `$00` x1 |
    | Kunio no Nekketsu (v1.6) | `SEGA SSF` | `$00` x1 |
    | Overdrive 2 | `SEGA SSF` | never writes it |

    `$00`, `$01` and `$03` use bits 0 and 1 and nothing else. Read krikzz's way those
    are **first-bank selects**, and writing bank 1 or bank 3 over the window that
    holds the vector table would break the running program immediately; read the Sega
    mapper's way they are exactly its documented idiom -- Phantasy Star IV "will
    always write $01 to $A130F1 before accessing the RAM" (CMD `gen-hw.txt` 4.3), and
    `$03` is that plus the write-protect bit. Demons of Asteborg is the cart MAME's
    comment names, it is the one with battery RAM (MAME's slot is `ssf_sram`), and it
    is the only one of the four that writes anything but `$00`.

    So: **these retail aftermarket cartridges are not krikzz's flashcart.** His
    document describes the Mega Everdrive's own mapper, which has an LED, an SD card
    and a USB port; a cartridge pressed for a game implements the bank registers and
    leaves $A130F1 as the Sega mapper had it. Both statements are true of different
    hardware, which is why the document and MAME disagree without either being wrong.

    Two further things the same run settled:

    - **The bank field is five bits, and four is not enough.** The largest bank
      written is `$1C` (Demons of Asteborg, twice). MAME's *legacy* `rom_ssf2` device
      masks `data & 0xf` and would fetch bank `$0C` for it; krikzz's five bits and the
      board's six both carry it. Corroboration that the numbering is krikzz's: his
      document reserves "Bank 28 ... for saves", and `$1C` is 28.
    - **Every write is a byte write to an odd address** ($A130F1, F3, F5, ... FF) --
      no game wrote a word, in 3,600 frames across the four. The high byte where all
      of krikzz's flag bits live is therefore never written by these cartridges, so
      the board's `write_time(address | 1, low)` dispatch loses nothing real here.
      (An earlier note in this session guessed that dispatch would have to carry the
      whole word before the mapper could be implemented. The observation says
      otherwise for these four carts, and it is recorded here rather than acted on.)

21. **The board runs Earthion's loading loop about twice as fast as BlastEm does,
    and nothing here can say which is right.** Chasing what looked like a divergence
    (the board appeared to run the game's VDP initialisation twice where BlastEm ran
    it once) showed there is no divergence at all: **the first 9,062 VDP register
    writes are identical, in order, between the two.** The board's stream is a strict
    superset -- it simply gets further. The apparent double initialisation is BlastEm
    reaching the same code later, and comparing by frame number rather than by
    content is what made it look structural.

    What is left is a rate difference, and it is a clean factor of two:

    | | frames for identical work | DMA triggers (reg 23) | median gap between triggers |
    | --- | ---: | ---: | ---: |
    | the board | **300** | 1,374 | **1,710 MCLK** |
    | BlastEm | **599** | 1,377 | **3,608 MCLK** |

    Emulated time per frame agrees (the board's 536,728,611 MCLK over 600 frames
    against BlastEm's 537,526,808), the work is the same, and the number of transfers
    is the same. The board simply advances half as far between one DMA and the next.

    It is **not** the interrupts: 556 VINTs over 600 frames, 0.93 a frame, with no
    doubling. It is **not** simply the transfer being undercharged either -- the
    difference is about 1,900 MCLK per DMA, where the whole cost of a 31-word
    transfer under Kabuto's formula is about 560. The likeliest remaining candidate
    is what the game does *between* transfers: a status bit polled in a loop, which
    the board would clear too early.

    **Why this is not recorded as a bug.** BlastEm is emulator-derived and therefore
    a detector: it can say the two disagree, never which is right, and no
    hardware-tier source here covers the question. And the disagreement points the
    *opposite* way to MAME's: against MAME the board runs **behind** (which is the
    whole of `FRAME_OFFSET` = 3 and the lag measured in milestone 8), and against
    BlastEm it runs **ahead**, by a factor of two. Those cannot both be the whole
    story, and reconciling them is the open question rather than either figure on its
    own. `scripts/blastem_events.py` is the instrument; the measurement above is
    reproducible with it.
