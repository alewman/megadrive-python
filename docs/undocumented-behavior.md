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
   measured delay after the line starts would settle it.
2. **The H counter's position in MCLK** and the H40 slow-down's position: in
   Nemesis's raw captures, not yet decoded (milestone 2).
3. **The delay from the F flag to the 68000's interrupt**: VDPTEST probes it
   without published hardware values.
4. **TMSS on Japanese consoles**: Nicole Express (2026) reports that a Japanese
   Model 2 enforces only the header check and a header-modified Hellfire failed
   on it and on a Genesis 3 while running on a VA3; one tester, not in Sega
   Retro. A lead to confirm, not a rule.
5. **The work-RAM slow-down** Kabuto saw but did not measure.
6. **VDPFIFOTesting's intermittent tests**: data-port writes during a DMA fill,
   and the fill's FIFO byte order, vary on real hardware (Nemesis). A board
   should pass them one way and say which.
7. **The YM2612's DAC sampling point** within its cycle, which differs between
   the Model 1 and Model 2 and which Kabuto did not publish.
8. **MD Soft Checker v0.30** (Sega's own diagnostic) is not available: the local
   romset's symlink is broken, and the forum link is a mega.nz page.
