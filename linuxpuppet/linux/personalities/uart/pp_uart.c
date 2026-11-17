/* uart personality: SERTEST's UART checks on the puppet (puppet_plan.md "later: serial").
 *
 * The checks are NOT written here: they are sertest_core.c / sertest_core_p2.c / sertest_defs.h of the SERTEST.EXE
 * repo (GIT_SRC_DIR/SERTEST.EXE/src_dos), the same source the DOS SERTEST.EXE is built from, compiled against the
 * Linux section of sertest_hw.h. This file only supplies what a program around the core must: a few libc
 * functions (no libc here), the timer (PIT channel 2, so the kernel's channel 0 tick is left alone), the reporting
 * hooks (one line per check on stdout) and argument parsing.
 *
 *   detect                       UART type, CPU, poll speed ("UART HW ..."), nothing else
 *   run [base=0x3F8] [irq=4] [com=N] [b=115200] [int] [ext] [nobrk] [nori]
 *                                SERTEST page 1 on the chosen panels (default both); EXT needs a loopback plug
 *
 * Output: "UART HW k=v ...", "UART CHK <INT|EXT> <STAGE> <PASS|FAIL|SKIP|NC|NA>", "UART NOTE <text>", and last
 * "UART DONE RESULT=PASS" or "UART DONE RESULT=FAIL <n>" (the number of failed checks). */
#define SERTEST_FREESTANDING 1      /* no C library here: sertest_defs.h skips the hosted headers */
#include "pp_rt.h"

/* ---- the few libc functions the core uses ---------------------------------------------------------------- */
#include <stddef.h>
#include <stdarg.h>
void *memset(void *d, int c, size_t n) { unsigned char *p = d; while (n--) *p++ = (unsigned char) c; return d; }
void *memcpy(void *d, const void *s, size_t n) { unsigned char *p = d; const unsigned char *q = s; while (n--) *p++ = *q++; return d; }
int memcmp(const void *a, const void *b, size_t n)
{
    const unsigned char *p = a, *q = b;
    for (; n; n--, p++, q++) if (*p != *q) return *p - *q;
    return 0;
}
size_t strlen(const char *s) { size_t n = 0; while (s[n]) n++; return n; }
char *strcpy(char *d, const char *s) { char *r = d; while ((*d++ = *s++)) ; return r; }
char *strncpy(char *d, const char *s, size_t n)
{
    char *r = d;
    while (n && (*d = *s)) { d++; s++; n--; }
    while (n--) *d++ = 0;
    return r;
}
int strcmp(const char *a, const char *b) { while (*a && *a == *b) a++, b++; return (unsigned char) *a - (unsigned char) *b; }

/* vsprintf for what the checks print: %s %c %d %u %x %X %%, optional 0 flag, width, l */
int vsprintf(char *out, const char *f, va_list ap)
{
    char *o = out;
    for (; *f; f++) {
        int w = 0, zero = 0, lng = 0;
        char tmp[16];
        int n = 0, base = 10, up = 0, neg = 0;
        unsigned long v;
        if (*f != '%') { *o++ = *f; continue; }
        f++;
        if (*f == '0') { zero = 1; f++; }
        while (*f >= '0' && *f <= '9') w = w * 10 + (*f++ - '0');
        if (*f == 'l') { lng = 1; f++; }
        switch (*f) {
        case 's': { const char *s = va_arg(ap, const char *); while (*s) *o++ = *s++; continue; }
        case 'c': *o++ = (char) va_arg(ap, int); continue;
        case '%': *o++ = '%'; continue;
        case 'd': { long d = lng ? va_arg(ap, long) : va_arg(ap, int); if (d < 0) { neg = 1; v = (unsigned long) -d; } else v = (unsigned long) d; break; }
        case 'u': v = lng ? va_arg(ap, unsigned long) : va_arg(ap, unsigned int); break;
        case 'X': up = 1; /* fall through */
        case 'x': base = 16; v = lng ? va_arg(ap, unsigned long) : va_arg(ap, unsigned int); break;
        default: *o++ = *f; continue;
        }
        do { int dg = (int) (v % (unsigned long) base); tmp[n++] = (char) (dg < 10 ? '0' + dg : (up ? 'A' : 'a') + dg - 10); v /= (unsigned long) base; } while (v);
        if (neg) tmp[n++] = '-';
        while (n < w) tmp[n++] = zero ? '0' : ' ';
        while (n) *o++ = tmp[--n];
    }
    *o = 0;
    return (int) (o - out);
}
int sprintf(char *out, const char *f, ...)
{
    va_list ap; int r;
    va_start(ap, f); r = vsprintf(out, f, ap); va_end(ap);
    return r;
}

#include "sertest_defs.h"

/* ---- machine layer: BDA, PIT channel 2 as the free-running counter -------------------------------------- */
static unsigned char bda_zero[0x100];
unsigned char *hw_bda_base = bda_zero;
static u8 port61_saved;

void hw_timer_begin(void)
{
    port61_saved = inb(0x61);
    outb(0x61, (u8) ((port61_saved & ~0x02) | 0x01));   /* gate on, speaker off */
    outb(0x43, 0xB4);                                   /* ch2, lo/hi, mode 2, binary */
    outb(0x42, 0);
    outb(0x42, 0);                                      /* reload 0 == 65536 */
}
void hw_timer_end(void) { outb(0x61, port61_saved); }
unsigned int hw_timer_read(void)
{
    u8 lo, hi;
    outb(0x43, 0x80);                                   /* latch ch2 */
    lo = inb(0x42);
    hi = inb(0x42);
    return ((unsigned int) hi << 8) | lo;
}

int hw_iir_pending = -1;
unsigned char hw_iir_take(void) { unsigned char v = (unsigned char) hw_iir_pending; hw_iir_pending = -1; return v; }

/* ---- hooks the core calls ---------------------------------------------------------------------------------- */
static int stop_req;
static void key_poll(void) { }
static void progress(int done, int total) { (void) done; (void) total; }
static void set_chk(int p, int i, int st)
{
    static const char *const nm[] = { "PEND", "RUN", "PASS", "FAIL", "SKIP", "NC", "NA" };
    chk_st[p][i] = st;
    if (st >= ST_PASS)
        pr("UART CHK %s %s %s\n", p ? "EXT" : "INT", chk[i].stage, nm[st]);
}

#include "sertest_core.c"
#include "sertest_core_p2.c"

static int num(const char *s, const char *key, u32 *v)
{
    int n = slen(key);
    int i;
    for (i = 0; i < n; i++) if (s[i] != key[i]) return 0;
    *v = atou(s + n);
    return 1;
}

int main(int argc, char **argv, char **envp)
{
    static const unsigned int defbase[4] = { 0x3F8, 0x2F8, 0x3E8, 0x2E8 };
    int detect_only = 0, timer_only = 0, i, j, fails = 0, fd;
    u32 v;
    void *m;
    (void) envp;

    for (i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (seq(a, "timerraw")) timer_only = 2;
        else if (seq(a, "timer")) timer_only = 1;
        else if (seq(a, "detect")) detect_only = 1;
        else if (seq(a, "run")) detect_only = 0;
        else if (seq(a, "int")) do_ext = 0;
        else if (seq(a, "ext")) do_int = 0;
        else if (seq(a, "nori")) no_ri = 1;
        else if (seq(a, "nobrk")) no_brk = 1;
        else if (num(a, "com=", &v)) { com_n = (int) v; if (com_n < 1 || com_n > 4) return 2; }
        else if (num(a, "base=", &v)) port = (unsigned int) v;
        else if (num(a, "irq=", &v)) irq = (int) v;
        else if (num(a, "b=", &v)) {
            for (j = 0; j < NBAUD && baud_tab[j] != v; j++) ;
            if (j == NBAUD) { pr("UART FAIL baud %u\n", v); return 2; }
            baud0 = j; nbaud = 1;
        } else { pr("UART FAIL arg %s\n", a); return 2; }
    }
    if (!do_int && !do_ext) return 2;
    if (sys_iopl(3) < 0) { pr("UART FAIL iopl\n"); return 1; }

    /* the BIOS data area (COM base addresses), when /dev/mem allows */
    fd = sys_open("/dev/mem", O_RDONLY, 0);
    if (fd >= 0) {
        m = sys_mmap_phys(fd, 0x1000, 0);
        if (m) hw_bda_base = (unsigned char *) m + 0x400;
    }
    if (!port) {
        hw_wptr bda = (hw_wptr) hw_bda(0x0000);
        port = bda[com_n - 1] ? bda[com_n - 1] : defbase[com_n - 1];
    }
    if (!irq) irq = (com_n & 1) ? 4 : 3;

    hw_timer_begin();
    clk_init();
    if (timer_only == 2) {                  /* raw counter reads of channel 2 and channel 0, 3 ms apart */
        int k;
        for (k = 0; k < 14; k++) {
            u32 c2, c0;
            outb(0x43, 0x80); c2 = inb(0x42); c2 |= (u32) inb(0x42) << 8;
            outb(0x43, 0x00); c0 = inb(0x40); c0 |= (u32) inb(0x40) << 8;
            pr("UART RAW k=%d ch2=%u ch0=%u p61=%x\n", k, c2, c0, inb(0x61));
            sleep_ms(3);
        }
        hw_timer_end();
        return 0;
    }
    if (timer_only) {                       /* is the PIT channel 2 counter really 1193182 Hz? */
        u32 t0, ms, big = 0, n = 0, prev = 0, d;
        unsigned long tk;
        t_start();
        t0 = now_ms();
        do {
            tk = t_now();
            d = (u32) (tk - prev);
            prev = (u32) tk;
            if (d > 3000) big++;
            n++;
            ms = now_ms() - t0;
        } while (ms < 1000);
        pr("UART TIMER ticks=%u ms=%u reads=%u big_steps=%u\n", (u32) t_now(), ms, n, big);
        hw_timer_end();
        return 0;
    }
    detect_cpu();
    cur_run_pg = 1;
    nnotes = notes_dropped = 0;
    detect_uart();
    cur_bi = top_bi();
    cur_lcr = 0x03;
    uart_init(MCR_LOOP);
    detect_typesw();
    drain_rx();
    cal_poll();
    pr("UART HW port=%x irq=%d uart=%s fifo=%d scr=%d cpu=%s poll16=%u cpu_top=%d\n", port, irq, uart_name,
       has_fifo, has_scr, cpu_name, (u32) poll16, cpu_top);

    if (!detect_only) {
        if (do_int) run_panel(0);
        if (do_ext) run_panel(1);
        for (i = 0; i < nnotes; i++) pr("UART NOTE %s\n", notes[i]);
        for (j = 0; j < 2; j++)
            for (i = 0; i < NCHK; i++)
                if (chk_st[j][i] == ST_FAIL) fails++;
        if (fails) pr("UART DONE RESULT=FAIL %d\n", fails);
        else pr("UART DONE RESULT=PASS\n");
    }
    hw_timer_end();
    cur_bi = top_bi();
    cur_lcr = 0x03;
    uart_init(MCR_DTR | MCR_RTS);
    return fails ? 1 : 0;
}
