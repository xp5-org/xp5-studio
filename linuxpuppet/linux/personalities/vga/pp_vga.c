/* vga personality: video primitives for the puppet (puppet_plan.md P3). One binary, one command per argv[1]:
 *   info                      VBE 4F00 + 4F01 for every listed mode (one "VGA vmode" line each), BDA mode
 *   mode M SEED [opts]        set mode M (< 0x100: INT10 AH=0; >= 0x100: VBE 4F02, LFB when the mode has one),
 *                             draw the block pattern for SEED, print layout, VRAM CRC, AC + DAC, vblank timing.
 *                             opts: vbl=N (vblanks to time, 0 = skip; default 30), nopat (draw nothing),
 *                             regseq=FILE (program the registers from FILE instead of INT10; M names the layout)
 *   crc M                     layout of M (no mode set) + VRAM CRC + palette: re-check after a snapshot restore
 *   text                      INT10 mode 3 and give the screen back to the kernel console (KD_TEXT)
 *   int10 AX BX CX DX [ES DI] raw INT10, prints the registers out
 *   regs                      dump the standard VGA registers as a REGSEQ program ("R " lines)
 *   regseq FILE               play a REGSEQ program: misc V | seq I V | crtc I V | gc I V | ac I V | dac I R G B |
 *                             pel V | outb P V | outw P V | inb P | sleep MS   (hex numbers; "R " prefix allowed)
 *
 * INT10 runs the card's own BIOS in vm86 (CONFIG_X86_LEGACY_VM86). The low megabyte is mapped at virtual 0: page 0
 * (IVT + BDA), the EBDA and A0000-FFFFF from /dev/mem (shared), the rest anonymous (scratch stack, stub, buffers),
 * so nothing the BIOS writes outside the BDA/EBDA/VRAM reaches Linux's RAM. vm86 needs vm.mmap_min_addr = 0, which
 * this program sets. While a mode other than the console's is up, the console is put in KD_GRAPHICS so the kernel
 * never draws into it.
 *
 * Pattern (mirrored by rmm_86box/puppet_vga.py, which recomputes CRC and expected screen from the "VGA layout" line):
 * blocks of 8x8 pixels (one cell in text modes), colour = h(bx, by, seed); 8 bpp modes load a fixed palette first. */
#include "pp_rt.h"

#define SYS_ioctl 54
#define SYS_vm86 166
#define MAP_PRIVATE 2
#define MAP_FIXED 0x10
#define MAP_ANON 0x20
#define PROT_EXEC 4
#define KDSETMODE 0x4B3A

static inline u32 inl(u16 p) { u32 v; __asm__ volatile("inl %1, %0" : "=a"(v) : "Nd"(p)); return v; }
static inline void outl(u16 p, u32 v) { __asm__ volatile("outl %0, %1" : : "a"(v), "Nd"(p)); }

static int fail(const char *what, int v) { pr("VGA FAIL %s %d\n", what, v); return 1; }

/* ---------------------------------------------------------------- low megabyte at virtual 0 */
static u8 *lm;                         /* == 0, hidden from the optimiser (address 0 is a valid mapping here) */
static int fd_mem = -1;

static u32 mmap_at(u32 va, u32 len, int flags, int fd, u32 phys)
{
    return (u32) sc(SYS_mmap2, (int) va, (int) len, PROT_READ | PROT_WRITE | PROT_EXEC, flags, fd, (int) (phys >> 12));
}
#define MMAP_BAD(r) ((r) >= 0xFFFFF000u)

static u16 rd16(u32 a) { return (u16) (lm[a] | (lm[a + 1] << 8)); }
static u32 rd32(u32 a) { return rd16(a) | ((u32) rd16(a + 2) << 16); }
static void wr16(u32 a, u16 v) { lm[a] = (u8) v; lm[a + 1] = (u8) (v >> 8); }
static u32 lin(u32 seg, u32 off) { return (seg << 4) + off; }

#define STUB_SEG 0x1000u               /* stub at 1000:0000, stack top 1000:FFF0 */
#define BUF_SEG 0x2000u                /* 2000:0000 VBE info, 2000:0400 mode info */

static int lm_init(void)
{
    u32 r, ebda;
    int fd = sys_open("/proc/sys/vm/mmap_min_addr", O_WRONLY, 0);
    if (fd >= 0) { sys_write(fd, "0\n", 2); sys_close(fd); }
    __asm__ volatile("xorl %0, %0" : "=r"(lm));
    fd_mem = sys_open("/dev/mem", O_RDWR | O_SYNC, 0);
    if (fd_mem < 0) return fail("open /dev/mem", fd_mem);
    r = mmap_at(0, 0x1000, MAP_SHARED | MAP_FIXED, fd_mem, 0);
    if (MMAP_BAD(r)) return fail("mmap page 0 (mmap_min_addr?)", (int) r);
    ebda = (u32) rd16(0x40E) << 4;
    if (ebda < 0x30000 || ebda >= 0xA0000) ebda = 0x9F000;
    ebda &= ~0xFFFu;
    r = mmap_at(0x1000, ebda - 0x1000, MAP_PRIVATE | MAP_ANON | MAP_FIXED, -1, 0);
    if (MMAP_BAD(r)) return fail("mmap scratch", (int) r);
    r = mmap_at(ebda, 0xA0000 - ebda, MAP_SHARED | MAP_FIXED, fd_mem, ebda);
    if (MMAP_BAD(r)) return fail("mmap ebda", (int) r);
    r = mmap_at(0xA0000, 0x60000, MAP_SHARED | MAP_FIXED, fd_mem, 0xA0000);
    if (MMAP_BAD(r)) return fail("mmap A0000-FFFFF", (int) r);
    lm[lin(STUB_SEG, 0)] = 0xCD; lm[lin(STUB_SEG, 1)] = 0xFF; lm[lin(STUB_SEG, 2)] = 0xF4;   /* int 0FFh; hlt */
    r = (u32) sys_iopl(3);
    if (r) return fail("iopl", (int) r);
    return 0;
}

/* ---------------------------------------------------------------- vm86 */
struct vm86_regs {
    i32 ebx, ecx, edx, esi, edi, ebp, eax;
    i32 null_ds, null_es, null_fs, null_gs, orig_eax, eip;
    u16 cs, csh; i32 eflags; i32 esp;
    u16 ss, ssh, es, esh, ds, dsh, fs, fsh, gs, gsh;
};
struct vm86plus {
    struct vm86_regs regs;
    u32 flags, screen_bitmap, cpu_type;
    u32 int_rev[8], int21_rev[8];
    u32 info; u8 dbg[32];
};
struct R { u32 eax, ebx, ecx, edx, esi, edi, ebp; u16 es, ds; };

static struct vm86plus v86;
static u32 int_depth;

static void push16(u16 x)
{
    v86.regs.esp = (v86.regs.esp - 2) & 0xFFFF;
    wr16(lin(v86.regs.ss, (u32) v86.regs.esp), x);
}
/* what the CPU does for INT n in real mode: push flags, cs, ip; clear IF/TF; jump through the IVT */
static void do_int(u32 n)
{
    push16((u16) v86.regs.eflags);
    push16(v86.regs.cs);
    push16((u16) v86.regs.eip);
    v86.regs.eflags &= ~0x300;
    v86.regs.eip = rd16(n * 4);
    v86.regs.cs = rd16(n * 4 + 2);
    int_depth++;
}

/* port I/O the kernel bounced back to us (it should not happen with the iopl(3) I/O bitmap, but be safe) */
static int emu_io(void)
{
    u32 a = lin(v86.regs.cs, (u32) v86.regs.eip & 0xFFFF), n = 0, op32 = 0;
    u8 op = lm[a];
    if (op == 0x66) { op32 = 1; op = lm[a + ++n]; }
    n++;
    switch (op) {
    case 0xE4: v86.regs.eax = (v86.regs.eax & ~0xFF) | inb(lm[a + n]); n++; break;
    case 0xE6: outb(lm[a + n], (u8) v86.regs.eax); n++; break;
    case 0xE5: if (op32) v86.regs.eax = (i32) inl(lm[a + n]); else v86.regs.eax = (v86.regs.eax & ~0xFFFF) | inw(lm[a + n]); n++; break;
    case 0xE7: if (op32) outl(lm[a + n], (u32) v86.regs.eax); else outw(lm[a + n], (u16) v86.regs.eax); n++; break;
    case 0xEC: v86.regs.eax = (v86.regs.eax & ~0xFF) | inb((u16) v86.regs.edx); break;
    case 0xEE: outb((u16) v86.regs.edx, (u8) v86.regs.eax); break;
    case 0xED: if (op32) v86.regs.eax = (i32) inl((u16) v86.regs.edx); else v86.regs.eax = (v86.regs.eax & ~0xFFFF) | inw((u16) v86.regs.edx); break;
    case 0xEF: if (op32) outl((u16) v86.regs.edx, (u32) v86.regs.eax); else outw((u16) v86.regs.edx, (u16) v86.regs.eax); break;
    default: return 0;
    }
    v86.regs.eip = (v86.regs.eip + (i32) n) & 0xFFFF;
    return 1;
}

static int int86(u32 n, struct R *r)
{
    u32 t0 = now_ms(), loops = 0;
    int k;
    for (k = 0; k < (int) sizeof v86; k++) ((u8 *) &v86)[k] = 0;
    v86.regs.eax = (i32) r->eax; v86.regs.ebx = (i32) r->ebx; v86.regs.ecx = (i32) r->ecx; v86.regs.edx = (i32) r->edx;
    v86.regs.esi = (i32) r->esi; v86.regs.edi = (i32) r->edi; v86.regs.ebp = (i32) r->ebp;
    v86.regs.es = r->es; v86.regs.ds = r->ds ? r->ds : 0x40; v86.regs.fs = 0; v86.regs.gs = 0;
    v86.regs.cs = STUB_SEG; v86.regs.eip = 0;
    v86.regs.ss = STUB_SEG; v86.regs.esp = 0xFFF0;
    v86.regs.eflags = 0x202;
    v86.cpu_type = 4;                                  /* CPU_486 */
    v86.int_rev[0xFF >> 5] |= 1u << (0xFF & 31);       /* int 0FFh (the stub) comes back to us */
    int_depth = 0;
    do_int(n);
    for (;;) {
        int ret = sc(SYS_vm86, 1, (int) &v86, 0, 0, 0, 0), type = ret & 0xFF, arg = (ret >> 8) & 0xFF;
        loops++;
        if (ret < 0 && ret > -4096) return fail("vm86 syscall", ret);
        if (type == 2) {                               /* VM86_INTx: revectored, or a vector the kernel won't follow */
            if (arg == 0xFF) break;
            do_int((u32) arg);
        } else if (type == 1) {                        /* VM86_UNKNOWN: #GP the kernel didn't emulate */
            if (!emu_io()) {
                u32 a = lin(v86.regs.cs, (u32) v86.regs.eip & 0xFFFF);
                pr("VGA FAIL vm86 fault at %x:%x bytes %x %x %x %x\n", v86.regs.cs, (u32) v86.regs.eip & 0xFFFF,
                   lm[a], lm[a + 1], lm[a + 2], lm[a + 3]);
                return 1;
            }
        } else if (type != 0 && type != 3 && type != 4) {
            pr("VGA FAIL vm86 return type %d arg %d\n", type, arg);
            return 1;
        }
        if (now_ms() - t0 > 20000) { pr("VGA FAIL int %x timed out at %x:%x\n", n, v86.regs.cs, (u32) v86.regs.eip); return 1; }
    }
    r->eax = (u32) v86.regs.eax; r->ebx = (u32) v86.regs.ebx; r->ecx = (u32) v86.regs.ecx; r->edx = (u32) v86.regs.edx;
    r->esi = (u32) v86.regs.esi; r->edi = (u32) v86.regs.edi; r->ebp = (u32) v86.regs.ebp; r->es = v86.regs.es; r->ds = v86.regs.ds;
    (void) loops;
    return 0;
}

/* ---------------------------------------------------------------- console */
static void kd_mode(int graphics)
{
    int fd = sys_open("/dev/tty0", O_RDWR, 0), r;
    if (fd < 0) { pr("VGA warn open /dev/tty0 %d\n", fd); return; }
    r = S3(SYS_ioctl, fd, KDSETMODE, graphics);
    if (r) pr("VGA warn KDSETMODE %d\n", r);
    sys_close(fd);
}

/* ---------------------------------------------------------------- registers */
static int mono(void) { return !(inb(0x3CC) & 1); }
static u16 crtc_port(void) { return mono() ? 0x3B4 : 0x3D4; }
static u16 stat_port(void) { return mono() ? 0x3BA : 0x3DA; }
static u8 rdi(u16 p, u8 i) { outb(p, i); return inb(p + 1); }
static void wri(u16 p, u8 i, u8 v) { outb(p, i); outb(p + 1, v); }
static u8 ac_rd(u8 i) { u8 v; inb(stat_port()); outb(0x3C0, i | 0x20); v = inb(0x3C1); inb(stat_port()); outb(0x3C0, 0x20); return v; }
static void ac_wr(u8 i, u8 v) { inb(stat_port()); outb(0x3C0, i < 0x10 ? i : (i | 0x20)); outb(0x3C0, v); inb(stat_port()); outb(0x3C0, 0x20); }

static void dump_regs(int ext)
{
    int i; u16 c = crtc_port();
    pr("R seq 0 1\nR misc %x\n", inb(0x3CC));
    for (i = 1; i <= 4; i++) pr("R seq %x %x\n", i, rdi(0x3C4, (u8) i));
    pr("R seq 0 3\nR crtc 11 %x\n", rdi(c, 0x11) & 0x7F);
    for (i = 0; i <= 0x18; i++) pr("R crtc %x %x\n", i, rdi(c, (u8) i));
    for (i = 0; i <= 8; i++) pr("R gc %x %x\n", i, rdi(0x3CE, (u8) i));
    for (i = 0; i <= 0x14; i++) pr("R ac %x %x\n", i, ac_rd((u8) i));
    pr("R pel %x\n", inb(0x3C6));
    if (ext)                            /* card-specific CRTC registers, as comments: shown, never replayed */
        for (i = 0x19; i < 0x70; i++) pr("R # crtc %x %x\n", i, rdi(c, (u8) i));
    outb(0x3C7, 0);
    for (i = 0; i < 256; i++) { u8 r = inb(0x3C9), g = inb(0x3C9), b = inb(0x3C9); pr("R dac %x %x %x %x\n", i, r, g, b); }
}

static char *tok(char **p)
{
    char *s = *p, *t;
    while (*s == ' ' || *s == '\t' || *s == '\r') s++;
    if (!*s) { *p = s; return 0; }
    t = s;
    while (*s && *s != ' ' && *s != '\t' && *s != '\r') s++;
    if (*s) *s++ = 0;
    *p = s;
    return t;
}
static u32 hx(const char *s) { u32 v = 0; for (; *s; s++) v = v * 16 + (u32) (*s <= '9' ? *s - '0' : (*s | 0x20) - 'a' + 10); return v; }

static int regseq(const char *path)
{
    static char buf[65536];
    int fd = sys_open(path, O_RDONLY, 0), n, nl = 0, ops = 0;
    char *p, *e;
    if (fd < 0) return fail("regseq open", fd);
    n = sys_read(fd, buf, sizeof buf - 1);
    sys_close(fd);
    if (n < 0) return fail("regseq read", n);
    buf[n] = 0;
    for (p = buf; *p; p = e) {
        char *a[6], *q;
        int k = 0;
        for (e = p; *e && *e != '\n'; e++) ;
        if (*e) *e++ = 0;
        nl++;
        q = p;
        while (k < 6 && (a[k] = tok(&q))) k++;
        if (!k || a[0][0] == '#') continue;
        if (seq(a[0], "R")) { int j; for (j = 1; j < k; j++) a[j - 1] = a[j]; k--; if (!k || a[0][0] == '#') continue; }
        ops++;
        if (seq(a[0], "misc") && k >= 2) outb(0x3C2, (u8) hx(a[1]));
        else if (seq(a[0], "seq") && k >= 3) wri(0x3C4, (u8) hx(a[1]), (u8) hx(a[2]));
        else if (seq(a[0], "crtc") && k >= 3) wri(crtc_port(), (u8) hx(a[1]), (u8) hx(a[2]));
        else if (seq(a[0], "gc") && k >= 3) wri(0x3CE, (u8) hx(a[1]), (u8) hx(a[2]));
        else if (seq(a[0], "ac") && k >= 3) ac_wr((u8) hx(a[1]), (u8) hx(a[2]));
        else if (seq(a[0], "pel") && k >= 2) outb(0x3C6, (u8) hx(a[1]));
        else if (seq(a[0], "dac") && k >= 5) { outb(0x3C8, (u8) hx(a[1])); outb(0x3C9, (u8) hx(a[2])); outb(0x3C9, (u8) hx(a[3])); outb(0x3C9, (u8) hx(a[4])); }
        else if (seq(a[0], "outb") && k >= 3) outb((u16) hx(a[1]), (u8) hx(a[2]));
        else if (seq(a[0], "outw") && k >= 3) outw((u16) hx(a[1]), (u16) hx(a[2]));
        else if (seq(a[0], "inb") && k >= 2) pr("VGA inb %x %x\n", hx(a[1]), inb((u16) hx(a[1])));
        else if (seq(a[0], "sleep") && k >= 2) sleep_ms((int) hx(a[1]));
        else { pr("VGA FAIL regseq line %d: %s\n", nl, a[0]); return 1; }
    }
    pr("VGA regseq ops=%d\n", ops);
    return 0;
}

/* ---------------------------------------------------------------- layouts */
enum { K_TEXT = 1, K_CGA4, K_CGA2, K_PLANAR, K_CHUNKY, K_DIRECT };
static const char *kname[] = { "?", "text", "cga4", "cga2", "planar", "chunky", "direct" };
struct L {
    u32 mode, kind, w, h, pitch, bpp, ncolors, planes, base, len;
    u32 rs, rp, gs, gp, bs, bp;
    u32 banked, gran;                  /* banked: no LFB, drawn through window A (4F05), gran in bytes */
    volatile u8 *va;
};

static int std_layout(u32 m, struct L *L)
{
    L->mode = m; L->bpp = 0; L->planes = 0; L->banked = 0; L->gran = 0; L->rs = L->rp = L->gs = L->gp = L->bs = L->bp = 0;
    switch (m) {
    case 0: case 1: case 2: case 3: case 7:
        L->kind = K_TEXT; L->w = rd16(0x44A); L->h = lm[0x484] ? lm[0x484] + 1u : 25u;
        if (!L->w) L->w = m < 2 ? 40 : 80;
        L->pitch = L->w * 2; L->ncolors = 16; L->base = m == 7 ? 0xB0000 : 0xB8000; break;
    case 4: case 5: L->kind = K_CGA4; L->w = 320; L->h = 200; L->pitch = 80; L->ncolors = 4; L->base = 0xB8000; break;
    case 6: L->kind = K_CGA2; L->w = 640; L->h = 200; L->pitch = 80; L->ncolors = 2; L->base = 0xB8000; break;
    case 0xD: L->kind = K_PLANAR; L->w = 320; L->h = 200; L->pitch = 40; L->ncolors = 16; L->planes = 4; L->base = 0xA0000; break;
    case 0xE: L->kind = K_PLANAR; L->w = 640; L->h = 200; L->pitch = 80; L->ncolors = 16; L->planes = 4; L->base = 0xA0000; break;
    case 0x10: L->kind = K_PLANAR; L->w = 640; L->h = 350; L->pitch = 80; L->ncolors = 16; L->planes = 4; L->base = 0xA0000; break;
    case 0x11: L->kind = K_PLANAR; L->w = 640; L->h = 480; L->pitch = 80; L->ncolors = 2; L->planes = 1; L->base = 0xA0000; break;
    case 0x12: L->kind = K_PLANAR; L->w = 640; L->h = 480; L->pitch = 80; L->ncolors = 16; L->planes = 4; L->base = 0xA0000; break;
    case 0x6A: L->kind = K_PLANAR; L->w = 800; L->h = 600; L->pitch = 100; L->ncolors = 16; L->planes = 4; L->base = 0xA0000; break;   /* VESA 800x600x16 */
    case 0x13: L->kind = K_CHUNKY; L->w = 320; L->h = 200; L->pitch = 320; L->ncolors = 256; L->bpp = 8; L->base = 0xA0000; break;
    default: return fail("no layout for mode", (int) m);
    }
    L->len = L->kind == K_PLANAR ? L->pitch * L->h : L->kind == K_TEXT ? L->pitch * L->h : L->kind == K_CHUNKY ? L->pitch * L->h : 0x4000;
    L->va = lm + L->base;
    return 0;
}

static u16 vbe_ver;
static int vbe_info(void)
{
    struct R r = {0};
    u32 b = lin(BUF_SEG, 0);
    lm[b] = 'V'; lm[b + 1] = 'B'; lm[b + 2] = 'E'; lm[b + 3] = '2';
    r.eax = 0x4F00; r.es = BUF_SEG; r.edi = 0;
    if (int86(0x10, &r)) return -1;
    if ((r.eax & 0xFFFF) != 0x4F || lm[b] != 'V' || lm[b + 1] != 'E') { vbe_ver = 0; return 0; }
    vbe_ver = rd16(b + 4);
    return 1;
}
/* 4F01 into 2000:0400; returns the info's linear address or 0 */
static u32 vbe_mode_info(u32 m)
{
    struct R r = {0};
    u32 b = lin(BUF_SEG, 0x400), k;
    for (k = 0; k < 256; k++) lm[b + k] = 0;
    r.eax = 0x4F01; r.ecx = m; r.es = BUF_SEG; r.edi = 0x400;
    if (int86(0x10, &r) || (r.eax & 0xFFFF) != 0x4F) return 0;
    return b;
}

static u32 lfb_va, lfb_len;
static int vbe_layout(u32 m, struct L *L, int map)
{
    u32 b = vbe_mode_info(m), attr, model, lp, wa;
    if (!b) return fail("4F01", (int) m);
    attr = rd16(b); model = lm[b + 27]; wa = lm[b + 2];
    L->banked = 0; L->gran = rd16(b + 4) * 1024u;
    L->mode = m; L->w = rd16(b + 18); L->h = rd16(b + 20); L->bpp = lm[b + 25]; L->planes = lm[b + 24];
    L->pitch = rd16(b + 16);
    lp = vbe_ver >= 0x300 ? rd16(b + 50) : 0;
    L->rs = lm[b + 31]; L->rp = lm[b + 32]; L->gs = lm[b + 33]; L->gp = lm[b + 34]; L->bs = lm[b + 35]; L->bp = lm[b + 36];
    L->base = rd32(b + 40);
    if (model == 0) { L->kind = K_TEXT; L->ncolors = 16; L->base = 0xB8000; L->pitch = L->w * 2; }
    else if (model == 3 && L->bpp == 4) { L->kind = K_PLANAR; L->ncolors = 16; L->planes = 4; L->base = 0xA0000; }
    else if (model == 4 && L->bpp == 8) { L->kind = K_CHUNKY; L->ncolors = 256; }
    else if (model == 6 && (L->bpp == 15 || L->bpp == 16 || L->bpp == 24 || L->bpp == 32)) { L->kind = K_DIRECT; L->ncolors = 0; }
    else { pr("VGA FAIL vbe mode %x model %u bpp %u unsupported\n", m, model, L->bpp); return 1; }
    if (L->kind == K_CHUNKY || L->kind == K_DIRECT) {
        if (!(attr & 0x80) || !L->base) {
            /* no linear frame buffer (VBE 1.2): window A, if it can be read and written */
            if ((wa & 7) != 7 || !L->gran) { pr("VGA FAIL vbe mode %x: no LFB and window A attr %x\n", m, wa); return 1; }
            L->banked = 1; L->base = (u32) rd16(b + 8) << 4;
        } else if (lp) L->pitch = lp;
    }
    L->len = L->pitch * L->h;
    if (L->kind == K_PLANAR && L->len > 0x10000) { pr("VGA FAIL planar mode %x larger than the A000 window\n", m); return 1; }
    if ((L->kind == K_CHUNKY || L->kind == K_DIRECT) && !L->banked) {
        if (map) {
            u32 len = (L->len + 0xFFF) & ~0xFFFu;
            u32 r = (u32) sc(SYS_mmap2, 0, (int) len, PROT_READ | PROT_WRITE, MAP_SHARED, fd_mem, (int) (L->base >> 12));
            if (MMAP_BAD(r)) return fail("mmap LFB", (int) r);
            lfb_va = r; lfb_len = len;
            L->va = (volatile u8 *) r;
        }
    } else L->va = lm + L->base;
    return 0;
}

static void pr_layout(const struct L *L)
{
    pr("VGA layout mode=%x kind=%s w=%u h=%u pitch=%u bpp=%u ncolors=%u planes=%u base=%x len=%u "
       "rs=%u rp=%u gs=%u gp=%u bs=%u bp=%u bda_mode=%x mono=%u banked=%u gran=%u\n",
       L->mode, kname[L->kind], L->w, L->h, L->pitch, L->bpp, L->ncolors, L->planes, L->base, L->len,
       L->rs, L->rp, L->gs, L->gp, L->bs, L->bp, lm[0x449], (u32) mono(), L->banked, L->gran);
}

/* ---------------------------------------------------------------- pattern + CRC */
static u32 hblk(u32 bx, u32 by, u32 seed)
{
    u32 v = (bx * 0x9E3779B1u) ^ ((by + 1u) * 0x85EBCA77u) ^ (seed * 0xC2B2AE3Du);
    v ^= v >> 15; v *= 0x2C1B3C6Du; v ^= v >> 12; v *= 0x297A2D39u; v ^= v >> 15;
    return v;
}
static u32 crc_tab[256];
static u32 crc_up(u32 c, const volatile u8 *p, u32 n)
{
    u32 k, j;
    if (!crc_tab[1])
        for (k = 0; k < 256; k++) { u32 v = k; for (j = 0; j < 8; j++) v = (v >> 1) ^ (0xEDB88320u & (0u - (v & 1u))); crc_tab[k] = v; }
    while (n--) c = crc_tab[(c ^ *p++) & 0xFF] ^ (c >> 8);
    return c;
}
/* text attribute for a cell: fg != bg, no blink; mono modes keep to attributes without underline */
static void text_cell(u32 v, int mono_, u8 *ch, u8 *at)
{
    u32 fg, bg;
    if (mono_) { static const u8 f[4] = {0, 7, 8, 15}; fg = f[v & 3]; bg = (v >> 2) & 1 ? 7 : 0; if ((fg & 7) == bg) fg = bg ? 0 : 15; }
    else { fg = v & 15; bg = (v >> 4) & 7; if (fg == bg) fg ^= 8; }
    *at = (u8) (fg | (bg << 4));
    *ch = (v >> 8) & 1 ? 0xDB : 0x20;
}

static u8 rowbuf[8192];
static u32 bcol[1024];

static void copy_row(volatile u8 *dst, const u8 *src, u32 n)
{
    u32 i = 0;
    for (; i + 4 <= n; i += 4) *(volatile u32 *) (dst + i) = *(const u32 *) (src + i);
    for (; i < n; i++) dst[i] = src[i];
}

static void read_row(u8 *dst, const volatile u8 *src, u32 n)
{
    u32 i = 0;
    for (; i + 4 <= n; i += 4) *(u32 *) (dst + i) = *(const volatile u32 *) (src + i);
    for (; i < n; i++) dst[i] = src[i];
}

/* frame offset -> window A, switching banks with 4F05 as needed */
static u32 cur_bank = 0xFFFFFFFFu, bank_switches;
static void set_bank(u32 bank)
{
    struct R r = {0};
    if (bank == cur_bank) return;
    r.eax = 0x4F05; r.ebx = 0; r.edx = bank;
    if (!int86(0x10, &r)) { cur_bank = bank; bank_switches++; }
}
static void put_lin(const struct L *L, u32 off, const u8 *src, u32 n)
{
    if (!L->banked) { copy_row(L->va + off, src, n); return; }
    while (n) {
        u32 in = off % L->gran, k = L->gran - in < n ? L->gran - in : n;
        if (k > 0x10000 - in) k = 0x10000 - in;
        set_bank(off / L->gran);
        copy_row(L->va + in, src, k);
        off += k; src += k; n -= k;
    }
}
static void read_row(u8 *dst, const volatile u8 *src, u32 n);
static void get_lin(const struct L *L, u32 off, u8 *dst, u32 n)
{
    if (!L->banked) { read_row(dst, L->va + off, n); return; }
    while (n) {
        u32 in = off % L->gran, k = L->gran - in < n ? L->gran - in : n;
        if (k > 0x10000 - in) k = 0x10000 - in;
        set_bank(off / L->gran);
        read_row(dst, L->va + in, k);
        off += k; dst += k; n -= k;
    }
}

static void pattern(const struct L *L, u32 seed)
{
    u32 x, y, p, nbx = (L->w + 7) / 8, Bpp = (L->bpp + 7) / 8;
    if (L->kind == K_TEXT) {
        for (y = 0; y < L->h; y++)
            for (x = 0; x < L->w; x++) {
                u8 ch, at;
                text_cell(hblk(x, y, seed), L->base == 0xB0000, &ch, &at);
                L->va[(y * L->w + x) * 2] = ch; L->va[(y * L->w + x) * 2 + 1] = at;
            }
        wri(crtc_port(), 0x0A, rdi(crtc_port(), 0x0A) | 0x20);   /* cursor off */
        return;
    }
    if (L->kind == K_CHUNKY) {                          /* fixed palette: index i = (i&7, i>>3&7, i>>6) steps */
        outb(0x3C8, 0);
        for (x = 0; x < 256; x++) { outb(0x3C9, (u8) ((x & 7) * 9)); outb(0x3C9, (u8) (((x >> 3) & 7) * 9)); outb(0x3C9, (u8) (((x >> 6) & 3) * 21)); }
    }
    if (L->kind == K_PLANAR) { wri(0x3CE, 1, 0); wri(0x3CE, 3, 0); wri(0x3CE, 5, rdi(0x3CE, 5) & ~3); wri(0x3CE, 8, 0xFF); }
    for (y = 0; y < L->h; y += 8) {
        u32 by = y / 8, rows = L->h - y < 8 ? L->h - y : 8, r;
        for (x = 0; x < nbx; x++) {
            u32 v = hblk(x, by, seed);
            bcol[x] = L->kind == K_DIRECT ? v : v % L->ncolors;
        }
        if (L->kind == K_CGA4 || L->kind == K_CGA2) {
            int sh = L->kind == K_CGA4 ? 2 : 1, ppb = 8 / sh;
            for (x = 0; x < L->pitch; x++) {
                u32 b = 0, i;
                for (i = 0; i < (u32) ppb; i++) b = (b << sh) | bcol[(x * ppb + i) / 8];
                rowbuf[x] = (u8) b;
            }
            for (r = 0; r < rows; r++) copy_row(L->va + ((y + r) & 1) * 0x2000 + ((y + r) >> 1) * L->pitch, rowbuf, L->pitch);
            continue;
        }
        if (L->kind == K_PLANAR) {
            for (p = 0; p < L->planes; p++) {
                wri(0x3C4, 2, (u8) (1u << p));
                for (x = 0; x < L->pitch; x++) rowbuf[x] = x < nbx && ((bcol[x] >> p) & 1) ? 0xFF : 0;
                for (r = 0; r < rows; r++) copy_row(L->va + (y + r) * L->pitch, rowbuf, L->pitch);
            }
            continue;
        }
        for (x = 0; x < L->pitch; x++) rowbuf[x] = 0;
        for (x = 0; x < L->w; x++) {
            u32 c = bcol[x / 8], px;
            if (L->kind == K_CHUNKY) { rowbuf[x] = (u8) c; continue; }
            px = (((c & 0xFF) >> (8 - L->rs)) << L->rp) | ((((c >> 8) & 0xFF) >> (8 - L->gs)) << L->gp) |
                 ((((c >> 16) & 0xFF) >> (8 - L->bs)) << L->bp);
            rowbuf[x * Bpp] = (u8) px; rowbuf[x * Bpp + 1] = (u8) (px >> 8);
            if (Bpp > 2) rowbuf[x * Bpp + 2] = (u8) (px >> 16);
            if (Bpp > 3) rowbuf[x * Bpp + 3] = (u8) (px >> 24);
        }
        for (r = 0; r < rows; r++) put_lin(L, (y + r) * L->pitch, rowbuf, L->pitch);
    }
    if (L->kind == K_PLANAR) wri(0x3C4, 2, 0x0F);
}

static u32 vram_crc(const struct L *L)
{
    u32 c = 0xFFFFFFFFu, p, y, gc4;
    if (L->kind == K_TEXT) return ~crc_up(c, L->va, L->w * L->h * 2);
    if (L->kind == K_CGA4 || L->kind == K_CGA2) { c = crc_up(c, L->va, 8000); return ~crc_up(c, L->va + 0x2000, 8000); }
    if (L->kind == K_PLANAR) {
        gc4 = rdi(0x3CE, 4);
        wri(0x3CE, 5, rdi(0x3CE, 5) & ~8);
        for (p = 0; p < L->planes; p++) {
            wri(0x3CE, 4, (u8) p);
            for (y = 0; y < L->h; y++) { read_row(rowbuf, L->va + y * L->pitch, L->pitch); c = crc_up(c, rowbuf, L->pitch); }
        }
        wri(0x3CE, 4, (u8) gc4);
        return ~c;
    }
    for (y = 0; y < L->h; y++) { get_lin(L, y * L->pitch, rowbuf, L->pitch); c = crc_up(c, rowbuf, L->pitch); }
    return ~c;
}

static void pr_palette(void)
{
    u32 i;
    puts_("VGA ac");
    for (i = 0; i <= 0x14; i++) pr(" %02x", ac_rd((u8) i));
    pr("\nVGA pel %02x\nVGA dac ", inb(0x3C6));
    outb(0x3C7, 0);
    for (i = 0; i < 768; i++) pr("%02x", inb(0x3C9));
    puts_("\n");
}

static u32 now_us(void) { int tv[2]; S3(SYS_gettimeofday, tv, 0, 0); return (u32) tv[0] * 1000000u + (u32) tv[1]; }
/* times every vretrace interval; the host takes the median, so an edge missed while this process was off the CPU
 * (one interval twice as long) does not skew the rate */
static int vbl(u32 n)
{
    static u32 iv[256];
    u16 sp = stat_port();
    u32 t = 0, prev = 0, i, lim = now_ms() + 3000 + n * 50;
    if (n > 256) n = 256;
    for (i = 0; i <= n; i++) {
        while (inb(sp) & 8) if (now_ms() > lim) return fail("vretrace stuck high", (int) i);
        while (!(inb(sp) & 8)) if (now_ms() > lim) return fail("no vretrace", (int) i);
        t = now_us();
        if (i) iv[i - 1] = t - prev;
        prev = t;
    }
    puts_("VGA vbl iv=");
    for (i = 0; i < n; i++) pr(i ? ",%u" : "%u", iv[i]);
    puts_("\n");
    return 0;
}

/* ---------------------------------------------------------------- commands */
static int cmd_info(void)
{
    u32 b, mp, k, m, mi, t0 = now_ms();
    int r = vbe_info();
    pr("VGA bda mode=%x cols=%u rows=%u mono=%u\n", lm[0x449], rd16(0x44A), lm[0x484] + 1u, (u32) mono());
    if (r < 0) return 1;
    if (!r) { pr("VGA vbe none\nVGA done ms=%u\n", now_ms() - t0); return 0; }
    b = lin(BUF_SEG, 0);
    mp = rd32(b + 14);
    pr("VGA vbe ver=%x mem_kb=%u cap=%x oem=", vbe_ver, rd16(b + 18) * 64u, rd32(b + 10));
    for (k = lin(rd32(b + 6) >> 16, rd32(b + 6) & 0xFFFF); lm[k] && k < 0x100000; k++) putc_(lm[k] >= 32 && lm[k] < 127 ? (char) lm[k] : '.');
    puts_("\n");
    {   /* the list may live in the info block we are about to overwrite with 4F01 results: copy it first */
        static u16 modes[256];
        u32 nm = 0;
        for (k = lin(mp >> 16, mp & 0xFFFF); nm < 255 && (m = rd16(k)) != 0xFFFF; k += 2) modes[nm++] = (u16) m;
        for (k = 0; k < nm; k++) {
            m = modes[k];
            mi = vbe_mode_info(m);
            if (!mi) { pr("VGA vmode m=%x info_failed=1\n", m); continue; }
            pr("VGA vmode m=%x attr=%x w=%u h=%u bpp=%u model=%u planes=%u pitch=%u lpitch=%u lfb=%x "
               "rs=%u rp=%u gs=%u gp=%u bs=%u bp=%u winseg=%x wina=%x gran=%u\n", m, rd16(mi), rd16(mi + 18), rd16(mi + 20), lm[mi + 25],
               lm[mi + 27], lm[mi + 24], rd16(mi + 16), vbe_ver >= 0x300 ? rd16(mi + 50) : 0, rd32(mi + 40),
               lm[mi + 31], lm[mi + 32], lm[mi + 33], lm[mi + 34], lm[mi + 35], lm[mi + 36], rd16(mi + 8),
               lm[mi + 2], rd16(mi + 4));
        }
    }
    pr("VGA done ms=%u\n", now_ms() - t0);
    return 0;
}

static int set_mode(u32 m, const char *rs, struct L *L)
{
    struct R r = {0};
    if (rs) { if (regseq(rs)) return 1; return std_layout(m, L); }
    if (m < 0x100) {
        /* the equipment word's initial video bits pick colour (10) or mono (11): a VGA BIOS refuses mode 7 while
         * they say colour, and the colour modes while they say mono (DOS MODE MONO / MODE CO80 do the same) */
        lm[0x410] = (u8) ((lm[0x410] & ~0x30) | (m == 7 ? 0x30 : 0x20));
        r.eax = m;
        if (int86(0x10, &r)) return 1;
        pr("VGA int10 ax=%x\n", r.eax & 0xFFFF);
        return std_layout(m, L);
    }
    if (vbe_info() <= 0) return fail("no VBE", 0);
    if (vbe_layout(m, L, 1)) return 1;
    r.eax = 0x4F02; r.ebx = m | ((L->kind == K_CHUNKY || L->kind == K_DIRECT) && !L->banked ? 0x4000 : 0);
    if (int86(0x10, &r)) return 1;
    pr("VGA int10 ax=%x\n", r.eax & 0xFFFF);
    if ((r.eax & 0xFFFF) != 0x4F) return fail("4F02", (int) (r.eax & 0xFFFF));
    return 0;
}

int main(int argc, char **argv, char **envp)
{
    const char *m = argc > 1 ? argv[1] : "info";
    struct L L;
    u32 t0 = now_ms();
    (void) envp;
    if (lm_init()) return 1;
    if (seq(m, "info")) return cmd_info();
    if (seq(m, "regs")) { dump_regs(argc > 2 && seq(argv[2], "ext")); return 0; }
    if (seq(m, "regseq")) return argc > 2 ? regseq(argv[2]) : 2;
    if (seq(m, "text")) {
        struct R r = {0};
        lm[0x410] = (u8) ((lm[0x410] & ~0x30) | 0x20);
        r.eax = 3;
        if (int86(0x10, &r)) return 1;
        kd_mode(0);
        pr("VGA text ax=%x bda_mode=%x\nVGA done ms=%u\n", r.eax & 0xFFFF, lm[0x449], now_ms() - t0);
        return 0;
    }
    if (seq(m, "int10")) {
        struct R r = {0};
        if (argc < 6) return 2;
        r.eax = atou(argv[2]); r.ebx = atou(argv[3]); r.ecx = atou(argv[4]); r.edx = atou(argv[5]);
        if (argc > 7) { r.es = (u16) atou(argv[6]); r.edi = atou(argv[7]); }
        if (int86(0x10, &r)) return 1;
        pr("VGA int10 ax=%x bx=%x cx=%x dx=%x si=%x di=%x es=%x\n", r.eax, r.ebx, r.ecx, r.edx, r.esi, r.edi, r.es);
        return 0;
    }
    if (seq(m, "crc") && argc > 2) {
        u32 mo = atou(argv[2]);
        if (mo < 0x100 ? std_layout(mo, &L) : (vbe_info() <= 0 || vbe_layout(mo, &L, 1))) return 1;
        pr_layout(&L);
        pr("VGA crc %08x len=%u\n", vram_crc(&L), L.len);
        pr_palette();
        pr("VGA done ms=%u\n", now_ms() - t0);
        return 0;
    }
    if (seq(m, "mode") && argc > 3) {
        u32 mo = atou(argv[2]), sd = atou(argv[3]), nv = 30, nopat = 0, i, tp;
        const char *rs = 0;
        for (i = 4; i < (u32) argc; i++) {
            if (argv[i][0] == 'v' && argv[i][1] == 'b' && argv[i][2] == 'l' && argv[i][3] == '=') nv = atou(argv[i] + 4);
            else if (seq(argv[i], "nopat")) nopat = 1;
            else if (argv[i][0] == 'r' && argv[i][1] == 'e' && argv[i][2] == 'g' && argv[i][6] == '=') rs = argv[i] + 7;
        }
        kd_mode(1);
        if (set_mode(mo, rs, &L)) return 1;
        pr_layout(&L);
        tp = now_ms();
        if (!nopat) pattern(&L, sd);
        pr("VGA pattern seed=%u ms=%u\n", sd, now_ms() - tp);
        tp = now_ms();
        pr("VGA crc %08x len=%u ms=%u banks=%u\n", vram_crc(&L), L.len, now_ms() - tp, bank_switches);
        pr_palette();
        if (nv && vbl(nv)) return 1;
        pr("VGA done ms=%u\n", now_ms() - t0);
        return 0;
    }
    pr("VGA unknown command %s\n", m);
    return 2;
}
