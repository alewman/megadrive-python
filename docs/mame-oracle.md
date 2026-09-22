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

and the Z80 one traces `pc,af,bc,de,hl,ix,iy,sp,af2,bc2,de2,hl2,totalcycles`
with `:genesis_snd_z80` visible, and watches $4000–$7FFF (YR/YW: YM2612, bank
register, VDP and PSG) and $8000–$FFFF (KR/KW: the 68000 window).

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
   byte for byte. So the two logs can be merged by time: the 68000's
   `totalcycles` × 7 and the Z80's × 15 are both master clocks from power-on.
   Anything that makes a run non-deterministic (input, a real-time clock)
   breaks this; check it the same way before relying on a merge.
3. **The Z80 action stays under 14 values.** sms-python found that a
   `logerror` with more than 14 values logs nothing on the Z80 (its
   `docs/mame-oracle.md`, trap 2); the 13 values here include BC′, DE′ and HL′.
   The 68000's action logs 22 values without trouble; the cause of the Z80 limit
   is unknown, so test before adding values.
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
