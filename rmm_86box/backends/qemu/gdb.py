# SPDX-License-Identifier: MIT
"""Placeholder: registers, memory and breakpoints over QEMU's gdbstub (start with gdb_port=).

Plan: a GDB remote-protocol client (packets $..#cs, acks, qSupported, g/G, m/M, Z0/z0,
Z2-Z4 watchpoints, c/s, stop replies) giving the RMM methods cpu.regs.get, mem.phys.read/write,
bp.*, step. Register layout per profile arch (i386, m68k, sparc) from the target XML
(qXfer:features:read). While gdb holds the CPU, QMP query-status says "debug"; the backend
reports that as paused.
"""


class GdbStub:
    def __init__(self, port, arch):
        self.port, self.arch = port, arch

    def connect(self):
        raise NotImplementedError("gdbstub debugging is not built yet")
