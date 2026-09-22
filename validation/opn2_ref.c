/* A headless harness around Nuked-OPN2 (reference/src/Nuked-OPN2, LGPL-2.1, pinned
 * at 335747d), built by validation/build_referees.sh and run as a referee.  Nothing
 * of Nuked-OPN2 is copied into megadrive-python: this file only drives it.
 *
 *   opn2_ref <events.bin> <out.bin> <cycles> <ym2612|ym3438> [every]
 *
 * events.bin: little-endian records of {u64 cycle; u8 port; u8 data}, sorted, where
 * `cycle` counts calls of OPN2_Clock (the chip's internal /6 cycle, 24 per output
 * sample).  Every write whose cycle equals the next call's index is applied before
 * that call, which is how a board's write at master clock T maps to T / 42.
 *
 * out.bin: one record per `every`-th cycle: {i16 mol; i16 mor; i16 status}, status
 * being the chip's own status byte (BUSY, timer B, timer A) with no read side effect
 * and widened to 16 bits so the record is three shorts and decodes without a loop.
 * With `every` = 1 that is the whole multiplexed DAC stream; the sample at the
 * chip's native rate is the sum of each group of 24.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "ym3438.h"

typedef struct { unsigned long long cycle; unsigned char port, data; } event_t;

int main(int argc, char **argv)
{
    if (argc < 5) {
        fprintf(stderr, "usage: %s events.bin out.bin cycles ym2612|ym3438 [every]\n", argv[0]);
        return 2;
    }
    unsigned long long cycles = strtoull(argv[3], NULL, 10);
    unsigned long long every = argc > 5 ? strtoull(argv[5], NULL, 10) : 1;
    int ym2612 = strcmp(argv[4], "ym2612") == 0;

    FILE *ef = fopen(argv[1], "rb");
    if (!ef) { perror(argv[1]); return 1; }
    fseek(ef, 0, SEEK_END);
    long bytes = ftell(ef);
    fseek(ef, 0, SEEK_SET);
    long count = bytes / 10;
    event_t *events = calloc(count ? count : 1, sizeof(event_t));
    for (long i = 0; i < count; i++) {
        unsigned char rec[10];
        if (fread(rec, 1, 10, ef) != 10) { fprintf(stderr, "short event file\n"); return 1; }
        unsigned long long t = 0;
        for (int b = 7; b >= 0; b--) t = (t << 8) | rec[b];
        events[i].cycle = t;
        events[i].port = rec[8];
        events[i].data = rec[9];
    }
    fclose(ef);

    FILE *of = fopen(argv[2], "wb");
    if (!of) { perror(argv[2]); return 1; }
    setvbuf(of, NULL, _IOFBF, 1 << 20);

    /* ym3438_mode_readmode makes the status readable from every port, which is the
     * ASIC; the discrete YM2612 answers on port 0 only (Eke, forum t=386 start=810).
     * The harness never calls OPN2_Read, so the flag only selects the DAC model. */
    OPN2_SetChipType(ym2612 ? ym3438_mode_ym2612 : 0);
    ym3438_t *chip = calloc(1, sizeof(ym3438_t));
    OPN2_Reset(chip);

    long next = 0;
    Bit16s buffer[2];
    for (unsigned long long c = 0; c < cycles; c++) {
        while (next < count && events[next].cycle <= c) {
            OPN2_Write(chip, events[next].port, events[next].data);
            next++;
        }
        OPN2_Clock(chip, buffer);
        if (c % every == 0) {
            int status = (chip->busy << 7) | (chip->timer_b_overflow_flag << 1)
                       | chip->timer_a_overflow_flag;
            unsigned char rec[6];
            rec[0] = buffer[0] & 0xff; rec[1] = (buffer[0] >> 8) & 0xff;
            rec[2] = buffer[1] & 0xff; rec[3] = (buffer[1] >> 8) & 0xff;
            rec[4] = status & 0xff;    rec[5] = 0;
            fwrite(rec, 1, 6, of);
        }
    }
    fclose(of);
    free(chip);
    free(events);
    return 0;
}
