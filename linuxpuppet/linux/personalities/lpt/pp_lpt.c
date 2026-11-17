/* lpt personality: LPTTEST's parallel port checks on the puppet. The checks are lpttest_core.c of the SERTEST.EXE repo
 * (GIT_SRC_DIR/SERTEST.EXE/src_dos), the same source LPTTEST.EXE is built from; this file only supplies a few libc
 * functions, the PIT channel 2 timer, the reporting hook and argument parsing.
 *
 *   run [base=0x378] [lpt=N]    all checks; an on-line printer must sit behind the port (86Box lpt1_device=text_prt)
 *
 * Output: "LPT HW base=..", "LPT CHK <NAME> <PASS|FAIL>", "LPT NOTE <text>", last "LPT DONE RESULT=PASS|FAIL <n>". */
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

static unsigned char bda_zero[0x100];
unsigned char *hw_bda_base = bda_zero;
static u8 port61_saved;
void hw_timer_begin(void)
{
    port61_saved = inb(0x61);
    outb(0x61, (u8) ((port61_saved & ~0x02) | 0x01));
    outb(0x43, 0xB4);
    outb(0x42, 0);
    outb(0x42, 0);
}
void hw_timer_end(void) { outb(0x61, port61_saved); }
unsigned int hw_timer_read(void)
{
    u8 lo, hi;
    outb(0x43, 0x80);
    lo = inb(0x42);
    hi = inb(0x42);
    return ((unsigned int) hi << 8) | lo;
}
int hw_iir_pending = -1;
unsigned char hw_iir_take(void) { unsigned char v = (unsigned char) hw_iir_pending; hw_iir_pending = -1; return v; }



static int lpt_res[16];
#include "lpttest_core.c"
static void lpt_set(int check, int state)
{
    static const char *const nm[] = { "PEND", "RUN", "PASS", "FAIL", "SKIP", "NC", "NA" };
    lpt_res[check] = state;
    if (state >= ST_PASS)
        pr("LPT CHK %s %s\n", lpt_chk_name[check], nm[state]);
}

int main(int argc, char **argv, char **envp)
{
    int i, fd, fails = 0, n = 1;
    u32 v;
    void *m;
    (void) envp;
    for (i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (seq(a, "run")) continue;
        else if (a[0]=='b' && a[1]=='a' && a[2]=='s' && a[3]=='e' && a[4]=='=') lpt_base = (unsigned int) atou(a + 5);
        else if (a[0]=='i' && a[1]=='r' && a[2]=='q' && a[3]=='=') lpt_irq = (int) atou(a + 4);
        else if (a[0]=='l' && a[1]=='p' && a[2]=='t' && a[3]=='=') n = (int) atou(a + 4);
        else { pr("LPT FAIL arg %s\n", a); return 2; }
    }
    if (sys_iopl(3) < 0) { pr("LPT FAIL iopl\n"); return 1; }
    fd = sys_open("/dev/mem", O_RDONLY, 0);
    if (fd >= 0) {
        m = sys_mmap_phys(fd, 0x1000, 0);
        if (m) hw_bda_base = (unsigned char *) m + 0x400;
    }
    if (!lpt_base) {
        hw_wptr bda = (hw_wptr) hw_bda(0x0008);
        lpt_base = bda[n - 1] ? bda[n - 1] : 0x378;
    }
    (void) v;
    hw_timer_begin();
    clk_init();
    cur_run_pg = 1;
    pr("LPT HW base=%x\n", lpt_base);
    lpt_run();
    for (i = 0; i < nnotes; i++) pr("LPT NOTE %s\n", notes[i]);
    for (i = 0; i < NLCHK; i++)
        if (lpt_res[i] == ST_FAIL) fails++;
    if (fails) pr("LPT DONE RESULT=FAIL %d\n", fails);
    else pr("LPT DONE RESULT=PASS\n");
    hw_timer_end();
    return fails ? 1 : 0;
}
