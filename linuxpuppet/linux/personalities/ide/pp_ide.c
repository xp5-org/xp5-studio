/* ide personality: the primary ATA channel (1F0-1F7, 3F6, IRQ14) driven straight at the ports, PIO only.
 *
 *   run [only=ctl|read|err]
 *
 * The guest boots from the floppy and the tinyconfig kernel has no IDE driver, so the channel is ours. The cell gives the
 * master a read-only raw image made by host/mkdisk.py: 16 cylinders, 16 heads, 63 sectors, every sector a function of its
 * LBA (pat() below is the same formula). Nothing is ever written to the disk (no WRITE, no FORMAT, no ERASE).
 * IRQ14 is masked at the slave PIC for the whole run and left masked: user mode cannot EOI it, and the edge latch
 * in the PIC would otherwise be delivered to the kernel later. IRR is read with interrupts off.
 * Output: "IDE CHK <NAME> <PASS|FAIL|NA>", "IDE NOTE <text>", last "IDE DONE RESULT=PASS|FAIL <n>"; also
 * "sector <lba> crc32 <hex>" lines for the host to compare with the image. */
#include "pp_rt.h"

#define CYLS 16u
#define HEADS 16u
#define SPT  63u
#define TOTAL (CYLS * HEADS * SPT)

#define P_DATA 0x1F0
#define P_ERR  0x1F1
#define P_CNT  0x1F2
#define P_LBA0 0x1F3
#define P_LBA1 0x1F4
#define P_LBA2 0x1F5
#define P_DEV  0x1F6
#define P_STAT 0x1F7
#define P_ALT  0x3F6

#define S_BSY 0x80
#define S_DRDY 0x40
#define S_DF  0x20
#define S_DSC 0x10
#define S_DRQ 0x08
#define S_ERR 0x01

static u8 sec[512 * 32];
static char notes[16][96];
static int nnotes, fails, checks;

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
    if (ok < 0) pr("IDE CHK %s NA\n", name);
    else if (ok) pr("IDE CHK %s PASS\n", name);
    else { fails++; pr("IDE CHK %s FAIL\n", name); }
}

static u8 pat(u32 lba, u32 i)
{
    u32 x = lba * 2654435761u + i * 40503u;
    return (u8) ((x ^ (x >> 13)) >> 5);
}
static int sector_ok(const u8 *p, u32 lba)
{
    u32 i;
    for (i = 0; i < 512; i++) if (p[i] != pat(lba, i)) return 0;
    return 1;
}

/* ---- the register protocol --------------------------------------------------------------------------------- */
static u8 alt(void) { return inb(P_ALT); }
static void delay400(void) { alt(); alt(); alt(); alt(); }

/* wait for BSY to clear (alt status, so no interrupt is acknowledged); returns the status or -1 on timeout */
static int wait_idle(u32 ms)
{
    u32 t0 = now_ms();
    for (;;) {
        u8 s = alt();
        if (!(s & S_BSY)) return s;
        if (now_ms() - t0 > ms) return -1;
    }
}
static int wait_drq(u32 ms)
{
    u32 t0 = now_ms();
    for (;;) {
        u8 s = alt();
        if (!(s & S_BSY) && (s & (S_DRQ | S_ERR | S_DF))) return s;
        if (now_ms() - t0 > ms) return -1;
    }
}

static void sel(u8 dev) { outb(P_DEV, dev); delay400(); }

static void setup_lba(u32 lba, u32 count)
{
    outb(P_DEV, (u8) (0xE0 | ((lba >> 24) & 0x0F)));
    outb(P_CNT, (u8) count);
    outb(P_LBA0, (u8) lba);
    outb(P_LBA1, (u8) (lba >> 8));
    outb(P_LBA2, (u8) (lba >> 16));
}
static void command(u8 c) { outb(P_STAT, c); delay400(); }

static void read_block(u8 *dst)
{
    int i;
    u16 *w = (u16 *) dst;
    for (i = 0; i < 256; i++) w[i] = inw(P_DATA);
}

/* READ SECTORS (0x20) or READ MULTIPLE (0xC4, block = multiple count) in LBA mode into sec[]; returns 1 when `count`
 * sectors arrived, 0 on error (last error byte in last_err, status in last_stat) */
static u8 last_err, last_stat;
static int pio_read(u32 lba, u32 count, u8 cmd, u32 block)
{
    u32 got = 0, k;
    setup_lba(lba, count);
    command(cmd);
    while (got < count) {
        int s = wait_drq(3000);
        if (s < 0) { last_stat = 0xEE; last_err = 0; return 0; }
        last_stat = (u8) s; last_err = inb(P_ERR);
        if ((s & (S_ERR | S_DF)) || !(s & S_DRQ)) return 0;
        for (k = 0; k < block && got < count; k++, got++) read_block(sec + got * 512);
    }
    if (wait_idle(3000) < 0) return 0;
    last_stat = alt();
    return !(last_stat & (S_ERR | S_DF));
}

static int have_ide;
static u16 id[256];

/* ======================================================================== CTL ======== */
static int chk_present(void)
{
    int s;
    sel(0xE0);
    s = alt();
    if (s == 0xFF || s == 0x7F) { note_s("status FF: no channel at 1F0"); have_ide = 0; return -1; }
    s = wait_idle(5000);
    if (s < 0) { note_s("master stays BSY"); return 0; }
    have_ide = 1;
    if (!(s & S_DRDY)) { nb_s("master not DRDY, status "); nb_u((u32) s, 1); nb_flush(); return 0; }
    if (inb(P_STAT) != (u8) s && (inb(P_STAT) & 0xC0) != 0x40) { note_s("status and alt status disagree"); return 0; }
    return 1;
}
static int chk_srst(void)
{
    int ok = 1, s;
    u8 c, l0, l1, l2;
    outb(P_ALT, 0x06);                 /* SRST + nIEN */
    sleep_ms(1);
    outb(P_ALT, 0x02);                 /* release, keep nIEN */
    sleep_ms(5);
    s = wait_idle(5000);
    if (s < 0) { note_s("BSY never cleared after SRST"); return 0; }
    sel(0xE0);
    c = inb(P_CNT); l0 = inb(P_LBA0); l1 = inb(P_LBA1); l2 = inb(P_LBA2);
    if (c != 1 || l0 != 1 || l1 != 0 || l2 != 0) {
        nb_s("signature cnt/lba0/1/2 = "); nb_u(c, 1); nb_s("/"); nb_u(l0, 1); nb_s("/"); nb_u(l1, 1); nb_s("/"); nb_u(l2, 1);
        nb_s(" want 1/1/0/0 (ATA disk)"); nb_flush(); ok = 0;
    }
    if (inb(P_ERR) != 0x01) { nb_s("diagnostic code "); nb_u(inb(P_ERR), 1); nb_s(" want 01"); nb_flush(); ok = 0; }
    if (!(s & S_DRDY)) { note_s("DRDY clear after SRST"); ok = 0; }
    return ok;
}
static int chk_ident(void)
{
    int ok = 1, i;
    u32 total;
    u16 *w = id;
    u32 sum = 0;
    char m[41];
    setup_lba(0, 0);
    command(0xEC);
    if (wait_drq(3000) < 0 || !(alt() & S_DRQ)) { note_s("IDENTIFY gave no DRQ"); return 0; }
    read_block((u8 *) id);
    wait_idle(1000);
    if (id[0] & 0x8000) { note_s("word0 bit15 set: ATAPI, not a disk"); ok = 0; }
    if (id[1] != CYLS || id[3] != HEADS || id[6] != SPT) {
        nb_s("CHS "); nb_u(id[1], 0); nb_s("/"); nb_u(id[3], 0); nb_s("/"); nb_u(id[6], 0); nb_s(" want 16/16/63"); nb_flush(); ok = 0;
    }
    /* LBA is mandatory only for ATA-4 and later drives: a small CHS disk on an old controller may leave word 49 bit 9 clear
     * and word 60/61 zero. When it advertises LBA the size must be right; otherwise the current CHS capacity (57/58) is. */
    total = id[60] | ((u32) id[61] << 16);
    if (id[49] & 0x0200) {
        if (total != TOTAL) { nb_s("LBA sectors "); nb_u(total, 0); nb_s(" want "); nb_u(TOTAL, 0); nb_flush(); ok = 0; }
    } else {
        u32 cur = id[57] | ((u32) id[58] << 16);
        note_s("IDENTIFY does not advertise LBA (CHS-only disk on this controller)");
        if ((id[53] & 1) && cur != TOTAL) { nb_s("current CHS capacity "); nb_u(cur, 0); nb_s(" want "); nb_u(TOTAL, 0); nb_flush(); ok = 0; }
    }
    for (i = 0; i < 20; i++) { m[i * 2] = (char) (w[27 + i] >> 8); m[i * 2 + 1] = (char) w[27 + i]; }
    m[40] = 0;
    for (i = 39; i >= 0 && m[i] == ' '; i--) m[i] = 0;
    if (!m[0]) { note_s("model string empty"); ok = 0; }
    else { nb_s("model '"); nb_s(m); nb_s("' multiple "); nb_u(id[47] & 0xFF, 0); nb_s(" caps "); nb_u(id[49], 1); nb_flush(); }
    if ((id[255] & 0xFF) == 0xA5) {
        for (i = 0; i < 256; i++) sum += (id[i] & 0xFF) + (id[i] >> 8);
        if (sum & 0xFF) { note_s("IDENTIFY checksum (word 255) wrong"); ok = 0; }
    }
    return ok;
}
static int chk_diag(void)
{
    int s;
    sel(0xE0);
    command(0x90);
    s = wait_idle(3000);
    if (s < 0) { note_s("EXECUTE DEVICE DIAGNOSTIC never finished"); return 0; }
    if (inb(P_ERR) != 0x01) { nb_s("diag code "); nb_u(inb(P_ERR), 1); nb_s(" want 01"); nb_flush(); return 0; }
    return 1;
}
static int chk_slave(void)
{
    u8 s;
    sel(0xF0);                         /* drive 1: not configured */
    s = alt();
    sel(0xE0);
    wait_idle(1000);
    if (s == 0xFF || s == 0x00 || s == 0x7F) return 1;
    if ((s & (S_BSY | S_DRDY)) == S_DRDY) { nb_s("absent slave answers DRDY, status "); nb_u(s, 1); nb_flush(); return 0; }
    return 1;
}
static int chk_invalid(void)
{
    int s;
    setup_lba(0, 0);
    command(0xFF);
    s = wait_idle(3000);
    if (s < 0) return 0;
    if (!(s & S_ERR)) { nb_s("unknown command 0xFF: status "); nb_u((u32) s, 1); nb_s(" (want ERR)"); nb_flush(); return 0; }
    if (!(inb(P_ERR) & 0x04)) { nb_s("unknown command: error "); nb_u(inb(P_ERR), 1); nb_s(" (want ABRT 04)"); nb_flush(); return 0; }
    return 1;
}
/* interrupt line: with nIEN=0 a command raises IRQ14, the alt status read does not clear it, the status read does */
static u8 irr_hi(void) { outb(0xA0, 0x0A); return inb(0xA0); }
static int chk_irq(void)
{
    int ok = 1, s;
    u8 imr = inb(0xA1);
    outb(0xA1, (u8) (imr | 0x40));     /* mask IRQ14 for good */
    __asm__ volatile("cli");
    inb(P_STAT);
    outb(P_ALT, 0x00);                 /* nIEN = 0 */
    setup_lba(5, 1);
    command(0x40);                     /* READ VERIFY: completes with an interrupt and no data */
    s = wait_idle(3000);
    if (s < 0) { __asm__ volatile("sti"); return 0; }
    if (!(irr_hi() & 0x40)) { note_s("IRQ14 not raised at command completion (nIEN=0)"); ok = 0; }
    alt();
    s = inb(P_STAT);                   /* acknowledges the drive interrupt */
    (void) s;
    /* with nIEN=1 the next completion must not raise the line again; the latch from before stays (edge PIC) */
    outb(P_ALT, 0x02);
    __asm__ volatile("sti");
    return ok;
}

/* ======================================================================== READ ======== */
static int chk_lba(void)
{
    static const u32 pts[] = { 0, 1, 62, 63, 1007, 1008, 12345, TOTAL - 1 };
    int i, ok = 1;
    for (i = 0; i < (int) (sizeof pts / sizeof pts[0]); i++) {
        if (!pio_read(pts[i], 1, 0x20, 1)) { nb_s("read lba "); nb_u(pts[i], 0); nb_s(" failed: status "); nb_u(last_stat, 1); nb_s(" err "); nb_u(last_err, 1); nb_flush(); ok = 0; continue; }
        if (!sector_ok(sec, pts[i])) { nb_s("lba "); nb_u(pts[i], 0); nb_s(" data wrong"); nb_flush(); ok = 0; }
        if (pts[i] == 0 || pts[i] == 12345) { pr("sector %u crc32 %08x\n", pts[i], ~crc32_up(~0u, sec, 512)); }
    }
    return ok;
}
static int chk_chs(void)
{
    static const u32 pts[][3] = { {0, 0, 1}, {0, 1, 1}, {1, 0, 1}, {3, 5, 7}, {15, 15, 63} };
    int i, ok = 1;
    for (i = 0; i < 5; i++) {
        u32 c = pts[i][0], h = pts[i][1], s = pts[i][2];
        u32 lba = (c * HEADS + h) * SPT + (s - 1);
        outb(P_DEV, (u8) (0xA0 | h));
        outb(P_CNT, 1);
        outb(P_LBA0, (u8) s);
        outb(P_LBA1, (u8) c);
        outb(P_LBA2, (u8) (c >> 8));
        command(0x20);
        if (wait_drq(3000) < 0 || !(alt() & S_DRQ)) { nb_s("CHS "); nb_u(c, 0); nb_s("/"); nb_u(h, 0); nb_s("/"); nb_u(s, 0); nb_s(" no DRQ"); nb_flush(); ok = 0; continue; }
        read_block(sec);
        wait_idle(1000);
        if (!sector_ok(sec, lba)) { nb_s("CHS "); nb_u(c, 0); nb_s("/"); nb_u(h, 0); nb_s("/"); nb_u(s, 0); nb_s(" is not lba "); nb_u(lba, 0); nb_flush(); ok = 0; }
    }
    return ok;
}
static int chk_multi(void)
{
    u32 k;
    int ok = 1;
    if (!pio_read(100, 8, 0x20, 1)) { note_s("8-sector READ SECTORS failed"); return 0; }
    for (k = 0; k < 8; k++) if (!sector_ok(sec + k * 512, 100 + k)) { nb_s("8-sector read: sector "); nb_u(100 + k, 0); nb_s(" wrong"); nb_flush(); ok = 0; }
    /* a transfer across a track boundary (sector count crosses 62 -> 63) */
    if (!pio_read(60, 8, 0x20, 1)) { note_s("track-crossing read failed"); return 0; }
    for (k = 0; k < 8; k++) if (!sector_ok(sec + k * 512, 60 + k)) { nb_s("track-crossing read: sector "); nb_u(60 + k, 0); nb_s(" wrong"); nb_flush(); ok = 0; }
    return ok;
}
static int chk_rwmult(void)
{
    u32 k, m = id[47] & 0xFF;
    int ok = 1, s;
    if (m == 0) { note_s("READ MULTIPLE not offered (IDENTIFY word 47 = 0)"); return -1; }
    if (m > 16) m = 16;
    setup_lba(0, (u8) m);              /* SET MULTIPLE MODE: sector count = block size */
    command(0xC6);
    s = wait_idle(3000);
    if (s < 0 || (s & S_ERR)) { note_s("SET MULTIPLE MODE refused"); return 0; }
    if (!pio_read(200, m * 2, 0xC4, m)) { nb_s("READ MULTIPLE failed: status "); nb_u(last_stat, 1); nb_s(" err "); nb_u(last_err, 1); nb_flush(); return 0; }
    for (k = 0; k < m * 2; k++) if (!sector_ok(sec + k * 512, 200 + k)) { nb_s("READ MULTIPLE sector "); nb_u(200 + k, 0); nb_s(" wrong"); nb_flush(); ok = 0; }
    return ok;
}
static int chk_256(void)
{
    u32 k, n = 0;
    int ok = 1;
    /* count 0 means 256 sectors; read them one by one into the same buffer, verify each */
    setup_lba(300, 0);
    command(0x20);
    for (k = 0; k < 256; k++) {
        int s = wait_drq(3000);
        if (s < 0 || !(s & S_DRQ)) { nb_s("count 0: stopped at sector "); nb_u(k, 0); nb_flush(); return 0; }
        read_block(sec);
        if (!sector_ok(sec, 300 + k)) ok = 0;
        else n++;
    }
    wait_idle(1000);
    if (!ok) { nb_s("count 0: "); nb_u(n, 0); nb_s(" of 256 sectors right"); nb_flush(); }
    if (alt() & S_DRQ) { note_s("count 0: DRQ still set after 256 sectors"); ok = 0; }
    return ok;
}
static int chk_verify(void)
{
    int s;
    setup_lba(10, 4);
    command(0x40);
    s = wait_idle(3000);
    if (s < 0 || (s & (S_ERR | S_DF))) { nb_s("READ VERIFY failed: status "); nb_u((u32) s, 1); nb_flush(); return 0; }
    if (s & S_DRQ) { note_s("READ VERIFY raised DRQ"); return 0; }
    inb(P_STAT);
    return 1;
}
static int chk_seekcmd(void)
{
    int s;
    setup_lba(2000, 0);
    command(0x70);
    s = wait_idle(3000);
    if (s < 0 || (s & (S_ERR | S_DF))) { nb_s("SEEK failed: status "); nb_u((u32) s, 1); nb_flush(); return 0; }
    if (!(s & S_DSC)) { note_s("SEEK complete but DSC clear"); return 0; }
    return 1;
}
static int chk_setfeat(void)
{
    int s;
    u8 mode = 2;                       /* PIO mode 2 is what every ATA disk takes; take the best the drive advertises */
    if (id[53] & 2) { if (id[64] & 2) mode = 4; else if (id[64] & 1) mode = 3; }
    outb(P_ERR, 0x03);                 /* set transfer mode */
    setup_lba(0, (u8) (0x08 | mode));  /* flow-control PIO, mode n */
    command(0xEF);
    s = wait_idle(3000);
    if (s < 0) return 0;
    if (s & S_ERR) { nb_s("SET FEATURES PIO mode "); nb_u(mode, 0); nb_s(" refused, error "); nb_u(inb(P_ERR), 1); nb_s(" (word 64 = "); nb_u(id[64], 1); nb_s(")"); nb_flush(); return 0; }
    return 1;
}

/* ======================================================================== ERR ======== */
static int chk_range(void)
{
    int ok = 1, s;
    /* one past the last sector: IDNF (error bit 4), ERR set, no DRQ */
    setup_lba(TOTAL, 1);
    command(0x20);
    s = wait_idle(3000);
    if (s < 0) return 0;
    if (!(s & S_ERR) || (s & S_DRQ)) { nb_s("read at lba "); nb_u(TOTAL, 0); nb_s(" (past the end): status "); nb_u((u32) s, 1); nb_s(" want ERR"); nb_flush(); ok = 0; }
    else if (!(inb(P_ERR) & 0x10)) { nb_s("past the end: error "); nb_u(inb(P_ERR), 1); nb_s(" want IDNF 10"); nb_flush(); ok = 0; }
    /* the drive must still work */
    if (!pio_read(7, 1, 0x20, 1) || !sector_ok(sec, 7)) { note_s("drive does not read after the error"); ok = 0; }
    return ok;
}
static int chk_verifyrange(void)
{
    int s, ok = 1;
    setup_lba(TOTAL - 1, 2);           /* last sector plus one that does not exist */
    command(0x40);
    s = wait_idle(3000);
    if (s < 0) return 0;
    if (!(s & S_ERR)) { nb_s("verify across the end: status "); nb_u((u32) s, 1); nb_s(" want ERR"); nb_flush(); ok = 0; }
    else if (!(inb(P_ERR) & 0x10)) { nb_s("verify across the end: error "); nb_u(inb(P_ERR), 1); nb_s(" want IDNF 10"); nb_flush(); ok = 0; }
    setup_lba(TOTAL + 100, 1);         /* wholly past the end */
    command(0x40);
    s = wait_idle(3000);
    if (s < 0) return 0;
    if (!(s & S_ERR)) { nb_s("verify at lba "); nb_u(TOTAL + 100, 0); nb_s(": status "); nb_u((u32) s, 1); nb_s(" want ERR"); nb_flush(); ok = 0; }
    inb(P_STAT);
    return ok;
}
static int chk_chsrange(void)
{
    int s;
    outb(P_DEV, 0xA0 | 0);             /* CHS: cylinder 16 does not exist */
    outb(P_CNT, 1);
    outb(P_LBA0, 1);
    outb(P_LBA1, (u8) CYLS);
    outb(P_LBA2, 0);
    command(0x20);
    s = wait_idle(3000);
    if (s < 0) return 0;
    if (!(s & S_ERR)) { nb_s("CHS cylinder 16: status "); nb_u((u32) s, 1); nb_s(" want ERR"); nb_flush(); return 0; }
    return 1;
}
static int chk_nodata(void)
{
    /* a command the disk should abort: READ SECTORS on the unselected-but-present slave is not testable;
     * use IDENTIFY PACKET DEVICE (0xA1) on an ATA disk: must abort, not hang */
    int s;
    setup_lba(0, 0);
    command(0xA1);
    s = wait_idle(3000);
    if (s < 0) return 0;
    if (!(s & S_ERR) || (s & S_DRQ)) { nb_s("IDENTIFY PACKET on an ATA disk: status "); nb_u((u32) s, 1); nb_s(" want ERR/ABRT"); nb_flush(); return 0; }
    return 1;
}

static int wants(const char *only, const char *grp) { return !only || seq(only, grp); }

int main(int argc, char **argv, char **envp)
{
    const char *only = 0;
    int i;
    (void) envp;
    if (argc < 2 || !seq(argv[1], "run")) { pr("usage: pp_ide run [only=ctl|read|err]\n"); return 2; }
    for (i = 2; i < argc; i++) if (argv[i][0] == 'o' && argv[i][1] == 'n' && argv[i][4] == '=') only = argv[i] + 5;
    if (sys_iopl(3) < 0) { pr("IDE FAIL iopl\n"); return 1; }
    outb(0xA1, (u8) (inb(0xA1) | 0x40));
    outb(P_ALT, 0x02);

    result("IDE_PRESENT", chk_present());
    if (!have_ide) { pr("IDE DONE RESULT=FAIL 1\n"); return 1; }

    if (wants(only, "ctl")) {
        result("IDE_SRST", chk_srst());
        result("IDE_IDENTIFY", chk_ident());
        result("IDE_DIAG", chk_diag());
        result("IDE_SLAVE", chk_slave());
        result("IDE_INVALID", chk_invalid());
        result("IDE_IRQ", chk_irq());
    }
    if (wants(only, "read")) {
        if (!id[0]) { setup_lba(0, 0); command(0xEC); if (wait_drq(3000) >= 0 && (alt() & S_DRQ)) { read_block((u8 *) id); wait_idle(1000); } }
        result("IDE_LBA", chk_lba());
        result("IDE_CHS", chk_chs());
        result("IDE_MULTISECT", chk_multi());
        result("IDE_RWMULT", chk_rwmult());
        result("IDE_COUNT256", chk_256());
        result("IDE_VERIFY", chk_verify());
        result("IDE_SEEK", chk_seekcmd());
        result("IDE_SETFEAT", chk_setfeat());
    }
    if (wants(only, "err")) {
        result("IDE_RANGE", chk_range());
        result("IDE_VERIFYRANGE", chk_verifyrange());
        result("IDE_CHSRANGE", chk_chsrange());
        result("IDE_ATAPICMD", chk_nodata());
    }
    outb(P_ALT, 0x02);
    for (i = 0; i < nnotes; i++) pr("IDE NOTE %s\n", notes[i]);
    pr("IDE DONE RESULT=%s %d\n", fails ? "FAIL" : "PASS", fails ? fails : checks);
    return fails ? 1 : 0;
}
