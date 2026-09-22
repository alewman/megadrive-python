/* A headless harness around Nuked-PSG (reference/src/Nuked-PSG, GPL-2.0+, pinned at
 * d15a168), built by validation/build_referees.sh and run as a referee.  Nothing of
 * Nuked-PSG is copied into megadrive-python: this file only drives it.
 *
 *   psg_ref <events.bin> <out.bin> <clocks> [every]
 *
 * events.bin: little-endian records of {u64 clock; u8 data; u8 pad}, sorted, where
 * `clock` counts YMPSG_Clock calls (the PSG's input clock: on the Mega Drive the
 * Z80's, MCLK / 15).  A write is applied before the call whose index it names.
 *
 * out.bin: one record per `every`-th clock: {u16 attenuations; i16 output}, where
 * `attenuations` is YMPSG_Read's four nibbles (tone 1, 2, 3, noise: the attenuation
 * each channel is driving this instant, 15 = silent) and `output` is
 * YMPSG_GetOutput * 8192 rounded, i.e. Nuked's own analogue level with its measured
 * volume curve.  The attenuations are the chip's digital state and are what the
 * board is judged on; the level is recorded beside them.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "ympsg.h"

typedef struct { unsigned long long clock; unsigned char data; } event_t;

int main(int argc, char **argv)
{
    if (argc < 4) {
        fprintf(stderr, "usage: %s events.bin out.bin clocks [every]\n", argv[0]);
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
        events[i].data = rec[8];
    }
    fclose(ef);

    FILE *of = fopen(argv[2], "wb");
    if (!of) { perror(argv[2]); return 1; }
    setvbuf(of, NULL, _IOFBF, 1 << 20);

    ympsg_t *chip = calloc(1, sizeof(ympsg_t));
    YMPSG_Init(chip);

    long next = 0;
    for (unsigned long long c = 0; c < clocks; c++) {
        while (next < count && events[next].clock <= c) {
            YMPSG_Write(chip, events[next].data);
            next++;
        }
        YMPSG_Clock(chip);
        if (c % every == 0) {
            unsigned short att = YMPSG_Read(chip);
            float out = YMPSG_GetOutput(chip);
            int level = (int)(out * 8192.f);
            unsigned char rec[4];
            rec[0] = att & 0xff; rec[1] = (att >> 8) & 0xff;
            rec[2] = level & 0xff; rec[3] = (level >> 8) & 0xff;
            fwrite(rec, 1, 4, of);
        }
    }
    fclose(of);
    free(chip);
    free(events);
    return 0;
}
