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
 *   NMD_RESET=<t0>:<t1>   hold the console's reset button from half-MCLK t0 to t1, as
 *                         Nuked-MD's own frontend does (md.wres and the arbiter's
 *                         ext_wres low; controller.c, F12).  Overdrive 2 needs it after
 *                         its first run ("requires pressing Reset after the first run",
 *                         Nuked-MD Notes.md).
 *   NMD_PLOT_LIST=<spec>  plot these frames, "a-b/s,c,...", to <NMD_PLOT>.<frame>.
 *   NMD_PICS=<dir>        plot *every* frame, zlib-compressed, appended to <dir>/pics.z,
 *                         indexed in <dir>/pics.idx (frame u32, offset u64, bytes u32,
 *                         half-MCLK u64); each record inflates to one NMD_PLOT file's
 *                         body (counts, samples, V counters).
 *   NMD_AUDIO=<file>      the chip's sound: FM left, FM right and PSG, each summed over
 *                         one FM sample period (2016 half-MCLK, as Nuked-MD's audio.c
 *                         windows them), as int32 triples.
 *   NMD_SAVE_EVERY=<n>    a save state every n frames, taken where md.c takes its own
 *   NMD_SAVE_DIR=<dir>    (pending_save_state), kept as <dir>/sNNNNNN.sav with the
 *                         driver's counters and the partial frame in sNNNNNN.drv.
 *   NMD_FMSIG=<file>      every change of the FM's BUSY latch (fm.c busy_latch[1]), its
 *                         timer flags, its slot-23 marker (fsm_sel23), its timer clock
 *                         (fsm_clock_timers) and /IC, as "half-MCLK busy timerA timerB
 *                         sel23 timerclock ic" lines.
 *   NMD_LOAD=<file.sav>   start from such a save (and its .drv): nothing is plotted,
 *                         traced into pictures or sounded until it has been loaded.
 *   NMD_TRACE=<path>      write the binary trace there (see the record below).
 *   NMD_TRACE_FROM=<t>    first half-MCLK traced (default 0).
 *   NMD_TRACE_TO=<t>      one past the last (default NMD_STOP).
 *   NMD_TRACE_MASK=<n>    bit 0: 68000/VDP bus cycles.  bit 1: Z80 bus cycles.
 *                         bit 2: the signal changes below.  bit 3: work-RAM and
 *                         cartridge selects (type 4).  Default 7.
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
 *                 type 4: a work-RAM (/RAS0) or cartridge (/CE0) select, low to
 *                 high -- the memory cycles of the VDP's DMA, which do not drive
 *                 /AS, as well as the 68000's own: bit 0 work RAM (else the
 *                 cartridge), 1 /BGACK was asserted, 2 the 68000 drove /AS, 3 R/W
 *                 read (all "at some point in the cycle"), 4 the select stayed low
 *                 and the address changed (a new read under the same select); addr
 *                 and data are the 68000 bus's.
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
#include "savestate.h"
#include <zlib.h>

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
static long plot_count_frames = 1; /* NMD_PLOT_FRAMES: this many from NMD_PLOT_FRAME on */
static long plot_frames_seen = 0;
/* NMD_PLOT_LIST: the frames to plot, as "a-b/s,c,d-e" (ranges inclusive, /s a step),
   each written to <NMD_PLOT>.<frame>; overrides NMD_PLOT_FRAME(S). */
#define PLOT_LIST_MAX 65536
static unsigned char plot_list[PLOT_LIST_MAX];
static int plot_use_list = 0;
static long plot_list_last = -1;
static int plot_line = -1, plot_x = 0, plot_ohsync = 1, plot_ovsync = 1, plot_done = 0;
static unsigned char plot_buf[PLOT_LINES][PLOT_SAMPLES][3];
static unsigned short plot_count[PLOT_LINES];
static FILE *pics_file = 0, *pics_index = 0; /* NMD_PICS */
static uint64_t pics_offset = 0;
static FILE *audio_file = 0;                 /* NMD_AUDIO */
static int32_t audio_sum[3], audio_buf[3 * 8192];
static int audio_n = 0;
static long save_every = 0;                  /* NMD_SAVE_EVERY */
static const char *save_dir = 0;
static int save_requested = 0;
static FILE *fmsig_file = 0;                 /* NMD_FMSIG */
static int fmsig_last = -1;
static const char *load_path = 0;            /* NMD_LOAD */
static int load_pending = 0;
static void audio_flush(void);
static unsigned short plot_vcount[PLOT_LINES]; /* the die's V counter at each row's /HSYNC */
/* the vertical-scroll log's file and window (see vdpsig_sample) */
static FILE *vdpsig_file = 0;
static uint64_t vdpsig_from = 0, vdpsig_to = ~0ull;
static int vs_watch = -1; /* NMD_VSRAM_WATCH: one more VSRAM word to log */
static FILE *vram_file = 0; /* NMD_VRAMSIG: the VRAM access log (vram_sample) */

static void plot_write(void)
{
    char name[4096];
    FILE *f;
    if (plot_count_frames > 1 || plot_use_list)
        snprintf(name, sizeof name, "%s.%ld", plot_file, plot_frames_seen);
    else
        snprintf(name, sizeof name, "%s", plot_file);
    f = fopen(name, "wb");
    unsigned int header[3] = { 0x504D444Eu, PLOT_LINES, PLOT_SAMPLES }; /* "NDMP" */
    if (!f) {
        fprintf(stderr, "cannot write %s\n", name);
        return;
    }
    fwrite(header, sizeof header, 1, f);
    fwrite(plot_count, sizeof plot_count, 1, f);
    fwrite(plot_buf, sizeof plot_buf, 1, f);
    fwrite(plot_vcount, sizeof plot_vcount, 1, f);
    fclose(f);
}

static void pics_write(void)
{
    static unsigned char body[sizeof plot_count + sizeof plot_buf + sizeof plot_vcount];
    static unsigned char packed[sizeof body + sizeof body / 100 + 1024];
    uLongf n = sizeof packed;
    memcpy(body, plot_count, sizeof plot_count);
    memcpy(body + sizeof plot_count, plot_buf, sizeof plot_buf);
    memcpy(body + sizeof plot_count + sizeof plot_buf, plot_vcount, sizeof plot_vcount);
    if (compress2(packed, &n, body, sizeof body, 1) != Z_OK) {
        fprintf(stderr, "compress failed at frame %ld\n", plot_frames_seen);
        return;
    }
    fwrite(packed, 1, n, pics_file);
    uint32_t frame = (uint32_t)plot_frames_seen, bytes = (uint32_t)n;
    uint64_t t = mcycles;
    fwrite(&frame, 4, 1, pics_index);
    fwrite(&pics_offset, 8, 1, pics_index);
    fwrite(&bytes, 4, 1, pics_index);
    fwrite(&t, 8, 1, pics_index);
    pics_offset += n;
}

void Video_PlotVDP(void)
{
    int hsync = ym.vdp.o_hsync, vsync = ym.vdp.o_vsync;
    if ((!plot_file && !pics_file) || plot_done || load_pending)
        return;
    if (plot_ovsync && !vsync) {
        if (pics_file) {
            if (plot_line >= 0)
                pics_write();
            memset(plot_count, 0, sizeof plot_count);
            memset(plot_buf, 0, sizeof plot_buf);
        } else if (plot_use_list) {
            if (plot_frames_seen < PLOT_LIST_MAX && plot_list[plot_frames_seen]) {
                plot_write();
                memset(plot_count, 0, sizeof plot_count);
            }
            if (plot_frames_seen >= plot_list_last)
                plot_done = 1;
        } else if (plot_frames_seen >= plot_frame && plot_frames_seen < plot_frame + plot_count_frames) {
            plot_write();
            memset(plot_count, 0, sizeof plot_count);
            if (plot_frames_seen == plot_frame + plot_count_frames - 1)
                plot_done = 1;
        }
        plot_frames_seen++;
        plot_line = -1;
        if (save_every && plot_frames_seen % save_every == 0 && !save_requested) {
            pending_save_state = 1; /* md.c saves at the top of its next loop */
            save_requested = 1;
        }
    }
    if (plot_ohsync && !hsync) {
        plot_line++;
        plot_x = 0;
        if (plot_line >= 0 && plot_line < PLOT_LINES)
            plot_vcount[plot_line] = (unsigned short)ym.vdp.l105[1];
    }
    plot_ohsync = hsync;
    plot_ovsync = vsync;
    if (!pics_file && (plot_use_list ? (plot_frames_seen >= PLOT_LIST_MAX || !plot_list[plot_frames_seen])
                       : (plot_frames_seen < plot_frame
                          || plot_frames_seen >= plot_frame + plot_count_frames))
        || plot_line < 0 || plot_line >= PLOT_LINES
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

enum { EV_M68K = 1, EV_Z80 = 2, EV_SIGNAL = 3, EV_MEM = 4 };

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
static uint64_t reset_from = ~0ull, reset_to = ~0ull; /* NMD_RESET */

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
/* The memory selects, [0] the cartridge's /CE0 and [1] work RAM's /RAS0. */
static int prev_sel[2] = {1, 1};
static uint64_t mem_start[2];
static uint32_t mem_start_zt[2], mem_addr[2];
static uint8_t mem_flags[2];
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
    if ((s = getenv("NMD_RESET"))) {
        char *end;
        reset_from = strtoull(s, &end, 0);
        if (*end == ':')
            reset_to = strtoull(end + 1, 0, 0);
    }
    dump_ram = getenv("NMD_DUMP_RAM");
    dump_zram = getenv("NMD_DUMP_ZRAM");
    dump_vram = getenv("NMD_DUMP_VRAM");
    plot_file = getenv("NMD_PLOT");
    if ((s = getenv("NMD_VDPSIG"))) vdpsig_file = fopen(s, "w");
    if ((s = getenv("NMD_VDPSIG_FROM"))) vdpsig_from = strtoull(s, 0, 0);
    if ((s = getenv("NMD_VDPSIG_TO"))) vdpsig_to = strtoull(s, 0, 0);
    if ((s = getenv("NMD_VSRAM_WATCH"))) vs_watch = (int)strtol(s, 0, 0);
    if ((s = getenv("NMD_VRAMSIG"))) vram_file = fopen(s, "w");
    if ((s = getenv("NMD_PLOT_FRAME"))) plot_frame = strtol(s, 0, 0);
    if ((s = getenv("NMD_PLOT_FRAMES"))) plot_count_frames = strtol(s, 0, 0);
    if ((s = getenv("NMD_PICS"))) {
        char name[4096];
        snprintf(name, sizeof name, "%s/pics.z", s);
        pics_file = fopen(name, "ab");
        snprintf(name, sizeof name, "%s/pics.idx", s);
        pics_index = fopen(name, "ab");
        if (!pics_file || !pics_index) {
            fprintf(stderr, "cannot open NMD_PICS in %s\n", s);
            exit(1);
        }
        fseek(pics_file, 0, SEEK_END);
        pics_offset = (uint64_t)ftell(pics_file);
    }
    if ((s = getenv("NMD_AUDIO"))) audio_file = fopen(s, "ab");
    if ((s = getenv("NMD_FMSIG"))) fmsig_file = fopen(s, "w");
    if ((s = getenv("NMD_SAVE_EVERY"))) save_every = strtol(s, 0, 0);
    save_dir = getenv("NMD_SAVE_DIR");
    if (save_every && !save_dir) save_dir = ".";
    if ((load_path = getenv("NMD_LOAD"))) {
        FILE *in = fopen(load_path, "rb"), *out = fopen(DEFAULT_SAVE_NAME, "wb");
        char block[65536];
        size_t n;
        if (!in || !out) {
            fprintf(stderr, "cannot stage %s\n", load_path);
            exit(1);
        }
        while ((n = fread(block, 1, sizeof block, in)) > 0)
            fwrite(block, 1, n, out);
        fclose(in);
        fclose(out);
        pending_load_state = 1; /* md.c loads at the top of its next loop */
        load_pending = 1;
    }
    if ((s = getenv("NMD_PLOT_LIST"))) {
        while (*s) {
            char *end;
            long a = strtol(s, &end, 0), b = a, step = 1;
            if (*end == '-') b = strtol(end + 1, &end, 0);
            if (*end == '/') step = strtol(end + 1, &end, 0);
            for (long f = a; f <= b && f < PLOT_LIST_MAX; f += step > 0 ? step : 1) {
                plot_list[f] = 1;
                if (f > plot_list_last) plot_list_last = f;
            }
            plot_use_list = 1;
            s = *end == ',' ? end + 1 : end;
            if (*end && *end != ',') break;
        }
    }
    if ((s = getenv("NMD_TRACE"))) {
        trace_file = fopen(s, "wb");
        if (!trace_file)
            fprintf(stderr, "cannot write %s\n", s);
    }
}

static void finish(void)
{
    flush_events();
    audio_flush();
    if (audio_file) fclose(audio_file);
    if (fmsig_file) fclose(fmsig_file);
    if (pics_file) fclose(pics_file);
    if (pics_index) fclose(pics_index);
    audio_file = pics_file = pics_index = 0;
    if (vram_file) {
        fclose(vram_file);
        vram_file = 0;
    }
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
   of VSRAM words 0 and 1, CRAM entry 0 and (NMD_VSRAM_WATCH) any one VSRAM word, plus every /HSYNC fall, each with the die's H and V counters
   (l106, l105) -- so where in a line a scroll value is taken can be read from the
   die model itself, not inferred. */
static int vs_l184 = -1, vs_l185 = -1, vs_l178 = -1, vs_w0 = -1, vs_w1 = -1, vs_c0 = -1,
    vs_hsync = 1, vs_wx = -1;

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
    if (vs_watch >= 0 && vs_watch < 40 && v->vsram[vs_watch] != vs_wx)
        fprintf(vdpsig_file, "%llu WX %d %d %d\n", (unsigned long long)mcycles, vs_wx = v->vsram[vs_watch], v->l106[1], v->l105[1]);
    if (v->color_ram[0] != vs_c0)
        fprintf(vdpsig_file, "%llu C0 %d %d %d\n", (unsigned long long)mcycles, vs_c0 = v->color_ram[0], v->l106[1], v->l105[1]);
    if (v->vsram[1] != vs_w1)
        fprintf(vdpsig_file, "%llu W1 %d %d %d\n", (unsigned long long)mcycles, vs_w1 = v->vsram[1], v->l106[1], v->l105[1]);
    if (v->vsram[0] != vs_w0)
        fprintf(vdpsig_file, "%llu W0 %d %d %d\n", (unsigned long long)mcycles, vs_w0 = v->vsram[0], v->l106[1], v->l105[1]);
}

/* The VDP's VRAM accesses, when asked for (NMD_VRAMSIG=file, inside the NMD_VDPSIG
   window): rebuilt from the VDP's DRAM pins by the ordinary DRAM protocol -- the row
   address on /RAS falling, the column on /CAS falling -- and logged as the raw chip
   address (row << 8 | column) with the die's H and V counters:

     R addr H V   a random-port read (/RAS, /CAS and /OE low, not a transfer)
     W addr H V bank data   a write (/WE0 low: bank 0, data on AD; /WE1: bank 1, RD)
     T addr H V   a transfer cycle (/OE low as /RAS falls): a row to the serial port,
                  starting at the column /CAS gives
     S n H V      the serial port shifts out its next byte (SC rising), n-th since T

   Which linear VRAM address a chip address is, is not assumed here; it is read off a
   test cartridge's known layout (docs/worklog.md). */
static int vr_ras = 1, vr_cas = 1, vr_oe = 1, vr_sc = 0, vr_row = 0, vr_dt = 0, vr_ser = 0;
static int vr_read = 0, vr_write = 0;

static void vram_sample(void)
{
    const vdp_t *v = &ym.vdp;
    int ras = v->o_vram_ras, cas = v->o_vram_cas, oe = v->o_vram_oe1, sc = v->o_vram_sc;
    int writing = !v->o_vram_we0 || !v->o_vram_we1;
    int addr, reading;
    if (!vram_file || mcycles < vdpsig_from || mcycles >= vdpsig_to)
        return;
    if (vr_ras && !ras) {
        vr_row = v->o_vram_ad & 255;
        vr_dt = !oe;
    }
    if (vr_dt && !ras && vr_cas && !cas) {
        /* a transfer's column arrives with /CAS: it is where the serial port starts */
        fprintf(vram_file, "T %d %d %d\n", vr_row << 8 | (v->o_vram_ad & 255), v->l106[1],
                v->l105[1]);
        vr_ser = 0;
    }
    addr = vr_row << 8;
    if (!ras && vr_cas && !cas)
        addr |= v->o_vram_ad & 255;
    if (!ras && !cas) {
        static int col = 0;
        if (vr_cas)
            col = v->o_vram_ad & 255;
        addr = vr_row << 8 | col;
        reading = !oe && !vr_dt;
        if (reading && !vr_read)
            fprintf(vram_file, "R %d %d %d\n", addr, v->l106[1], v->l105[1]);
        if (writing && !vr_write)
            /* the bank the byte goes to (/WE0 -> 0, /WE1 -> 1) and its data pins */
            fprintf(vram_file, "W %d %d %d %d %d\n", addr, v->l106[1], v->l105[1],
                    v->o_vram_we0 ? 1 : 0, (v->o_vram_we0 ? v->o_vram_rd : v->o_vram_ad) & 255);
        vr_read = reading;
        vr_write = writing;
    } else {
        vr_read = vr_write = 0;
    }
    if (!vr_sc && sc)
        fprintf(vram_file, "S %d %d %d\n", vr_ser++, v->l106[1], v->l105[1]);
    vr_ras = ras;
    vr_cas = cas;
    vr_oe = oe;
    vr_sc = sc;
}

/* Called once per half-MCLK, after md.c has resolved every bus. */
/* What the driver itself must carry across a save: its frame count and where it was
   in the frame being plotted, so that a run from a save numbers and draws frames as
   the run that saved it did. */
static void driver_state(const char *path, int save)
{
    FILE *f = fopen(path, save ? "wb" : "rb");
    size_t (*io)(void *, size_t, size_t, FILE *) =
        save ? (size_t (*)(void *, size_t, size_t, FILE *))fwrite : fread;
    if (!f) {
        fprintf(stderr, "cannot open %s\n", path);
        exit(1);
    }
    io(&plot_frames_seen, sizeof plot_frames_seen, 1, f);
    io(&plot_line, sizeof plot_line, 1, f);
    io(&plot_x, sizeof plot_x, 1, f);
    io(&plot_ohsync, sizeof plot_ohsync, 1, f);
    io(&plot_ovsync, sizeof plot_ovsync, 1, f);
    io(plot_count, sizeof plot_count, 1, f);
    io(plot_vcount, sizeof plot_vcount, 1, f);
    io(plot_buf, sizeof plot_buf, 1, f);
    io(audio_sum, sizeof audio_sum, 1, f);
    fclose(f);
}

/* md.c calls load_state at the top of its loop.  Nuked-MD's own (savestate.c, built
   here as nmd_inner_load_state by validation/build_nuked_md.sh) restores the chip
   structure whole, including six pointers FC1004_Init aims back into it: valid in
   the process that saved, stale in any other.  So they are aimed again here, before a
   single cycle runs from the loaded state. */
int nmd_inner_load_state(const char *filename);
int load_state(const char *filename)
{
    int result = nmd_inner_load_state(filename);
    ym.arb.ext_vaddress_out = &ym.o_vaddress;
    ym.arb.ext_data_out = &ym.o_vdata;
    ym.ioc.ext_vdata_out = &ym.o_vdata;
    ym.ioc.ext_zdata_out = &ym.o_zdata;
    ym.ioc.ext_vaddress_out = &ym.o_vaddress;
    ym.tmss.ext_data_out = &ym.o_vdata;
    return result;
}

static void audio_flush(void)
{
    if (audio_file && audio_n)
        fwrite(audio_buf, sizeof(int32_t), (size_t)audio_n * 3, audio_file);
    audio_n = 0;
}

void Audio_Update(void)
{
    int i, as, tracing, zact;

    if (!inited)
        setup();

    if (load_pending) {
        if (pending_load_state)
            return; /* the frames before the load are not this run's */
        char drv[4096];
        snprintf(drv, sizeof drv, "%.*s.drv", (int)(strlen(load_path) - 4), load_path);
        driver_state(drv, 0);
        load_pending = 0;
        fprintf(stderr, "loaded %s at half-MCLK %llu, frame %ld\n", load_path,
                (unsigned long long)mcycles, plot_frames_seen);
    }
    if (save_requested && !pending_save_state) {
        char name[4096];
        snprintf(name, sizeof name, "%s/s%06ld.sav", save_dir, plot_frames_seen);
        if (rename(DEFAULT_SAVE_NAME, name) == 0) {
            snprintf(name, sizeof name, "%s/s%06ld.drv", save_dir, plot_frames_seen);
            driver_state(name, 1);
        } else {
            fprintf(stderr, "save at frame %ld was not written\n", plot_frames_seen);
        }
        save_requested = 0;
    }
    if (fmsig_file) {
        int now = (ym.fm.busy_latch[1] & 1) | (ym.fm.status_timer_a_dlatch & 1) << 1
                  | (ym.fm.status_timer_b_dlatch & 1) << 2 | (ym.fm.fsm_sel23 & 1) << 3
                  | (ym.fm.fsm_clock_timers & 1) << 4 | (ym.fm.input.ic & 1) << 5;
        if (now != fmsig_last) {
            fprintf(fmsig_file, "%llu %d %d %d %d %d %d\n", (unsigned long long)mcycles,
                    now & 1, (now >> 1) & 1, (now >> 2) & 1, (now >> 3) & 1, (now >> 4) & 1,
                    (now >> 5) & 1);
            fmsig_last = now;
        }
    }
    if (audio_file) {
        audio_sum[0] += ym.fm.out_l;
        audio_sum[1] += ym.fm.out_r;
        audio_sum[2] += ym.vdp.psg.psg_out;
        if (mcycles % 2016 == 0) {
            memcpy(audio_buf + 3 * audio_n, audio_sum, sizeof audio_sum);
            memset(audio_sum, 0, sizeof audio_sum);
            if (++audio_n == 8192)
                audio_flush();
        }
    }

    vdpsig_sample();
    vram_sample();

    for (i = 0; i < presses; i++)
        if (mcycles == press_at[i])
            controller_buttons_state_1 = press_mask[i];
    if (mcycles == reset_from || mcycles == reset_to) {
        md.wres = mcycles == reset_to; /* active low: 0 while the button is held */
        ym.arb.input.ext_wres = md.wres;
    }

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

    /* --- the memory selects (the VDP's DMA reads among them) ------------------- */
    if (trace_mask & 8) {
        int sel[2];
        sel[0] = ym.o_ce0 == state_z ? 1 : (ym.o_ce0 & 1);  /* cartridge */
        sel[1] = ym.vdp.o_ras0 & 1;                         /* work RAM */
        for (i = 0; i < 2; i++) {
            if (prev_sel[i] && !sel[i]) {
                mem_start[i] = mcycles;
                mem_start_zt[i] = zclk_edges;
                mem_flags[i] = i ? 1 : 0;
            }
            if (!sel[i]) {
                uint32_t a = (uint32_t)(md.vaddress * 2) & 0xffffff;
                /* A select can stay low across reads (the cartridge's /CE0 does through
                 * a DMA): each new address on the bus while it is low is a new read. */
                if (!prev_sel[i] && a != mem_addr[i]) {
                    if (tracing)
                        emit(EV_MEM, mem_start[i], mem_start_zt[i], mem_addr[i],
                             (uint16_t)(md.vdata & 0xffff),
                             (uint16_t)(mcycles - mem_start[i]), mem_flags[i] | 16);
                    mem_start[i] = mcycles;
                    mem_start_zt[i] = zclk_edges;
                    mem_flags[i] = i ? 1 : 0;
                }
                if (ym.o_bgack != state_z) mem_flags[i] |= 2;
                if (m68k.o_as != state_z) mem_flags[i] |= 4;
                if (md.rw & 1) mem_flags[i] |= 8;
                mem_addr[i] = a;
            }
            if (!prev_sel[i] && sel[i] && tracing)
                emit(EV_MEM, mem_start[i], mem_start_zt[i], mem_addr[i],
                     (uint16_t)(md.vdata & 0xffff), (uint16_t)(mcycles - mem_start[i]),
                     mem_flags[i]);
            prev_sel[i] = sel[i];
        }
    }

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
