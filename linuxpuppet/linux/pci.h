/* PCI discovery for the r128 guests, shared by the stage inits (include after sys.h). */
#ifndef R128_PCI_H
#define R128_PCI_H
#include "sys.h"

static int slurp(const char *path, char *b, int max)
{
    int fd = sys_open(path, O_RDONLY), n;
    if (fd < 0) return -1;
    n = sys_read(fd, b, max - 1);
    sys_close(fd);
    if (n < 0) n = 0;
    b[n] = 0;
    return n;
}

static u32 hexval(const char *s)
{
    u32 v = 0;
    if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) s += 2;
    for (; ; s++) {
        if (*s >= '0' && *s <= '9') v = v * 16 + (*s - '0');
        else if (*s >= 'a' && *s <= 'f') v = v * 16 + (*s - 'a' + 10);
        else if (*s >= 'A' && *s <= 'F') v = v * 16 + (*s - 'A' + 10);
        else break;
    }
    return v;
}

static int exists(const char *p) { int fd = sys_open(p, O_RDONLY); if (fd >= 0) sys_close(fd); return fd >= 0; }

static int has_str(const char *hay, const char *needle)
{
    int n = slen(needle);
    for (; *hay; hay++) { int i = 0; while (i < n && hay[i] == needle[i]) i++; if (i == n) return 1; }
    return 0;
}

/* /sys/bus/pci/devices has no stat-able listing without getdents; probe bus 0..1, dev 0..31, fn 0 by name. */
static int find_card(char *name)
{
    int bus, dev, i; char p[96], v[16];
    static const char hx[] = "0123456789abcdef";
    for (bus = 0; bus < 4; bus++)
        for (dev = 0; dev < 32; dev++) {
            const char *pre = "/sys/bus/pci/devices/0000:";
            int n = 0;
            for (i = 0; pre[i]; i++) p[n++] = pre[i];
            p[n++] = hx[bus >> 4]; p[n++] = hx[bus & 15]; p[n++] = ':';
            p[n++] = hx[dev >> 4]; p[n++] = hx[dev & 15]; p[n++] = '.'; p[n++] = '0';
            p[n] = 0;
            mcopy(name, p, n + 1);
            {
                char f[128]; int k = 0;
                mcopy(f, p, n); k = n; mcopy(f + k, "/vendor", 8);
                if (slurp(f, v, sizeof v) < 0) continue;
                if (hexval(v) != 0x1002) continue;
                mcopy(f + k, "/device", 8);
                if (slurp(f, v, sizeof v) < 0) continue;
                if (hexval(v) == 0x5046) return 1;
            }
        }
    return 0;
}

#endif
