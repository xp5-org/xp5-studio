/* pp_rt.h: runtime for puppet personalities (puppet_plan.md 2.3). Freestanding i386 Linux, no libc, i486-safe.
 * A personality is a static program the host PUTs into the puppet's RAM root and runs with EXEC; stdout and
 * stderr go to /pp/run/<pid>.out, which the host reads back with WAIT/GET. Write `int main(int, char **, char **)`
 * and include this header once; build flags come from build_linux_guest.sh (same as the puppet's /init). */
#ifndef PP_RT_H
#define PP_RT_H

typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef unsigned long long u64;
typedef int i32;

#define SYS_exit_group 252
#define SYS_read 3
#define SYS_write 4
#define SYS_open 5
#define SYS_close 6
#define SYS_lseek 19
#define SYS_getpid 20
#define SYS_iopl 110
#define SYS_nanosleep 162
#define SYS_gettimeofday 78
#define SYS_mmap2 192
#define SYS_munmap 91
#define O_RDONLY 0
#define O_WRONLY 1
#define O_RDWR 2
#define O_CREAT 0100
#define O_TRUNC 01000
#define O_SYNC 0x101000
#define PROT_READ 1
#define PROT_WRITE 2
#define MAP_SHARED 1

static inline int sc(int n, int a, int b, int c, int d, int e, int f)
{
    int r;
    register int ebp __asm__("ebp") = f;
    __asm__ volatile("int $0x80" : "=a"(r) : "0"(n), "b"(a), "c"(b), "d"(c), "S"(d), "D"(e), "r"(ebp) : "memory");
    return r;
}
#define S3(n, a, b, c) sc(n, (int)(a), (int)(b), (int)(c), 0, 0, 0)

static inline int sys_open(const char *p, int fl, int mode) { return S3(SYS_open, p, fl, mode); }
static inline int sys_close(int fd) { return S3(SYS_close, fd, 0, 0); }
static inline int sys_read(int fd, void *b, int n) { return S3(SYS_read, fd, b, n); }
static inline int sys_write(int fd, const void *b, int n) { return S3(SYS_write, fd, b, n); }
static inline void sys_exit(int code) { for (;;) sc(SYS_exit_group, code, 0, 0, 0, 0, 0); }
static inline int sys_iopl(int lvl) { return S3(SYS_iopl, lvl, 0, 0); }
static inline void *sys_mmap_phys(int fd, u32 len, u32 phys)
{
    int r = sc(SYS_mmap2, 0, (int) len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, (int) (phys >> 12));
    return (r < 0 && r > -4096) ? (void *) 0 : (void *) r;
}
static inline void sleep_ms(int ms)
{
    int ts[2]; ts[0] = ms / 1000; ts[1] = (ms % 1000) * 1000000;
    S3(SYS_nanosleep, ts, 0, 0);
}
static inline u32 now_ms(void)
{
    int tv[2];
    S3(SYS_gettimeofday, tv, 0, 0);
    return (u32) tv[0] * 1000u + (u32) tv[1] / 1000u;
}

/* port I/O: call sys_iopl(3) first */
static inline u8 inb(u16 p) { u8 v; __asm__ volatile("inb %1, %0" : "=a"(v) : "Nd"(p)); return v; }
static inline void outb(u16 p, u8 v) { __asm__ volatile("outb %0, %1" : : "a"(v), "Nd"(p)); }
static inline u16 inw(u16 p) { u16 v; __asm__ volatile("inw %1, %0" : "=a"(v) : "Nd"(p)); return v; }
static inline void outw(u16 p, u16 v) { __asm__ volatile("outw %0, %1" : : "a"(v), "Nd"(p)); }

static __attribute__((unused)) int slen(const char *s) { int n = 0; while (s[n]) n++; return n; }
static __attribute__((unused)) int seq(const char *a, const char *b) { while (*a && *a == *b) a++, b++; return *a == *b; }
static __attribute__((unused)) u32 atou(const char *s)
{
    u32 v = 0, b = 10;
    if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) { b = 16; s += 2; }
    for (;; s++) {
        u32 d = (*s >= '0' && *s <= '9') ? (u32) (*s - '0') : (*s >= 'a' && *s <= 'f') ? (u32) (*s - 'a' + 10)
              : (*s >= 'A' && *s <= 'F') ? (u32) (*s - 'A' + 10) : 99;
        if (d >= b) return v;
        v = v * b + d;
    }
}

/* -- output: small buffered stdout, flushed at newline and at exit --------------------------- */
static char pp_ob[512];
static int pp_on;
static void pp_flush(void) { if (pp_on) sys_write(1, pp_ob, pp_on); pp_on = 0; }
static void putc_(char c) { pp_ob[pp_on++] = c; if (c == '\n' || pp_on == sizeof pp_ob) pp_flush(); }
static void puts_(const char *s) { while (*s) putc_(*s++); }
static void putu(u32 v, int base, int width)
{
    char b[12]; int i = 0;
    do { b[i++] = "0123456789abcdef"[v % (u32) base]; v /= (u32) base; } while (v);
    while (i < width) b[i++] = '0';
    while (i) putc_(b[--i]);
}
/* %s %d %u %x %08x %c %% */
static __attribute__((unused)) void pr(const char *f, ...)
{
    __builtin_va_list ap;
    __builtin_va_start(ap, f);
    for (; *f; f++) {
        int w = 0;
        if (*f != '%') { putc_(*f); continue; }
        f++;
        while (*f >= '0' && *f <= '9') w = w * 10 + (*f++ - '0');
        switch (*f) {
        case 's': puts_(__builtin_va_arg(ap, const char *)); break;
        case 'd': { int v = __builtin_va_arg(ap, int); if (v < 0) { putc_('-'); v = -v; } putu((u32) v, 10, w); break; }
        case 'u': putu(__builtin_va_arg(ap, u32), 10, w); break;
        case 'x': putu(__builtin_va_arg(ap, u32), 16, w); break;
        case 'c': putc_((char) __builtin_va_arg(ap, int)); break;
        default: putc_(*f);
        }
    }
    __builtin_va_end(ap);
}

static __attribute__((unused)) u32 crc32_up(u32 c, const u8 *p, u32 n)
{
    int k;
    while (n--) { c ^= *p++; for (k = 0; k < 8; k++) c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1u))); }
    return c;
}

int main(int argc, char **argv, char **envp);

__attribute__((used)) static void pp_start(u32 *sp)
{
    int argc = (int) sp[0];
    char **argv = (char **) (sp + 1);
    int r = main(argc, argv, argv + argc + 1);
    pp_flush();
    sys_exit(r);
}

/* entry: [esp] = argc, argv..., 0, envp..., 0. Keep the 16-byte call alignment gcc assumes. */
__attribute__((naked, noreturn)) void _start(void)
{
    __asm__ volatile("xorl %ebp, %ebp\n\tmovl %esp, %eax\n\tandl $-16, %esp\n\tsubl $12, %esp\n\t"
                     "pushl %eax\n\tcall pp_start\n\thlt\n");
}
#endif
