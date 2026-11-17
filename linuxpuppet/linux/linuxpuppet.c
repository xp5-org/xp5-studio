/* linuxpuppet: guest /init for the Linux puppet (puppet_plan.md). Freestanding (no libc), i486.
 * It maps the mailbox anchor at PUP_ANCHOR_PHYS through /dev/mem, publishes its identity, sets state READY
 * and then runs host commands forever. The host talks to it only through RMM mem.phys.read/write; the
 * console lines are for a person watching, nothing depends on them. */
#include "pci.h"   /* slurp, hexval (+ sys.h: the freestanding syscall layer) */
#include "puppet_abi.h"

#ifndef PUPPET_BUILD_ID
#define PUPPET_BUILD_ID "linuxpuppet-dev"
#endif

#define SYS_newuname 122

static volatile u8 *mb;
static char buf[16384];
static int fd_mem = -1;

#define R32(off)    (*(volatile u32 *) (mb + (off)))
#define W32(off, v) (*(volatile u32 *) (mb + (off)) = (u32) (v))

static u32 crctab[256];
static void crc_init(void)
{
    u32 i, c; int k;
    for (i = 0; i < 256; i++) {
        for (c = i, k = 0; k < 8; k++) c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1u)));
        crctab[i] = c;
    }
}
/* running form: crc_up(~0, ...) ... then ~ at the end */
static u32 crc_up(u32 c, const volatile u8 *p, u32 n)
{
    while (n--) c = crctab[(c ^ *p++) & 255] ^ (c >> 8);
    return c;
}
static u32 crc32(const volatile u8 *p, u32 n) { return ~crc_up(0xFFFFFFFFu, p, n); }

static void put_str(u32 off, const char *s, int max)
{
    int i = 0;
    for (; i < max - 1 && s[i] && s[i] != '\n'; i++) mb[off + i] = (u8) s[i];
    for (; i < max; i++) mb[off + i] = 0;
}

/* value after "key" up to the end of the line, from a /proc text file already in buf; 0 if absent */
static const char *field(const char *text, const char *key)
{
    int n = slen(key);
    const char *p = text;
    while (*p) {
        int i = 0;
        while (i < n && p[i] == key[i]) i++;
        if (i == n) {
            p += n;
            while (*p == ' ' || *p == '\t' || *p == ':') p++;
            return p;
        }
        while (*p && *p != '\n') p++;
        if (*p) p++;
    }
    return 0;
}

static int pfx(const char *s, const char *p) { while (*p) if (*s++ != *p++) return 0; return 1; }

static u32 decval(const char *s)
{
    u32 v = 0;
    while (*s >= '0' && *s <= '9') v = v * 10 + (u32) (*s++ - '0');
    return v;
}

static void identity(void)
{
    static char uts[6 * 65];
    const char *v;
    u32 n = 0;

    put_str(PUP_ID_BUILD, PUPPET_BUILD_ID, 32);
    if (sc(SYS_newuname, (int) uts, 0, 0, 0, 0, 0) == 0) put_str(PUP_ID_KERNEL, uts + 2 * 65, 32);
    if (slurp("/proc/cpuinfo", buf, sizeof buf) > 0) {
        v = field(buf, "model name");
        if (!v) v = field(buf, "cpu family");
        put_str(PUP_ID_CPU, v ? v : "?", PUP_ID_CPU_LEN);
    }
    if (slurp("/proc/meminfo", buf, sizeof buf) > 0 && (v = field(buf, "MemTotal"))) W32(PUP_ID_MEM_KB, decval(v));
    /* /proc/bus/pci/devices: "BBDD\tVVVVDDDD\t..." per line; absent on a machine without PCI */
    if (slurp("/proc/bus/pci/devices", buf, sizeof buf) > 0) {
        const char *p = buf;
        while (*p && n < PUP_ID_PCI_MAX) {
            u32 bdf = hexval(p);
            while (*p && *p != '\t') p++;
            if (*p) p++;
            W32(PUP_ID_PCI + n * 8, bdf);
            W32(PUP_ID_PCI + n * 8 + 4, hexval(p));
            n++;
            while (*p && *p != '\n') p++;
            if (*p) p++;
        }
    }
    W32(PUP_ID_PCI_N, n);
    W32(PUP_ID_GEN, 0);
    if (slurp("/proc/cmdline", buf, sizeof buf) > 0) {
        const char *g = buf;
        for (; *g; g++)
            if ((g == buf || g[-1] == ' ') && pfx(g, "puppet.gen=")) { W32(PUP_ID_GEN, decval(g + 11)); break; }
    }
}

#include "puppet_loader.h"

static void run(u32 op)
{
    u32 status = PUP_E_OK, len = 0, i;
    u32 vals[8] = { 0 };

    switch (op) {
    case PUP_OP_PING:
        vals[0] = R32(PUP_HEARTBEAT);
        vals[1] = R32(PUP_CMD_ARGS);
        break;
    case PUP_OP_INFO:
        identity();
        vals[0] = R32(PUP_ID_PCI_N);
        vals[1] = R32(PUP_ID_MEM_KB);
        break;
    case PUP_OP_ECHO:
        len = R32(PUP_CMD_LEN);
        if (len > PUP_PAYLOAD_MAX) { status = PUP_E_INVAL; len = 0; break; }
        vals[0] = crc32(mb + PUP_PAYLOAD, len);   /* the payload stays where it is: echo = same bytes back */
        break;
    case PUP_OP_PUT:   status = op_put(vals); break;
    case PUP_OP_GET:   status = op_get(vals, &len); break;
    case PUP_OP_EXEC:  status = op_exec(vals); break;
    case PUP_OP_WAIT:  status = op_wait(vals, &len); break;
    case PUP_OP_KILL:  status = op_kill(); break;
    case PUP_OP_RM:    status = op_rm(); break;
    case PUP_OP_KEXEC: status = op_kexec(vals); break;
    default:
        status = PUP_E_NOSYS;
    }
    for (i = 0; i < 8; i++) W32(PUP_RSP_VALS + i * 4, vals[i]);
    W32(PUP_RSP_STATUS, status);
    W32(PUP_RSP_LEN, len);
    W32(PUP_RSP_CRC, crc32(mb + PUP_PAYLOAD, len));
}

void c_main(void)
{
    int fd, i;
    u32 last;

    sys_mkdir("/proc"); sys_mkdir("/sys"); sys_mkdir("/dev");
    sys_mount("proc", "/proc", "proc");
    sys_mount("sysfs", "/sys", "sysfs");
    sys_mount("devtmpfs", "/dev", "devtmpfs");
    sys_mkdir("/pp"); sys_mkdir("/pp/run");
    crc_init();
    out_open();
    out_s("PUPPET boot " PUPPET_BUILD_ID "\n");

    fd = fd_mem = sys_open("/dev/mem", O_RDWR | O_SYNC);
    mb = fd >= 0 ? (volatile u8 *) sys_mmap(PUP_ANCHOR_SIZE, PROT_READ | PROT_WRITE, fd, PUP_ANCHOR_PHYS) : 0;
    if (!mb) {
        out_s(fd < 0 ? "PUPPET FAIL open /dev/mem\n" : "PUPPET FAIL mmap anchor\n");
        for (;;) sys_sleep(1);
    }

    /* clear, then magic last among the header fields so a half-written anchor never looks valid */
    W32(PUP_STATE, PUP_ST_BOOTING);
    for (i = 4; i < 0x400; i++) mb[i] = 0;
    W32(PUP_VERSION, PUP_ABI_VERSION);
    W32(PUP_BOOT_STAGE, 1);
    identity();
    W32(PUP_BULK_PHYS, 0);      /* P0: no bulk window yet */
    W32(PUP_BULK_SIZE, 0);
    W32(PUP_MB_LINEAR, (u32) mb);
    mcopy((void *) mb, "86PUPPET", 8);
    last = R32(PUP_CMD_SEQ);    /* 0: the host's first command is seq 1 */
    W32(PUP_RSP_SEQ, last);
    W32(PUP_BOOT_STAGE, 2);
    W32(PUP_STATE, PUP_ST_READY);

    out_s("PUPPET READY kernel=");
    out_s((const char *) (mb + PUP_ID_KERNEL));
    out_s(" mem=");
    out_d(R32(PUP_ID_MEM_KB));
    out_s("kB pci=");
    out_d(R32(PUP_ID_PCI_N));
    out_s("\n");

    for (;;) {
        u32 seq = R32(PUP_CMD_SEQ);
        W32(PUP_HEARTBEAT, R32(PUP_HEARTBEAT) + 1);
        if (seq == last) {
            if (kids_running()) { reap(); nap_ms(2); }   /* leave the CPU to the test child */
            continue;
        }
        if (seq != last + 1) {          /* lost or repeated command: refuse it, resync on it */
            W32(PUP_RSP_STATUS, PUP_E_INVAL);
            W32(PUP_RSP_VALS, last);
            W32(PUP_RSP_LEN, 0);
            last = seq;
            W32(PUP_RSP_SEQ, seq);
            continue;
        }
        W32(PUP_STATE, PUP_ST_BUSY);
        run(R32(PUP_CMD_OP));
        last = seq;
        W32(PUP_STATE, PUP_ST_READY);
        W32(PUP_RSP_SEQ, seq);          /* doorbell: last write */
        if (kexec_armed) kexec_go();    /* replied first; the host sees the reply, then the new boot */
    }
}

__attribute__((naked, noreturn)) void _start(void)
{
    __asm__ volatile("andl $-16, %esp\n\tcall c_main\n");
}
