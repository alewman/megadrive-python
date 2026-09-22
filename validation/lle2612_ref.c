/* A headless harness around YM2608-LLE's YM2612 path (reference/src/YM2608-LLE,
 * GPL-2.0, pinned at 7a2aca7; `fmopna_2612.c` is the NMOS YM2612 from HardWareMan's
 * die shot), built by validation/build_referees.sh and run as a referee.  Nothing of
 * YM2608-LLE is copied into megadrive-python: this file only drives its pins.
 *
 *   lle2612_ref <events.bin> <out.bin> <chip_clocks> [every]
 *
 * events.bin: little-endian records of {u64 clock; u8 port; u8 data}, sorted, where
 * `clock` counts chip clocks (MCLK / 7).  A write is driven on the pins for
 * WRITE_CLOCKS chip clocks from that clock: /CS and /WR low, A0 = port bit 0, A1 =
 * port bit 1, D = data -- the level-triggered write of `fmopna_impl.c` line 286.
 *
 * out.bin: one record per `every`-th chip clock: {i16 o_mol; i16 o_mor}.  These are
 * the chip's two DAC pins: a 9-bit two's-complement sample with 1 added to a
 * non-negative value, or the bare sign (+1 / -1) in the gaps between channels
 * (`fmopna_impl.c` lines 5824-5853).  Nuked-OPN2 in its YM2612 mode produces three
 * times these (`ym3438.c` line 987, "Amplify signal").
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "fmopna_2612.h"

/* Long enough for the write to be seen by both internal phases and by the /6
 * prescaler; the board's own writes are far further apart (Kabuto: about 33.6 Z80
 * cycles between register writes). */
#define WRITE_CLOCKS 16

typedef struct { unsigned long long clock; unsigned char port, data; } event_t;

int main(int argc, char **argv)
{
    if (argc < 4) {
        fprintf(stderr, "usage: %s events.bin out.bin chip_clocks [every]\n", argv[0]);
        return 2;
    }
    unsigned long long clocks = strtoull(argv[3], NULL, 10);
    unsigned long long every = argc > 4 ? strtoull(argv[4], NULL, 10) : 1;

    FILE *ef = fopen(argv[1], "rb");
    if (!ef) { perror(argv[1]); return 1; }
    fseek(ef, 0, SEEK_END);
    long count = ftell(ef) / 10;
    fseek(ef, 0, SEEK_SET);
    event_t *events = calloc(count ? count : 1, sizeof(event_t));
    for (long i = 0; i < count; i++) {
        unsigned char rec[10];
        if (fread(rec, 1, 10, ef) != 10) { fprintf(stderr, "short event file\n"); return 1; }
        unsigned long long t = 0;
        for (int b = 7; b >= 0; b--) t = (t << 8) | rec[b];
        events[i].clock = t;
        events[i].port = rec[8];
        events[i].data = rec[9];
    }
    fclose(ef);

    FILE *of = fopen(argv[2], "wb");
    if (!of) { perror(argv[2]); return 1; }
    setvbuf(of, NULL, _IOFBF, 1 << 20);

    fmopna_2612_t *chip = calloc(1, sizeof(fmopna_2612_t));
    chip->input.ic = 0;   /* /IC asserted */
    chip->input.cs = 1;
    chip->input.wr = 1;
    chip->input.rd = 1;
    chip->input.test = 1;
    for (int i = 0; i < 4096; i++) {   /* hold reset, as a console's does */
        FMOPNA_2612_Clock(chip, 0);
        FMOPNA_2612_Clock(chip, 1);
    }
    chip->input.ic = 1;

    long next = 0;
    unsigned long long write_until = 0;
    for (unsigned long long c = 0; c < clocks; c++) {
        if (c >= write_until) {
            chip->input.cs = 1;
            chip->input.wr = 1;
            if (next < count && events[next].clock <= c) {
                chip->input.cs = 0;
                chip->input.wr = 0;
                chip->input.a0 = events[next].port & 1;
                chip->input.a1 = (events[next].port >> 1) & 1;
                chip->input.data = events[next].data;
                write_until = c + WRITE_CLOCKS;
                next++;
            }
        }
        FMOPNA_2612_Clock(chip, 0);
        FMOPNA_2612_Clock(chip, 1);
        if (c % every == 0) {
            int l = chip->o_mol, r = chip->o_mor;
            unsigned char rec[4];
            rec[0] = l & 0xff; rec[1] = (l >> 8) & 0xff;
            rec[2] = r & 0xff; rec[3] = (r >> 8) & 0xff;
            fwrite(rec, 1, 4, of);
        }
    }
    fclose(of);
    free(chip);
    free(events);
    return 0;
}
