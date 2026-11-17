/* scsi personality: an Adaptec AHA-154x (ISA, 24-bit mailbox interface) driven straight at its ports, with the generated
 * test disk (host/mkdisk.py) as target 0.
 *
 *   run [base=334] [irq=11] [dma=6] [id=7] [only=host|disk|err]
 *
 * Defaults are the AHA-154xB the cell uses: ports 0x334-0x337, IRQ 11, DMA 6, host SCSI id 7. The adapter is a bus
 * master: mailboxes, CCBs, scatter/gather lists and data all live in the 32 KB at 0x90000 the kernel is told to leave alone.
 * Only READ-type commands go to the disk (INQUIRY, TEST UNIT READY, READ CAPACITY, READ(6)/(10), MODE SENSE, REQUEST
 * SENSE); nothing is written. IRQ is masked at the PIC and left masked: user mode cannot EOI it, the edge latch
 * would otherwise reach the kernel later. IRR is read with interrupts off.
 * Output: "SCSI CHK <NAME> <PASS|FAIL|NA>", "SCSI NOTE <text>", last "SCSI DONE RESULT=PASS|FAIL <n>"; also
 * "sector <lba> crc32 <hex>" for the host to compare with the image. */
#include "pp_rt.h"

#define BUF_PHYS 0x90000u
#define BUF_SIZE 0x8000u
#define O_MB   0x000u      /* 1 out + 1 in mailbox, 4 bytes each (the adapter walks the mailboxes round-robin, so one slot) */
#define O_CCB  0x040u
#define O_SG   0x0C0u
#define O_INQ  0x100u
#define O_DATA 0x1000u     /* 16 KB */
#define O_DATA2 0x6000u

#define TOTAL 16128u

static u8 *buf;
static u16 base = 0x334;
static u32 irq = 11, dma = 6, hostid = 7;

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
    char t[12]; int i = 0, b = hex ? 16 : 10;
    do { t[i++] = "0123456789abcdef"[v % (u32) b]; v /= (u32) b; } while (v);
    while (i && nbn < 94) nb[nbn++] = t[--i];
}
static void nb_flush(void) { nb[nbn] = 0; note_s(nb); nbn = 0; }
static void result(const char *name, int ok)
{
    checks++;
    if (ok < 0) pr("SCSI CHK %s NA\n", name);
    else if (ok) pr("SCSI CHK %s PASS\n", name);
    else { fails++; pr("SCSI CHK %s FAIL\n", name); }
}

static u8 pat(u32 lba, u32 i)
{
    u32 x = lba * 2654435761u + i * 40503u;
    return (u8) ((x ^ (x >> 13)) >> 5);
}
static int blocks_ok(const u8 *p, u32 lba, u32 n)
{
    u32 k, i;
    for (k = 0; k < n; k++) for (i = 0; i < 512; i++) if (p[k * 512 + i] != pat(lba + k, i)) return 0;
    return 1;
}

/* ---- ports ---------------------------------------------------------------------------------------------- */
#define P_CTL  (base + 0)
#define P_STAT (base + 0)
#define P_DATA (base + 1)
#define P_INT  (base + 2)

#define ST_STST 0x80
#define ST_DF   0x40
#define ST_INIT 0x20
#define ST_IDLE 0x10
#define ST_CDF  0x08
#define ST_DIN  0x04
#define ST_INV  0x01

static int wait_stat(u8 mask, u8 want, u32 ms)
{
    u32 t0 = now_ms();
    for (;;) {
        if ((inb(P_STAT) & mask) == want) return 1;
        if (now_ms() - t0 > ms) return 0;
    }
}
static int wait_int(u8 mask, u32 ms)
{
    u32 t0 = now_ms();
    for (;;) {
        if (inb(P_INT) & mask) return 1;
        if (now_ms() - t0 > ms) return 0;
    }
}
static void irst(void) { outb(P_CTL, 0x20); sleep_ms(1); }

/* a host adapter command: command byte, params out, results in. Returns the number of result bytes or -1 on a
 * protocol timeout. `hacc` receives whether the command-complete interrupt (bit 2) came. */
static u8 hres[64];
static int hacc;
static int hcmd(u8 cmd, const u8 *par, int np, int nres)
{
    int i, n = 0;
    if (!wait_stat(ST_CDF, 0, 500)) return -1;
    outb(P_DATA, cmd);
    for (i = 0; i < np; i++) {
        if (!wait_stat(ST_CDF, 0, 500)) return -1;
        outb(P_DATA, par[i]);
    }
    while (n < nres) {
        if (!wait_stat(ST_DIN, ST_DIN, 1000)) return n ? n : -1;
        hres[n++] = inb(P_DATA);
    }
    hacc = wait_int(0x04, 1000);
    return n;
}

static int have_scsi;
static int reset_hba(void)
{
    outb(P_CTL, 0x80);                 /* hard reset */
    sleep_ms(5);
    if (!wait_stat(ST_STST, 0, 8000)) return 0;
    sleep_ms(5);
    return 1;
}

static void put24(u8 *p, u32 v) { p[0] = (u8) (v >> 16); p[1] = (u8) (v >> 8); p[2] = (u8) v; }
static u32 get24(const u8 *p) { return ((u32) p[0] << 16) | ((u32) p[1] << 8) | p[2]; }

static int mb_ready;
static int mb_init(void)
{
    u8 p[4];
    p[0] = 1;
    put24(p + 1, BUF_PHYS + O_MB);
    if (hcmd(0x01, p, 4, 0) < 0) return 0;
    irst();
    mb_ready = 1;
    return 1;
}

/* one SCSI command through mailbox 0. dir: 1 = data in, 2 = data out, 0 = none. Returns mailbox-in code (or -1 timeout).
 * Fills host/target status and the auto-sense bytes. */
static u8 hoststat, tgtstat, sense[18];
static u8 mbi_code;
static u32 ccb_datalen_after;
static int irr_seen;
static int ccb_run(u8 op, u8 id, u8 dir, const u8 *cdb, u8 cdblen, u32 dptr, u32 dlen, int check_irr)
{
    u8 *c = buf + O_CCB, *mb = buf + O_MB;
    u8 imr;
    u32 i;
    for (i = 0; i < 96; i++) c[i] = 0;
    c[0] = op;
    c[1] = (u8) ((id << 5) | (dir << 3));
    c[2] = cdblen;
    c[3] = 14;                         /* request-sense allocation */
    put24(c + 4, dlen);
    put24(c + 7, dptr);
    for (i = 0; i < cdblen; i++) c[18 + i] = cdb[i];
    for (i = 0; i < 16; i++) mb[i] = 0;
    put24(mb + 1, BUF_PHYS + O_CCB);
    mb[0] = 1;                         /* MBO start */
    irr_seen = 0;
    imr = inb(0xA1);
    outb(0xA1, (u8) (imr | (1u << (irq - 8))));
    __asm__ volatile("cli");
    outb(P_DATA, 0x02);                /* start SCSI command */
    if (!wait_int(0x01, 5000)) { __asm__ volatile("sti"); return -1; }
    if (check_irr) { outb(0xA0, 0x0A); irr_seen = (inb(0xA0) >> (irq - 8)) & 1; }
    __asm__ volatile("sti");
    mbi_code = mb[4];
    hoststat = c[14]; tgtstat = c[15];
    for (i = 0; i < 18; i++) sense[i] = c[18 + cdblen + i];
    ccb_datalen_after = get24(c + 4);
    irst();
    return mbi_code;
}
static int simple(u8 id, const u8 *cdb, u8 cl, u8 dir, u32 off, u32 len)
{
    int r = ccb_run(0, id, dir, cdb, cl, BUF_PHYS + off, len, 0);
    return r;
}
static int good(void) { return mbi_code == 1 && hoststat == 0 && tgtstat == 0; }

static void why(const char *what)
{
    nb_s(what); nb_s(": mbi "); nb_u(mbi_code, 0); nb_s(" host "); nb_u(hoststat, 1); nb_s(" target "); nb_u(tgtstat, 1); nb_flush();
}

/* ======================================================================== HOST ======== */
static int chk_present(void)
{
    u8 s = inb(P_STAT);
    if (s == 0xFF) { nb_s("status FF at "); nb_u(base, 1); nb_s(": no adapter"); nb_flush(); have_scsi = 0; return -1; }
    have_scsi = 1;
    return 1;
}
static int chk_reset(void)
{
    u8 s;
    if (!reset_hba()) { note_s("self-test never finished after HRST"); return 0; }
    s = inb(P_STAT);
    if ((s & (ST_INIT | ST_IDLE)) != (ST_INIT | ST_IDLE) || (s & (ST_DF | ST_STST | ST_CDF | ST_DIN))) {
        nb_s("status after HRST "); nb_u(s, 1); nb_s(" want 30 (INIT, IDLE)"); nb_flush(); return 0;
    }
    /* soft reset must give the same state */
    outb(P_CTL, 0x40);
    sleep_ms(5);
    if (!wait_stat(ST_STST, 0, 8000)) { note_s("SRST: self-test never finished"); return 0; }
    s = inb(P_STAT);
    if ((s & (ST_INIT | ST_IDLE)) != (ST_INIT | ST_IDLE)) { nb_s("status after SRST "); nb_u(s, 1); nb_s(" want 30"); nb_flush(); return 0; }
    return 1;
}
static int chk_invalid(void)
{
    u8 s;
    int ok = 1;
    if (!wait_stat(ST_CDF, 0, 500)) return 0;
    outb(P_DATA, 0xFE);                /* no such command */
    sleep_ms(5);
    s = inb(P_STAT);
    if (!(s & ST_INV)) { nb_s("0xFE: status "); nb_u(s, 1); nb_s(", INVDCMD (01) not set"); nb_flush(); ok = 0; }
    irst();
    /* the next valid command must clear it (the Adaptec/BusLogic drivers rely on that, not on the interrupt reset) */
    if (hcmd(0x04, 0, 0, 4) != 4) { note_s("adapter inquiry after an invalid command failed"); ok = 0; }
    irst();
    s = inb(P_STAT);
    if (s & ST_INV) { nb_s("INVDCMD still set after a valid command, status "); nb_u(s, 1); nb_flush(); ok = 0; }
    if (!wait_stat(ST_CDF, 0, 500)) { note_s("adapter stuck in command phase after an invalid command"); ok = 0; }
    return ok;
}
static int chk_inquiry(void)
{
    int n = hcmd(0x04, 0, 0, 4);
    if (n != 4) { nb_s("adapter inquiry returned "); nb_u((u32) (n < 0 ? 0 : n), 0); nb_s(" bytes, want 4"); nb_flush(); return 0; }
    irst();
    nb_s("inquiry "); nb_u(hres[0], 1); nb_s(" "); nb_u(hres[1], 1); nb_s(" "); nb_u(hres[2], 1); nb_s(" "); nb_u(hres[3], 1); nb_flush();
    if (!hacc) { note_s("no command-complete interrupt after adapter inquiry"); return 0; }
    if (hres[0] < 0x41 || hres[0] > 0x47) { nb_s("board id "); nb_u(hres[0], 1); nb_s(" is not 'A'..'G'"); nb_flush(); return 0; }
    return 1;
}
static int chk_config(void)
{
    u8 wd = (u8) (1u << dma), wi;
    int n = hcmd(0x0B, 0, 0, 3);
    if (n != 3) { nb_s("get configuration returned "); nb_u((u32) (n < 0 ? 0 : n), 0); nb_s(" bytes, want 3"); nb_flush(); return 0; }
    irst();
    wi = (u8) (irq <= 12 ? 1u << (irq - 9) : 1u << (irq - 9));        /* 9..12 -> 01..08, 14 -> 20, 15 -> 40 */
    if (hres[0] != wd || hres[1] != wi || (hres[2] & 7) != hostid) {
        nb_s("config dma/irq/id = "); nb_u(hres[0], 1); nb_s("/"); nb_u(hres[1], 1); nb_s("/"); nb_u(hres[2], 1);
        nb_s(" want "); nb_u(wd, 1); nb_s("/"); nb_u(wi, 1); nb_s("/"); nb_u(hostid, 1); nb_flush(); return 0;
    }
    return 1;
}
static int chk_echo(void)
{
    static const u8 v[3] = { 0x5A, 0xA5, 0x00 };
    int i;
    for (i = 0; i < 3; i++) {
        u8 p = v[i];
        int n = hcmd(0x1F, &p, 1, 1);
        if (n != 1) { note_s("ECHO gave no data byte"); return 0; }
        irst();
        if (hres[0] != v[i]) { nb_s("ECHO "); nb_u(v[i], 1); nb_s(" came back "); nb_u(hres[0], 1); nb_flush(); return 0; }
    }
    return 1;
}
static int chk_devs(void)
{
    int n = hcmd(0x0A, 0, 0, 8), i, ok = 1;
    if (n != 8) { nb_s("installed devices returned "); nb_u((u32) (n < 0 ? 0 : n), 0); nb_s(" bytes, want 8"); nb_flush(); return 0; }
    irst();
    for (i = 0; i < 8; i++) {
        u8 want = (i == 0) ? 1 : 0;
        if ((u32) i == hostid) want = 0;
        if (hres[i] != want) { nb_s("installed devices: target "); nb_u((u32) i, 0); nb_s(" = "); nb_u(hres[i], 1); nb_s(" want "); nb_u(want, 1); nb_flush(); ok = 0; }
    }
    return ok;
}
static int chk_mbinit(void)
{
    u8 s;
    if (!mb_init()) { note_s("mailbox init: protocol timeout"); return 0; }
    s = inb(P_STAT);
    if ((s & ST_INIT) || !(s & ST_IDLE)) { nb_s("status after mailbox init "); nb_u(s, 1); nb_s(" want INIT clear, IDLE set"); nb_flush(); return 0; }
    return 1;
}

/* ======================================================================== DISK ======== */
static int chk_inqdev(void)
{
    static const u8 cdb[6] = { 0x12, 0, 0, 0, 36, 0 };
    u8 *d = buf + O_INQ;
    u32 i;
    char v[9], p[17];
    for (i = 0; i < 64; i++) d[i] = 0xEE;
    if (simple(0, cdb, 6, 1, O_INQ, 36) < 0) { note_s("INQUIRY timed out"); return 0; }
    if (!good()) { why("INQUIRY"); return 0; }
    for (i = 0; i < 8; i++) v[i] = (char) d[8 + i];
    v[8] = 0;
    for (i = 0; i < 16; i++) p[i] = (char) d[16 + i];
    p[16] = 0;
    nb_s("target 0: '"); nb_s(v); nb_s("' '"); nb_s(p); nb_s("' type "); nb_u(d[0], 1); nb_s(" ansi "); nb_u(d[2] & 7, 0); nb_flush();
    if ((d[0] & 0x1F) != 0 || (d[0] >> 5) != 0) { note_s("INQUIRY: not a connected direct-access device"); return 0; }
    if (d[4] + 5u < 36u) { note_s("INQUIRY: additional length too short"); return 0; }
    for (i = 8; i < 36; i++) if (d[i] < 0x20 || d[i] > 0x7E) { note_s("INQUIRY: vendor/product not printable ASCII"); return 0; }
    if (d[36] != 0xEE) { note_s("INQUIRY wrote past the 36 bytes asked for"); return 0; }
    return 1;
}
static int chk_tur(void)
{
    static const u8 cdb[6] = { 0, 0, 0, 0, 0, 0 };
    if (simple(0, cdb, 6, 0, 0, 0) < 0) { note_s("TEST UNIT READY timed out"); return 0; }
    if (!good()) { why("TEST UNIT READY"); return 0; }
    return 1;
}
static int chk_readcap(void)
{
    static const u8 cdb[10] = { 0x25, 0, 0, 0, 0, 0, 0, 0, 0, 0 };
    u8 *d = buf + O_INQ;
    u32 last, bl;
    if (simple(0, cdb, 10, 1, O_INQ, 8) < 0) { note_s("READ CAPACITY timed out"); return 0; }
    if (!good()) { why("READ CAPACITY"); return 0; }
    last = ((u32) d[0] << 24) | ((u32) d[1] << 16) | ((u32) d[2] << 8) | d[3];
    bl = ((u32) d[4] << 24) | ((u32) d[5] << 16) | ((u32) d[6] << 8) | d[7];
    if (last != TOTAL - 1 || bl != 512) { nb_s("capacity last lba "); nb_u(last, 0); nb_s(" block "); nb_u(bl, 0); nb_s(" want "); nb_u(TOTAL - 1, 0); nb_s("/512"); nb_flush(); return 0; }
    return 1;
}
static int read10(u32 lba, u32 n, u32 off)
{
    u8 cdb[10];
    cdb[0] = 0x28; cdb[1] = 0;
    cdb[2] = (u8) (lba >> 24); cdb[3] = (u8) (lba >> 16); cdb[4] = (u8) (lba >> 8); cdb[5] = (u8) lba;
    cdb[6] = 0; cdb[7] = (u8) (n >> 8); cdb[8] = (u8) n; cdb[9] = 0;
    return simple(0, cdb, 10, 1, off, n * 512);
}
static int chk_read10(void)
{
    static const u32 pts[] = { 0, 1, 62, 63, 1008, 12345, TOTAL - 1 };
    int i, ok = 1;
    for (i = 0; i < (int) (sizeof pts / sizeof pts[0]); i++) {
        if (read10(pts[i], 1, O_DATA) < 0) { nb_s("READ(10) lba "); nb_u(pts[i], 0); nb_s(" timed out"); nb_flush(); return 0; }
        if (!good()) { nb_s("READ(10) lba "); nb_u(pts[i], 0); nb_s(": "); why(""); ok = 0; continue; }
        if (!blocks_ok(buf + O_DATA, pts[i], 1)) { nb_s("READ(10) lba "); nb_u(pts[i], 0); nb_s(" data wrong"); nb_flush(); ok = 0; }
        if (pts[i] == 0 || pts[i] == 12345) pr("sector %u crc32 %08x\n", pts[i], ~crc32_up(~0u, buf + O_DATA, 512));
    }
    return ok;
}
static int chk_read6(void)
{
    u8 cdb[6];
    cdb[0] = 0x08; cdb[1] = 0; cdb[2] = 0; cdb[3] = 77; cdb[4] = 4; cdb[5] = 0;   /* lba 77, 4 blocks */
    if (simple(0, cdb, 6, 1, O_DATA, 4 * 512) < 0) { note_s("READ(6) timed out"); return 0; }
    if (!good()) { why("READ(6)"); return 0; }
    if (!blocks_ok(buf + O_DATA, 77, 4)) { note_s("READ(6) lba 77 x4 data wrong"); return 0; }
    return 1;
}
static int chk_multi(void)
{
    if (read10(100, 16, O_DATA) < 0) { note_s("16-block READ(10) timed out"); return 0; }
    if (!good()) { why("16-block READ(10)"); return 0; }
    if (!blocks_ok(buf + O_DATA, 100, 16)) { note_s("16-block READ(10) data wrong"); return 0; }
    return 1;
}
static int chk_sg(void)
{
    u8 cdb[10] = { 0x28, 0, 0, 0, 0x01, 0x2C, 0, 0, 6, 0 };      /* lba 300, 6 blocks */
    u8 *sg = buf + O_SG;
    int r, ok = 1;
    u32 i;
    for (i = 0; i < 6 * 512; i++) buf[O_DATA + i] = 0xEE, buf[O_DATA2 + i] = 0xEE;
    put24(sg + 0, 2 * 512); put24(sg + 3, BUF_PHYS + O_DATA);          /* 2 blocks here */
    put24(sg + 6, 4 * 512); put24(sg + 9, BUF_PHYS + O_DATA2);         /* 4 blocks there */
    r = ccb_run(2, 0, 1, cdb, 10, BUF_PHYS + O_SG, 12, 0);
    if (r < 0) { note_s("scatter/gather READ timed out"); return 0; }
    if (!good()) { why("scatter/gather READ"); return 0; }
    if (!blocks_ok(buf + O_DATA, 300, 2)) { note_s("scatter/gather: first segment wrong"); ok = 0; }
    if (!blocks_ok(buf + O_DATA2, 302, 4)) { note_s("scatter/gather: second segment wrong"); ok = 0; }
    return ok;
}
static int chk_residual(void)
{
    static const u8 cdb[6] = { 0x12, 0, 0, 0, 36, 0 };
    int r = ccb_run(3, 0, 1, cdb, 6, BUF_PHYS + O_INQ, 64, 0);   /* ask for 64, INQUIRY only gives 36 */
    if (r < 0) { note_s("residual-length INQUIRY timed out"); return 0; }
    if (mbi_code != 1 || hoststat != 0 || tgtstat != 0) { why("residual-length INQUIRY"); return 0; }
    if (ccb_datalen_after != 28) { nb_s("residual after a 36-byte answer to 64: "); nb_u(ccb_datalen_after, 0); nb_s(" want 28"); nb_flush(); return 0; }
    return 1;
}
static int chk_modesense(void)
{
    static const u8 cdb[6] = { 0x1A, 0, 0x3F, 0, 0xFF, 0 };   /* all pages */
    u8 *d = buf + O_INQ;
    u32 i;
    for (i = 0; i < 16; i++) d[i] = 0xEE;
    if (simple(0, cdb, 6, 1, O_INQ, 255) < 0) { note_s("MODE SENSE timed out"); return 0; }
    if (!good()) { why("MODE SENSE(6)"); return 0; }
    if (d[0] < 3) { note_s("MODE SENSE: data length below the 3-byte header"); return 0; }
    if (d[1] != 0) { nb_s("MODE SENSE: medium type "); nb_u(d[1], 1); nb_s(" want 0"); nb_flush(); return 0; }
    return 1;
}
static int chk_irq(void)
{
    static const u8 cdb[6] = { 0, 0, 0, 0, 0, 0 };
    int r;
    u8 s;
    r = ccb_run(0, 0, 0, cdb, 6, 0, 0, 1);
    if (r < 0) return 0;
    if (!irr_seen) { nb_s("IRQ "); nb_u(irq, 0); nb_s(" not latched in the slave PIC at command completion"); nb_flush(); return 0; }
    /* ccb_run acknowledged it with the interrupt reset: flags must now be clear */
    s = inb(P_INT);
    if (s & 0x8F) { nb_s("interrupt flags "); nb_u(s, 1); nb_s(" after reset, want clear"); nb_flush(); return 0; }
    return 1;
}

/* ======================================================================== ERR ======== */
static int chk_range(void)
{
    int ok = 1;
    if (read10(TOTAL, 1, O_DATA) < 0) { note_s("READ past the end timed out"); return 0; }
    if (tgtstat != 2) { nb_s("READ lba "); nb_u(TOTAL, 0); nb_s(": target status "); nb_u(tgtstat, 1); nb_s(" want 02 (CHECK CONDITION), host "); nb_u(hoststat, 1); nb_flush(); ok = 0; }
    else if ((sense[2] & 0x0F) != 5 || sense[12] != 0x21) { nb_s("sense key/asc "); nb_u(sense[2] & 0x0F, 1); nb_s("/"); nb_u(sense[12], 1); nb_s(" want 5/21 (ILLEGAL REQUEST, LBA out of range)"); nb_flush(); ok = 0; }
    if (read10(0, 1, O_DATA) < 0 || !good() || !blocks_ok(buf + O_DATA, 0, 1)) { note_s("adapter or disk wrong after the error"); ok = 0; }
    return ok;
}
static int chk_selto(void)
{
    static const u8 cdb[6] = { 0, 0, 0, 0, 0, 0 };
    int r = ccb_run(0, 3, 0, cdb, 6, 0, 0, 0);     /* target 3: nothing there */
    if (r < 0) { note_s("command to an empty target never completed"); return 0; }
    if (hoststat != 0x11) { nb_s("target 3: host status "); nb_u(hoststat, 1); nb_s(" want 11 (selection timeout), mbi "); nb_u(mbi_code, 0); nb_flush(); return 0; }
    if (mbi_code != 4) { nb_s("target 3: mailbox-in code "); nb_u(mbi_code, 0); nb_s(" want 4 (error)"); nb_flush(); return 0; }
    return 1;
}
static int chk_badcmd(void)
{
    static const u8 cdb[6] = { 0xFF, 0, 0, 0, 0, 0 };
    int r = ccb_run(0, 0, 0, cdb, 6, 0, 0, 0);
    if (r < 0) { note_s("unknown SCSI opcode never completed"); return 0; }
    if (tgtstat != 2) { nb_s("opcode FF: target status "); nb_u(tgtstat, 1); nb_s(" want 02, host "); nb_u(hoststat, 1); nb_flush(); return 0; }
    if ((sense[2] & 0x0F) != 5 || sense[12] != 0x20) { nb_s("opcode FF: sense key/asc "); nb_u(sense[2] & 0x0F, 1); nb_s("/"); nb_u(sense[12], 1); nb_s(" want 5/20 (INVALID COMMAND OPERATION CODE)"); nb_flush(); return 0; }
    return 1;
}
static int chk_reqsense(void)
{
    static const u8 tur[6] = { 0, 0, 0, 0, 0, 0 };
    static const u8 rs[6] = { 0x03, 0, 0, 0, 18, 0 };
    u8 *d = buf + O_INQ;
    ccb_run(0, 0, 0, tur, 6, 0, 0, 0);
    if (simple(0, rs, 6, 1, O_INQ, 18) < 0) { note_s("REQUEST SENSE timed out"); return 0; }
    if (!good()) { why("REQUEST SENSE"); return 0; }
    if ((d[0] & 0x7F) != 0x70 && (d[0] & 0x7F) != 0x71) { nb_s("sense response code "); nb_u(d[0], 1); nb_s(" want 70/71"); nb_flush(); return 0; }
    if ((d[2] & 0x0F) != 0) { nb_s("sense key after a good TEST UNIT READY: "); nb_u(d[2] & 0x0F, 1); nb_s(" want 0"); nb_flush(); return 0; }
    return 1;
}

static int wants(const char *only, const char *grp) { return !only || seq(only, grp); }
static u32 argval(const char *s, int hex)
{
    u32 v = 0;
    for (; *s; s++) {
        int d = (*s >= '0' && *s <= '9') ? *s - '0' : (*s >= 'a' && *s <= 'f') ? *s - 'a' + 10 : -1;
        if (d < 0 || (!hex && d > 9)) break;
        v = v * (hex ? 16u : 10u) + (u32) d;
    }
    return v;
}

int main(int argc, char **argv, char **envp)
{
    const char *only = 0;
    int i, fd;
    (void) envp;
    if (argc < 2 || !seq(argv[1], "run")) { pr("usage: pp_scsi run [base=334] [irq=11] [dma=6] [id=7] [only=host|disk|err]\n"); return 2; }
    for (i = 2; i < argc; i++) {
        const char *a = argv[i];
        if (a[0] == 'o' && a[1] == 'n' && a[4] == '=') only = a + 5;
        else if (a[0] == 'b' && a[4] == '=') base = (u16) argval(a + 5, 1);
        else if (a[0] == 'i' && a[1] == 'r' && a[3] == '=') irq = argval(a + 4, 0);
        else if (a[0] == 'd' && a[1] == 'm' && a[3] == '=') dma = argval(a + 4, 0);
        else if (a[0] == 'i' && a[1] == 'd' && a[2] == '=') hostid = argval(a + 3, 0);
    }
    if (sys_iopl(3) < 0) { pr("SCSI FAIL iopl\n"); return 1; }
    fd = sys_open("/dev/mem", O_RDWR, 0);
    if (fd < 0) { pr("SCSI FAIL /dev/mem\n"); return 1; }
    buf = (u8 *) sys_mmap_phys(fd, BUF_SIZE, BUF_PHYS);
    if (!buf) { pr("SCSI FAIL mmap buffer\n"); return 1; }
    outb(0xA1, (u8) (inb(0xA1) | (1u << (irq - 8))));

    result("SCSI_PRESENT", chk_present());
    if (!have_scsi) { pr("SCSI DONE RESULT=FAIL 1\n"); return 1; }

    if (wants(only, "host")) {
        result("SCSI_RESET", chk_reset());
        result("SCSI_INVALID", chk_invalid());
        result("SCSI_ADAPTER_INQ", chk_inquiry());
        result("SCSI_CONFIG", chk_config());
        result("SCSI_ECHO", chk_echo());
        result("SCSI_DEVS", chk_devs());
    } else if (!reset_hba()) { pr("SCSI NOTE adapter did not reset\n"); }
    if (!mb_init() && !mb_ready) { pr("SCSI NOTE mailbox init failed\n"); }
    if (wants(only, "host")) result("SCSI_MBINIT", chk_mbinit());

    if (wants(only, "disk")) {
        result("SCSI_INQUIRY", chk_inqdev());
        result("SCSI_TUR", chk_tur());
        result("SCSI_READCAP", chk_readcap());
        result("SCSI_READ10", chk_read10());
        result("SCSI_READ6", chk_read6());
        result("SCSI_MULTI", chk_multi());
        result("SCSI_SG", chk_sg());
        result("SCSI_RESIDUAL", chk_residual());
        result("SCSI_MODESENSE", chk_modesense());
        result("SCSI_IRQ", chk_irq());
    }
    if (wants(only, "err")) {
        result("SCSI_RANGE", chk_range());
        result("SCSI_SELTIMEOUT", chk_selto());
        result("SCSI_BADCMD", chk_badcmd());
        result("SCSI_REQSENSE", chk_reqsense());
    }
    for (i = 0; i < nnotes; i++) pr("SCSI NOTE %s\n", notes[i]);
    pr("SCSI DONE RESULT=%s %d\n", fails ? "FAIL" : "PASS", fails ? fails : checks);
    return fails ? 1 : 0;
}
