/* selftest personality: proves the loader (puppet_plan.md 2.3, phase P1b). One binary, one mode per argv[1]:
 *   echo ARGS..   prints its argv and envp, exit 0
 *   exit N        exits with N
 *   spin          prints a tick a second until killed (KILL test)
 *   crc PATH      prints the CRC32 and size of a file (cross-check with the host's PUT)
 *   cmos          reads the RTC through ports 0x70/0x71 (iopl): a test program sees the hardware directly
 *   work R SEED MS  deterministic CPU + RAM load for snapshot checks (plan 2.4): R rounds of xorshift32 over a
 *                 64 KB buffer, a CRC line per round, MS ms sleep between rounds (timers in play). The host
 *                 computes the same CRCs (linuxpuppet.work_crcs), so a save/restore mid-run must not change them */
#include "pp_rt.h"

static int crc_file(const char *p)
{
    static u8 b[8192];
    u32 c = 0xFFFFFFFFu, n = 0; int fd = sys_open(p, O_RDONLY, 0), r;
    if (fd < 0) { pr("SELFTEST crc open %s failed %d\n", p, fd); return 2; }
    while ((r = sys_read(fd, b, sizeof b)) > 0) { c = crc32_up(c, b, (u32) r); n += (u32) r; }
    sys_close(fd);
    pr("SELFTEST crc %s size=%u crc=%08x\n", p, n, ~c);
    return 0;
}

static u32 crc_tab[256];
static u32 crc_fast(const u8 *p, u32 n)
{
    u32 c = 0xFFFFFFFFu, k, j;
    if (!crc_tab[1])
        for (k = 0; k < 256; k++) { u32 v = k; for (j = 0; j < 8; j++) v = (v >> 1) ^ (0xEDB88320u & (0u - (v & 1u))); crc_tab[k] = v; }
    while (n--) c = crc_tab[(c ^ *p++) & 0xFF] ^ (c >> 8);
    return ~c;
}

static int work(u32 rounds, u32 x, u32 ms)
{
    static u32 wb[16384];
    u32 r, i, c = 0, t0 = now_ms();
    if (!x) x = 1;
    pr("SELFTEST work start rounds=%u pid=%d\n", rounds, S3(SYS_getpid, 0, 0, 0));
    for (r = 0; r < rounds; r++) {
        for (i = 0; i < 16384; i++) { x ^= x << 13; x ^= x >> 17; x ^= x << 5; wb[i] ^= x; }
        c = crc_fast((const u8 *) wb, sizeof wb);
        pr("SELFTEST work r=%u crc=%08x\n", r, c);
        if (ms) sleep_ms((int) ms);
    }
    pr("SELFTEST work done rounds=%u crc=%08x t=%ums\n", rounds, c, now_ms() - t0);
    return 0;
}

static u8 cmos(u8 reg) { outb(0x70, reg); return inb(0x71); }

int main(int argc, char **argv, char **envp)
{
    int i;
    const char *m = argc > 1 ? argv[1] : "echo";
    if (seq(m, "echo")) {
        pr("SELFTEST echo argc=%d pid=%d\n", argc, S3(SYS_getpid, 0, 0, 0));
        for (i = 0; i < argc; i++) pr("SELFTEST argv[%d]=%s\n", i, argv[i]);
        for (i = 0; envp[i]; i++) pr("SELFTEST env %s\n", envp[i]);
        return 0;
    }
    if (seq(m, "exit")) return argc > 2 ? (int) atou(argv[2]) : 1;
    if (seq(m, "spin")) {
        u32 t0 = now_ms();
        for (i = 0;; i++) { pr("SELFTEST spin %d t=%ums\n", i, now_ms() - t0); sleep_ms(1000); }
    }
    if (seq(m, "crc")) return argc > 2 ? crc_file(argv[2]) : 2;
    if (seq(m, "cmos")) {
        int r = sys_iopl(3);
        u8 s0, s1;
        if (r) { pr("SELFTEST cmos iopl failed %d\n", r); return 3; }
        while (cmos(0x0a) & 0x80) ;      /* update in progress */
        s0 = cmos(0); sleep_ms(1100); s1 = cmos(0);
        pr("SELFTEST cmos sec=%x then %x status_b=%x\n", s0, s1, cmos(0x0b));
        return s0 == s1 ? 4 : 0;         /* the RTC must tick */
    }
    if (seq(m, "work"))
        return work(argc > 2 ? atou(argv[2]) : 100, argc > 3 ? atou(argv[3]) : 1, argc > 4 ? atou(argv[4]) : 50);
    pr("SELFTEST unknown mode %s\n", m);
    return 2;
}
