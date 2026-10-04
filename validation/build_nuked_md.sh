#!/bin/sh
# Build milestone 6's referee, Nuked-MD, headless, from the pinned source in
# reference/src/Nuked-MD (fetched by scripts/fetch_references.py, never committed
# here).  Companion to validation/build_referees.sh, which builds milestone 4's three
# sound referees the same way.
#
#   sh validation/build_nuked_md.sh
#
# The binary lands in reference/build-nuked-md/nukedmd-headless (git-ignored).
#
# Nuked-MD is GPL-2.0 (nukeykt, ogamespec, andkorzh).  It is read and run as a
# referee; none of it is copied into megadrive-python.  What this repository
# contributes is two files of its own, both MIT with the rest of the tree:
#
#   validation/nuked_md_headless.c  the three back-end functions md.c calls
#                                   (Audio_*, Video_*) plus the bus tracer
#   validation/nuked_md_sdl.h       a stand-in for the SDL2 declarations md.c and
#                                   controller.c use, so no SDL is needed
#
# and which are compiled *against* Nuked-MD's headers, as validation/opn2_ref.c and
# the other two are compiled against theirs.  The binary is built locally and run
# locally; it is not distributed.
#
# Two build switches, both deliberate:
#
#   -DTMSS_ENABLE=0   Nuked-MD's FC1004 carries TMSS.  With TMSS enabled the bank
#                     register (tmss.c, dff3) powers up *clear* and the cartridge is
#                     decoded only after "SEGA" reaches $A14000, which no pre-TMSS
#                     test ROM writes.  With it 0 the same flip-flop powers up set
#                     (SDFFS instead of SDFFR), which is the pre-TMSS board -- the
#                     Model 1 VA7 the README calls the closest retail machine is
#                     itself a TMSS board, so this is one of the referee's named
#                     departures from any real console and is recorded as such.
#   -w                Nuked-MD is built with warnings off upstream too; this tree's
#                     gcc 15 would otherwise emit thousands of them.
#
# Built and used with gcc 15.2.0 (Ubuntu 15.2.0-16ubuntu1) on 2026-09-23, against
# Nuked-MD @ 594ca2a ("FJ3002 FM wip"), the revision scripts/pinned_repos.json pins.
set -e
root=$(cd "$(dirname "$0")/.." && pwd)
src="$root/reference/src/Nuked-MD"
# NMD_BUILD_DIR builds elsewhere (an experiment), leaving the tools' binary alone.
out="${NMD_BUILD_DIR:-$root/reference/build-nuked-md}"

if [ ! -f "$src/md.c" ]; then
    echo "reference/src/Nuked-MD is missing; run scripts/fetch_references.py first" >&2
    exit 1
fi

mkdir -p "$out" "$out/sdl-stub"
# md.c and controller.c say #include "SDL.h"; give them ours, on the include path
# ahead of anything else, without touching the referee's tree.
cp "$root/validation/nuked_md_sdl.h" "$out/sdl-stub/SDL.h"

CC=${CC:-gcc}
CFLAGS=${CFLAGS:--O2 -w -Wno-incompatible-pointer-types -DTMSS_ENABLE=0}

echo "nukedmd-headless (Nuked-MD @ $(cd "$src" && git rev-parse --short HEAD 2>/dev/null || echo 594ca2a))"
# savestate.c's load_state is renamed so that the driver's own can wrap it (it re-aims
# the chip's internal pointers after a load; see nuked_md_headless.c).
$CC $CFLAGS -I "$out/sdl-stub" -I "$src" -Dload_state=nmd_inner_load_state \
    -c "$src/savestate.c" -o "$out/savestate.o"
$CC $CFLAGS -I "$out/sdl-stub" -I "$src" -o "$out/nukedmd-headless" \
    "$root/validation/nuked_md_headless.c" \
    "$src/md.c" "$src/68k.c" "$src/z80.c" "$src/fc1004.c" "$src/vdp.c" \
    "$src/fm.c" "$src/arbiter.c" "$src/iochip.c" "$src/tmss.c" "$src/vram.c" \
    "$src/cartridge.c" "$src/controller.c" "$out/savestate.o" "$src/fj3002_fm.c" -lz

echo "built $out/nukedmd-headless"
