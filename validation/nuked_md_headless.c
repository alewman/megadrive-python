/* Headless driver and bus tracer for Nuked-MD, milestone 6's referee.
 *
 * Nuked-MD (nukeykt, ogamespec, andkorzh; GPL-2.0) is a gate-level model of the Mega
 * Drive chipset built from decapped-die photographs.  It is *read and run* here, never
 * copied: this file is megadrive-python's own (MIT) and contains none of it.  It
 * supplies the three functions Nuked-MD's md.c calls into its SDL audio and video
 * back ends, so that the same md.c runs with no window, no audio device and no
 * wall-clock pacing, and it samples the board's pins once per half-MCLK to write the
 * bus-level trace milestone 6 compares against.
 *
 * Built by validation/build_nuked_md.sh; see docs/worklog.md, milestone 6.
 *
 * Everything is driven by the environment, so that one binary serves every comparison
 * and the Python side (scripts/nuked_md.py) never has to rebuild it:
 *
 *   NMD_STOP=<t>          stop the run at half-MCLK t (required; there is no window
 *                         to close).  Dumps are written first.
 *   NMD_PRESS=<t>:<mask>  set pad 1's buttons to <mask> at half-MCLK t.  Repeatable,
 *                         semicolon-separated: "100:128;200:0".  The mask is
 *                         Nuked-MD's controller.h bit set (START = 128).
 *   NMD_TRACE=<path>      write the binary trace there (see the record below).
 *   NMD_TRACE_FROM=<t>    first half-MCLK traced (default 0).
 *   NMD_TRACE_TO=<t>      one past the last (default NMD_STOP).
 *   NMD_TRACE_MASK=<n>    bit 0: 68000/VDP bus cycles.  bit 1: Z80 bus cycles.
 *                         bit 2: the signal changes below.  Default 7.
 *   NMD_DUMP_RAM=<path>   68000 work RAM (64 KB) at the stop.
 *   NMD_DUMP_ZRAM=<path>  Z80 RAM (8 KB) at the stop.
 *   NMD_DUMP_VRAM=<path>  Nuked-MD's own vram_save() blob at the stop.
 *   NMD_PROGRESS=<n>      print "at <t>" every n half-MCLK on stderr.
 *
 * The trace is a stream of 24-byte little-endian records, packed:
 *
 *   uint64 t      half-MCLK the event began at (Nuked-MD's `mcycles`; 2 per MCLK)
 *   uint32 zt     Z80 clock (ZCLK) rising edges counted since the run began
 *   uint32 addr   byte address
 *   uint16 data   the data bus at the end of the cycle
 *   uint16 len    the cycle's length in half-MCLK (0 for a signal change)
 *   uint8  type   1 = a bus cycle on the 68000's bus, 2 = one on the Z80's,
 *                 3 = a signal change (addr = which, data = its new value)
 *   uint8  flags  type 1: bit 0 R/W (1 = read), 1 UDS, 2 LDS, 3 the 68000 drove
 *                 /AS (0 = the VDP did, i.e. a DMA cycle), 4 /DTACK was ever
 *                 asserted during the cycle.
 *                 type 2: bit 0 /RD, 1 /WR, 2 /MREQ, 3 /IORQ, 4 /M1 (all "was
 *                 asserted at some point in the cycle").
 *   uint16 pad
 *
 * A "bus cycle" on the 68000's bus is /AS low to /AS high; on the Z80's bus it is
 * (/MREQ or /IORQ) low to both high.  Lengths are therefore directly comparable with
 * what the board charges: a 68000 read with no wait states is 4 CPU clocks = 28 MCLK
 * = 56 half-MCLK from one /AS fall to the next, and the Z80's wait states show up as
 * a longer type-2 cycle and as extra ZCLK edges between two of them.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#include "common.h"
#include "controller.h"
#include "md.h"
#include "fc1004.h"
#include "vram.h"

/* --- what md.c calls into audio.c and video.c -------------------------------- */

void Audio_Init(char *f, int ntsc) { (void)f; (void)ntsc; }
void Audio_Shutdown(void) {}
void Video_Init(char *f, int ntsc) { (void)f; (void)ntsc; }
void Video_Shutdown(void) {}
void Video_Blit(void) {}
/* The picture, when asked for (NMD_PLOT=file, NMD_PLOT_FRAME=n): the VDP's RGB
   outputs sampled at every rising edge of its clock, which md.c reports here, one row
   per /HSYNC and one frame per /VSYNC, both counted on their falling edges from the
   start of the run.  Written our own way from the chip's output pins; the frame is
   raw so that nothing about where the picture sits in the line is assumed. */
#define PLOT_LINES 400
#define PLOT_SAMPLES 2048
static const char *plot_file = 0;
static long plot_frame = -1;
static long plot_frames_seen = 0;
static int plot_line = -1, plot_x = 0, plot_ohsync = 1, plot_ovsync = 1, plot_done = 0;
static unsigned char plot_buf[PLOT_LINES][PLOT_SAMPLES][3];
static unsigned short plot_count[PLOT_LINES];
static unsigned short plot_vcount[PLOT_LINES]; /* the die's V counter at each row's /HSYNC */
/* the vertical-scroll log's file and window (see vdpsig_sample) */
static FILE *vdpsig_file = 0;
static uint64_t vdpsig_from = 0, vdpsig_to = ~0ull;

static void plot_write(void)
{
    FILE *f = fopen(plot_file, "wb");
    unsigned int header[3] = { 0x504D444Eu, PLOT_LINES, PLOT_SAMPLES }; /* "NDMP" */
    if (!f) {
        fprintf(stderr, "cannot write %s\n", plot_file);
        return;
    }
    fwrite(header, sizeof header, 1, f);
    fwrite(plot_count, sizeof plot_count, 1, f);
    fwrite(plot_buf, sizeof plot_buf, 1, f);
    fwrite(plot_vcount, sizeof plot_vcount, 1, f);
    fclose(f);
}

void Video_PlotVDP(void)
{
    int hsync = ym.vdp.o_hsync, vsync = ym.vdp.o_vsync;
    if (!plot_file || plot_done)
        return;
    if (plot_ovsync && !vsync) {
        if (plot_frames_seen == plot_frame) {
            plot_write();
            plot_done = 1;
        }
        plot_frames_seen++;
        plot_line = -1;
    }
    if (plot_ohsync && !hsync) {
        plot_line++;
        plot_x = 0;
        if (plot_line >= 0 && plot_line < PLOT_LINES)
            plot_vcount[plot_line] = (unsigned short)ym.vdp.l105[1];
    }
    plot_ohsync = hsync;
    plot_ovsync = vsync;
    if (plot_frames_seen != plot_frame || plot_line < 0 || plot_line >= PLOT_LINES
        || plot_x >= PLOT_SAMPLES)
        return;
    plot_buf[plot_line][plot_x][0] = (unsigned char)ym.vdp.rgb_out[0];
    plot_buf[plot_line][plot_x][1] = (unsigned char)ym.vdp.rgb_out[1];
    plot_buf[plot_line][plot_x][2] = (unsigned char)ym.vdp.rgb_out[2];
    plot_x++;
    plot_count[plot_line] = (unsigned short)plot_x;
}
void Video_UpdateTitle(uint64_t ms) { (void)ms; }

/* --- the trace record -------------------------------------------------------- */

#pragma pack(push, 1)
typedef struct {
    uint64_t t;
    uint32_t zt;
    uint32_t addr;
    uint16_t data;
    uint16_t len;
    uint8_t type;
    uint8_t flags;
    uint16_t pad;
} nmd_event;
#pragma pack(pop)

enum { EV_M68K = 1, EV_Z80 = 2, EV_SIGNAL = 3 };

/* Signal numbers for EV_SIGNAL records.  These are the board-level lines milestone 5
   models and has no hardware judge for: the Z80's bus handover, its reset, the
   arbiter's refresh strobe and the VDP taking the bus for a DMA. */
enum {
    SIG_BUSREQ = 0,   /* /ZBR from the arbiter to the Z80 (0 = requested) */
    SIG_BUSACK = 1,   /* /BUSAK from the Z80 (0 = granted) */
    SIG_ZRESET = 2,   /* /ZRES from the arbiter (0 = held in reset) */
    SIG_BGACK  = 3,   /* /BGACK on the 68000's bus (0 = the VDP has it) */
    SIG_REFRESH = 4,  /* the arbiter's ext_ref strobe */
    SIG_WAIT   = 5,   /* /WAIT to the Z80 (0 = held) */
    SIG_SOUND  = 6,   /* /SOUND, the FM's chip select (0 = selected) */
    SIG_VRES   = 7    /* the VDP's own reset */
};

/* --- state ------------------------------------------------------------------- */

#define MAX_PRESSES 32

static uint64_t stop_at = ~0ull;
static uint64_t trace_from = 0, trace_to = 0;
static unsigned trace_mask = 7;
static uint64_t progress_every = 0;
static const char *dump_ram = 0, *dump_zram = 0, *dump_vram = 0;
static FILE *trace_file = 0;
static int inited;

static uint64_t press_at[MAX_PRESSES];
static int press_mask[MAX_PRESSES];
static int presses;

static uint32_t zclk_edges;
static int prev_zclk = -1;

/* the 68000's bus cycle under way */
static int prev_as = 1;
static uint64_t m68k_start;
static uint32_t m68k_start_zt;
static uint32_t m68k_addr;
static uint8_t m68k_flags;

/* the Z80's bus cycle under way */
static int prev_zact;
static uint64_t z80_start;
static uint32_t z80_start_zt;
static uint32_t z80_addr;
static uint8_t z80_flags;

/* last value of each traced signal */
static int sig_last[8];
static int sig_seen;

static nmd_event evbuf[4096];
static size_t evcount;

static void flush_events(void)
{
    if (evcount && trace_file)
        fwrite(evbuf, sizeof(nmd_event), evcount, trace_file);
    evcount = 0;
}

static void emit(uint8_t type, uint64_t t, uint32_t zt, uint32_t addr, uint16_t data,
                 uint16_t len, uint8_t flags)
{
    nmd_event *e;
    if (!trace_file)
        return;
    if (evcount == sizeof evbuf / sizeof evbuf[0])
        flush_events();
    e = &evbuf[evcount++];
    e->t = t;
    e->zt = zt;
    e->addr = addr;
    e->data = data;
    e->len = len;
    e->type = type;
    e->flags = flags;
    e->pad = 0;
}

static void parse_presses(const char *spec)
{
    while (*spec && presses < MAX_PRESSES) {
        char *end;
        uint64_t t = strtoull(spec, &end, 0);
        if (end == spec)
            break;
        if (*end != ':')
            break;
        press_at[presses] = t;
        press_mask[presses] = (int)strtol(end + 1, &end, 0);
        presses++;
        if (*end != ';')
            break;
        spec = end + 1;
    }
}

static void dump_blob(const char *path, const void *p, size_t n)
{
    FILE *f;
    if (!path)
        return;
    f = fopen(path, "wb");
    if (!f) {
        fprintf(stderr, "cannot write %s\n", path);
        return;
    }
    fwrite(p, 1, n, f);
    fclose(f);
}

static void setup(void)
{
    const char *s;
    inited = 1;
    if ((s = getenv("NMD_STOP"))) stop_at = strtoull(s, 0, 0);
    if ((s = getenv("NMD_TRACE_FROM"))) trace_from = strtoull(s, 0, 0);
    trace_to = stop_at;
    if ((s = getenv("NMD_TRACE_TO"))) trace_to = strtoull(s, 0, 0);
    if ((s = getenv("NMD_TRACE_MASK"))) trace_mask = (unsigned)strtoul(s, 0, 0);
    if ((s = getenv("NMD_PROGRESS"))) progress_every = strtoull(s, 0, 0);
    if ((s = getenv("NMD_PRESS"))) parse_presses(s);
    dump_ram = getenv("NMD_DUMP_RAM");
    dump_zram = getenv("NMD_DUMP_ZRAM");
    dump_vram = getenv("NMD_DUMP_VRAM");
    plot_file = getenv("NMD_PLOT");
    if ((s = getenv("NMD_VDPSIG"))) vdpsig_file = fopen(s, "w");
    if ((s = getenv("NMD_VDPSIG_FROM"))) vdpsig_from = strtoull(s, 0, 0);
    if ((s = getenv("NMD_VDPSIG_TO"))) vdpsig_to = strtoull(s, 0, 0);
    if ((s = getenv("NMD_PLOT_FRAME"))) plot_frame = strtol(s, 0, 0);
    if ((s = getenv("NMD_TRACE"))) {
        trace_file = fopen(s, "wb");
        if (!trace_file)
            fprintf(stderr, "cannot write %s\n", s);
    }
}

static void finish(void)
{
    flush_events();
    if (vdpsig_file) {
        fclose(vdpsig_file);
        vdpsig_file = 0;
    }
    if (trace_file) {
        fclose(trace_file);
        trace_file = 0;
    }
    dump_blob(dump_ram, ram, sizeof ram);
    dump_blob(dump_zram, zram, sizeof zram);
    if (dump_vram) {
        FILE *f = fopen(dump_vram, "wb");
        if (f) {
            vram_save(f);
            fclose(f);
        }
    }
}

/* The vertical-scroll path, when asked for (NMD_VDPSIG=file, NMD_VDPSIG_FROM/TO in
   half-MCLK): one text line per change of the VDP's two vertical-scroll latches
   (l184, l185), of the flag that picks between them in full-screen mode (l178), and
   of VSRAM words 0 and 1, plus every /HSYNC fall, each with the die's H and V counters
   (l106, l105) -- so where in a line a scroll value is taken can be read from the
   die model itself, not inferred. */
static int vs_l184 = -1, vs_l185 = -1, vs_l178 = -1, vs_w0 = -1, vs_w1 = -1, vs_hsync = 1;

static void vdpsig_sample(void)
{
    const vdp_t *v = &ym.vdp;
    if (!vdpsig_file || mcycles < vdpsig_from || mcycles >= vdpsig_to)
        return;
    if (vs_hsync && !v->o_hsync)
        fprintf(vdpsig_file, "%llu H 0 %d %d\n", (unsigned long long)mcycles, v->l106[1], v->l105[1]);
    vs_hsync = v->o_hsync;
    if (v->l184 != vs_l184)
        fprintf(vdpsig_file, "%llu L184 %d %d %d\n", (unsigned long long)mcycles, vs_l184 = v->l184, v->l106[1], v->l105[1]);
    if (v->l185 != vs_l185)
        fprintf(vdpsig_file, "%llu L185 %d %d %d\n", (unsigned long long)mcycles, vs_l185 = v->l185, v->l106[1], v->l105[1]);
    if (v->l178[1] != vs_l178)
        fprintf(vdpsig_file, "%llu L178 %d\n", (unsigned long long)mcycles, vs_l178 = v->l178[1]);
    if (v->vsram[1] != vs_w1)
        fprintf(vdpsig_file, "%llu W1 %d %d %d\n", (unsigned long long)mcycles, vs_w1 = v->vsram[1], v->l106[1], v->l105[1]);
    if (v->vsram[0] != vs_w0)
        fprintf(vdpsig_file, "%llu W0 %d %d %d\n", (unsigned long long)mcycles, vs_w0 = v->vsram[0], v->l106[1], v->l105[1]);
}

/* Called once per half-MCLK, after md.c has resolved every bus. */
void Audio_Update(void)
{
    int i, as, tracing, zact;

    if (!inited)
        setup();

    vdpsig_sample();

    for (i = 0; i < presses; i++)
        if (mcycles == press_at[i])
            controller_buttons_state_1 = press_mask[i];

    /* ZCLK edges: the Z80's own time base, so that a type-2 cycle's length can be
       read in T-states and its wait states counted. */
    {
        int zclk = ym.o_zclk == state_z ? 0 : ym.o_zclk;
        if (prev_zclk == 0 && zclk)
            zclk_edges++;
        prev_zclk = zclk;
    }

    tracing = trace_file && mcycles >= trace_from && mcycles < trace_to;

    /* --- the 68000's bus ---------------------------------------------------- */
    as = md.as & 1;
    if (trace_mask & 1) {
        if (prev_as && !as) {
            m68k_start = mcycles;
            m68k_start_zt = zclk_edges;
            m68k_addr = (uint32_t)(md.vaddress * 2) & 0xffffff;
            m68k_flags = 0;
        }
        if (!as) {
            if (md.rw & 1) m68k_flags |= 1;
            if (!(md.uds & 1)) m68k_flags |= 2;
            if (!(md.lds & 1)) m68k_flags |= 4;
            if (m68k.o_as != state_z) m68k_flags |= 8;
            if (!(md.dtack & 1)) m68k_flags |= 16;
            m68k_addr = (uint32_t)(md.vaddress * 2) & 0xffffff;
        }
        if (!prev_as && as && tracing)
            emit(EV_M68K, m68k_start, m68k_start_zt, m68k_addr,
                 (uint16_t)(md.vdata & 0xffff),
                 (uint16_t)(mcycles - m68k_start), m68k_flags);
    }
    prev_as = as;

    /* --- the Z80's bus ------------------------------------------------------ */
    zact = !(md.mreq & 1) || !(md.iorq & 1);
    if (trace_mask & 2) {
        if (!prev_zact && zact) {
            z80_start = mcycles;
            z80_start_zt = zclk_edges;
            z80_addr = (uint32_t)md.zaddress & 0xffff;
            z80_flags = 0;
        }
        if (zact) {
            if (!(md.rd & 1)) z80_flags |= 1;
            if (!(md.wr & 1)) z80_flags |= 2;
            if (!(md.mreq & 1)) z80_flags |= 4;
            if (!(md.iorq & 1)) z80_flags |= 8;
            if (!z80.o_m1) z80_flags |= 16;
            z80_addr = (uint32_t)md.zaddress & 0xffff;
        }
        if (prev_zact && !zact && tracing)
            emit(EV_Z80, z80_start, z80_start_zt, z80_addr,
                 (uint16_t)(md.zdata & 0xff),
                 (uint16_t)(mcycles - z80_start), z80_flags);
    }
    prev_zact = zact;

    /* --- board signals ------------------------------------------------------ */
    if (trace_mask & 4) {
        int now[8];
        now[SIG_BUSREQ] = ym.o_zbr == state_z ? 1 : ym.o_zbr;
        now[SIG_BUSACK] = z80.o_busak & 1;
        now[SIG_ZRESET] = ym.o_zres == state_z ? 1 : ym.o_zres;
        now[SIG_BGACK] = ym.o_bgack == state_z ? 1 : 0;
        now[SIG_REFRESH] = ym.arb.ext_ref & 1;
        now[SIG_WAIT] = ym.o_wait == state_z ? 1 : ym.o_wait;
        now[SIG_SOUND] = ym.arb.ext_sound & 1;
        now[SIG_VRES] = ym.arb.ext_vres & 1;
        for (i = 0; i < 8; i++) {
            if (sig_seen && now[i] == sig_last[i])
                continue;
            if (tracing)
                emit(EV_SIGNAL, mcycles, zclk_edges, (uint32_t)i, (uint16_t)now[i], 0, 0);
            sig_last[i] = now[i];
        }
        sig_seen = 1;
    }

    if (progress_every && (mcycles % progress_every) == 0)
        fprintf(stderr, "at %llu\n", (unsigned long long)mcycles);

    if (mcycles >= stop_at) {
        finish();
        exit(0);
    }
}
