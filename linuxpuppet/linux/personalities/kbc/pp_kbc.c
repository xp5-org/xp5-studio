/* kbc personality: the 8042 keyboard controller and the A20 gate, checked at ports 0x60/0x64/0x92.
 *
 *   run [only=ctl|a20|kbd]
 *
 * The controller is polled with its interrupt-enable bits cleared, so nothing here raises IRQ1/IRQ12. The command
 * byte is saved and restored. The A20 gate is only READ (output port bit 1, port 0x92 bit 1): the running Linux
 * lives above 1 MB and switching A20 off under it would wrap its own memory. Output: "KBC CHK <NAME> <PASS|FAIL|NA>",
 * "KBC NOTE <text>", last "KBC DONE RESULT=PASS|FAIL <n>". */
#include "pp_rt.h"

static char notes[16][96];
static int nnotes;
static int fails;

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
static void nb_u(u32 v)
{
    char t[12]; int i = 0;
    do { t[i++] = "0123456789abcdef"[v % 16u]; v /= 16u; } while (v);
    while (i && nbn < 94) nb[nbn++] = t[--i];
}
static void nb_flush(void) { nb[nbn] = 0; note_s(nb); nbn = 0; }

static void result(const char *name, int ok)
{
    if (ok < 0) pr("KBC CHK %s NA\n", name);
    else if (ok) pr("KBC CHK %s PASS\n", name);
    else { fails++; pr("KBC CHK %s FAIL\n", name); }
}

#define KB_DATA 0x60
#define KB_STAT 0x64
#define ST_OBF 0x01
#define ST_IBF 0x02

static int wait_ibf(void)
{
    u32 t0 = now_ms();
    while (inb(KB_STAT) & ST_IBF)
        if (now_ms() - t0 > 200) return 0;
    return 1;
}
/* >= 0: the byte; -1: nothing within `ms` */
static int rd_data(u32 ms)
{
    u32 t0 = now_ms();
    for (;;) {
        if (inb(KB_STAT) & ST_OBF) return inb(KB_DATA);
        if (now_ms() - t0 > ms) return -1;
    }
}
static void flush(void) { int n = 0; while ((inb(KB_STAT) & ST_OBF) && n++ < 64) { (void) inb(KB_DATA); sleep_ms(1); } }
static int cmd(u8 c) { if (!wait_ibf()) return 0; outb(KB_STAT, c); return 1; }
static int dat(u8 d) { if (!wait_ibf()) return 0; outb(KB_DATA, d); return 1; }

static int cb_read(void)
{
    flush();
    if (!cmd(0x20)) return -1;
    return rd_data(200);
}
static int cb_write(u8 v) { return cmd(0x60) && dat(v); }

/* ================================================================== controller ====== */
static int chk_selftest(void)
{
    int r, st;
    flush();
    if (!cmd(0xAA)) { note_s("input buffer stayed full before the self test"); return 0; }
    r = -1;
    {
        u32 t0 = now_ms();
        st = 0;
        while (now_ms() - t0 < 500) {
            st = inb(KB_STAT);
            if (st & ST_OBF) break;
        }
        if (!(st & ST_OBF)) { note_s("no OBF within 500 ms of command AA"); return 0; }
        r = inb(KB_DATA);
    }
    if (inb(KB_STAT) & ST_OBF) { note_s("OBF still set after reading the result"); return 0; }
    if (r != 0x55) { nb_s("self test result "); nb_u((u32) r); nb_s(", want 55"); nb_flush(); return 0; }
    return 1;
}

static int chk_iftest(void)
{
    int r, bad = 0, naux = 0;
    flush();
    if (!cmd(0xAB)) return 0;
    r = rd_data(500);
    if (r != 0x00) { nb_s("keyboard interface test "); nb_u((u32) r); nb_s(", want 0"); nb_flush(); bad++; }
    flush();
    if (!cmd(0xA9)) return 0;
    r = rd_data(500);
    if (r < 0) naux = 1;
    else if (r != 0x00) { nb_s("aux interface test "); nb_u((u32) r); nb_s(", want 0"); nb_flush(); bad++; }
    if (naux) note_s("aux interface test: no answer (no PS/2 mouse port)");
    return !bad;
}

static int chk_cmdbyte(void)
{
    static const u8 pat[] = { 0x30, 0x34, 0x70, 0x33, 0x31, 0x32, 0x74, 0x30, 0x10, 0x20, 0x01, 0x02, 0x03, 0x00 };
    int orig = cb_read(), i, bad = 0;
    if (orig < 0) { note_s("command byte read (20) gave nothing"); return 0; }
    for (i = 0; i < (int) sizeof(pat); i++) {
        int got;
        if (!cb_write(pat[i])) { note_s("command byte write stuck"); bad++; break; }
        got = cb_read();
        if (got < 0) {
            int again;
            nb_s("wrote "); nb_u(pat[i]); nb_s(": 20 gave nothing (st "); nb_u(inb(KB_STAT));
            again = cb_read();
            nb_s("), 2nd try "); nb_u((u32) again);
            nb_flush(); bad++; flush(); continue;
        }
        /* bit 7 is reserved (0); bit 3 is reserved and may read 0 */
        if ((got & 0x77) != (pat[i] & 0x77)) {
            if (bad++ < 8) { nb_s("wrote "); nb_u(pat[i]); nb_s(" read "); nb_u((u32) got); nb_flush(); }
        }
    }
    cb_write((u8) orig);
    if (cb_read() != orig) { note_s("command byte not restored"); bad++; }
    return !bad;
}

/* commands D2 (write kbd output buffer) and D3 (aux): the byte comes back out of port 60 and sets OBF */
static int chk_loop(void)
{
    static const u8 pat[] = { 0x5A, 0xA5, 0x00, 0xFF };
    int orig = cb_read(), i, bad = 0;
    if (orig < 0) return 0;
    cb_write(0x30);                              /* both ports disabled, no IRQ enabled */
    for (i = 0; i < (int) sizeof(pat); i++) {
        int got;
        flush();
        if (!cmd(0xD2) || !dat(pat[i])) { note_s("D2 write stuck"); bad++; break; }
        got = rd_data(200);
        if (got != pat[i]) { nb_s("D2 wrote "); nb_u(pat[i]); nb_s(" read "); nb_u((u32) got); nb_flush(); bad++; }
    }
    flush();
    cb_write((u8) orig);
    flush();
    return !bad;
}

/* ================================================================== A20 gate ======== */
static int outport(void)
{
    flush();
    if (!cmd(0xD0)) return -1;
    return rd_data(200);
}

static int chk_outport(void)
{
    int o = outport();
    if (o < 0) { note_s("output port read (D0) gave nothing"); return 0; }
    if (!(o & 0x02)) { nb_s("output port "); nb_u((u32) o); nb_s(": A20 bit 1 clear while Linux runs above 1 MB"); nb_flush(); return 0; }
    if (!(o & 0x01)) { nb_s("output port "); nb_u((u32) o); nb_s(": bit 0 (system reset) held low"); nb_flush(); return 0; }
    return 1;
}

/* Port 92 (fast A20) has its own gate latch, ORed with the controller's: bit 1 must read back what is written.
 * Only done while the controller's gate holds A20 on, so clearing the latch cannot switch A20 off. Bit 0 (fast
 * reset) is never written. */
static int chk_a20_92(void)
{
    int o = outport(), bad = 0;
    u8 p = inb(0x92), saved = p;
    if (p == 0xFF) { note_s("port 92 reads FF: no fast A20 port"); return -1; }
    if (o < 0 || !(o & 0x02)) { note_s("controller A20 gate not on, port 92 latch not exercised"); return -1; }
    outb(0x92, (u8) ((p | 0x02) & ~0x01));
    if (!(inb(0x92) & 0x02)) { nb_s("wrote bit 1 set, port 92 reads "); nb_u(inb(0x92)); nb_flush(); bad++; }
    outb(0x92, (u8) (p & ~0x03));
    if (inb(0x92) & 0x02) { nb_s("wrote bit 1 clear, port 92 reads "); nb_u(inb(0x92)); nb_flush(); bad++; }
    outb(0x92, (u8) (saved & ~0x01));
    return !bad;
}

/* A20 really is on: the same byte written 1 MB apart must not alias... checked through /dev/mem in the vga
 * personality's way would need a mapping; the gate bits above are what the controller reports. */

/* ================================================================== keyboard ======== */
static int chk_kbd(int which)
{
    int orig = cb_read(), r, r2, bad = 0;
    if (orig < 0) return 0;
    cb_write(0x00);                              /* clock enabled, no IRQ, no translation: polled */
    flush();
    if (which == 0) {                            /* 0xEE: echo */
        if (!dat(0xEE)) { cb_write((u8) orig); return 0; }
        r = rd_data(500);
        if (r < 0) { note_s("no keyboard answered EE"); cb_write((u8) orig); return -1; }
        if (r != 0xEE) { nb_s("echo EE came back "); nb_u((u32) r); nb_flush(); bad++; }
    } else {                                     /* 0xF2: identify, FA then AB 83 (MF2) */
        if (!dat(0xF2)) { cb_write((u8) orig); return 0; }
        r = rd_data(500);
        if (r < 0) { note_s("no keyboard answered F2"); cb_write((u8) orig); return -1; }
        if (r != 0xFA) { nb_s("F2 ack came back "); nb_u((u32) r); nb_flush(); bad++; }
        r = rd_data(200);
        r2 = rd_data(200);
        if (r != 0xAB || r2 != 0x83) { nb_s("keyboard id "); nb_u((u32) r); nb_s(" "); nb_u((u32) r2); nb_s(", want ab 83"); nb_flush(); bad++; }
    }
    flush();
    cb_write((u8) orig);
    flush();
    return !bad;
}

static int want(const char *only, const char *grp) { return !only || seq(only, grp); }

int main(int argc, char **argv, char **envp)
{
    const char *only = 0;
    int i;
    (void) envp;
    for (i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (seq(a, "run")) continue;
        if (a[0]=='o' && a[1]=='n' && a[2]=='l' && a[3]=='y' && a[4]=='=') only = a + 5;
        else { pr("KBC FAIL arg %s\n", a); return 2; }
    }
    if (sys_iopl(3) < 0) { pr("KBC FAIL iopl\n"); return 1; }
    pr("KBC HW start\n");
    if (want(only, "ctl")) {
        result("KBC_SELFTEST", chk_selftest());
        result("KBC_IFTEST", chk_iftest());
        result("KBC_CMDBYTE", chk_cmdbyte());
        result("KBC_LOOP", chk_loop());
    }
    if (want(only, "a20")) {
        result("A20_OUTPORT", chk_outport());
        result("A20_PORT92", chk_a20_92());
    }
    if (want(only, "kbd")) {
        result("KBD_ECHO", chk_kbd(0));
        result("KBD_ID", chk_kbd(1));
    }
    for (i = 0; i < nnotes; i++) pr("KBC NOTE %s\n", notes[i]);
    if (fails) pr("KBC DONE RESULT=FAIL %d\n", fails);
    else pr("KBC DONE RESULT=PASS\n");
    pp_flush();
    return fails ? 1 : 0;
}
