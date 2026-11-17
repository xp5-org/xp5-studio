/* fdc personality: the floppy controller (82077/765 class) driven straight at the ports, 0x3F0-0x3F7, IRQ6, DMA 2.
 *
 *   run [only=ctl|seek|read]
 *
 * The guest boots from this floppy and the tinyconfig kernel has no floppy driver, so the controller is ours. Nothing is
 * ever written to the disk: only RESET, SPECIFY, VERSION, SENSE INTERRUPT, RECALIBRATE, SEEK, READ ID and READ DATA (DMA
 * into the 32 KB at 0x90000 the kernel is told to leave alone). The DOR and the controller's seek position are put back.
 * Output: "FDC CHK <NAME> <PASS|FAIL|NA>", "FDC NOTE <text>" detail lines, last "FDC DONE RESULT=PASS|FAIL <n>". */
#include "pp_rt.h"

#define FDC     0x3F0
#define DOR     (FDC + 2)
#define MSR     (FDC + 4)
#define DATA    (FDC + 5)
#define DIR     (FDC + 7)
#define BUF_PHYS 0x90000u
#define BUF_SIZE 0x8000u

static u8 *buf;
static char notes[16][96];
static int nnotes, fails, checks;
static u8 dor_saved;

static void note_s(const char *s)
{
    int i = 0;
    if (nnotes >= 16) return;
    while (s[i] && i < 95) { notes[nnotes][i] = s[i]; i++; }
    notes[nnotes++][i] = 0;
}
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
    if (ok < 0) pr("FDC CHK %s NA\n", name);
    else if (ok) pr("FDC CHK %s PASS\n", name);
    else { fails++; pr("FDC CHK %s FAIL\n", name); }
}

/* ---- FIFO protocol ---------------------------------------------------------------------------------------- */
static int wait_msr(u8 mask, u8 want, u32 ms)
{
    u32 t0 = now_ms();
    for (;;) {
        if ((inb(MSR) & mask) == want) return 1;
        if (now_ms() - t0 > ms) return 0;
    }
}
static int fput(u8 b)
{
    if (!wait_msr(0xC0, 0x80, 500)) return 0;
    outb(DATA, b);
    return 1;
}
/* result phase: read bytes while the controller has them (RQM, DIO, busy); returns the count */
static u8 res[16];
static int fres(void)
{
    int n = 0;
    if (!wait_msr(0xC0, 0xC0, 3000)) return 0;
    while (n < 16 && (inb(MSR) & 0xD0) == 0xD0) { res[n++] = inb(DATA); }
    return n;
}
static int fcmd(const u8 *c, int n)
{
    int i;
    for (i = 0; i < n; i++) if (!fput(c[i])) return 0;
    return 1;
}
/* a command that ends in an interrupt with no result phase (recalibrate, seek): wait for the controller to go idle,
 * then SENSE INTERRUPT. Returns 1 and fills st0/pcn. */
static int sense(u8 *st0, u8 *pcn)
{
    u8 c = 0x08;
    int n;
    if (!wait_msr(0x10, 0x00, 3000)) return 0;     /* seek finished (no drive busy bit left, CB clear) */
    if (!fcmd(&c, 1)) return 0;
    n = fres();
    if (n < 2) return 0;
    *st0 = res[0]; *pcn = res[1];
    return 1;
}

static u8 irr_lo(void) { outb(0x20, 0x0A); return inb(0x20); }

/* ---- 8237 DMA channel 2 --------------------------------------------------------------------------------- */
static void dma2_prog(u32 phys, u32 len)
{
    u32 cnt = len - 1;
    outb(0x0A, 0x06);
    outb(0x0C, 0);
    outb(0x0B, 0x46);                 /* single, increment, no autoinit, write to memory, channel 2 */
    outb(0x04, (u8) phys);
    outb(0x04, (u8) (phys >> 8));
    outb(0x81, (u8) (phys >> 16));
    outb(0x0C, 0);
    outb(0x05, (u8) cnt);
    outb(0x05, (u8) (cnt >> 8));
    inb(0x08);                        /* clear stale terminal-count flags */
    outb(0x0A, 0x02);
}
static u32 dma2_count(void)
{
    u32 l, h;
    outb(0x0C, 0);
    l = inb(0x05); h = inb(0x05);
    return l | (h << 8);
}

/* ---- controller bring-up --------------------------------------------------------------------------------- */
static int have_fdc;
static void motor_on(void)
{
    outb(DOR, 0x1C);                  /* drive 0 selected, motor A, DMA/IRQ enabled, not in reset */
    sleep_ms(60);
}
static int bring_up(void)
{
    u8 c[3], st0, pcn;
    int i;
    outb(DIR, 0x00);                  /* 500 kbit/s (1.44 MB) */
    outb(DOR, 0x00);                  /* reset */
    sleep_ms(2);
    outb(DOR, 0x0C);                  /* out of reset, DMA enabled, motors off */
    sleep_ms(5);
    /* a reset raises an interrupt; four SENSE INTERRUPTs (one per drive) clear it */
    for (i = 0; i < 4; i++) { if (!sense(&st0, &pcn)) break; }
    c[0] = 0x03; c[1] = 0xDF; c[2] = 0x02;
    if (!fcmd(c, 3)) return 0;
    motor_on();
    return 1;
}

/* ======================================================================== CTL ======== */
static int chk_idle(void)
{
    u8 m = inb(MSR);
    if (m == 0xFF) { note_s("MSR reads FF: no controller at 3F0"); have_fdc = 0; return -1; }
    have_fdc = 1;
    if (m != 0x80) { nb_s("idle MSR is "); nb_u(m, 1); nb_s(" (want 80: ready, host-bound, not busy)"); nb_flush(); return 0; }
    return 1;
}
static int chk_reset(void)
{
    u8 s0[4], pcn, c;
    int i, ok = 1;
    outb(DOR, 0x00);
    sleep_ms(2);
    if (inb(MSR) & 0x80) { note_s("MSR RQM still set while in reset"); ok = 0; }
    outb(DOR, 0x0C);
    sleep_ms(5);
    for (i = 0; i < 4; i++) {
        if (!sense(&s0[i], &pcn)) { nb_s("no sense-interrupt answer for drive "); nb_u((u32) i, 0); nb_flush(); ok = 0; break; }
        if (s0[i] != (u8) (0xC0 | i)) {
            nb_s("reset sense "); nb_u((u32) i, 0); nb_s(": ST0 "); nb_u(s0[i], 1); nb_s(" want "); nb_u((u32) (0xC0 | i), 1);
            nb_flush(); ok = 0;
        }
    }
    /* now nothing is pending: SENSE INTERRUPT must answer the invalid-command byte 0x80 */
    c = 0x08;
    if (fcmd(&c, 1)) {
        int n = fres();
        if (n != 1 || res[0] != 0x80) { nb_s("sense with nothing pending gave "); nb_u(res[0], 1); nb_s(" x"); nb_u((u32) n, 0); nb_flush(); ok = 0; }
    } else ok = 0;
    return ok;
}
static int chk_version(void)
{
    u8 c = 0x10;
    int n;
    if (!fcmd(&c, 1)) { note_s("VERSION not accepted"); return 0; }
    n = fres();
    if (n != 1) { note_s("VERSION gave no result byte"); return 0; }
    nb_s("version byte "); nb_u(res[0], 1); nb_s(res[0] == 0x90 ? " (82077AA)" : res[0] == 0x80 ? " (NEC765)" : " (unknown)"); nb_flush();
    return res[0] == 0x90 || res[0] == 0x80;
}
static int chk_invalid(void)
{
    u8 c = 0x1F;                       /* no such command */
    int n;
    if (!fcmd(&c, 1)) return 0;
    n = fres();
    if (n != 1 || res[0] != 0x80) { nb_s("invalid command gave "); nb_u(res[0], 1); nb_s(" x"); nb_u((u32) n, 0); nb_flush(); return 0; }
    return 1;
}
static int chk_dor(void)
{
    /* the DOR is write-only on a plain AT; the checkable part is that the motor/select bits change what the MSR and
     * the DIR report: with no drive selected there must be no seek-busy bits, and the controller must stay idle */
    int ok = 1;
    outb(DOR, 0x0C);
    if ((inb(MSR) & 0x0F) != 0) { note_s("drive-busy bits set with motors off"); ok = 0; }
    motor_on();
    if ((inb(MSR) & 0xC0) != 0x80) { note_s("MSR not ready after motor on"); ok = 0; }
    return ok;
}

/* ======================================================================== SEEK ======== */
static int chk_recal(void)
{
    u8 c[2] = { 0x07, 0x00 }, st0, pcn;
    if (!fcmd(c, 2)) { note_s("RECALIBRATE not accepted"); return 0; }
    if (!sense(&st0, &pcn)) { note_s("no SENSE INTERRUPT after RECALIBRATE"); return 0; }
    if (st0 != 0x20 || pcn != 0) { nb_s("recalibrate: ST0 "); nb_u(st0, 1); nb_s(" PCN "); nb_u(pcn, 0); nb_s(" (want 20 / 0)"); nb_flush(); return 0; }
    return 1;
}
static int chk_seek(void)
{
    static const u8 trk[] = { 5, 40, 79, 1, 0 };
    u8 c[3], st0, pcn;
    int i, ok = 1;
    for (i = 0; i < 5; i++) {
        c[0] = 0x0F; c[1] = 0x00; c[2] = trk[i];
        if (!fcmd(c, 3)) { note_s("SEEK not accepted"); return 0; }
        if (!sense(&st0, &pcn)) { note_s("no SENSE INTERRUPT after SEEK"); return 0; }
        if (st0 != 0x20 || pcn != trk[i]) {
            nb_s("seek "); nb_u(trk[i], 0); nb_s(": ST0 "); nb_u(st0, 1); nb_s(" PCN "); nb_u(pcn, 0); nb_flush(); ok = 0;
        }
    }
    return ok;
}
static int spt;                          /* sectors per track, learned by probing reads (set in chk_geometry) */
static int read_sectors(int cyl, int head, int sec, int eot, u32 bytes);
static int seek_to(int cyl);
/* READ ID after a real read of a known sector: the ID must belong to cyl 0 / head 0 and be a sector of that track. The
 * boot floppy of these cells runs in "turbo" mode, where successive READ IDs need not walk the track; the count of
 * distinct ids is only noted. */
static int chk_readid(void)
{
    u8 c[2], seen[64], i;
    int k, n, ok = 1, distinct = 0;
    for (i = 0; i < 64; i++) seen[i] = 0;
    if (!seek_to(0)) { note_s("seek to 0 before READ ID failed"); return 0; }
    n = read_sectors(0, 0, 5, 5, 512);
    if (n != 7 || res[0] != 0) { note_s("read of sector 5 before READ ID failed"); return 0; }
    for (k = 0; k < 20; k++) {
        c[0] = 0x4A; c[1] = 0x00;
        if (!fcmd(c, 2)) { note_s("READ ID not accepted"); return 0; }
        n = fres();
        if (n != 7) { nb_s("READ ID result is "); nb_u((u32) n, 0); nb_s(" bytes"); nb_flush(); return 0; }
        if ((res[0] & 0xC0) || res[1] || res[2]) { nb_s("READ ID ST0 "); nb_u(res[0], 1); nb_s(" ST1 "); nb_u(res[1], 1); nb_s(" ST2 "); nb_u(res[2], 1); nb_flush(); return 0; }
        if ((res[3] != 0 || res[4] != 0 || res[6] != 2 || res[5] < 1 || res[5] > 36) && ok) {
            int j;
            nb_s("READ ID on cyl 0 head 0 gave ST0-N");
            for (j = 0; j < 7; j++) { nb_s(" "); nb_u(res[j], 1); }
            nb_s(" after reading sector 5"); nb_flush(); ok = 0;
        }
        if (res[5] < 64 && !seen[res[5]]) { seen[res[5]] = 1; distinct++; }
    }
    nb_s("READ ID: "); nb_u((u32) distinct, 0); nb_s(" distinct sector ids in 20 reads"); nb_flush();
    return ok;
}
/* geometry by reading: the last sector of a 1.44 MB track (18) reads, sector 19 does not */
static int chk_geometry(void)
{
    int n18, n19, ok = 1;
    if (!seek_to(0)) return 0;
    n18 = read_sectors(0, 0, 18, 18, 512);
    ok = (n18 == 7 && (res[0] & 0xC0) == 0);
    n19 = read_sectors(0, 0, 19, 19, 512);
    if (ok && !(n19 == 7 && (res[0] & 0xC0) == 0x40)) { note_s("sector 19 read did not fail: not an 18-sector track"); ok = 0; }
    if (!ok && !(n18 == 7 && (res[0] & 0xC0) == 0)) note_s("sector 18 of cyl 0 head 0 does not read");
    spt = ok ? 18 : 0;
    return ok;
}

/* ======================================================================== READ ======== */
/* READ DATA, DMA, one head. returns result count; leaves result in res[] */
static int read_sectors(int cyl, int head, int sec, int eot, u32 bytes)
{
    u8 c[9];
    u32 i;
    for (i = 0; i < BUF_SIZE; i++) buf[i] = 0xA5;
    dma2_prog(BUF_PHYS, bytes);
    c[0] = 0x46; c[1] = (u8) ((head << 2) | 0); c[2] = (u8) cyl; c[3] = (u8) head; c[4] = (u8) sec; c[5] = 2;
    c[6] = (u8) eot; c[7] = 0x1B; c[8] = 0xFF;
    if (!fcmd(c, 9)) return 0;
    return fres();
}
static int seek_to(int cyl)
{
    u8 c[3], st0, pcn;
    c[0] = 0x0F; c[1] = 0x00; c[2] = (u8) cyl;
    if (!fcmd(c, 3)) return 0;
    return sense(&st0, &pcn) && st0 == 0x20 && pcn == cyl;
}
static u8 sect1[512], sect2[512];
static int chk_read1(void)
{
    u32 i;
    int n, ok = 1;
    u8 irr0 = irr_lo();
    if (!seek_to(0)) { note_s("seek to 0 before read failed"); return 0; }
    n = read_sectors(0, 0, 1, 1, 512);
    if (n != 7) { nb_s("READ DATA result is "); nb_u((u32) n, 0); nb_s(" bytes"); nb_flush(); return 0; }
    if (res[0] != 0x00 || res[1] != 0x00 || res[2] != 0x00) {
        nb_s("READ DATA ST0 "); nb_u(res[0], 1); nb_s(" ST1 "); nb_u(res[1], 1); nb_s(" ST2 "); nb_u(res[2], 1); nb_flush(); return 0;
    }
    if (res[3] != 1 || res[4] != 0 || res[5] != 1 || res[6] != 2) {   /* 82077: EOT reached, MT=0 -> C+1, H, R=1 */
        nb_s("read 1 sector: ST0-N"); { int j; for (j = 0; j < 7; j++) { nb_s(" "); nb_u(res[j], 1); } }
        nb_s(" (want 0 0 0 1 0 1 2)"); nb_flush(); ok = 0;
    }
    for (i = 0; i < 512; i++) sect1[i] = buf[i];
    if (sect1[510] != 0x55 || sect1[511] != 0xAA) {
        nb_s("boot sector signature "); nb_u(sect1[510], 1); nb_u(sect1[511], 1); nb_s(" (want 55aa)"); nb_flush(); ok = 0;
    }
    if (buf[512] != 0xA5) { note_s("DMA ran past the 512-byte count"); ok = 0; }
    nb_s("sector 0 crc32 "); nb_u(~crc32_up(0xFFFFFFFFu, sect1, 512), 1); nb_s(", IRR6 before "); nb_u((irr0 >> 6) & 1, 0); nb_flush();
    return ok;
}
static int chk_dmatc(void)
{
    int n;
    u32 cnt;
    u8 st;
    if (!seek_to(0)) return 0;
    n = read_sectors(0, 0, 1, 1, 512);
    if (n != 7 || res[0] != 0) { note_s("read for the TC check failed"); return 0; }
    st = inb(0x08);
    cnt = dma2_count();
    if (cnt != 0xFFFF) { nb_s("DMA 2 count after a full sector is "); nb_u(cnt, 1); nb_s(" (want ffff)"); nb_flush(); return 0; }
    if (!(st & 0x04)) { nb_s("DMA status "); nb_u(st, 1); nb_s(": channel 2 terminal-count flag not set"); nb_flush(); return 0; }
    return 1;
}
static int chk_track(void)
{
    u32 i, bytes;
    int n, ok = 1, s;
    if (spt < 1 || spt > 18) { note_s("sectors per track unknown or beyond the 32 KB buffer"); return -1; }
    bytes = (u32) spt * 512;
    if (!seek_to(0)) return 0;
    n = read_sectors(0, 0, 1, spt, bytes);
    if (n != 7) { nb_s("track read result is "); nb_u((u32) n, 0); nb_s(" bytes"); nb_flush(); return 0; }
    if (res[0] != 0 || res[1] != 0 || res[2] != 0) {
        nb_s("track read ST0 "); nb_u(res[0], 1); nb_s(" ST1 "); nb_u(res[1], 1); nb_s(" ST2 "); nb_u(res[2], 1);
        nb_s(" at R "); nb_u(res[5], 0); nb_flush(); return 0;
    }
    if (buf[bytes] != 0xA5) { note_s("track read overran its DMA count"); ok = 0; }
    for (i = 0; i < 512; i++) if (buf[i] != sect1[i]) { note_s("sector 1 differs between a 1-sector and a track read"); ok = 0; break; }
    /* every sector on its own must equal its slice of the track */
    for (s = 2; s <= spt && s <= 6; s++) {
        n = read_sectors(0, 0, s, s, 512);
        if (n != 7 || res[0] != 0) { nb_s("single read of sector "); nb_u((u32) s, 0); nb_s(" failed"); nb_flush(); ok = 0; continue; }
        for (i = 0; i < 512; i++) sect2[i] = buf[i];
        n = read_sectors(0, 0, 1, spt, bytes);
        if (n != 7) { ok = 0; break; }
        for (i = 0; i < 512; i++) if (buf[(u32) (s - 1) * 512 + i] != sect2[i]) {
            nb_s("sector "); nb_u((u32) s, 0); nb_s(" differs between a single and a track read"); nb_flush(); ok = 0; break;
        }
    }
    return ok;
}
static int chk_heads(void)
{
    u32 i, diff = 0;
    int n, ok = 1, cyls[3] = { 0, 40, 79 }, k;
    for (k = 0; k < 3; k++) {
        if (!seek_to(cyls[k])) { nb_s("seek to "); nb_u((u32) cyls[k], 0); nb_s(" failed"); nb_flush(); return 0; }
        n = read_sectors(cyls[k], 0, 1, 1, 512);
        if (n != 7 || res[0] != 0 || res[1] != 0) { nb_s("read cyl "); nb_u((u32) cyls[k], 0); nb_s(" head 0 failed, ST0 "); nb_u(res[0], 1); nb_s(" ST1 "); nb_u(res[1], 1); nb_flush(); ok = 0; continue; }
        for (i = 0; i < 512; i++) sect2[i] = buf[i];
        n = read_sectors(cyls[k], 1, 1, 1, 512);
        if (n != 7 || (res[0] & 0xFB) != 0 || res[1] != 0) { nb_s("read cyl "); nb_u((u32) cyls[k], 0); nb_s(" head 1 failed, ST0 "); nb_u(res[0], 1); nb_s(" ST1 "); nb_u(res[1], 1); nb_flush(); ok = 0; continue; }
        if (res[3] != (u8) (cyls[k] + 1) || res[4] != 1) { nb_s("head 1 end position C "); nb_u(res[3], 0); nb_s(" H "); nb_u(res[4], 0); nb_s(" (want C+1, H 1)"); nb_flush(); ok = 0; }
        for (i = 0; i < 512; i++) if (buf[i] != sect2[i]) diff++;
    }
    /* an unwritten or empty head-1 track can equal head 0 only by chance on all three cylinders: the boot floppy has data on
     * both heads, so identical sectors on every probed cylinder means the head bit is ignored */
    if (ok && diff == 0) { note_s("head 0 and head 1 returned identical sectors on cyl 0, 40 and 79"); ok = 0; }
    return ok;
}
static int chk_nosect(void)
{
    int n;
    if (!seek_to(0)) return 0;
    n = read_sectors(0, 0, 0x20, 0x20, 512);
    if (n != 7) { nb_s("missing-sector read gave "); nb_u((u32) n, 0); nb_s(" result bytes"); nb_flush(); return 0; }
    if ((res[0] & 0xC0) != 0x40 || !(res[1] & 0x05)) {
        nb_s("read of sector 20h: ST0 "); nb_u(res[0], 1); nb_s(" ST1 "); nb_u(res[1], 1); nb_s(" (want ST0 40, ST1 bit 2 or 0)"); nb_flush(); return 0;
    }
    /* clean up: the error leaves the controller idle, a sense would answer 80 */
    return 1;
}
static int chk_dir(void)
{
    u8 d = inb(DIR);
    nb_s("DIR "); nb_u(d, 1); nb_flush();
    /* bit 7 is the disk-change latch; on a floppy that never changed it reads 0 once the motor is on */
    return (d & 0x80) == 0;
}

/* ======================================================================== main ======== */
static int wants(const char *only, const char *grp) { return !only || seq(only, grp); }

int main(int argc, char **argv, char **envp)
{
    const char *only = 0;
    int i, fd;
    (void) envp;
    if (argc < 2 || !seq(argv[1], "run")) { pr("usage: pp_fdc run [only=ctl|seek|read]\n"); return 2; }
    for (i = 2; i < argc; i++) if (argv[i][0] == 'o' && argv[i][1] == 'n' && argv[i][4] == '=') only = argv[i] + 5;
    if (sys_iopl(3) < 0) { pr("FDC FAIL iopl\n"); return 1; }
    fd = sys_open("/dev/mem", O_RDWR, 0);
    if (fd < 0) { pr("FDC FAIL /dev/mem\n"); return 1; }
    buf = (u8 *) sys_mmap_phys(fd, BUF_SIZE, BUF_PHYS);
    if (!buf) { pr("FDC FAIL mmap dma buffer\n"); return 1; }
    dor_saved = 0x1C;

    result("FDC_IDLE", chk_idle());
    if (!have_fdc) { pr("FDC DONE RESULT=FAIL 1\n"); return 1; }
    if (!bring_up()) { pr("FDC NOTE controller did not come up (specify rejected)\n"); pr("FDC DONE RESULT=FAIL 1\n"); return 1; }

    if (wants(only, "ctl")) {
        result("FDC_RESET", chk_reset());
        if (!bring_up()) { pr("FDC NOTE controller did not come back after reset check\n"); fails++; }
        result("FDC_VERSION", chk_version());
        result("FDC_INVALID", chk_invalid());
        result("FDC_DOR", chk_dor());
    }
    if (wants(only, "seek")) {
        result("FDC_RECAL", chk_recal());
        result("FDC_SEEK", chk_seek());
        result("FDC_GEOMETRY", chk_geometry());
        result("FDC_READID", chk_readid());
    }
    if (wants(only, "read")) {
        if (!spt) spt = 18;                      /* "only=read": assume 1.44 MB; FDC_TRACK catches a wrong guess */
        result("FDC_READ1", chk_read1());
        result("FDC_DMATC", chk_dmatc());
        result("FDC_TRACK", chk_track());
        result("FDC_HEADS", chk_heads());
        result("FDC_NOSECT", chk_nosect());
        result("FDC_DIR", chk_dir());
    }
    seek_to(0);
    outb(DOR, dor_saved);
    for (i = 0; i < nnotes; i++) pr("FDC NOTE %s\n", notes[i]);
    pr("FDC DONE RESULT=%s %d\n", fails ? "FAIL" : "PASS", fails ? fails : checks);
    return fails ? 1 : 0;
}
