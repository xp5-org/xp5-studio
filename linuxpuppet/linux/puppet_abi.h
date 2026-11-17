/* Linux puppet mailbox ABI v1 (see sourcedir/86box_remote_monitor/puppet_plan.md, section 2.1).
 * The host side mirrors these offsets in xp5-studio/rmm_86box/linuxpuppet.py: change both together. */
#ifndef PUPPET_ABI_H
#define PUPPET_ABI_H

#define PUP_ANCHOR_PHYS   0x80000u    /* reserved from the kernel with memmap=64K$0x80000 */
#define PUP_ANCHOR_SIZE   0x10000u
#define PUP_ABI_VERSION   2u          /* v2: loader ops (PUT..KEXEC) + PUP_ID_GEN */

/* byte offsets in the anchor */
#define PUP_MAGIC        0x0000      /* "86PUPPET" */
#define PUP_VERSION      0x0008
#define PUP_STATE        0x000C
#define PUP_CMD_SEQ      0x0010      /* host writes it last */
#define PUP_CMD_OP       0x0014
#define PUP_CMD_FLAGS    0x0018
#define PUP_CMD_LEN      0x001C
#define PUP_CMD_ARGS     0x0020      /* u32[8] */
#define PUP_RSP_SEQ      0x0040      /* doorbell word: guest writes it last */
#define PUP_RSP_STATUS   0x0044
#define PUP_RSP_LEN      0x0048
#define PUP_RSP_CRC      0x004C
#define PUP_RSP_VALS     0x0050      /* u32[8] */
#define PUP_HEARTBEAT    0x0070
#define PUP_BULK_PHYS    0x0074
#define PUP_BULK_SIZE    0x0078
#define PUP_BOOT_STAGE   0x007C
/* identity block, filled at boot and by INFO */
#define PUP_ID_BUILD     0x0080      /* char[32] */
#define PUP_ID_KERNEL    0x00A0      /* char[32], uname release */
#define PUP_ID_CPU       0x00C0      /* char[56], /proc/cpuinfo "model name" (or "cpu family") */
#define PUP_ID_CPU_LEN   56
#define PUP_MB_LINEAR    0x00F8      /* u32, the guest linear address the puppet maps the anchor at (doorbell watch) */
#define PUP_ID_GEN       0x00FC      /* u32, puppet.gen=N from /proc/cmdline (0 if absent): a kexec'd boot shows N+1 */
#define PUP_ID_MEM_KB    0x0100      /* u32, MemTotal */
#define PUP_ID_PCI_N     0x0104      /* u32, entries that follow */
#define PUP_ID_PCI       0x0108      /* {u32 bus<<8|devfn, u32 vendor<<16|device}[PUP_ID_PCI_MAX] */
#define PUP_ID_PCI_MAX   ((0x0400 - 0x0108) / 8)
#define PUP_PAYLOAD      0x0400      /* command payload in, response payload out */
#define PUP_PAYLOAD_MAX  (PUP_ANCHOR_SIZE - PUP_PAYLOAD)

#define PUP_ST_BOOTING   0u
#define PUP_ST_READY     1u
#define PUP_ST_BUSY      2u
#define PUP_ST_FAULT     3u

#define PUP_OP_PING      1u          /* vals[0] = heartbeat, vals[1] = args[0] */
#define PUP_OP_INFO      2u          /* refreshes the identity block; vals[0] = pci entries, vals[1] = MemTotal kB */
#define PUP_OP_ECHO      3u          /* payload returned as is; vals[0] = crc32 of what the guest read */

/* loader (plan 2.3). Status = errno of the failing syscall. */
#define PUP_OP_PUT       4u          /* payload path\0data; args offset, flags, mode; vals bytes, size, file crc (FINAL) */
#define PUP_OP_GET       5u          /* payload path\0; args offset, max; payload out = data; vals bytes, size */
#define PUP_OP_EXEC      6u          /* payload cwd\0argv..\0\0env..; out -> /pp/run/<pid>.out; vals pid */
#define PUP_OP_WAIT      7u          /* args pid, timeout ms, out offset, out max; vals state, code, out size, run ms */
#define PUP_OP_KILL      8u          /* args pid, signal */
#define PUP_OP_RM        9u          /* payload path\0: unlink, else rmdir */
#define PUP_OP_KEXEC     10u         /* payload kernel\0initrd\0cmdline\0; args bit0 execute; vals segments, kernel bytes */
#define PUP_PUT_CREATE   1u          /* O_TRUNC on open */
#define PUP_PUT_FINAL    2u          /* chmod to args[2], CRC the whole file into vals[2] */
#define PUP_KEXEC_RUN    1u
#define PUP_KID_RUNNING  0u
#define PUP_KID_EXITED   1u
#define PUP_KID_SIGNALED 2u
#define PUP_KIDS_MAX     8

#define PUP_E_OK         0u
#define PUP_E_NOSYS      38u
#define PUP_E_INVAL      22u
#define PUP_E_NOENT      2u
#define PUP_E_SRCH       3u
#define PUP_E_NOEXEC     8u
#define PUP_E_BUSY       16u

#endif
