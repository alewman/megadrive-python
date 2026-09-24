# Polish: deferred changes, to make in one pass at the end

Decided by the user, 2026-09-22: **core-side changes are batched, not adopted as
they land.** Switching a pinned core revision mid-build re-opens every
acceptance run for one line of cleanup, and more core changes are likely before
the board is finished. So a workaround stays until this list is worked through
in one pass, with the acceptance runs re-run once at the end.

The rule for adding to this list: anything that works, is labelled where it
lives, and would be written differently once something outside this repository
changes. Not a to-do list for the board's own milestones, and not a place to
park a known bug: a bug goes in `docs/undocumented-behavior.md` or is fixed.

One section below bends that rule deliberately, and says so: "Waiting on a
decision the user has to make" carries changes to `src/` that a milestone
diagnosed and did not make. They wait on a decision rather than on anything
outside the repository, and they are here so that one queue holds everything
the final pass has to work through.

Each entry says what to do, what to re-run afterwards, and where the workaround
lives.

## Waiting on a core release or revision

| What | Where the workaround lives | What to do | Re-run after |
| --- | --- | --- | --- |
| `M68000CPU.step_clocks` is public since m68000-python 44c26f0 (PR #1, merged 2026-09-22); the board reads the private `_cycles` | `Board.m68k_access_clock` (one line) | bump the pin in `pyproject.toml` from 51ee1a5 to the merge commit, reinstall in both venvs, use the property | the milestone-1 lockstep and milestone-2 acceptance runs, to show nothing moved |
| Z80 RESET clears `I` and `R` since z80-python PR #15 (merged 2026-09-22), but no release carries it and the board depends on `z80-python>=0.4.0` from PyPI | `System._z80_reset_line` (labelled) | when a release carries it: raise the floor, drop the workaround | the milestone-1 lockstep (Sonic 3's driver reads `R`) |
| z80-python reports only an instruction's total, so the board cannot say *where inside an instruction* a Z80 bus access falls. The arbiter needs that: it places each bank access at the step's start plus the delays already charged, which is why two reads of one `POP` land almost together. Milestone 6 measured the consequence exactly: Nuked-MD charges the `pop`'s first read 2.95 wait states and its second 3.61 (MoD's hardware: the second "at least 4", mean 3.5), where the board charged 4.08 then 2.30 -- the same total, the opposite split. **The timing session made this the board's largest remaining arbiter difference**: with the release rule corrected the first read is now 2.97 against the referee's 2.95, and the second 2.04 against its 3.60, so the pair's mean falls to 2.50 against 3.27 -- the compensation is gone and only the missing accessor is left | `Board._bank_delay` (labelled), and `System.z80_probe`, which re-runs the instruction on a probe bus to estimate the same thing for the bus grant | propose the Z80 equivalent of m68000-python's `step_clocks` -- a read-only "T-states so far in this step", readable from inside a bus callable -- and place each access at it | `scripts/run_bus_arbiter.py`'s three experiments, and the milestone-1 lockstep |

*(The four rows that waited on a decision about milestone 6's findings -- the
extra clock on $A00000-$A0FFFF, the bus grant's propagation, the DMA's start-up
cost and the work-RAM refresh -- were taken in the timing session of 2026-09-23
and are gone from this list. What each became is in [worklog.md](worklog.md),
"Session 6"; the work-RAM refresh stays as open item 5 of
[undocumented-behavior.md](undocumented-behavior.md), with what was tried.)*

## Waiting on a decision the user has to make

Two changes to `src/` that milestone 8 diagnosed and did not make, because a
sweep is not the place to change the thing being swept. Each is a decision
rather than a repair: the board's present behaviour is defensible, and the
alternative is better for a reason the user should weigh. Three more have been retired: the oversize
guard and the renderer's two buffers were taken on 2026-09-24, and the $A130F0
conflict was settled by observation the same day (below).

| What | What the board does now | What to do instead | Re-run after |
| --- | --- | --- | --- |
| **An odd initial PC raises out of the constructor.** `Phantasy Star Fukkokuban (Japan)` has initial PC `00CB187F`. It is not an interleaved dump (neither a whole-file nor an SMD 16 KB de-interleave gives a valid header or an even PC): the dump is bad | `System.__init__` raises `GroupZero (3, 13310079, False, True)` from `m68k.reset()`'s prefetch, which the sweep records as `error:` | a real 68000 takes an address error *during* reset exception processing, which is a double bus fault: the CPU halts and the screen stays black. MAME shows exactly that (256x224, every pixel 0, checked here). Constructing a `System` should not raise; it should halt | the sweep's `error:` rows |
| **These carts are not on the SSF2 mapper; they are on Krikzz's Extended SSF, which is a different device.** Its specification is the designer's own: `extended_ssf-v2.txt`, krikzz.com, 2014-09-22, pinned in `scripts/pinned_files.json` (specification tier). Differences from the Sega/SSF2 mapper the board implements: the bank field is **5 bits** (32 x 512 KB = 16 MB), not the 6 bits `ssf2tnc.txt` gives; the bank registers are the **even** words $A130F2-$A130FE for windows 1-7; **$A130F0 is not SRAM control at all** but mapper control, `[PXWL.... ...RRRRR]` -- P an enable bit that must be set for the write to take, X 32x mode, W ROM write-enable (the ROM can be written as RAM), L an LED, and the low 5 bits **the first bank**, which this mapper can switch and SSF2 cannot. It also adds hardware multiply and divide at $A130D0-$A130DA, and SD and USB IO. Activation is documented: the header at $100 reads `"SEGA SSF"` instead of `"SEGA GENESIS"` | `Cartridge.write_time` treats $A130F1 as the Sega mapper's SRAM enable and write-protect, ignores $A130F0, masks banks with `& 0x3F`, and never lets bank 0 move | decide how far to go. **The $A130F0 conflict that stood in the way is resolved** (2026-09-24): the games were observed, they use $A130F1 as the Sega mapper's SRAM control exactly as MAME has it, they never write the high byte krikzz's flag bits live in, and they need the fifth bank bit ($1C is written). So the work is the **five-bit bank field plus $A130F0 left as it is**, not krikzz's mapper-control word; item 20 of [undocumented-behavior.md](undocumented-behavior.md) has the evidence. The header rule covers 14 of the romset's images (3 Demons of Asteborg, all 9 Earthion, 2 Kunio no Nekketsu) **and Overdrive 2**, which declares `"SEGA SSF"` and currently runs on `rom_titan`. It does not cover the other 6 Demons of Asteborg, which use a game-specific `"SEGA DOA"`, nor Astebros, which is 14.75 MB under a plain `"SEGA MEGA DRIVE"` header | `run_mapper.py`, the sweep, and milestone 5's Overdrive 2 runs |

*(The oversize guard that stood here was taken on 2026-09-24, with the user's
decision to do it first: `Cartridge.__init__` now marks an image larger than
`ROM_WINDOW` with no mapper slot `unsupported`, 29 of the romset's images among
them. What it corrected is in [worklog.md](worklog.md), "Two fixes taken from the
triage". The two mapper rows below stay: naming a cartridge unsupported is not the
same as running it.)*

## Waiting on something on the machine

| What | Where the workaround lives | What to do | Re-run after |
| --- | --- | --- | --- |
| MDFourier's own analyser cannot be built here: `reference/src/MDFourier` needs FFTW and libFLAC and neither header is installed | `scripts/run_mdfourier.py` measures block levels itself (aligned on the signal's two syncs, RMS about each block's mean) instead of calling `mdfourier` | when the two libraries are available, build MDFourier and compare with its own output, which does per-frequency work this script does not | the MDFourier comparison, and the `PSG_GAIN` and `FILTER_HZ` fits in `src/megadrive_python/audio.py` that came out of it |

## Conventions and documents

| What | Why it waits |
| --- | --- |
| Restate every claim in `docs/validation.md` against its final acceptance run | the numbers move while milestones land; one pass at the end is cheaper and less error-prone than editing them each time |

## How to work the list

1. Take the whole list at once, in the order above.
2. Bump pins and reinstall in `.venv` and `.venv-pypy`.
3. Remove each workaround and its label together.
4. Re-run everything in the "re-run after" column, and the full test suite.
5. Record the results in `docs/worklog.md` and the certification record, and
   delete the entries from this file.
