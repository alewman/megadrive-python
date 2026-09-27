#!/bin/sh
# Build BlastEm, unchanged, as a libretro core: the reference picture for
# scripts/blastem_frames.py (docs/worklog.md, Session 9 "The debug register was not it",
# and Session 13).
#
#   sh validation/build_blastem_libretro.sh
#
# The pinned tree (reference/src/blastem, fetched by scripts/fetch_references.py) is
# copied to reference/build-blastem-libretro/ (git-ignored) and built there with its
# own Makefile.libretro, so the pinned tree stays as fetched: a patched oracle stops
# being an independent one. BlastEm is GPL-3.0; the core is built locally and run as a
# detector, never distributed, and nothing of it is copied into megadrive-python.
#
# On Windows the same Makefile builds blastem_libretro.dll under MSYS2's MinGW-w64
# shell (it picks the name from the platform); pass that path to blastem_frames.py.
#
# Built and used with gcc 15.2.0 (Ubuntu 15.2.0-16ubuntu1), BlastEm 542164d, 2026-09-27.
set -e
root=$(cd "$(dirname "$0")/.." && pwd)
src="$root/reference/src/blastem"
out="$root/reference/build-blastem-libretro"
rm -rf "$out"
cp -r "$src" "$out"
find "$out" -name '*.o' -delete
make -C "$out" -f Makefile.libretro -j"${JOBS:-8}" >/dev/null
ls "$out"/blastem_libretro.*
