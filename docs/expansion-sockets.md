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
| Picture | composites its pixels over the Mega Drive's output | none of its own; DMA from word RAM into the VDP |
| Sound | PWM, in on the cartridge audio lines | RF5C164 PCM + CD-DA, in on the expansion audio lines |
| Into the 68000 | (its interrupts go to the SH-2s) | interrupt level 2 |

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

1. **Cartridge slot** -- what `Board.cartridge` already is. The existing `Cartridge`
   method set (`read_word`, `read_byte`, `write_word`, `write_byte`, `read_time`,
   `write_time`) becomes a named Protocol, widened by one method: `claims(top) -> bool`,
   so a device in the slot can take `$80-$9F` and parts of `$A1` (the 32X's `$A151xx`).
   The 32X is then a slot device that holds a `Cartridge`. The `Lockup`s stay as the
   answer when nothing claims the range.
2. **Expansion port** -- new; `None` by default. Same method set plus `cart_present`
   (the /CART sense line it reads) so it can map at `$000000` or `$400000`, and the
   board routes `$400000-$7FFFFF` and its `$A12xxx` window to it. With a cartridge in
   and a CD attached, the slot keeps `$000000-$3FFFFF`.
3. **Bus timing** -- every slot/port access already passes through `_bus_cycle` and the
   arbiter; the devices add their own wait states through `board.stall`, exactly as the
   VDP does (`_on_edge(vdp.hold)`). No new mechanism; the Protocol documents the rule.
4. **Clock domains** -- `System.step` gains a list of `catch_up(mclk)` devices after
   `catch_up_z80`. A device in MCLK's domain (32X) converts exactly; an asynchronous one
   (CD) keeps its own rational ratio and remainder. The 68000-led scheduler is kept; a
   device that must interrupt the 68000 mid-instruction is out of scope until one is
   shown to need it (the sub-CPU handshakes are register polls).
5. **Interrupt sources** -- `set_ipl(max(vdp.ipl, *device.ipl))`.
6. **Picture** -- a per-line hook after the VDP finishes a line, handing the device the
   line's colour indices *and* a backdrop mask before RGB conversion, at the line's
   output-delay-corrected timing (`render.OUTPUT_DELAY`). The 32X composites there.
7. **Sound** -- `AudioPath.mix` takes a list of `(samples, rate, gain)` sources, the FM
   and PSG being the first two. Gains for add-on audio come from recordings where we
   have them and are labelled assumptions where we do not.
8. **Reset** -- the reset button and the 68000's RESET output reach the devices
   (`System.press_reset` and the reset exception path).

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

## Open decisions for Aubrey

- Repository and package names: `sega32x-python` / `sega32x_python` (a package cannot start
  with a digit) and `segacd-python` / `segacd_python` -- or `mars-python`, `megacd-python`?
- Whether the sockets PR waits for the remaining `megadrive-python` work or goes first.
