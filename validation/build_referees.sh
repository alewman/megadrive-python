#!/bin/sh
# Build milestone 4's three audio referees from the pinned die-derived sources in
# reference/src/ (fetched by scripts/fetch_references.py, never committed here).
#
#   sh validation/build_referees.sh
#
# The binaries land in reference/build-referees/ (git-ignored), beside the
# reference/build-nuked-md/ the previous session built the same way.  Nothing of the
# referees' source is copied into megadrive-python: validation/*.c only drive them.
#
# Licences: Nuked-OPN2 LGPL-2.1, Nuked-PSG GPL-2.0+, YM2608-LLE GPL-2.0.  The
# binaries are built locally and run as referees; they are not distributed.
#
# Built and used with gcc 15.2.0 (Ubuntu 15.2.0-16ubuntu1) on 2026-09-22.
set -e
root=$(cd "$(dirname "$0")/.." && pwd)
src="$root/reference/src"
out="$root/reference/build-referees"
mkdir -p "$out"

CC=${CC:-gcc}
CFLAGS=${CFLAGS:--O2 -w}

echo "opn2_ref  (Nuked-OPN2 @ 335747d)"
$CC $CFLAGS -I "$src/Nuked-OPN2" -o "$out/opn2_ref" \
    "$root/validation/opn2_ref.c" "$src/Nuked-OPN2/ym3438.c"

echo "psg_ref   (Nuked-PSG @ d15a168)"
$CC $CFLAGS -I "$src/Nuked-PSG" -o "$out/psg_ref" \
    "$root/validation/psg_ref.c" "$src/Nuked-PSG/ympsg.c"

echo "lle2612_ref (YM2608-LLE @ 7a2aca7, fmopna_2612.c)"
$CC $CFLAGS -I "$src/YM2608-LLE" -o "$out/lle2612_ref" \
    "$root/validation/lle2612_ref.c" "$src/YM2608-LLE/fmopna_2612.c"

echo "built in $out"
