/* linuxpuppet loader ops (puppet_plan.md 2.3): PUT/GET files in the RAM root, EXEC/WAIT/KILL test children,
 * RM, and KEXEC into another kernel. Included once by linuxpuppet.c after mb/buf/crc32/pfx/decval exist.
 * Every op returns a status (errno of the failing syscall, 0 = ok) and fills vals[] / the payload area. */

#define SYS_fork 2
#define SYS_unlink 10
#define SYS_execve 11
#define SYS_chdir 12
#define SYS_chmod 15
#define SYS_lseek 19
#define SYS_getpid 20
#define SYS_kill 37
#define SYS_rmdir 40
#define SYS_dup2 63
#define SYS_gettimeofday 78
#define SYS_wait4 114
#define SYS_exit_group 252
#define SYS_kexec_load 283
#define O_WRONLY 1
#define O_CREAT 0100
#define O_TRUNC 01000
#define MAP_PRIVATE 2
#define EISDIR 21
#define ENOTDIR 20

static int sys_err(int r) { return (r < 0 && r > -4096) ? -r : 0; }

static u32 now_ms(void)
{
    int tv[2];
    sc(SYS_gettimeofday, (int) tv, 0, 0, 0, 0, 0);
    return (u32) tv[0] * 1000u + (u32) tv[1] / 1000u;
}

static void nap_ms(int ms)
{
    int ts[2]; ts[0] = ms / 1000; ts[1] = (ms % 1000) * 1000000;
    S3(SYS_nanosleep, ts, 0, 0);
}

static int file_size(int fd)
{
    int n = S3(SYS_lseek, fd, 0, 2);   /* SEEK_END; /proc files say 0 */
    S3(SYS_lseek, fd, 0, 0);
    return n < 0 ? 0 : n;
}

/* copy a NUL-terminated string out of the payload (the payload is overwritten by replies); 0 if it does not fit */
static const volatile u8 *pl_str(const volatile u8 *p, const volatile u8 *end, char *dst, int max)
{
    int i = 0;
    while (p < end && *p) {
        if (i >= max - 1) return 0;
        dst[i++] = (char) *p++;
    }
    if (p >= end) return 0;
    dst[i] = 0;
    return p + 1;
}

static void mkparents(const char *path)
{
    char d[256]; int i;
    for (i = 0; path[i] && i < 255; i++) {
        if (i > 0 && path[i] == '/') { d[i] = 0; sys_mkdir(d); }
        d[i] = path[i];
    }
}

static void fmt_u(char *b, u32 v)
{
    char t[12]; int i = 0, j = 0;
    do { t[i++] = (char) ('0' + v % 10); v /= 10; } while (v);
    while (i) b[j++] = t[--i];
    b[j] = 0;
}

static void scat(char *d, const char *s) { while (*d) d++; while ((*d++ = *s++)) ; }

/* -- PUT / GET / RM ------------------------------------------------------------------------- */
static u32 op_put(u32 *vals)
{
    char path[256];
    u32 len = R32(PUP_CMD_LEN), off = R32(PUP_CMD_ARGS), fl = R32(PUP_CMD_ARGS + 4), mode = R32(PUP_CMD_ARGS + 8);
    const volatile u8 *end = mb + PUP_PAYLOAD + (len > PUP_PAYLOAD_MAX ? PUP_PAYLOAD_MAX : len);
    const volatile u8 *d = pl_str(mb + PUP_PAYLOAD, end, path, sizeof path);
    int fd, r, e;
    u32 n;

    if (!d || path[0] != '/') return PUP_E_INVAL;
    mkparents(path);
    fd = sys_open(path, O_WRONLY | O_CREAT | ((fl & PUP_PUT_CREATE) ? O_TRUNC : 0));
    if ((e = sys_err(fd))) return (u32) e;
    if (off && (e = sys_err(S3(SYS_lseek, fd, off, 0)))) { sys_close(fd); return (u32) e; }
    n = (u32) (end - d);
    while (n) {                          /* write() straight from the /dev/mem mapping */
        r = sys_write(fd, (const void *) d, (int) n);
        if (r <= 0) { sys_close(fd); return r < 0 ? (u32) -r : PUP_E_INVAL; }
        d += r; n -= (u32) r; vals[0] += (u32) r;
    }
    vals[1] = (u32) file_size(fd);
    sys_close(fd);
    if (fl & PUP_PUT_FINAL) {
        u32 c = 0xFFFFFFFFu;
        S3(SYS_chmod, path, mode ? mode : 0644, 0);
        fd = sys_open(path, O_RDONLY);
        if ((e = sys_err(fd))) return (u32) e;
        while ((r = sys_read(fd, buf, sizeof buf)) > 0) c = crc_up(c, (const u8 *) buf, (u32) r);
        sys_close(fd);
        vals[2] = ~c;
    }
    return 0;
}

static u32 op_get(u32 *vals, u32 *rlen)
{
    char path[256];
    u32 len = R32(PUP_CMD_LEN), off = R32(PUP_CMD_ARGS), max = R32(PUP_CMD_ARGS + 4);
    int fd, r, e;

    if (len > PUP_PAYLOAD_MAX || !pl_str(mb + PUP_PAYLOAD, mb + PUP_PAYLOAD + len, path, sizeof path)) return PUP_E_INVAL;
    if (!max || max > PUP_PAYLOAD_MAX) max = PUP_PAYLOAD_MAX;
    fd = sys_open(path, O_RDONLY);
    if ((e = sys_err(fd))) return (u32) e;
    vals[1] = (u32) file_size(fd);
    if (off && (e = sys_err(S3(SYS_lseek, fd, off, 0)))) { sys_close(fd); return (u32) e; }
    while (*rlen < max && (r = sys_read(fd, (void *) (mb + PUP_PAYLOAD + *rlen), (int) (max - *rlen))) > 0) *rlen += (u32) r;
    sys_close(fd);
    vals[0] = *rlen;
    if (!vals[1]) vals[1] = off + *rlen;  /* /proc and friends: size unknown, report what was read */
    return 0;
}

static u32 op_rm(void)
{
    char path[256];
    u32 len = R32(PUP_CMD_LEN);
    int e;
    if (len > PUP_PAYLOAD_MAX || !pl_str(mb + PUP_PAYLOAD, mb + PUP_PAYLOAD + len, path, sizeof path)) return PUP_E_INVAL;
    e = sys_err(S3(SYS_unlink, path, 0, 0));
    if (e == EISDIR || e == 1 /* EPERM: unlink on a dir */) e = sys_err(S3(SYS_rmdir, path, 0, 0));
    return (u32) e;
}

/* -- EXEC / WAIT / KILL ----------------------------------------------------------------------- */
static struct kid { int pid; u32 state, code, t0, t1, reported; } kids[PUP_KIDS_MAX];

static int kids_running(void)
{
    int i, n = 0;
    for (i = 0; i < PUP_KIDS_MAX; i++) n += kids[i].pid && kids[i].state == PUP_KID_RUNNING;
    return n;
}

/* collect every exited child (PID 1 also inherits orphans: those are just reaped) */
static void reap(void)
{
    int st, pid, i;
    while ((pid = sc(SYS_wait4, -1, (int) &st, 1 /* WNOHANG */, 0, 0, 0)) > 0)
        for (i = 0; i < PUP_KIDS_MAX; i++)
            if (kids[i].pid == pid && kids[i].state == PUP_KID_RUNNING) {
                kids[i].t1 = now_ms();
                if ((st & 0x7f) == 0) { kids[i].state = PUP_KID_EXITED; kids[i].code = (u32) (st >> 8) & 255; }
                else { kids[i].state = PUP_KID_SIGNALED; kids[i].code = (u32) st & 0x7f; }
            }
}

static void out_path(char *p, int pid)
{
    char n[12];
    p[0] = 0; scat(p, "/pp/run/"); fmt_u(n, (u32) pid); scat(p, n); scat(p, ".out");
}

static u32 op_exec(u32 *vals)
{
    static char strs[4096];
    static char *argv[33], *envp[17];
    static char *defenv[] = { "PATH=/pp/bin:/", "PUPPET_ANCHOR=0x80000", 0 };
    char cwd[256];
    u32 len = R32(PUP_CMD_LEN);
    const volatile u8 *end = mb + PUP_PAYLOAD + (len > PUP_PAYLOAD_MAX ? PUP_PAYLOAD_MAX : len);
    const volatile u8 *p = pl_str(mb + PUP_PAYLOAD, end, cwd, sizeof cwd);
    int na = 0, ne = 0, used = 0, slot, pid;
    char **env;

    for (slot = 0; slot < PUP_KIDS_MAX; slot++)
        if (!kids[slot].pid || (kids[slot].state != PUP_KID_RUNNING && kids[slot].reported)) break;
    if (slot == PUP_KIDS_MAX) return PUP_E_BUSY;
    if (!p) return PUP_E_INVAL;
    /* argv until an empty string, then env until an empty string or the end */
    while (p && p < end && *p) {
        if (na == 32) return PUP_E_INVAL;
        argv[na++] = strs + used;
        p = pl_str(p, end, strs + used, (int) sizeof strs - used);
        used += slen(argv[na - 1]) + 1;
    }
    if (!p || !na) return PUP_E_INVAL;
    argv[na] = 0;
    if (p < end) p++;
    while (p && p < end && *p) {
        if (ne == 16) return PUP_E_INVAL;
        envp[ne++] = strs + used;
        p = pl_str(p, end, strs + used, (int) sizeof strs - used);
        used += slen(envp[ne - 1]) + 1;
    }
    envp[ne] = 0;
    env = ne ? envp : defenv;

    pid = sc(SYS_fork, 0, 0, 0, 0, 0, 0);
    if (pid < 0) return (u32) -pid;
    if (pid == 0) {                      /* child: never touches the mailbox */
        char op[48]; int fd;
        out_path(op, sc(SYS_getpid, 0, 0, 0, 0, 0, 0));
        fd = sc(SYS_open, (int) op, O_WRONLY | O_CREAT | O_TRUNC, 0644, 0, 0, 0);
        if (fd >= 0) { S3(SYS_dup2, fd, 1, 0); S3(SYS_dup2, fd, 2, 0); if (fd > 2) sys_close(fd); }
        fd = sys_open("/dev/null", O_RDONLY);
        if (fd >= 0 && fd != 0) { S3(SYS_dup2, fd, 0, 0); sys_close(fd); }
        if (cwd[0]) S3(SYS_chdir, cwd, 0, 0);
        fd = S3(SYS_execve, argv[0], argv, env);
        sys_write(1, "puppet: execve failed\n", 22);
        sc(SYS_exit_group, 127, 0, 0, 0, 0, 0);
    }
    kids[slot].pid = pid; kids[slot].state = PUP_KID_RUNNING; kids[slot].code = 0;
    kids[slot].t0 = now_ms(); kids[slot].t1 = 0; kids[slot].reported = 0;
    vals[0] = (u32) pid;
    return 0;
}

static u32 op_wait(u32 *vals, u32 *rlen)
{
    int pid = (int) R32(PUP_CMD_ARGS), i, fd, r;
    u32 tmo = R32(PUP_CMD_ARGS + 4), off = R32(PUP_CMD_ARGS + 8), max = R32(PUP_CMD_ARGS + 12), t0 = now_ms();
    struct kid *k = 0;
    char op[48];

    for (i = 0; i < PUP_KIDS_MAX; i++) if (kids[i].pid == pid) k = &kids[i];
    if (!k) return PUP_E_SRCH;
    for (;;) {
        reap();
        if (k->state != PUP_KID_RUNNING || now_ms() - t0 >= tmo) break;
        nap_ms(5);
    }
    vals[0] = k->state; vals[1] = k->code;
    vals[3] = (k->state == PUP_KID_RUNNING ? now_ms() : k->t1) - k->t0;
    if (k->state != PUP_KID_RUNNING) k->reported = 1;
    out_path(op, pid);
    fd = sys_open(op, O_RDONLY);
    if (fd >= 0) {
        vals[2] = (u32) file_size(fd);
        if (max > PUP_PAYLOAD_MAX) max = PUP_PAYLOAD_MAX;
        if (max && (!off || S3(SYS_lseek, fd, off, 0) >= 0))
            while (*rlen < max && (r = sys_read(fd, (void *) (mb + PUP_PAYLOAD + *rlen), (int) (max - *rlen))) > 0) *rlen += (u32) r;
        sys_close(fd);
    }
    return 0;
}

static u32 op_kill(void)
{
    int pid = (int) R32(PUP_CMD_ARGS), sig = (int) R32(PUP_CMD_ARGS + 4), i;
    for (i = 0; i < PUP_KIDS_MAX; i++) if (kids[i].pid == pid) break;
    if (i == PUP_KIDS_MAX || pid <= 1) return PUP_E_SRCH;   /* only our own children, never PID 1 */
    return (u32) sys_err(S3(SYS_kill, pid, sig ? sig : 9, 0));
}

/* -- KEXEC ------------------------------------------------------------------------------------
 * No kexec-tools (no libc): the puppet builds the x86 32-bit boot protocol entry itself.
 *   0x90000  boot_params zero page (setup header copied from the bzImage, e820 from /proc/iomem,
 *            screen_info from the BIOS data area), 0x91000 command line, 0x92000 trampoline
 *            (esi = boot_params, ebx/edi/ebp = 0, jmp 0x100000: startup_32 loads its own GDT)
 *   0x100000 protected-mode kernel (non-relocatable puppet kernels are linked there)
 *   after it, page aligned past init_size: the initrd, if one was given                          */
#define KX_BP    0x90000u
#define KX_ENTRY (KX_BP + 0x2000u)
#define KX_KERN  0x100000u
struct kseg { const void *buf; u32 bufsz; u32 mem; u32 memsz; };
static u8 kx_lo[0x3000] __attribute__((aligned(4096)));
static struct kseg kx_seg[3];
static int kx_nseg, kexec_armed;

#define B8(o)   (kx_lo[(o)])
#define B16(o)  (*(u16 *) (kx_lo + (o)))
#define B32(o)  (*(u32 *) (kx_lo + (o)))

static const u8 *map_file(const char *path, u32 *size, int *err)
{
    int fd = sys_open(path, O_RDONLY), r;
    *err = sys_err(fd);
    if (*err) return 0;
    *size = (u32) file_size(fd);
    r = sc(SYS_mmap2, 0, (int) ((*size + 4095) & ~4095u), PROT_READ, MAP_PRIVATE, fd, 0);
    sys_close(fd);
    if (r < 0 && r > -4096) { *err = -r; return 0; }
    return (const u8 *) r;
}

/* top-level "start-end : System RAM" lines of /proc/iomem -> e820 type 1 entries */
static void kx_e820(void)
{
    const char *p = buf;
    u32 n = 0, kb_high = 0;
    if (slurp("/proc/iomem", buf, sizeof buf) <= 0) return;
    while (*p && n < 128) {
        if (*p != ' ') {
            u32 a = hexval(p), b;
            const char *q = p;
            while (*q && *q != '-') q++;
            b = hexval(q + 1);
            while (*q && *q != ':' && *q != '\n') q++;
            if (*q == ':' && pfx(q + 2, "System RAM") && b > a) {
                u32 o = 0x2d0 + n * 20;
                B32(o) = a; B32(o + 4) = 0; B32(o + 8) = b - a + 1; B32(o + 12) = 0; B32(o + 16) = 1;
                if (b >= 0x100000) kb_high += (b + 1 - (a > 0x100000 ? a : 0x100000)) >> 10;
                n++;
            }
        }
        while (*p && *p != '\n') p++;
        if (*p) p++;
    }
    B8(0x1e8) = (u8) n;
    B32(0x1e0) = kb_high;               /* alt_mem_k */
}

static void kx_screen(void)
{
    volatile u8 *bda = fd_mem >= 0 ? (volatile u8 *) sys_mmap(4096, PROT_READ, fd_mem, 0) : 0;
    u8 mode = 3, cols = 80, rows = 25, cx = 0, cy = 24;
    if (bda) {
        mode = bda[0x449]; cols = bda[0x44a]; rows = (u8) (bda[0x484] + 1);
        cx = bda[0x450]; cy = bda[0x451];
        sc(SYS_munmap, (int) bda, 4096, 0, 0, 0, 0);
    }
    if (cols != 40 && cols != 80 && cols != 132) cols = 80;
    if (rows < 25 || rows > 60) rows = 25;
    B8(0x00) = cx; B8(0x01) = cy;       /* orig_x/y: the new kernel goes on below the old text */
    B8(0x06) = mode; B8(0x07) = cols;
    B16(0x0a) = mode == 7 ? 0x10 : 0;   /* ega_bx BL=0x10: no EGA BIOS, i.e. a real MDA */
    B8(0x0e) = rows; B8(0x0f) = mode == 7 ? 0 : 1;
    B16(0x10) = mode == 7 ? 14 : 16;
}

static u32 op_kexec(u32 *vals)
{
    char kpath[256], ipath[256], cmd[1024];
    u32 len = R32(PUP_CMD_LEN), fl = R32(PUP_CMD_ARGS), ksz, isz = 0, hlen, ver, i, cmax;
    const volatile u8 *end = mb + PUP_PAYLOAD + (len > PUP_PAYLOAD_MAX ? PUP_PAYLOAD_MAX : len);
    const volatile u8 *p = pl_str(mb + PUP_PAYLOAD, end, kpath, sizeof kpath);
    const u8 *k, *ird = 0;
    int err, r;
    u32 setup, koff, init;

    if (p) p = pl_str(p, end, ipath, sizeof ipath);
    if (p) p = pl_str(p, end, cmd, sizeof cmd);
    if (!p) return PUP_E_INVAL;
    k = map_file(kpath, &ksz, &err);
    if (!k) return (u32) err;
    if (ksz < 0x1000 || k[0x202] != 'H' || k[0x203] != 'd' || k[0x204] != 'r' || k[0x205] != 'S') return PUP_E_NOEXEC;
    ver = k[0x206] | (u32) k[0x207] << 8;
    if (ver < 0x0205) return PUP_E_NOEXEC;
    setup = k[0x1f1] ? k[0x1f1] : 4;
    koff = (setup + 1) * 512;
    if (koff >= ksz) return PUP_E_NOEXEC;
    init = ver >= 0x020a ? *(const u32 *) (k + 0x260) : (ksz - koff) * 4;

    for (i = 0; i < sizeof kx_lo; i++) kx_lo[i] = 0;
    hlen = 0x202 + k[0x201] - 0x1f1;    /* setup header ends where its jump lands */
    if (hlen > 0x280 - 0x1f1) hlen = 0x280 - 0x1f1;
    for (i = 0; i < hlen; i++) kx_lo[0x1f1 + i] = k[0x1f1 + i];
    B8(0x210) = 0xff;                   /* type_of_loader: undefined */
    B8(0x211) = (u8) ((B8(0x211) | 0x01) & ~0x40);   /* LOADED_HIGH, not KEEP_SEGMENTS */
    B32(0x214) = KX_KERN;               /* code32_start */
    B32(0x228) = KX_BP + 0x1000;        /* cmd_line_ptr */
    kx_e820();
    kx_screen();

    /* empty cmdline: this boot's, with puppet.gen bumped (the host waits for that gen in the anchor) */
    if (!cmd[0] && slurp("/proc/cmdline", buf, sizeof buf) > 0) {
        char *s = buf, *o = cmd, num[12];
        u32 gen = R32(PUP_ID_GEN) + 1;
        while (*s && *s != '\n' && o < cmd + sizeof cmd - 40) {
            if ((s == buf || s[-1] == ' ') && pfx(s, "puppet.gen=")) { while (*s && *s != ' ' && *s != '\n') s++; continue; }
            *o++ = *s++;
        }
        while (o > cmd && o[-1] == ' ') o--;
        *o = 0;
        fmt_u(num, gen);
        scat(cmd, " puppet.gen="); scat(cmd, num);
    }
    cmax = ver >= 0x0206 ? *(const u32 *) (k + 0x238) : 255;
    if (cmax > 0xfff) cmax = 0xfff;
    for (i = 0; cmd[i] && i < cmax; i++) kx_lo[0x1000 + i] = (u8) cmd[i];

    {   /* mov $KX_BP,%esi; xor %ebx,%ebx; xor %edi,%edi; xor %ebp,%ebp; mov $KX_KERN,%eax; jmp *%eax */
        static const u8 tr[] = { 0xbe, 0, 0, 0, 0, 0x31, 0xdb, 0x31, 0xff, 0x31, 0xed, 0xb8, 0, 0, 0, 0, 0xff, 0xe0 };
        for (i = 0; i < sizeof tr; i++) kx_lo[0x2000 + i] = tr[i];
        B32(0x2001) = KX_BP; B32(0x200c) = KX_KERN;
    }

    kx_seg[0].buf = kx_lo; kx_seg[0].bufsz = sizeof kx_lo; kx_seg[0].mem = KX_BP; kx_seg[0].memsz = sizeof kx_lo;
    kx_seg[1].buf = k + koff; kx_seg[1].bufsz = ksz - koff; kx_seg[1].mem = KX_KERN;
    kx_seg[1].memsz = (ksz - koff + 4095) & ~4095u;
    kx_nseg = 2;
    if (ipath[0]) {
        u32 at = (KX_KERN + (init > ksz - koff ? init : ksz - koff) + 4095) & ~4095u;
        ird = map_file(ipath, &isz, &err);
        if (!ird) return (u32) err;
        kx_seg[2].buf = ird; kx_seg[2].bufsz = isz; kx_seg[2].mem = at; kx_seg[2].memsz = (isz + 4095) & ~4095u;
        kx_nseg = 3;
        /* the kernel only reads these from boot_params, which is already in kx_lo */
        B32(0x218) = at; B32(0x21c) = isz;
    }
    r = sc(SYS_kexec_load, (int) KX_ENTRY, kx_nseg, (int) kx_seg, 3 << 16 /* KEXEC_ARCH_386 */, 0, 0);
    if ((err = sys_err(r))) return (u32) err;
    vals[0] = (u32) kx_nseg; vals[1] = ksz - koff; vals[2] = isz; vals[3] = init;
    if (fl & PUP_KEXEC_RUN) kexec_armed = 1;
    return 0;
}

static void kexec_go(void)
{
    out_s("PUPPET KEXEC\n");
    nap_ms(1500);                       /* let the host's poll see the reply before the anchor is rewritten */
    sc(SYS_sync, 0, 0, 0, 0, 0, 0);
    sc(SYS_reboot, (int) 0xfee1dead, 672274793, 0x45584543 /* LINUX_REBOOT_CMD_KEXEC */, 0, 0, 0);
    kexec_armed = 0;                    /* only back here if the reboot failed */
    W32(PUP_STATE, PUP_ST_FAULT);
}
