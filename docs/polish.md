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

Each entry says what to do, what to re-run afterwards, and where the workaround
lives.

## Waiting on a core release or revision

| What | Where the workaround lives | What to do | Re-run after |
| --- | --- | --- | --- |
| `M68000CPU.step_clocks` is public since m68000-python 44c26f0 (PR #1, merged 2026-09-22); the board reads the private `_cycles` | `Board.m68k_access_clock` (one line) | bump the pin in `pyproject.toml` from 51ee1a5 to the merge commit, reinstall in both venvs, use the property | the milestone-1 lockstep and milestone-2 acceptance runs, to show nothing moved |
| Z80 RESET clears `I` and `R` since z80-python PR #15 (merged 2026-09-22), but no release carries it and the board depends on `z80-python>=0.4.0` from PyPI | `System._z80_reset_line` (labelled) | when a release carries it: raise the floor, drop the workaround | the milestone-1 lockstep (Sonic 3's driver reads `R`) |
| z80-python reports only an instruction's total, so the board cannot say *where inside an instruction* a Z80 bus access falls. The arbiter needs that: it places each bank access at the step's start plus the delays already charged, which is why two reads of one `POP` land almost together. Milestone 6 measured the consequence exactly: Nuked-MD charges the `pop`'s first read 2.95 wait states and its second 3.61 (MoD's hardware: the second "at least 4", mean 3.5), where the board charges 4.08 then 2.30 -- the same total, the opposite split | `Board._bank_delay` (labelled), and `System.z80_probe`, which re-runs the instruction on a probe bus to estimate the same thing for the bus grant | propose the Z80 equivalent of m68000-python's `step_clocks` -- a read-only "T-states so far in this step", readable from inside a bus callable -- and place each access at it | `scripts/run_bus_arbiter.py`'s three experiments, and the milestone-1 lockstep |

## Waiting on a decision the user has to make

These are not workarounds: they are places where milestone 6 found a judge for
something the board models differently, and where changing it would re-open an
earlier milestone's acceptance run.  They are listed here so that one pass can
take them together with the core bumps above, and they are each a `QUESTION:` in
[worklog.md](worklog.md), milestone 6.

| What | Where it lives | What Nuked-MD says | Re-run after |
| --- | --- | --- | --- |
| A 68000 access to $A00000-$A0FFFF costs one extra 68000 clock (MAME's `before_delay(1)`, its own comment "TODO: unverified") | `Board.read_*`/`write_*`, which charge nothing | 68 of 68 such accesses held 7 MCLK | the milestone-1 lockstep (re-recorded from MAME; this *removes* a named-difference class), `scripts/run_bus_arbiter.py`, Overdrive 2's DAC rate |
| The Z80 bus grant is about 7.4 68000 cycles early: the board models Zilog UM0080's machine-cycle rule and none of the arbiter's propagation | `System.z80_busack`, `arbiter.busack_grant` | the $A11100 write to /BUSAK is 9.71-16.86 cycles, the board 1.43-6.29 | `scripts/run_z80_bus_test.py`, the milestone-1 lockstep, `scripts/run_nuked_md.py roms` |
| The DMA has no start-up cost | `VDP._run_transfer` | +8.0 cycles fitted, 11.0 for one word against the board's 1.1 (Kabuto: +5.6) | `scripts/run_dma_cost.py`, VDPFIFOTesting, `scripts/run_nuked_md.py dma` |
| The work-RAM refresh re-arms 805 MCLK after the access it stretched | `Board._bus_cycle`, `RAM_REFRESH_AFTER_MCLK` | no single period fits every program; the rule itself is wrong | the HV captures, VDPFIFOTesting, `scripts/run_nuked_md.py roms` |

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
