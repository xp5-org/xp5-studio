/* snd personality: Sound Blaster 16 checks on the puppet, driven straight at the ports (no kernel sound driver).
 *
 *   run  [base=0x220] [secs=N]     guest-observable checks, one "SND CHK <NAME> <PASS|FAIL|NA>" line each:
 *            RESET    DSP reset answers 0xAA                       VERSION  DSP version is 4.x (SB16)
 *            MIXER    mixer register write/readback                CONFIG   mixer 0x80/0x81 IRQ and DMA decode
 *            SPEAKER  DSP D1/D3/D8 speaker on/off state
 *            PCM8     8-bit DAC block drains at the requested rate (PIT timebase, +-3 %)
 *            IRQ8     the 8-bit block end raises the card IRQ (PIC request register) and the DSP status flag
 *            PCM16    16-bit stereo DAC block drains at the requested rate
 *            IRQ16    the 16-bit block end raises the IRQ
 *            ADC8     8-bit ADC clocks samples at the requested rate (host input is not needed to clock)
 *            ADC16    16-bit ADC clocks samples at the requested rate
 *   play file=/pp/tmp/x.pcm [rate=44100]   stream a host-made 16-bit stereo PCM file through the ring, then two silent
 *            halves so the DAC rests; prints "SND PLAY START/END" with the guest's own elapsed time.
 *   tone [hz=1000] [secs=5] [rate=44100] [amp=12000]   auto-init 16-bit stereo sine out (L = hz, R = hz*1.8) for a
 *            host-side recorder of the 86Box sink; prints "SND TONE START hz=.. rate=.." / "SND TONE END".
 *   rec file=/pp/tmp/x.raw src=mic|linel|liner|linelr|stereo|stereox [bits=8|16] [rate=] [secs=]   record the ADC
 *            (the host feeds the VM's capture device) into a raw file in the DSP's format (8-bit unsigned / 16-bit
 *            signed; interleaved when stereo); prints "SND REC START/END".
 *
 * The DMA buffer is 32 KB of low RAM the kernel is told to leave alone (memmap=32K$0x90000 in linuxpuppet.append):
 * user space cannot know a physical address otherwise. Last line: "SND DONE RESULT=PASS|FAIL <n>". */
#include "pp_rt.h"

#define BUF_PHYS  0x90000u
#define BUF_SIZE  0x8000u

static unsigned sb = 0x220;
static int sb_irq = 5, sb_dma8 = 1, sb_dma16 = 5;
static unsigned char *buf;

/* ---- timebase: PIT channel 2, free running down counter, accumulated into 32 bits ---------------------------- */
static u8 port61_saved;
static u16 t_last;
static u32 t_total;
static void timer_begin(void)
{
    port61_saved = inb(0x61);
    outb(0x61, (u8) ((port61_saved & ~0x02) | 0x01));
    outb(0x43, 0xB4);
    outb(0x42, 0);
    outb(0x42, 0);
}
static u16 pit_read(void)
{
    u8 lo, hi;
    outb(0x43, 0x80);
    lo = inb(0x42);
    hi = inb(0x42);
    return (u16) (((u16) hi << 8) | lo);
}
/* ticks of 1/1193182 s since timer_start(); must be polled at least every ~50 ms */
static u32 tk(void)
{
    u16 c = pit_read();
    t_total += (u16) (t_last - c);
    t_last = c;
    return t_total;
}
static void timer_start(void) { t_last = pit_read(); t_total = 0; }
static void spin_ms(int ms)
{
    u32 end;
    timer_start();
    end = (u32) ms * 1193u;
    while (tk() < end) ;
}

/* ---- notes --------------------------------------------------------------------------------------------- */
static char notes[8][96];
static int nnotes;
static void note_s(const char *s)
{
    int i = 0;
    if (nnotes >= 8) return;
    while (s[i] && i < 95) { notes[nnotes][i] = s[i]; i++; }
    notes[nnotes++][i] = 0;
}
static char nb[96];
static int nbn;
static void nb_s(const char *s) { while (*s && nbn < 90) nb[nbn++] = *s++; }
static void nb_d(long v)
{
    char t[12]; int i = 0;
    if (v < 0) { nb[nbn++] = '-'; v = -v; }
    do { t[i++] = (char) ('0' + v % 10); v /= 10; } while (v);
    while (i) nb[nbn++] = t[--i];
}
static void nb_x(unsigned v)
{
    int i;
    for (i = 3; i >= 0; i--) nb[nbn++] = "0123456789ABCDEF"[(v >> (i * 4)) & 15];
}
static void nb_begin(void) { nbn = 0; }
static void nb_end(void) { nb[nbn] = 0; note_s(nb); }

/* ---- DSP / mixer ------------------------------------------------------------------------------------- */
static int dsp_wait_w(void)
{
    u32 n;
    for (n = 0; n < 2000000u; n++)
        if (!(inb((u16) (sb + 0x0C)) & 0x80)) return 1;
    return 0;
}
static int dsp_w(u8 v)
{
    if (!dsp_wait_w()) return 0;
    outb((u16) (sb + 0x0C), v);
    return 1;
}
static int dsp_r(void)
{
    u32 n;
    for (n = 0; n < 2000000u; n++)
        if (inb((u16) (sb + 0x0E)) & 0x80) return inb((u16) (sb + 0x0A));
    return -1;
}
static int dsp_reset(void)
{
    outb((u16) (sb + 0x06), 1);
    spin_ms(1);
    outb((u16) (sb + 0x06), 0);
    return dsp_r() == 0xAA;
}
static void mix_w(u8 r, u8 v) { outb((u16) (sb + 4), r); outb((u16) (sb + 5), v); }
static u8 mix_r(u8 r) { outb((u16) (sb + 4), r); return inb((u16) (sb + 5)); }

/* ---- 8237 DMA ----------------------------------------------------------------------------------------- */
static const u8 dma_page[4] = { 0x87, 0x83, 0x81, 0x82 };
/* mode: 0x48 | 0x44 base: DAC (memory -> device) is 0x08 "read", ADC is 0x04 "write"; auto = 0x10 */
static void dma8_prog(int ch, u32 phys, u32 len, int record, int autoinit)
{
    u8 mode = (u8) ((record ? 0x44 : 0x48) | (autoinit ? 0x10 : 0) | ch);
    u32 cnt = len - 1;
    outb(0x0A, (u8) (4 | ch));
    outb(0x0C, 0);
    outb(0x0B, mode);
    outb((u16) (ch * 2), (u8) phys);
    outb((u16) (ch * 2), (u8) (phys >> 8));
    outb(dma_page[ch], (u8) (phys >> 16));
    outb(0x0C, 0);
    outb((u16) (ch * 2 + 1), (u8) cnt);
    outb((u16) (ch * 2 + 1), (u8) (cnt >> 8));
    outb(0x0A, (u8) ch);
}
/* 16-bit: channel 4+n on the slave controller, addresses are word addresses, counts are words */
static void dma16_prog(int ch, u32 phys, u32 words, int record, int autoinit)
{
    int n = ch & 3;
    u8 mode = (u8) ((record ? 0x44 : 0x48) | (autoinit ? 0x10 : 0) | n);
    u32 w = phys >> 1, cnt = words - 1;
    outb(0xD4, (u8) (4 | n));
    outb(0xD8, 0);
    outb(0xD6, mode);
    outb((u16) (0xC0 + n * 4), (u8) w);
    outb((u16) (0xC0 + n * 4), (u8) (w >> 8));
    outb(0x8B, (u8) (phys >> 16));
    outb(0xD8, 0);
    outb((u16) (0xC2 + n * 4), (u8) cnt);
    outb((u16) (0xC2 + n * 4), (u8) (cnt >> 8));
    outb(0xD4, (u8) n);
}
static void dma_mask(int ch)
{
    if (ch >= 4) outb(0xD4, (u8) (4 | (ch & 3)));
    else outb(0x0A, (u8) (4 | ch));
}

/* ---- PIC request register --------------------------------------------------------------------------- */
static u8 irr_lo(void) { outb(0x20, 0x0A); return inb(0x20); }
static u8 irr_hi(void) { outb(0xA0, 0x0A); return inb(0xA0); }
static int irq_pending(int irq) { return irq < 8 ? (irr_lo() >> irq) & 1 : (irr_hi() >> (irq - 8)) & 1; }

/* ---- common block runner: returns elapsed ticks until the DSP interrupt flag, or 0 on timeout -------------- */
static u32 g_ticks;
static int g_irq_seen, g_irq_stale;

/* kind: 0 = 8-bit DAC, 1 = 16-bit stereo DAC, 2 = 8-bit ADC, 3 = 16-bit ADC (stereo signed) */
static int run_block(int kind, u32 rate, u32 frames)
{
    int rec = kind >= 2, is16 = kind & 1, ch = is16 ? sb_dma16 : sb_dma8;
    u32 bytes = is16 ? frames * 4 : frames;     /* 16-bit blocks are always stereo here */
    u32 samples = is16 ? frames * 2 : frames;
    u32 i, tmo;
    u8 st = is16 ? 0x02 : 0x01;
    if (bytes > BUF_SIZE) bytes = BUF_SIZE, frames = is16 ? bytes / 4 : bytes, samples = is16 ? frames * 2 : frames;
    if (!rec) {
        if (is16) for (i = 0; i < bytes; i++) buf[i] = 0;
        else for (i = 0; i < bytes; i++) buf[i] = 0x80;
    }
    if (!dsp_reset()) return 0;
    dsp_w(0xD1);
    mix_w(0x82, 0);                         /* (read-only on hardware; harmless) */
    if (is16) dma16_prog(ch, BUF_PHYS, samples, rec, 0);
    else dma8_prog(ch, BUF_PHYS, bytes, rec, 0);
    g_irq_stale = irq_pending(sb_irq);
    dsp_w(rec ? 0x42 : 0x41);
    dsp_w((u8) (rate >> 8));
    dsp_w((u8) rate);
    timer_start();
    if (is16) {
        dsp_w(rec ? 0xBE : 0xB6);           /* B6 = 16-bit out, single cycle; BE = in */
        dsp_w(0x30);                        /* signed stereo */
        dsp_w((u8) (samples - 1));
        dsp_w((u8) ((samples - 1) >> 8));
    } else {
        dsp_w(rec ? 0xC8 : 0xC0);           /* C0 = 8-bit out, single cycle; C8 = in */
        dsp_w(0x00);                        /* unsigned mono */
        dsp_w((u8) (bytes - 1));
        dsp_w((u8) ((bytes - 1) >> 8));
    }
    tmo = (u32) ((double) frames * 1193182.0 / (double) rate) * 2u + 1193182u / 5u;
    g_ticks = 0;
    g_irq_seen = 0;
    while (tk() < tmo) {
        if (mix_r(0x82) & st) {             /* block done */
            g_ticks = tk();
            g_irq_seen = irq_pending(sb_irq);
            break;
        }
    }
    /* acknowledge, stop */
    if (is16) inb((u16) (sb + 0x0F)); else inb((u16) (sb + 0x0E));
    dsp_w(0xD0);
    dma_mask(ch);
    dsp_reset();
    (void) tmo;
    return g_ticks != 0;
}

/* ---- checks ---------------------------------------------------------------------------------------- */
enum { C_RESET, C_VERSION, C_MIXER, C_CONFIG, C_SPEAKER, C_PCM8, C_IRQ8, C_PCM16, C_IRQ16, C_ADC8, C_ADC16, NCHK };
static const char *const cname[NCHK] = { "RESET", "VERSION", "MIXER", "CONFIG", "SPEAKER", "PCM8", "IRQ8", "PCM16", "IRQ16", "ADC8", "ADC16" };
static int cres[NCHK];     /* 0 pending, 1 pass, 2 fail, 3 na */

static void cset(int c, int r)
{
    static const char *const s[] = { "PEND", "PASS", "FAIL", "NA" };
    cres[c] = r;
    pr("SND CHK %s %s\n", cname[c], s[r]);
}

/* rate error in per mille: measured = frames / elapsed */
static int rate_check(int c, int kind, u32 rate, u32 frames)
{
    long meas, err;
    if (!run_block(kind, rate, frames) || !g_ticks) {
        nb_begin(); nb_s(cname[c]); nb_s(": block never finished"); nb_end();
        return 0;
    }
    meas = (long) ((double) frames * 1193182.0 / (double) g_ticks);
    err = (meas - (long) rate) * 1000 / (long) rate;
    nb_begin(); nb_s(cname[c]); nb_s(": "); nb_d(rate); nb_s(" Hz asked, "); nb_d(meas); nb_s(" Hz measured ("); nb_d(err); nb_s(" permille)"); nb_end();
    return err > -30 && err < 30;
}

static int irq_decode(u8 v)
{
    switch (v & 0x0F) { case 1: return 2; case 2: return 5; case 4: return 7; case 8: return 10; default: return -1; }
}
static int dma_decode(u8 v, int hi)
{
    int i;
    for (i = hi ? 5 : 0; i < (hi ? 8 : 4); i++) if (v & (1 << i)) return i;
    return -1;
}

static __attribute__((noinline)) void do_checks(void)
{
    int i, ok;
    u8 v, saved_l;
    static const u8 mpat[] = { 0x08, 0x10, 0xA8, 0xF8, 0x50 };

    ok = dsp_reset();
    cset(C_RESET, ok ? 1 : 2);
    if (!ok) {
        nb_begin(); nb_s("no DSP answer at "); nb_x(sb); nb_end();
        return;
    }

    ok = 0;
    if (dsp_w(0xE1)) {
        int ma = dsp_r(), mi = dsp_r();
        nb_begin(); nb_s("DSP version "); nb_d(ma); nb_s("."); nb_d(mi); nb_end();
        ok = ma == 4;
    }
    cset(C_VERSION, ok ? 1 : 2);

    /* SB16 master volume registers 0x30/0x31 hold the upper 5 bits */
    ok = 1;
    saved_l = mix_r(0x30);
    for (i = 0; i < (int) sizeof mpat; i++) {
        mix_w(0x30, mpat[i]);
        v = mix_r(0x30);
        if ((v & 0xF8) != (mpat[i] & 0xF8)) {
            if (ok) { nb_begin(); nb_s("mixer 0x30 wrote "); nb_x(mpat[i]); nb_s(" read "); nb_x(v); nb_end(); }
            ok = 0;
        }
    }
    mix_w(0x30, saved_l);
    cset(C_MIXER, ok ? 1 : 2);

    {
        int ir = irq_decode(mix_r(0x80)), d8 = dma_decode(mix_r(0x81), 0), d16 = dma_decode(mix_r(0x81), 1);
        nb_begin(); nb_s("mixer says IRQ "); nb_d(ir); nb_s(" DMA "); nb_d(d8); nb_s(" HDMA "); nb_d(d16); nb_end();
        if (ir >= 0) sb_irq = ir;
        if (d8 >= 0) sb_dma8 = d8;
        if (d16 >= 0) sb_dma16 = d16;
        cset(C_CONFIG, ir >= 0 && d8 >= 0 && d16 >= 0 ? 1 : 2);
    }

    dsp_w(0xD1);
    dsp_w(0xD8);
    ok = dsp_r() == 0xFF;
    dsp_w(0xD3);
    dsp_w(0xD8);
    ok = ok && dsp_r() == 0x00;
    cset(C_SPEAKER, ok ? 1 : 2);

    cset(C_PCM8, rate_check(C_PCM8, 0, 11025, 4096) ? 1 : 2);
    if (g_irq_stale) { note_s("IRQ8: request already pending before the block (stale)"); cset(C_IRQ8, 3); }
    else cset(C_IRQ8, g_irq_seen ? 1 : 2);

    cset(C_PCM16, rate_check(C_PCM16, 1, 44100, 4096) ? 1 : 2);
    if (g_irq_stale) { note_s("IRQ16: request already pending before the block (stale)"); cset(C_IRQ16, 3); }
    else cset(C_IRQ16, g_irq_seen ? 1 : 2);

    cset(C_ADC8, rate_check(C_ADC8, 2, 22050, 4096) ? 1 : 2);
    cset(C_ADC16, rate_check(C_ADC16, 3, 44100, 4096) ? 1 : 2);
}

/* ---- tone out for a host recorder ----------------------------------------------------------------------- */
static double dcos(double x)         /* Taylor, |x| < pi */
{
    double t = 1.0, s = 1.0, x2 = x * x;
    int k;
    for (k = 1; k < 14; k++) { t = -t * x2 / ((2 * k - 1) * (2 * k)); s += t; }
    return s;
}
static void mixer_known(void);
static __attribute__((noinline)) void do_tone(u32 hz, u32 secs, u32 rate, int amp)
{
    /* both channels: whole cycles in the 8192-frame half buffer so the auto-init loop is seamless */
    u32 frames = BUF_SIZE / 4, i, cycL, cycR;
    double wl, wr, yl1, yl2, yr1, yr2, cl, cr;
    short *s = (short *) buf;
    u32 half = frames;                 /* one DMA run is the whole buffer, looped */
    cycL = (hz * frames + rate / 2) / rate; if (!cycL) cycL = 1;
    cycR = (hz * 9 / 5 * frames + rate / 2) / rate; if (!cycR) cycR = 1;
    wl = 6.283185307179586 * (double) cycL / (double) frames;
    wr = 6.283185307179586 * (double) cycR / (double) frames;
    cl = 2.0 * dcos(wl);
    cr = 2.0 * dcos(wr);
    /* resonator seeded at sample -2, -1 of sin(w n) */
    {
        double sl1, sl2, sr1, sr2;
        /* sin(-w) and sin(-2w) via sin(x) = cos(x - pi/2) */
        sl1 = -dcos(wl - 1.5707963267948966) ; sl2 = -dcos(2.0 * wl - 1.5707963267948966);
        sr1 = -dcos(wr - 1.5707963267948966) ; sr2 = -dcos(2.0 * wr - 1.5707963267948966);
        yl1 = sl1; yl2 = sl2; yr1 = sr1; yr2 = sr2;
    }
    for (i = 0; i < frames; i++) {
        double l = cl * yl1 - yl2, r = cr * yr1 - yr2;
        yl2 = yl1; yl1 = l; yr2 = yr1; yr1 = r;
        s[i * 2] = (short) (l * amp);
        s[i * 2 + 1] = (short) (r * amp);
    }
    (void) half;
    if (!dsp_reset()) { pr("SND FAIL no DSP\n"); return; }
    mixer_known();
    dsp_w(0xD1);
    dma16_prog(sb_dma16, BUF_PHYS, frames * 2, 0, 1);
    dsp_w(0x41);
    dsp_w((u8) (rate >> 8));
    dsp_w((u8) rate);
    dsp_w(0xB6);                        /* 16-bit out, auto-init */
    dsp_w(0x30);
    dsp_w((u8) (frames * 2 - 1));       /* auto-init: one interrupt per whole buffer */
    dsp_w((u8) ((frames * 2 - 1) >> 8));
    {
        u32 secs_left = secs;
        pr("SND TONE START hz=%u,%u rate=%u secs=%u\n", (u32) (cycL * rate / frames), (u32) (cycR * rate / frames), rate, secs);
        timer_start();
        while (secs_left) {
            u32 t0 = tk();
            while (tk() - t0 < 1193182u) ;
            secs_left--;
        }
    }
    /* leave the DAC at rest, not on the last sample (a held level streams out as DC): flush the looped buffer to
     * silence and let two full rings play out before stopping, like SB16ECHO.C run_play() does */
    for (i = 0; i < frames * 2; i++) s[i] = 0;
    spin_ms(2 * (int) (frames * 1000u / rate) + 100);
    dsp_w(0xD0);
    dma_mask(sb_dma16);
    dsp_reset();
    pr("SND TONE END\n");
}


static void mixer_known(void)
{
    mix_w(0x00, 0);                         /* mixer reset: power-on defaults */
    mix_w(0x30, 0xF8); mix_w(0x31, 0xF8);   /* master L/R full */
    mix_w(0x32, 0xF8); mix_w(0x33, 0xF8);   /* voice (DAC) L/R full */
}

/* ---- play a host-made stream: 16-bit stereo PCM file streamed through the auto-init ring --------------------- */
static unsigned char pcm[3u << 19];                 /* 1.5 MB */

static __attribute__((noinline)) int load_pcm(const char *path)
{
    int fd = sys_open(path, O_RDONLY, 0), n, total = 0;
    if (fd < 0) return -1;
    while (total < (int) sizeof pcm && (n = sys_read(fd, pcm + total, (int) sizeof pcm - total)) > 0) total += n;
    sys_close(fd);
    return total;
}

static __attribute__((noinline)) void do_play(const char *path, u32 rate)
{
    int len = load_pcm(path);
    u32 half = BUF_SIZE / 2, pos = 0, i, fills = 0, tail = 0;
    u32 words = BUF_SIZE / 2;               /* 16-bit words in the ring */
    int playing, last;
    u32 t_ms;
    if (len <= 0) { pr("SND FAIL cannot read %s\n", path); return; }
    len &= ~3;
    /* prefill both halves */
    for (i = 0; i < BUF_SIZE; i++) buf[i] = 0;
    for (i = 0; i < BUF_SIZE && pos < (u32) len; i++) buf[i] = pcm[pos++];
    if (!dsp_reset()) { pr("SND FAIL no DSP\n"); return; }
    mixer_known();
    dsp_w(0xD1);
    dma16_prog(sb_dma16, BUF_PHYS, words, 0, 1);
    dsp_w(0x41);
    dsp_w((u8) (rate >> 8));
    dsp_w((u8) rate);
    dsp_w(0xB6);
    dsp_w(0x30);
    dsp_w((u8) (words - 1));
    dsp_w((u8) ((words - 1) >> 8));
    pr("SND PLAY START bytes=%u rate=%u\n", (u32) len, rate);
    timer_start();
    last = 0;
    for (;;) {
        u32 lo, hi, rem;
        outb(0xD8, 0);
        lo = inb(0xC6); hi = inb(0xC6);
        rem = lo | (hi << 8);
        playing = rem > words / 2 ? 0 : 1;
        tk();                               /* keep the timebase accumulating */
        if (playing == last) continue;
        /* half 'last' just finished: refill it */
        {
            unsigned char *d = buf + (u32) last * half;
            u32 n = 0;
            while (n < half && pos < (u32) len) d[n++] = pcm[pos++];
            if (n < half) { while (n < half) d[n++] = 0; tail++; }
            fills++;
            last = playing;
            if (tail >= 3) break;           /* two silent halves have played: the DAC sits at rest */
        }
    }
    t_ms = (u32) ((double) tk() / 1193.182);
    dsp_w(0xD0);
    dma_mask(sb_dma16);
    dsp_reset();
    pr("SND PLAY END halves=%u ms=%u expect_ms=%u\n", fills, t_ms,
       (u32) ((double) (len / 4) * 1000.0 / (double) rate));
}


/* ---- record from the ADC into a file: the host injects a signal into the VM's capture device ----------------- */
#define IN_MIC    0x01
#define IN_LINE_R 0x08
#define IN_LINE_L 0x10

static int pfx(const char *a, const char *p) { while (*p) if (*a++ != *p++) return 0; return 1; }

static __attribute__((noinline)) int save_pcm(const char *path, u32 n)
{
    int fd = sys_open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644), w = 0, r;
    if (fd < 0) return -1;
    while (w < (int) n && (r = sys_write(fd, pcm + w, (int) n - w)) > 0) w += r;
    sys_close(fd);
    return w;
}

/* src: 0 mic, 1 linel, 2 liner, 3 linelr, 4 stereo, 5 stereox. Output is the DSP's own format: 8-bit unsigned or
 * 16-bit signed, mono or stereo, interleaved. */
static __attribute__((noinline)) void do_rec(const char *path, int src, int bits, u32 rate, u32 secs)
{
    static const u8 selL[6] = { IN_MIC, IN_LINE_L, IN_LINE_R, IN_LINE_L | IN_LINE_R, IN_LINE_L, IN_LINE_R };
    static const u8 selR[6] = { IN_MIC, IN_LINE_L, IN_LINE_R, IN_LINE_L | IN_LINE_R, IN_LINE_R, IN_LINE_L };
    int stereo = src >= 4, wide = bits == 16, last = 0;
    u32 half = BUF_SIZE / 2, units = wide ? BUF_SIZE / 2 : BUF_SIZE;   /* DMA units in the ring */
    u32 want = secs * rate * (u32) (bits / 8) * (u32) (stereo ? 2 : 1), got = 0, i, t_ms;
    u16 cport = wide ? 0xC6 : (u16) (sb_dma8 * 2 + 1);
    if (want > sizeof pcm) want = sizeof pcm & ~3u;
    for (i = 0; i < BUF_SIZE; i++) buf[i] = wide ? 0 : 0x80;
    if (!dsp_reset()) { pr("SND FAIL no DSP\n"); return; }
    mixer_known();
    mix_w(0x3D, selL[src]);
    mix_w(0x3E, selR[src]);
    if (wide) dma16_prog(sb_dma16, BUF_PHYS, units, 1, 1);
    else dma8_prog(sb_dma8, BUF_PHYS, units, 1, 1);
    dsp_w(0x42);
    dsp_w((u8) (rate >> 8));
    dsp_w((u8) rate);
    if (wide) { dsp_w(0xBE); dsp_w(stereo ? 0x30 : 0x10); }
    else { dsp_w(0xCE); dsp_w(stereo ? 0x20 : 0x00); }
    dsp_w((u8) (units - 1));
    dsp_w((u8) ((units - 1) >> 8));
    pr("SND REC START src=%d bits=%d stereo=%d rate=%u want=%u in3d=%x in3e=%x\n", src, bits, stereo, rate, want,
       mix_r(0x3D), mix_r(0x3E));
    timer_start();
    while (got < want) {
        u32 lo, hi, rem, playing;
        outb(0x0C, 0);
        if (wide) outb(0xD8, 0);
        lo = inb(cport); hi = inb(cport);
        rem = lo | (hi << 8);
        playing = rem > units / 2 ? 0 : 1;
        if (tk() > 1193182u * (secs + 3)) break;
        if ((int) playing == last) continue;
        {
            u32 n = half;
            if (got + n > want) n = want - got;
            for (i = 0; i < n; i++) pcm[got + i] = buf[(u32) last * half + i];
            got += n;
            last = (int) playing;
        }
    }
    t_ms = (u32) ((double) tk() / 1193.182);
    dsp_w(0xD0);
    dma_mask(wide ? sb_dma16 : sb_dma8);
    dsp_reset();
    if (save_pcm(path, got) != (int) got) { pr("SND FAIL write %s\n", path); return; }
    pr("SND REC END bytes=%u ms=%u expect_ms=%u\n", got, t_ms, secs * 1000u);
}

static __attribute__((noinline)) int map_buf(void)
{
    int fd;
    void *m;
    if (sys_iopl(3) < 0) { pr("SND FAIL iopl\n"); return 1; }
    fd = sys_open("/dev/mem", O_RDWR, 0);
    if (fd < 0) { pr("SND FAIL /dev/mem\n"); return 1; }
    m = sys_mmap_phys(fd, BUF_SIZE, BUF_PHYS);
    if (!m) { pr("SND FAIL mmap dma buffer\n"); return 1; }
    buf = (unsigned char *) m;
    return 0;
}

int main(int argc, char **argv, char **envp)
{
    int i, fails = 0, tone = 0, rec = 0, src = 3, bits = 16;
    const char *pfile = 0;
    u32 hz = 1000, secs = 5, rate = 44100, amp = 12000;
    (void) envp;
    for (i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (seq(a, "run") || seq(a, "play")) continue;
        else if (seq(a, "rec")) rec = 1;
        else if (pfx(a, "src=")) {
            a += 4;
            src = seq(a, "mic") ? 0 : seq(a, "linel") ? 1 : seq(a, "liner") ? 2 : seq(a, "linelr") ? 3
                  : seq(a, "stereo") ? 4 : seq(a, "stereox") ? 5 : -1;
            if (src < 0) { pr("SND FAIL src %s\n", a); return 2; }
        }
        else if (pfx(a, "bits=")) bits = (int) atou(a + 5);
        else if (seq(a, "tone")) tone = 1;
        else if (a[0] == 'f' && a[1] == 'i' && a[2] == 'l' && a[3] == 'e' && a[4] == '=') pfile = a + 5;
        else if (a[0] == 'b' && a[1] == 'a' && a[2] == 's' && a[3] == 'e' && a[4] == '=') sb = atou(a + 5);
        else if (a[0] == 'h' && a[1] == 'z' && a[2] == '=') hz = atou(a + 3);
        else if (a[0] == 's' && a[1] == 'e' && a[2] == 'c' && a[3] == 's' && a[4] == '=') secs = atou(a + 5);
        else if (a[0] == 'r' && a[1] == 'a' && a[2] == 't' && a[3] == 'e' && a[4] == '=') rate = atou(a + 5);
        else if (a[0] == 'a' && a[1] == 'm' && a[2] == 'p' && a[3] == '=') amp = atou(a + 4);
        else { pr("SND FAIL arg %s\n", a); return 2; }
    }
    if (map_buf()) return 1;
    timer_begin();
    timer_start();
    pr("SND HW base=%x\n", sb);
    if (rec) {
        if (!pfile || (bits != 8 && bits != 16)) { pr("SND FAIL rec needs file= and bits=8|16\n"); return 2; }
        do_rec(pfile, src, bits, rate, secs);
    } else if (pfile) {
        do_play(pfile, rate);
    } else if (tone) {
        do_tone(hz, secs, rate, (int) amp);
    } else {
        do_checks();
        for (i = 0; i < nnotes; i++) pr("SND NOTE %s\n", notes[i]);
        for (i = 0; i < NCHK; i++)
            if (cres[i] == 2) fails++;
        if (fails) pr("SND DONE RESULT=FAIL %d\n", fails);
        else pr("SND DONE RESULT=PASS\n");
    }
    outb(0x61, port61_saved);
    return fails ? 1 : 0;
}
