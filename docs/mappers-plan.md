# Closing the cartridge gaps: mappers and special chips

Started 2026-10-29. Oracle for every board here is MAME (0.285 tree under
`reference/src/mame/.../bus/megadrive/`, newer `cart/` layout for boards 0.285 lacks);
that is a detector, not hardware, and each board's docs entry says so. Hardware-tier
sources (Plutiedev, CMD, Eke, Krikzz, Notaz for the SVP) win where they exist.

Gap measured on the staged romset (`/data/emu/roms/megadrive`): ~100 entries load as
plain ROM with `Cartridge.unsupported` set (Virtua Racing is `rom_svp`, 3 entries).

## Tasks

1. **Infrastructure** (additive; the 32X work pins this repo, so protocols only grow):
   board classes registered by MAME slot name (`cartridge.BOARDS`), `upper` flag for
   boards that decode `$400000-$7FFFFF`, optional word-wide `read_time_word`.
2. **Unlicensed and pirate mappers** (MAME `rom.cpp` family): protection readbacks,
   Realtec, Lion King 2/3, Pokemon Stadium, Chinese Fighter 3, Smart Mouse, Squirrel
   King, SMW64, Tekken Special, Futbol Argentino 96, TC2000, Sanguo 5, multicarts
   (`rom_mcpir`, 18-in-1, Gold Mega 250), Codemasters 2-in-1, Tectoy Sports, 3-in-1 FWT.
3. **Super Fighter Team boards** (Beggar Prince, Legend of Wukong, Star Odyssey).
4. **Pier Solar** (`rom_stm95`: EEPROM + banking).
5. **SVP / Virtua Racing**: needs an SSP1601 core, so a new repo `ssp1601-python`
   (CPUs always own a core), own Phase 0 (oracles: MAME `ssp1601`, GPGX `ssp16`,
   Notaz's SVP notes), then the board side.
6. **Lock-on and pass-through**: Sonic & Knuckles lock-on (`rom_sk`), Game Genie,
   Action Replay, Sega Channel (decide whether the adapter is in scope).
7. **Oversize images with no board named**: Coloco DX, FX Unit Yuki, Curse of Illmoore
   Bay, the 5.25 MB Virtual Console compilation, Pier Solar's 8 MB dumps - check header
   and code for which mapper each wants.
8. **ROM sources** (Aubrey, 2026-10-29): sweep against the full myrient No-Intro
   mirrors (`Genesis`, `Genesis (Aftermarket)`, `Genesis (Private)`), not only the 1g1r
   staged set whose 842 dangling names are stale renames (`(Beta 1)` vs dated betas).
   Add `compat_sweep.py --roms DIR` (read-only; rom-farmer owns regeneration).
9. **Test and homebrew ROMs from the mirror as cart oracles**: sweep the aftermarket
   and private sets (diagnostics, demos, mapper-exercising homebrew) the way
   famicom-python used its test ROMs; keep failures as the next smoke list.
   Not done before: only 240p, MoD and r57shell were run individually.
