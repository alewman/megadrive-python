# Documents

Read in this order.

| Document | What it holds |
| --- | --- |
| [start-here.md](start-here.md) | The machine as a board author needs it: clocks, the 68000 and Z80 memory maps, the bus between them, the VDP's programming model, sound, I/O, cartridges, TMSS, the models, and where the cores fit |
| [timing.md](timing.md) | The clock tree in master clocks, the line and frame, the H and V counters (Nemesis's logic-analyser captures), interrupts, the VRAM access slots, DMA speed, bus sharing, FM write timing |
| [undocumented-behavior.md](undocumented-behavior.md) | Model and revision differences, bus and VDP quirks, the YM2612 against the YM3438, known-wrong sources, open items |
| [validation.md](validation.md) | Every oracle found, ranked by tier (hardware-captured, hardware-corrected, die-derived, specification, research, emulator-derived), with pins and licences; what is not available |
| [mame-oracle.md](mame-oracle.md) | The verified MAME 0.285 two-run trace recipe for both CPUs, its measured output, and nine traps |
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
