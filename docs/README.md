# Documents

Read in this order.

| Document | What it holds |
| --- | --- |
| [start-here.md](start-here.md) | The machine as a board author needs it: clocks, the 68000 and Z80 memory maps, the bus between them, the VDP's programming model, sound, I/O, cartridges, TMSS, the models, and where the cores fit |
| [timing.md](timing.md) | The clock tree in master clocks, the line and frame, the H and V counters (Nemesis's logic-analyser captures), interrupts, the VRAM access slots, DMA speed, bus sharing, FM write timing |
| [undocumented-behavior.md](undocumented-behavior.md) | Model and revision differences, bus and VDP quirks, the YM2612 against the YM3438, known-wrong sources, open items |
| [validation.md](validation.md) | Every oracle found, ranked by tier (hardware-captured, hardware-corrected, die-derived, specification, research, emulator-derived), with pins and licences; what is not available |
| [mame-oracle.md](mame-oracle.md) | The verified MAME 0.285 two-run trace recipe for both CPUs, its measured output, and nine traps |
| [worklog.md](worklog.md) | What each build session ran, with commands, results, decisions and their reasons, and the open `QUESTION:` items |
| [polish.md](polish.md) | Changes deferred to one pass at the end: core pins and workarounds waiting on a release, and the documents to restate against the final runs |
| [handoff-brief.md](handoff-brief.md) | The task for the session that builds the board: prerequisites, requirements, ten milestones with acceptance tests, constraints, size and speed |

## Findings made while gathering (2026-09-22)

These came out of checking the sources against each other and against MAME,
not out of any single document:

1. **MAME evaluates a debugger action against the visible CPU, not the traced
   one**, so one run can log only one CPU's registers; a Z80 trace action
   naming Z80 registers while the 68000 is visible logs nothing at all. Two
   deterministic runs, merged by master clock, give both CPUs
   ([mame-oracle.md](mame-oracle.md), traps 1 and 2).
2. **Nuked-MD exists**: the whole Mega Drive chipset emulated from decapped
   silicon (VDP with PSG, arbiter, I/O chip, TMSS, FM, NMOS 68000 and Z80). It
   is the strongest detector available and can generate the bus-level traces no
   hardware capture provides ([validation.md](validation.md)).
3. **Nuked-OPN2 models the YM3438, not the Model 1's YM2612**; YM2608-LLE has a
   die-derived YM2612 path. The ladder effect and the BUSY flag separate them
   ([undocumented-behavior.md](undocumented-behavior.md)).
4. **The only raw hardware capture of the VDP is Nemesis's HV and status
   dumps** (2010); everything else measured on hardware is published as prose,
   diagrams and recordings.
5. **The Z80 cannot read 68000 RAM through its bank; it can write it**
   (MoD's test ROM, several machines). CMD's 2001 note agrees; a report that
   some models allow reads has no evidence here.
6. **VDPFIFOTesting is not 100 % on hardware**: a Genesis 3 fails its VSRAM
   tests and three are intermittent. The acceptance test is "what hardware
   passes", not "all".
7. **Overdrive 2's correct output depends on the VDP revision** (about 30 % of
   Model 1s and 5 % of Model 2s glitch): a stress case, not a judge.
8. **Two local romset symlinks are broken**: MD Soft Checker v0.30 (Sega's
   diagnostic) and the Teradrive TMSS BIOS.

## Findings made while building (see [worklog.md](worklog.md) for the runs)

9. **Nemesis photographed his MD1600 running his sprite masking ROM**, and those two
   pictures are the judge for milestone 3: neither holds a red pixel, so all nine
   tests pass on his machine in both widths. They are pinned beside the ROM.
10. **CMD's `genvdp.txt` is wrong about interlace mode 2's pattern addressing**
    (Exodus, Genesis Plus GX and MAME agree against him), and his window bug is the
    *partial* column at plane A's left edge, not the first whole one.
11. **MAME does not darken the backdrop in shadow/highlight mode**; Kabuto, CMD's
    `ste.htm` and Genesis Plus GX all say the hardware does. That is most of what the
    board and MAME differ by on the shadow/highlight test ROMs.
12. **A BUSACK delay cannot be modelled while the milestone-1 lockstep stands**: the
    arbiter's wait moves the Z80/68000 interleaving off MAME's, which the lockstep
    replays instruction for instruction
    ([undocumented-behavior.md](undocumented-behavior.md), open item 8).
13. **The two die-derived FM models disagree about the DAC's multiplexing order**
    (milestone 4): YM2608-LLE, a transcription of the YM2612's own die, drives a
    channel's sample on the first of its four cycles and the bare sign on the other
    three; Nuked-OPN2's YM2612 mode -- which its own comment calls "not verified" --
    drives the sign first and the sample last. The 24 values of an output sample are
    the same either way, so nothing audible turns on it
    ([undocumented-behavior.md](undocumented-behavior.md), "The YM2612" item 6).
14. **The Sega PSG's attenuation ladder is not 2 dB a step**: Nuked-PSG's measured
    curve gives 0.772 and 0.622 where the ideal gives 0.794 and 0.631. sms-python
    uses the ideal one; this board uses the measured one.
15. **Nothing pinned gives the console's audio filter a corner frequency or the PSG
    and FM a mixing ratio.** Kabuto measured the shapes (first order on the Model 1,
    second on the Model 2, "quite high" distortion on the Model 2) but published no
    numbers, so the board's are fitted to the MDFourier recordings and labelled.
