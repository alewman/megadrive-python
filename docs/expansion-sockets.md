# Expansion sockets: how a 32X or a Sega CD plugs into this board

Status: **design, no code** (2026-10-21). Agreed direction with Aubrey: the add-ons are
their own boards in their own repositories -- `sega32x-python` and `segacd-python` --
which pin this repository by git revision the way this one pins its cores. Nothing is
copied out of here. This board gains a small number of explicit sockets and nothing
else; with nothing plugged in it must behave exactly as it does today.

## Why separate repositories

A Sega CD is a second computer (its own 68000, its own crystal, a BIOS, a drive, PCM,
a graphics ASIC); a 32X is two SH-2s with a framebuffer and a PWM. Folding either in as
a "variant" (the `sms-python` shape for SMS/GG/SG) would double this board and make every
Mega Drive change carry add-on code. A fork would break the never-copy rule. Separate
boards on top keep this one close to done and give each add-on its own milestones,
oracles and claims.

## The hardware facts the sockets follow

| | 32X | Sega CD |
|---|---|---|
| Attaches at | the cartridge slot; the cartridge goes through it | the expansion port |
| 68000 space it owns | `$000000-$3FFFFF` (re-mapped), `$800000-$9FFFFF`, `$A15100-$A153FF` | `$000000-$3FFFFF` with no cartridge (else `$400000-$7FFFFF`), `$A12000-$A120FF` |
| Clock | SH-2 = MCLK x 3/7: the Mega Drive's domain | its own 50 MHz crystal: asynchronous |
| Picture | switches, per pixel and in analogue, between the Mega Drive's finished RGB and its own, driven by YS | none of its own; DMA from word RAM into the VDP (one word late) |
| Sound | PWM, in on the cartridge audio lines | RF5C164 PCM + CD-DA, in on the expansion audio lines |
| Into the 68000 | none (its interrupts go to the SH-2s) | none: level 2 runs main -> **sub** (IFL2); the first draft had this backwards |

Every row above is to be re-sourced in each add-on's Phase 0 docs; this table only
says which socket each fact needs.

## Every place the board reaches outside itself today

Found by reading the code (2026-10-21, at d7882e1); each is a call site a socket replaces.

- 68000 bus: `Board.read_word/read_byte/write_word/write_byte` -- `top < 0x40` goes to
  `self.cartridge`; `0x80 <= top < 0xA0` raises `Lockup("... (32X area) without a 32X")`;
  `$400000-$7FFFFF` reads open bus.
- `$A1xxxx` system registers: `_system_read_word` / `_system_write`, which already hand
  `$A130xx` to `cartridge.read_time/write_time`.
- VDP DMA source: `Board.dma_read_word` (`board.py:829`).
- Z80 bank window: `_z80_bank_read/_z80_bank_write`, `Board.z80_peek`, and the
  pre-arbitration read in `System` (`system.py:479`) -- the same `Lockup` for `$80-$9F`.
- Interrupts: `cpu.set_ipl(vdp.ipl)` in `System.step` (`system.py:595`).
- Time: `System.step` runs one 68000 instruction, then `catch_up_z80`; `run_frame` ends
  with `board.sound_sync`.
- Picture: `render.py` turns VDP output into RGB.
- Sound: `audio.AudioPath.mix(fm, psg)`.

## The sockets

Each is a narrow `typing.Protocol`, so it transcribes to a Rust trait for
`megadrive-rust` without redesign.

Revised 2026-10-21 after both add-on Phase 0s (`segacd-python/docs/sockets.md`,
`sega32x-python/docs/handoff-brief.md`), which found the first draft short in the places
marked *(revised)*.

1. **Cartridge slot** -- what `Board.cartridge` already is. The existing `Cartridge`
   method set (`read_word`, `read_byte`, `write_word`, `write_byte`, `read_time`,
   `write_time`) becomes a named Protocol. *(revised)* Claims are **address ranges**,
   not a top byte: the `$A1` page is shared by the I/O chip, Z80 control, TMSS and the
   32X (`$A15100-$A153FF`). "No cartridge" becomes a real state (today `None` turns
   into `Cartridge(b"")`): the RAM cart and /CART sense depend on it. The 32X is a slot
   device that holds a `Cartridge`; the `Lockup`s stay as the answer when nothing claims
   a range.
2. **Expansion port** -- new; `None` by default. Same method set plus the /CART sense,
   so it maps at `$000000` (no cartridge) or `$400000`; the board routes those ranges
   and `$A12000-$A120FF` to it. *(revised)* Two board pieces must follow the port rather
   than sit beside it: `$A10001` bit 5 (/DISK, today the static `Machine.expansion`)
   and the TMSS overlay, which swaps `self.cartridge` and must sit above the port.
3. **One address decode for every bus master** *(new)* -- the 68000, the Z80 bank
   window (`_z80_bank_read/_write`, `z80_peek`, the arbiter probe at `system.py:479`)
   and the VDP DMA source (`dma_read_word`) all go through the same claims. Today each
   reaches `board.cartridge` directly. DMA additionally gets a `dma_start()` notice and
   the open-bus value, for word RAM's one-word-late rule (GPGX applies the same to SVP).
4. **Bus timing** -- every slot/port access carries its master-clock time, and the
   device adds its own wait states through `board.stall`, as the VDP does. *(revised)*
   The device's `catch_up(mclk)` must be callable **inside** an access, not only after
   the instruction: a 68000 reading the CD's or the 32X's communication registers must
   see the other CPUs up to that clock (GPGX syncs at `$A12000/1/3/4/8` and cites games
   that break without it). Overshoot bound: one sub-CPU instruction (~680 MCLK for DIVS),
   stated as a limitation.
5. **Clock domains** -- `System.step` gains a list of `catch_up(mclk)` devices after
   `catch_up_z80`. The 32X converts exactly (x3/7); the CD keeps its own rational ratio
   and remainder. The 68000-led scheduler is kept.
6. **Video timing** *(new)* -- the 32X takes HSYNC, VSYNC, EDCLK, YS and NTSC/PAL off the
   slot; its HBLK/VBLK/PEN bits, HINT/VINT and register latch need those edges and H32/H40
   changes at master-clock precision. The VDP publishes its edges as they happen.
7. **Picture** *(revised)* -- per pixel: the Mega Drive's **finished RGB**, YS and the
   shadow/highlight state, plus the line's mode and pixel clock, handed over before
   output. The device returns the pixel that leaves the console. (The 32X assumes H40
   even when the Mega Drive runs H32; NFL Quarterback Club shows it.)
8. **Sound** -- `AudioPath.mix` takes a list of sources, each with its own **changeable**
   rate and a gain; the FM and PSG are the first two. Add-on gains are labelled
   assumptions: no recording of either add-on's mix exists.
9. **Reset** *(revised)* -- three distinct events reach the devices: power-on, the
   reset button, and the 68000's RESET instruction. The CD is reset only at power-on
   (GPGX: "FRES is only asserted on Power ON"); the reset button interrupts the SH-2s.

Dropped: the interrupt socket. Neither add-on interrupts the main 68000, and `max()`
was the wrong model anyway: the VDP encodes the 68000's level, and an outside one
enters through EXINT.

Needed outside this board: `m68000-python` cannot pause an instruction mid-access, and
a CD sub-CPU access to word RAM it does not own gets no /DTACK and hangs on hardware
(KRIKzz). `segacd-python`'s brief proposes a core PR; `sh2-python` must expose the FRT's
FTOA output, which the 32X wires to the SH-2 interrupt lines.

## The gate

With no device in either socket the board must be byte-for-byte what it is now:

- the MAME lockstep replays (`0 unexplained`),
- VDPFIFOTesting 122/122 and the HV captures to the word,
- FM and PSG cycle-exact against their referees,
- the compat sweep moving **0 rows**,
- speed within noise (the hooks are `None`-checked, like `m68k_accesses`).

One branch, one PR, as a board-side change. `megadrive-rust` is frozen at
`megadrive-rust-pin-1`; it takes the sockets later as a re-pin.

## Order

1. These sockets, behind the gate.
2. `sh2-python` core rungs (independent of this board).
3. `sega32x-python`: the slot device; same clock domain, so it tests the socket design
   cheaply.
4. `segacd-python`: the port device; two clock domains and a drive.
5. Sega CD + 32X together: one device in each socket -- the check that the sockets are right.

Also: the full jgenesis clone in `reference/` (git-ignored) carries Sega's three 32X
boot ROMs in `backend/s32x-core/src/*.bin`. They were never committed; whether this
repo's pin should leave them out is an open decision.

## Open decisions for Aubrey

- Repository and package names: `sega32x-python` / `sega32x_python` (a package cannot start
  with a digit) and `segacd-python` / `segacd_python` -- or `mars-python`, `megacd-python`?
- Whether the sockets PR waits for the remaining `megadrive-python` work or goes first.
