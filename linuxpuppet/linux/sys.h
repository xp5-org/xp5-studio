/* Freestanding i386 Linux syscall layer for the r128 guest tests: no libc, no libgcc (gcc -m32 -nostdlib). */
#ifndef R128_SYS_H
#define R128_SYS_H

typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef unsigned long long u64;
typedef int i32;

#define SYS_exit 1
#define SYS_read 3
#define SYS_write 4
#define SYS_open 5
#define SYS_close 6
#define SYS_stat 106
#define SYS_mmap2 192
#define SYS_munmap 91
#define SYS_mount 21
#define SYS_mkdir 39
#define SYS_ioctl 54
#define SYS_syslog 103
#define SYS_getdents 141
#define SYS_reboot 88
#define SYS_sync 36
#define SYS_nanosleep 162

#define O_RDONLY 0
#define O_RDWR 2
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

static inline int sys_open(const char *p, int fl) { return S3(SYS_open, p, fl, 0); }
static inline int sys_close(int fd) { return S3(SYS_close, fd, 0, 0); }
static inline int sys_read(int fd, void *b, int n) { return S3(SYS_read, fd, b, n); }
static inline int sys_write(int fd, const void *b, int n) { return S3(SYS_write, fd, b, n); }
static inline int sys_ioctl(int fd, u32 req, void *arg) { return S3(SYS_ioctl, fd, req, arg); }
static inline int sys_mkdir(const char *p) { return S3(SYS_mkdir, p, 0755, 0); }
static inline int sys_mount(const char *s, const char *t, const char *fs)
{
    return sc(SYS_mount, (int)s, (int)t, (int)fs, 0, 0, 0);
}
static inline int sys_klog_read_all(char *b, int n) { return S3(SYS_syslog, 3, b, n); }
static inline void *sys_mmap(u32 len, int prot, int fd, u32 off_bytes)
{
    int r = sc(SYS_mmap2, 0, len, prot, MAP_SHARED, fd, off_bytes >> 12);
    return (r < 0 && r > -4096) ? (void *) 0 : (void *) r;
}
static inline void sys_poweroff(void)
{
    sc(SYS_sync, 0, 0, 0, 0, 0, 0);
    sc(SYS_reboot, (int) 0xfee1dead, 672274793, 0x4321fedc, 0, 0, 0);   /* LINUX_REBOOT_CMD_POWER_OFF */
}
static inline void sys_sleep(int sec)
{
    int ts[2]; ts[0] = sec; ts[1] = 0;
    S3(SYS_nanosleep, ts, 0, 0);
}

static int slen(const char *s) { int n = 0; while (s[n]) n++; return n; }
static void *mcopy(void *d, const void *s, u32 n) { u8 *a = d; const u8 *b = s; while (n--) *a++ = *b++; return d; }

/* ---- output: lines go to the VGA console (OCR gate) and into a memory log that is sent to the host as XMODEM/CRC ----
 * (plain console text through 86Box's pipe loses bytes, so the log goes out with ACKs, as in the DOS stages). */
static int fd_tty = -1, fd_com = -1;
static char logbuf[131072];
static u32 loglen;
static void out_open(void)
{
    fd_tty = sys_open("/dev/tty0", O_RDWR);
    fd_com = sys_open("/dev/ttyS0", O_RDWR);
}
static void log_add(const char *s, int n)
{
    while (n-- > 0 && loglen < sizeof logbuf) logbuf[loglen++] = *s++;
}
static void out_s(const char *s)
{
    int n = slen(s);
    if (fd_tty >= 0) sys_write(fd_tty, s, n);
    log_add(s, n);
}
static void out_log_only(const char *s, int n) { log_add(s, n); }
static void out_x(u32 v, int digits)
{
    char b[9]; int i;
    for (i = 0; i < digits; i++) b[i] = "0123456789ABCDEF"[(v >> (4 * (digits - 1 - i))) & 15];
    b[digits] = 0; out_s(b);
}
static void out_d(u32 v)
{
    char b[12]; int i = 11; b[i] = 0;
    do { b[--i] = '0' + (v % 10); v /= 10; } while (v);
    out_s(b + i);
}

/* ---- COM1 XMODEM/CRC sender (host: linux/r128_xmodem_rx.py sends 'C', ACKs each CRC-good block) ---- */
struct kterm { u32 iflag, oflag, cflag, lflag; u8 line; u8 cc[19]; };
static u16 crc16(const u8 *p, int n)
{
    u16 crc = 0; int i, k;
    for (i = 0; i < n; i++) {
        crc ^= (u16) p[i] << 8;
        for (k = 0; k < 8; k++) crc = (crc & 0x8000) ? (u16) ((crc << 1) ^ 0x1021) : (u16) (crc << 1);
    }
    return crc;
}
/* one byte from COM1, -1 after ~1 s (VMIN=0, VTIME=10) */
static int com_getc(void)
{
    u8 c;
    return sys_read(fd_com, &c, 1) == 1 ? c : -1;
}
static int got_c;
/* Put COM1 in raw mode and wait up to ~240 s for the host's 'C'. 0 on success, else a short reason.
 * A stage may call this early (to hold its boot banner until the host receiver is ready); xmodem_send then skips the wait. */
static const char *com_wait_c(void)
{
    struct kterm t; int c = -1, tries;
    if (got_c) return 0;
    if (fd_com < 0) return "no ttyS0";
    if (sys_ioctl(fd_com, 0x5401, &t) < 0) return "TCGETS failed";
    t.iflag &= ~(0x1u | 0x2u | 0x8u | 0x20u | 0x40u | 0x80u | 0x100u | 0x400u | 0x1000u);   /* raw */
    t.oflag &= ~0x1u;
    t.lflag &= ~(0x8u | 0x40u | 0x2u | 0x1u | 0x8000u);   /* ECHO ECHONL ICANON ISIG IEXTEN */
    t.cflag = (t.cflag & ~0x30u) | 0x30u;               /* CS8 */
    t.cc[6] = 0; t.cc[5] = 10;                          /* VMIN 0, VTIME 1 s */
    if (sys_ioctl(fd_com, 0x5402, &t) < 0) return "TCSETS failed";
    for (tries = 0; tries < 240; tries++) { c = com_getc(); if (c == 'C') break; }
    if (c != 'C') return "host never sent C";
    got_c = 1;
    return 0;
}
/* Send logbuf as XMODEM/CRC. 0 on success, else a short reason. */
static const char *xmodem_send(void)
{
    u8 blk[133]; u32 nblk = (loglen + 127) / 128, i, left; int c = 0, tries, k;
    const char *e = com_wait_c();
    if (e) return e;
    sys_ioctl(fd_com, 0x540B, (void *) 0);      /* TCFLSH TCIFLUSH: drop the 'C's the receiver sent while the tests ran */
    for (i = 0; i < nblk; i++) {
        left = loglen - i * 128;
        for (k = 0; k < 128; k++) blk[3 + k] = 0x1A;
        mcopy(blk + 3, logbuf + i * 128, left > 128 ? 128 : left);
        blk[0] = 1; blk[1] = (u8) (i + 1); blk[2] = (u8) ~(i + 1);
        { u16 crc = crc16(blk + 3, 128); blk[131] = crc >> 8; blk[132] = crc & 255; }
        for (tries = 0; tries < 10; tries++) {
            int w = 0, r;
            while (w < 133) { r = sys_write(fd_com, blk + w, 133 - w); if (r <= 0) return "write failed"; w += r; }
            for (k = 0; k < 15; k++) { c = com_getc(); if (c == 6 || c == 0x15 || c == 0x18) break; }
            if (c == 6) break;
            if (c == 0x18) return "host cancelled";
        }
        if (c != 6) return "block never ACKed";
    }
    blk[0] = 4;
    for (tries = 0; tries < 10; tries++) {
        sys_write(fd_com, blk, 1);
        for (k = 0; k < 15; k++) { c = com_getc(); if (c == 6) return 0; if (c >= 0) break; }
    }
    return "EOT never ACKed";
}
#endif
