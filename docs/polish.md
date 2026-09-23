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
| z80-python reports only an instruction's total, so the board cannot say *where inside an instruction* a Z80 bus access falls. The arbiter needs that: it places each bank access at the step's start plus the delays already charged, which is why two reads of one `POP` land almost together. Milestone 6 measured the consequence exactly: Nuked-MD charges the `pop`'s first read 2.95 wait states and its second 3.61 (MoD's hardware: the second "at least 4", mean 3.5), where the board charged 4.08 then 2.30 -- the same total, the opposite split. **The timing session made this the board's largest remaining arbiter difference**: with the release rule corrected the first read is now 2.97 against the referee's 2.95, and the second 2.04 against its 3.60, so the pair's mean falls to 2.50 against 3.27 -- the compensation is gone and only the missing accessor is left | `Board._bank_delay` (labelled), and `System.z80_probe`, which re-runs the instruction on a probe bus to estimate the same thing for the bus grant | propose the Z80 equivalent of m68000-python's `step_clocks` -- a read-only "T-states so far in this step", readable from inside a bus callable -- and place each access at it | `scripts/run_bus_arbiter.py`'s three experiments, and the milestone-1 lockstep |

*(The four rows that waited on a decision about milestone 6's findings -- the
extra clock on $A00000-$A0FFFF, the bus grant's propagation, the DMA's start-up
cost and the work-RAM refresh -- were taken in the timing session of 2026-09-23
and are gone from this list. What each became is in [worklog.md](worklog.md),
"Session 6"; the work-RAM refresh stays as open item 5 of
[undocumented-behavior.md](undocumented-behavior.md), with what was tried.)*

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
