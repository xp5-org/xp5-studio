/* plat personality: the chipset every PC has, checked at the ports -- 8254 PIT (channel 2), MC146818 RTC/CMOS,
 * 8259 PICs and 8237 DMA controllers. Nothing here depends on a card; all of it is on every cell.
 *
 *   run [only=pit|rtc|pic|dma]
 *
 * Linux owns PIT channel 0 and the PICs (their interrupt mask is only read, never written), so the PIT checks use
 * channel 2 (the speaker timer) and the DMA checks use channels the tinyconfig kernel never claims. CMOS bytes and
 * DMA registers that are written are restored. Output: "PLAT CHK <NAME> <PASS|FAIL|NA>", "PLAT NOTE <text>" detail
 * lines, last "PLAT DONE RESULT=PASS|FAIL <n>". */
#include "pp_rt.h"

#define PIT_HZ 1193182u

static char notes[16][96];
static int nnotes;
static int fails, checks;

static void note_s(const char *s)
{
    int i = 0;
    if (nnotes >= 16) return;
    while (s[i] && i < 95) { notes[nnotes][i] = s[i]; i++; }
    notes[nnotes++][i] = 0;
}

/* a note with up to three numbers: the text carries %u/%x placeholders resolved by pr(); done by hand here */
static char nb[96];
static int nbn;
static void nb_s(const char *s) { while (*s && nbn < 94) nb[nbn++] = *s++; }
static void nb_u(u32 v, int hex)
{
    char t[12]; int i = 0, base = hex ? 16 : 10;
    do { t[i++] = "0123456789abcdef"[v % (u32) base]; v /= (u32) base; } while (v);
    while (i && nbn < 94) nb[nbn++] = t[--i];
}
static void nb_flush(void) { nb[nbn] = 0; note_s(nb); nbn = 0; }

static void result(const char *name, int ok)
{
    checks++;
    if (ok < 0) pr("PLAT CHK %s NA\n", name);
    else if (ok) pr("PLAT CHK %s PASS\n", name);
    else { fails++; pr("PLAT CHK %s FAIL\n", name); }
}

/* ======================================================================== PIT channel 2 ======= */
static u8 p61_saved;
static void pit_begin(void) { p61_saved = inb(0x61); outb(0x61, (u8) ((p61_saved & ~0x02) | 0x01)); }
static void pit_end(void) { outb(0x61, p61_saved); }
static int out2(void) { return (inb(0x61) >> 5) & 1; }
static void pit_prog(u8 ctl, u32 count, int two)
{
    outb(0x43, ctl);
    outb(0x42, (u8) count);
    if (two) outb(0x42, (u8) (count >> 8));
}
static u32 pit_latch(void)
{
    u8 lo, hi;
    outb(0x43, 0x80);
    lo = inb(0x42);
    hi = inb(0x42);
    return ((u32) hi << 8) | lo;
}

/* 16-bit access: load N in mode 2, the counter must be counting down from N, hi and lo in the right order */
static int chk_pit_rw(void)
{
    static const u32 n[] = { 0x1234, 0xFFFF, 0x0100, 0xA5C3 };
    int i, bad = 0;
    for (i = 0; i < 4; i++) {
        u32 c;
        pit_prog(0xB4, n[i], 1);                /* ch2, lo/hi, mode 2 */
        c = pit_latch();
        if (c > n[i] || n[i] - c > 0x80) {
            if (bad++ < 2) { nb_s("N="); nb_u(n[i], 1); nb_s(" first latch="); nb_u(c, 1); nb_flush(); }
        }
    }
    /* lobyte-only and hibyte-only access */
    pit_prog(0xA4, 0x30, 0);                    /* ch2, rw=2 (hi only), mode 2: count 0x3000 */
    {
        u32 c;
        outb(0x43, 0x80);
        c = inb(0x42);
        if (c > 0x30 || c < 0x2F) { bad++; nb_s("hi-only latch="); nb_u(c, 1); nb_flush(); }
    }
    pit_prog(0x94, 0xC0, 0);                    /* rw=1 (lo only), mode 2: count 0xC0 */
    {
        u32 c;
        outb(0x43, 0x80);
        c = inb(0x42);
        if (c > 0xC0 || c < 0x10) { bad++; nb_s("lo-only latch="); nb_u(c, 1); nb_flush(); }
    }
    return !bad;
}

/* the count falls: two latches a few reads apart, the second is smaller (mod the reload) */
static int chk_pit_count(void)
{
    u32 a, b, d;
    pit_prog(0xB4, 0xFFFF, 1);
    a = pit_latch();
    sleep_ms(5);
    b = pit_latch();
    d = (a - b) & 0xFFFF;
    /* 5 ms = 5966 counts; nanosleep lasts at least that, upper bound is the scheduler (not an emulator number) */
    if (d < 5900 || d > 30000) { nb_s("5 ms sleep spanned "); nb_u(d, 0); nb_s(" counts (want 5966..30000)"); nb_flush(); return 0; }
    return 1;
}

static int chk_pit_mode0(void)
{
    u32 t0;
    int seen = 0;
    pit_prog(0xB0, 3000, 1);                    /* mode 0: OUT low until terminal count, 2.5 ms */
    if (out2()) { note_s("mode 0 OUT2 high right after the load"); return 0; }
    t0 = now_ms();
    while (now_ms() - t0 < 50)
        if (out2()) { seen = 1; break; }
    if (!seen) { note_s("mode 0 OUT2 never went high within 50 ms"); return 0; }
    t0 = now_ms();
    pit_prog(0xB0, 0xFFFF, 1);                  /* reload: OUT must drop again */
    if (out2()) { note_s("mode 0 OUT2 stayed high after reload"); return 0; }
    return 1;
}

static int chk_pit_mode3(void)
{
    u32 t0, ms, edges = 0, exp_e;
    int last;
    pit_prog(0xB6, 2000, 1);                    /* mode 3, 2000 counts = 1.676 ms per period */
    last = out2();
    t0 = now_ms();
    while ((ms = now_ms() - t0) < 40) {
        int o = out2();
        if (o != last) { edges++; last = o; }
    }
    exp_e = ms * 2 * PIT_HZ / 2000u / 1000u;
    if (edges * 4 < exp_e * 3 || edges * 4 > exp_e * 5) {
        nb_s("mode 3 OUT2 edges "); nb_u(edges, 0); nb_s(" in "); nb_u(ms, 0); nb_s(" ms, want ~"); nb_u(exp_e, 0);
        nb_flush();
        return 0;
    }
    return 1;
}

static int chk_pit_bcd(void)
{
    u32 c;
    int i;
    pit_prog(0xB5, 0x9999, 1);                  /* mode 2, BCD */
    for (i = 0; i < 8; i++) {
        c = pit_latch();
        if ((c & 0xF) > 9 || ((c >> 4) & 0xF) > 9 || ((c >> 8) & 0xF) > 9 || ((c >> 12) & 0xF) > 9) {
            nb_s("BCD count "); nb_u(c, 1); nb_s(" has a digit above 9"); nb_flush();
            return 0;
        }
    }
    if (c > 0x9999 || c < 0x9000) { nb_s("BCD count "); nb_u(c, 1); nb_s(" not falling from 9999"); nb_flush(); return 0; }
    return 1;
}

static int chk_pit_rb(void)
{
    u8 st, lo;
    pit_prog(0xB4, 0x4000, 1);
    outb(0x43, 0xC8);                           /* read-back: latch count + status of channel 2 */
    st = inb(0x42);
    lo = inb(0x42);
    (void) inb(0x42);
    if ((st & 0x3F) != 0x34) { nb_s("status byte "); nb_u(st, 1); nb_s(", want x4 (rw 11, mode 2, binary) in bits 5-0 = 34"); nb_flush(); return 0; }
    (void) lo;
    outb(0x43, 0xE8);                           /* status only */
    st = inb(0x42);
    if ((st & 0x3F) != 0x34) { nb_s("status-only read "); nb_u(st, 1); nb_flush(); return 0; }
    return 1;
}

/* ========================================================================== RTC / CMOS ======= */
static u8 cmos_rd(u8 r) { outb(0x70, (u8) (r & 0x7F)); return inb(0x71); }
static void cmos_wr(u8 r, u8 v) { outb(0x70, (u8) (r & 0x7F)); outb(0x71, v); }

static u32 rtc_val(u8 raw)
{
    if (cmos_rd(0x0B) & 0x04) return raw;
    return (u32) ((raw >> 4) * 10 + (raw & 0xF));
}

static int chk_rtc_fmt(void)
{
    u32 sec, mn, hr, dom, mon, yr;
    u8 b = cmos_rd(0x0B);
    int bad = 0;
    if (!(cmos_rd(0x0D) & 0x80)) { note_s("reg D bit 7 (battery/RAM valid) clear"); bad++; }
    while (cmos_rd(0x0A) & 0x80) ;              /* wait out an update */
    sec = rtc_val(cmos_rd(0)); mn = rtc_val(cmos_rd(2));
    dom = rtc_val(cmos_rd(7)); mon = rtc_val(cmos_rd(8)); yr = rtc_val(cmos_rd(9));
    {
        u8 h = cmos_rd(4);
        if (!(b & 0x02)) { u8 pm = h & 0x80; h &= 0x7F; hr = rtc_val(h); if (pm) hr += 12; }  /* 12 h */
        else hr = rtc_val(h);
    }
    if (sec > 59 || mn > 59 || hr > 23 || dom < 1 || dom > 31 || mon < 1 || mon > 12 || yr > 99) {
        nb_s("time out of range: "); nb_u(yr, 0); nb_s("-"); nb_u(mon, 0); nb_s("-"); nb_u(dom, 0); nb_s(" ");
        nb_u(hr, 0); nb_s(":"); nb_u(mn, 0); nb_s(":"); nb_u(sec, 0); nb_flush();
        bad++;
    }
    if ((cmos_rd(0x0A) & 0x70) != 0x20) { nb_s("reg A divider "); nb_u(cmos_rd(0x0A), 1); nb_s(" not 32.768 kHz (x2y)"); nb_flush(); bad++; }
    return !bad;
}

/* seconds advance, once per second, and the update-in-progress bit rises between the steps */
static int chk_rtc_tick(void)
{
    u32 t0, t1, t2, s0, s1;
    int uip = 0;
    while (cmos_rd(0x0A) & 0x80) ;
    s0 = cmos_rd(0);
    t0 = now_ms();
    while (1) {
        u8 a = cmos_rd(0x0A);
        if (a & 0x80) uip = 1;
        s1 = cmos_rd(0);
        if (s1 != s0 || now_ms() - t0 > 2500) break;
    }
    t1 = now_ms();
    if (s1 == s0) { note_s("seconds register did not change in 2.5 s"); return 0; }
    s0 = s1;
    while (1) { s1 = cmos_rd(0); if (s1 != s0 || now_ms() - t1 > 2500) break; }
    t2 = now_ms();
    if (s1 == s0) { note_s("seconds register stopped"); return 0; }
    if (t2 - t1 < 900 || t2 - t1 > 1100) { nb_s("one second took "); nb_u(t2 - t1, 0); nb_s(" ms of guest wall time"); nb_flush(); return 0; }
    if (!uip) { note_s("update-in-progress bit never seen set"); return 0; }
    return 1;
}

/* the RTC's second against the PIT: 1 193 182 counts (crystal agreement, in the emulated machine's own time) */
static int chk_rtc_pit(void)
{
    u32 s0, last, now, total = 0, t0;
    pit_prog(0xB4, 0, 1);                       /* mode 2, count 65536: free running */
    s0 = cmos_rd(0);
    t0 = now_ms();
    while (cmos_rd(0) == s0 && now_ms() - t0 < 2500) ;   /* sync to a second edge */
    s0 = cmos_rd(0);
    last = pit_latch();
    t0 = now_ms();
    while (1) {
        now = pit_latch();
        total += (last - now) & 0xFFFF;
        last = now;
        if (cmos_rd(0) != s0) break;
        if (now_ms() - t0 > 2500) { note_s("second edge not seen"); return 0; }
    }
    now = pit_latch();
    total += (last - now) & 0xFFFF;
    if (total < PIT_HZ - PIT_HZ / 50 || total > PIT_HZ + PIT_HZ / 50) {
        nb_s("one RTC second = "); nb_u(total, 0); nb_s(" PIT counts, want 1193182 +-2%"); nb_flush();
        return 0;
    }
    return 1;
}

/* periodic flag: rate select 6 = 1024 Hz sets reg C bit 6 (PF) on its own, no interrupt enabled */
static int chk_rtc_periodic(void)
{
    u8 a = cmos_rd(0x0A);
    u32 n = 0, last, now, ticks = 0;
    cmos_wr(0x0A, (u8) ((a & 0xF0) | 0x06));
    (void) cmos_rd(0x0C);
    pit_prog(0xB4, 0, 1);
    last = pit_latch();
    while (ticks < PIT_HZ / 4) {                /* a quarter second of PIT time */
        u8 c = cmos_rd(0x0C);
        if (c & 0x40) n++;
        now = pit_latch();
        ticks += (last - now) & 0xFFFF;
        last = now;
    }
    cmos_wr(0x0A, a);
    /* 256 periods expected. The flag is polled, so a period can be missed when the poll is slower than 0.98 ms:
     * a count of at least 70% proves the flag runs at 1024 Hz; more than 105% proves it runs fast. */
    if (n * 100 < 256 * 70 || n * 100 > 256 * 105) { nb_s("PF set "); nb_u(n, 0); nb_s(" times in 1/4 s, want ~256 (1024 Hz)"); nb_flush(); return 0; }
    return 1;
}

static int chk_rtc_alarm(void)
{
    u8 sa = cmos_rd(1), sb = cmos_rd(3), sc_ = cmos_rd(5), b = cmos_rd(0x0B);
    u8 s;
    u32 t0;
    int ok = 0;
    while (cmos_rd(0x0A) & 0x80) ;
    s = cmos_rd(0);
    {
        u32 v = rtc_val(s) + 2;
        if (v > 59) v -= 60;
        cmos_wr(1, (b & 0x04) ? (u8) v : (u8) (((v / 10) << 4) | (v % 10)));
    }
    cmos_wr(3, 0xFF);                            /* don't care hour/minute: matches every one */
    cmos_wr(5, 0xFF);
    (void) cmos_rd(0x0C);
    t0 = now_ms();
    while (now_ms() - t0 < 3500)
        if (cmos_rd(0x0C) & 0x20) { ok = 1; break; }
    cmos_wr(1, sa); cmos_wr(3, sb); cmos_wr(5, sc_);
    if (!ok) note_s("alarm flag (reg C bit 5) not set within 3.5 s of a match 2 s ahead");
    return ok;
}

static int chk_cmos_ram(void)
{
    static const u8 pat[] = { 0x55, 0xAA, 0x00, 0xFF };
    u8 saved[16];
    int i, k, bad = 0;
    for (i = 0; i < 16; i++) saved[i] = cmos_rd((u8) (0x70 + i));
    for (k = 0; k < 4; k++) {
        for (i = 0; i < 16; i++) cmos_wr((u8) (0x70 + i), (u8) (pat[k] ^ i));
        for (i = 0; i < 16; i++) {
            u8 g = cmos_rd((u8) (0x70 + i));
            if (g != (u8) (pat[k] ^ i)) { if (bad++ < 2) { nb_s("cmos "); nb_u(0x70 + i, 1); nb_s(" wrote "); nb_u((u8) (pat[k] ^ i), 1); nb_s(" read "); nb_u(g, 1); nb_flush(); } }
        }
    }
    for (i = 0; i < 16; i++) cmos_wr((u8) (0x70 + i), saved[i]);
    return !bad;
}

/* ============================================================================== PIC ======= */
static u8 pic_irr(u16 port) { outb(port, 0x0A); return inb(port); }
static u8 pic_isr(u16 port) { u8 v; outb(port, 0x0B); v = inb(port); outb(port, 0x0A); return v; }

static int chk_pic_state(void)
{
    u8 m1 = inb(0x21), s1 = inb(0xA1);
    int bad = 0;
    if (inb(0x21) != m1 || inb(0xA1) != s1) { note_s("IMR changed between two reads"); bad++; }
    if (m1 & 0x05) { nb_s("master IMR "); nb_u(m1, 1); nb_s(" masks the timer or cascade line"); nb_flush(); bad++; }
    if (pic_isr(0x20) & 0x01) { note_s("IRQ0 shows in service while a user program runs"); bad++; }
    if (pic_isr(0xA0)) { note_s("slave ISR nonzero while idle"); bad++; }
    return !bad;
}

/* with interrupts off the timer request has to stay pending in the master's IRR. No system call may run in the window
 * (the kernel re-enables interrupts inside one and would take the request), so the time base is PIT channel 2. */
static int irr_try(u32 *polls_out, u8 *irr_out, u8 *isr_out)
{
    u32 ticks = 0, last, now, polls = 0;
    u8 irr = 0;
    int seen = 0;
    pit_prog(0xB4, 0, 1);
    last = pit_latch();
    __asm__ volatile("cli" ::: "memory");
    while (ticks < PIT_HZ / 8) {                /* 125 ms: 12 ticks at HZ=100 */
        irr = pic_irr(0x20);
        polls++;
        if (irr & 0x01) { seen = 1; break; }
        now = pit_latch();
        ticks += (last - now) & 0xFFFF;
        last = now;
    }
    *isr_out = pic_isr(0x20);
    __asm__ volatile("sti" ::: "memory");
    *polls_out = polls; *irr_out = irr;
    return seen;
}

static int chk_pic_irr(void)
{
    u32 polls;
    u8 irr, isr, st;
    int seen;
    pit_begin();
    seen = irr_try(&polls, &irr, &isr);
    if (!seen) {
        int k;
        outb(0x43, 0xC2);
        st = inb(0x40);
        nb_s("IRQ0 never in the master IRR with interrupts off for 125 ms: "); nb_u(polls, 0); nb_s(" polls, last IRR "); nb_u(irr, 1);
        nb_s(", ISR "); nb_u(isr, 1); nb_s(", IMR "); nb_u(inb(0x21), 1); nb_s(", ch0 status "); nb_u(st, 1);
        nb_flush();
        for (k = 0; k < 3; k++) {               /* diagnostics only; the verdict stays the first attempt's */
            int r = irr_try(&polls, &irr, &isr);
            nb_s("  retry "); nb_u((u32) k, 0); nb_s(r ? " saw IRQ0 after " : " saw nothing in "); nb_u(polls, 0); nb_s(" polls"); nb_flush();
            sleep_ms(30);
        }
    }
    pit_end();
    return seen;
}

static int chk_pic_elcr(void)
{
    u8 e0 = inb(0x4D0), e1 = inb(0x4D1);
    if (e0 == 0xFF && e1 == 0xFF) { note_s("no ELCR (0x4D0/0x4D1 float)"); return -1; }
    if ((e0 & 0x07) || (e1 & 0x21)) { nb_s("ELCR "); nb_u(e0, 1); nb_s("/"); nb_u(e1, 1); nb_s(": IRQ 0,1,2,8,13 must be edge triggered"); nb_flush(); return 0; }
    return 1;
}

/* =============================================================================== DMA ======= */
struct dch { const char *name; u16 addr, cnt, page, clrff, mask; u8 cbit; int word; };
static const struct dch dchs[] = {
    { "0", 0x00, 0x01, 0x87, 0x0C, 0x0A, 0, 0 },
    { "2", 0x04, 0x05, 0x81, 0x0C, 0x0A, 2, 0 },
    { "3", 0x06, 0x07, 0x82, 0x0C, 0x0A, 3, 0 },
    { "5", 0xC4, 0xC6, 0x8B, 0xD8, 0xD4, 1, 1 },
    { "6", 0xC8, 0xCA, 0x89, 0xD8, 0xD4, 2, 1 },
    { "7", 0xCC, 0xCE, 0x8A, 0xD8, 0xD4, 3, 1 },
};
#define NDCH 6

static int chk_dma_regs(void)
{
    static const u16 pat[] = { 0x1234, 0xA55A, 0x0001, 0xFFFE };
    int i, k, bad = 0;
    for (i = 0; i < NDCH; i++) {
        const struct dch *d = &dchs[i];
        outb(d->mask, (u8) (0x04 | d->cbit));   /* masked while we poke it */
        for (k = 0; k < 4; k++) {
            u16 a, c;
            outb(d->clrff, 0);
            outb(d->addr, (u8) pat[k]); outb(d->addr, (u8) (pat[k] >> 8));
            outb(d->cnt, (u8) ~pat[k]); outb(d->cnt, (u8) (~pat[k] >> 8));
            outb(d->clrff, 0);
            a = inb(d->addr); a |= (u16) (inb(d->addr) << 8);
            c = inb(d->cnt); c |= (u16) (inb(d->cnt) << 8);
            if (a != pat[k] || c != (u16) ~pat[k]) {
                if (bad++ < 3) { nb_s("ch"); nb_s(d->name); nb_s(" addr/count wrote "); nb_u(pat[k], 1); nb_s("/"); nb_u((u16) ~pat[k], 1);
                                 nb_s(" read "); nb_u(a, 1); nb_s("/"); nb_u(c, 1); nb_flush(); }
            }
        }
        outb(d->clrff, 0);
        outb(d->addr, 0); outb(d->addr, 0);
        outb(d->cnt, 0); outb(d->cnt, 0);
    }
    return !bad;
}

static int chk_dma_page(void)
{
    int i, k, bad = 0;
    for (i = 0; i < NDCH; i++) {
        const struct dch *d = &dchs[i];
        u8 saved = inb(d->page);
        for (k = 0; k < 2; k++) {
            u8 v = k ? 0xAA : 0x55, g;
            outb(d->page, v);
            g = inb(d->page);
            if (g != v && bad++ < 3) { nb_s("page reg "); nb_u(d->page, 1); nb_s(" wrote "); nb_u(v, 1); nb_s(" read "); nb_u(g, 1); nb_flush(); }
        }
        outb(d->page, saved);
    }
    return !bad;
}

/* a software-requested block-mode verify transfer of 4 units: terminal count in the status register, count wraps */
static int chk_dma_verify(void)
{
    int w, bad = 0;
    for (w = 0; w < 2; w++) {
        u16 base = w ? 0xC0 : 0x00, stat = w ? 0xD0 : 0x08, req = w ? 0xD2 : 0x09, mode = w ? 0xD6 : 0x0B;
        u16 mask = w ? 0xD4 : 0x0A, clrff = w ? 0xD8 : 0x0C, a_, c_;
        u8 ch = 2;                              /* DMA1 ch2 / DMA2 ch6 (index 2 of the controller) */
        u32 t0;
        int tc = 0;
        u8 st;
        a_ = (u16) (base + ch * (w ? 4 : 2));
        c_ = (u16) (a_ + (w ? 2 : 1));
        outb(mask, (u8) (0x04 | ch));
        outb(clrff, 0);
        outb(a_, 0x00); outb(a_, 0x10);         /* address 0x1000 (units) */
        outb(c_, 3); outb(c_, 0);               /* 4 transfers */
        outb(mode, (u8) (0x80 | 0x00 | ch));    /* block mode, verify (00), address increment, no auto-init */
        (void) inb(stat);                       /* clear stale TC bits */
        outb(mask, ch);                         /* unmask */
        outb(req, (u8) (0x04 | ch));            /* software request */
        t0 = now_ms();
        while (now_ms() - t0 < 50) {
            st = inb(stat);
            if (st & (1 << ch)) { tc = 1; break; }
        }
        outb(mask, (u8) (0x04 | ch));
        if (!tc) { nb_s(w ? "DMA2 ch6" : "DMA1 ch2"); nb_s(" verify: no terminal count in 50 ms"); nb_flush(); bad++; continue; }
        {
            u16 cnt, adr;
            outb(clrff, 0);
            adr = inb(a_); adr |= (u16) (inb(a_) << 8);
            cnt = inb(c_); cnt |= (u16) (inb(c_) << 8);
            if (cnt != 0xFFFF) { nb_s(w ? "DMA2 ch6" : "DMA1 ch2"); nb_s(" verify: count after TC "); nb_u(cnt, 1); nb_s(", want ffff"); nb_flush(); bad++; }
            if (adr != 0x1004) { nb_s(w ? "DMA2 ch6" : "DMA1 ch2"); nb_s(" verify: address after 4 units "); nb_u(adr, 1); nb_s(", want 1004"); nb_flush(); bad++; }
        }
        outb(clrff, 0);
        outb(a_, 0); outb(a_, 0); outb(c_, 0); outb(c_, 0);
    }
    return !bad;
}

/* ============================================================================== main ======= */
static int want(const char *only, const char *grp) { return !only || seq(only, grp); }

int main(int argc, char **argv, char **envp)
{
    const char *only = 0;
    int i;
    (void) envp;
    for (i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (seq(a, "run")) continue;
        if (a[0]=='o' && a[1]=='n' && a[2]=='l' && a[3]=='y' && a[4]=='=') only = a + 5;
        else { pr("PLAT FAIL arg %s\n", a); return 2; }
    }
    if (sys_iopl(3) < 0) { pr("PLAT FAIL iopl\n"); return 1; }
    pr("PLAT HW start\n");
    if (want(only, "pit")) {
        pit_begin();
        result("PIT_RW", chk_pit_rw());
        result("PIT_COUNT", chk_pit_count());
        result("PIT_MODE0", chk_pit_mode0());
        result("PIT_MODE3", chk_pit_mode3());
        result("PIT_BCD", chk_pit_bcd());
        result("PIT_READBACK", chk_pit_rb());
        pit_end();
    }
    if (want(only, "rtc")) {
        result("RTC_FORMAT", chk_rtc_fmt());
        result("RTC_TICK", chk_rtc_tick());
        pit_begin();
        result("RTC_VS_PIT", chk_rtc_pit());
        result("RTC_PERIODIC", chk_rtc_periodic());
        pit_end();
        result("RTC_ALARM", chk_rtc_alarm());
        result("CMOS_RAM", chk_cmos_ram());
    }
    if (want(only, "pic")) {
        result("PIC_STATE", chk_pic_state());
        result("PIC_IRR", chk_pic_irr());
        result("PIC_ELCR", chk_pic_elcr());
    }
    if (want(only, "dma")) {
        result("DMA_REGS", chk_dma_regs());
        result("DMA_PAGE", chk_dma_page());
        result("DMA_VERIFY", chk_dma_verify());
    }
    for (i = 0; i < nnotes; i++) pr("PLAT NOTE %s\n", notes[i]);
    if (fails) pr("PLAT DONE RESULT=FAIL %d\n", fails);
    else pr("PLAT DONE RESULT=PASS\n");
    pp_flush();
    return fails ? 1 : 0;
}
