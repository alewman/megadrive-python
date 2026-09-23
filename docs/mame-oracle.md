# MAME as a detector: the verified recipe

MAME 0.285 (`/usr/games/mame`) is a detector for this board, never a judge
([validation.md](validation.md)), and the weakest Mega Drive emulator of those
pinned for VDP timing. Its value is the same as for sms-python: it runs every
cartridge, it is deterministic, and its debugger can log both CPUs' state before
every instruction and every access to the devices, which is what a lockstep and
a compatibility sweep need.

## The command

```sh
python scripts/mame_trace.py genesis "/data/emu/roms/megadrive/Sonic The Hedgehog (USA, Europe).zip" --seconds 5
```

It runs MAME twice in a fresh directory (default
`tests/mame_traces/<driver>-<cart stem>`, ignored), once per CPU, each time as

```sh
mame genesis -cart <image> -rompath "/data/emu/source/myrient.erista.me/files/MAME/ROMs (non-merged)" \
  -homepath <out> -snapshot_directory <out>/snap-<cpu> -video none -sound none -nothrottle \
  -noreadconfig -skip_gameinfo -debug -debugger none -log -seconds_to_run <n> \
  -autoboot_script <out>/<cpu>.lua
```

and keeps each run's `error.log` as `m68k.log` and `z80.log`, and the exit
snapshot as `snap/exit.png`. The Lua scripts are generated in
`scripts/mame_trace.py`; the 68000 one is:

```lua
local dbg = manager.machine.debugger
dbg.visible_cpu = manager.machine.devices[":maincpu"]
dbg:command('trace M.trace,:maincpu,noloop,{logerror "M %X %X %X %X %X %X %X %X %X %X %X %X %X %X %X %X %X %X %X %d %d %d\\n",curpc,sr,d0,d1,d2,d3,d4,d5,d6,d7,a0,a1,a2,a3,a4,a5,a6,usp,sp,totalcycles,beamy,beamx}')
dbg:command('wpset c00000,20,r,1,{logerror "VR %X %X %X %d\\n",wpaddr,wpsize,wpdata,totalcycles; g}')
-- ... VW, AR/AW ($A00000-$A0FFFF), IR/IW ($A10000-$A1001F), BR/BW ($A11100, $A11200)
dbg:command("go")
```

and the Z80 one traces `pc,af,bc,de,hl,ix,iy,sp,af2,bc2,de2,hl2,r,totalcycles`
with `:genesis_snd_z80` visible, and watches $4000–$7FFF (YR/YW: YM2612, bank
register, VDP and PSG) and $8000–$FFFF (KR/KW: the 68000 window). It also watches
the 68000's accesses to $A00000–$A0FFFF, $A11100 and $A11200 (MR/MW, `wpset
a00000:maincpu,...`), logged with the Z80's own `totalcycles`: markers that place
those accesses in the Z80's stream as MAME interleaved them ("Interleaving",
below).

## What it produced (2026-09-22)

Sonic The Hedgehog (USA, Europe), `genesis`, 5 emulated seconds, 46 s wall:

| Log | Lines |
| --- | --- |
| `m68k.log` | 4,590,817 instructions; VDP 112 reads, 6,291 writes; Z80 area 3,660 reads, 7,213 writes; I/O 430 reads, 427 writes; bus request/reset 216 reads, 441 writes |
| `z80.log` | 1,740,378 instructions; YM2612 54,009 writes; 68000 window 27,000 reads (the "SEGA" voice sample) |

Two runs of the tool gave identical counts, and the exit snapshot is the
320×224 SEGA screen.

## The traps, all found the hard way

1. **Actions are evaluated against the debugger's visible CPU, not the traced
   one.** A trace of `:genesis_snd_z80` whose action logs `pc` while the 68000
   is visible logs the 68000's PC ($268, $266, ... in the Z80's lines); one
   that logs `af` logs nothing at all, silently. So a run can log only one
   CPU's registers: `mame_trace.py` makes one run per CPU, each with itself
   visible. (Found 2026-09-22 in trace actions; watchpoint actions presumably
   behave the same, so each run watches only its own CPU's windows.)
2. **The two runs are the same run.** MAME is deterministic for a cartridge
   without input: both runs recorded the same 6,091 VDP writes in 3 s of Sonic,
   byte for byte. Anything that makes a run non-deterministic (input, a real-time
   clock) breaks this; check it the same way before relying on a merge. **But the
   two logs cannot be merged by `totalcycles`** (corrected 2026-09-22; this item
   first said the 68000's × 7 and the Z80's × 15 were both master clocks from
   power-on). The 68000's count is; the Z80's is not: it stands still while the
   Z80 is held in reset and restarts near 0 after each reset (Sonic's first Z80
   instruction is at count 0, the 68000 having released it at its cycle 984),
   and through a bus request it counts on by MAME's timeslices rather than by
   the hold (a 347.2-cycle hold shows as a jump of 348 to 354). Use the markers
   ("Interleaving").
3. **The Z80 action stays at 14 values or fewer.** sms-python found that a
   `logerror` with more than 14 values logs nothing on the Z80 (its
   `docs/mame-oracle.md`, trap 2); the 14 values here (R added 2026-09-22, after
   Sonic 3's driver read it with `LD A,R`) log correctly. The 68000's action logs
   22 values without trouble; the cause of the Z80 limit is unknown, so test
   before adding values.
4. **MAME loads a zip's first member.** `mame_trace.py` extracts the image
   (`.md`, `.gen`, `.bin`, `.smd`) itself.
5. **The Z80's accesses to the 68000's bus appear on the 68000's bus in MAME.**
   m68000-python's Genesis lockstep found that the Z80's bank-register writes and
   bank-window accesses show up in the 68000's watched windows
   (`sega/megadriv.cpp`, `z80_read_68k_banked_data`); its reader drops them
   (`/data/emu/m68000-python/validation/mame_trace.py`). The same applies to
   the `A`, `V` and `I` lines here: attribute each access to a CPU before
   comparing.
6. **`totalcycles` is each CPU's own clock**, and each access line carries the
   cycle of the instruction that made it, not the bus cycle (as for the Z80 in
   sms-python, trap 5). Sub-instruction timing cannot be read from these logs.
7. **Given a file, MAME guesses the cartridge board.** sms-python found MAME's
   guess wrong for some Sega 8-bit images and loads those by software-list name
   (its `scripts/mame_media.py`). The Mega Drive's loader does the same kind of
   guessing: `base_md_cart_slot_device::get_cart_type`
   (`src/devices/bus/megadrive/md_slot.cpp`) matches code signatures for
   unlicensed mappers and protection. Check a file-loaded cartridge's board
   against `/usr/share/games/mame/hash/megadriv.xml` before trusting a
   comparison.
8. **Interrupt acceptance may wander.** On the SMS, MAME's Z80 took interrupts
   up to about ten cycles away from where the VDP raised them, from frame to
   frame (sms-python trap 7), so the lockstep takes interrupts where MAME took
   them. Expect the same here and measure it before comparing timing.
9. **TMSS**: `genesis` has no TMSS; `genesis_tmss` does and needs the boot ROM
   in its romset.
10. **Given a file, MAME's Mega Drive loader gives almost every cartridge 64 KB of
    battery RAM it does not have.** Trap 8 above says MAME guesses the board from
    the image; what it guesses when nothing matches is `rom_sramsafe` --
    "Unfortunately, there are ROMs without correct info in the header, hence, when
    loading from fullpath we do the SRAM mapping anyway" (`md_slot.cpp`
    `get_cart_type`) -- which answers $200000-$20FFFF with RAM where the board
    mirrors its ROM. Measured over the local romset (milestone 8): of the 1,799
    images MAME's `megadriv` software list names, **1,523 would run on a different
    board in MAME if handed to it as a file**, 1,347 of them exactly this way. So a
    listed image is offered to MAME **by its software-list name**
    (`scripts/mame_media.py`, after sms-python's), from a scratch rompath holding
    that one image under the name the list expects; then both machines run the board
    the list gives. An unlisted image has to be given as a file, and
    `mame_media.mame_guess` -- a port of `get_cart_type`'s signature table -- says
    which board MAME then puts it on, so that a difference can be attributed to that
    rather than to the board.

## Interleaving

MAME runs the 68000 for a timeslice, then the Z80 up to the same time; a write to
$A11100 or $A11200 ends the 68000's slice and sets a timer that applies the
request or reset after the Z80 has caught up. Three consequences, found in the
lockstep (docs/worklog.md):

- the Z80 honours a bus request only at an instruction boundary (MAME 0.285
  `src/devices/cpu/z80/z80.lst`, the `ffff` block), and keeps running after the
  68000's write while it catches up to the 68000's time;
- an instruction the Z80 is inside when its slice ends finishes in its *next*
  slice, after the 68000's: Sonic's 68000 reads $A01FFD as $FD although the Z80's
  `LD (HL),H` storing $1F there was logged before the 68000's bus request;
- the markers' Z80 counts do not say how far into an instruction the Z80 was.

So the markers give the order of every 68000 access to the Z80's side against the
Z80's instruction stream, to within the one Z80 instruction a marker falls inside.
`scripts/lockstep_mame.py` settles that instruction with the board's machine-cycle
estimate and, when the Z80's next registers or the 68000's next read of Z80 RAM
say MAME ran it on the other side, counts that as a named difference.

## Lockstep and replay

m68000-python's `validation/lockstep.py` already ran 28,249,660 instructions of
Genesis Altered Beast identical to MAME, replaying device reads from the trace
and checking writes. The board's lockstep extends that to both CPUs: step the
68000 and the Z80 against their logs in master-clock order, replay the reads of
devices that do not exist yet, and check every write. As in sms-python, a read
that differs is patched from the log and reported, so one difference does not
end the run.

## Driver facts

- `genesis` (NTSC, no TMSS), `megadriv` (PAL Europe), `megadrij` (NTSC
  Japan), `genesis_tmss`: `sega/mdconsole.cpp`. No BIOS is needed except for
  `genesis_tmss`.
- CPU tags `:maincpu` (68000) and `:genesis_snd_z80`; the VDP is
  `video/315_5313.cpp`, which embeds the 315-5124 for Mode 4; the YM2612 is
  `ymfm`'s OPN2 (`3rdparty/ymfm`).
- Snapshots work with `-video none`; the exit snapshot is 320×224 for H40.
