# linuxpuppet: the Linux puppet (puppet_plan.md). i486 so it boots on every 486+ 86Box machine.
CONFIG_M486=y
CONFIG_M586TSC=n
# the host reads/writes the mailbox anchor through RMM; the guest maps it with /dev/mem
CONFIG_DEVMEM=y
CONFIG_STRICT_DEVMEM=n
CONFIG_IO_STRICT_DEVMEM=n
# port I/O for the video primitives (P3)
CONFIG_X86_IOPL_IOPERM=y
# floppy boot variant: the floppy driver is not needed (syslinux loads kernel + initramfs), but the
# MDA console is wanted for mono-primary video cells; it (and ISA video cards) need ISA support
CONFIG_ISA=y
CONFIG_MDA_CONSOLE=y
# vm86 for INT10/ROM_POST into the cards' own video BIOS (P3); built now so P0 proves it links
CONFIG_X86_LEGACY_VM86=y
# loader (puppet_plan.md 2.3): KEXEC swaps the whole kernel without the BIOS; the puppet builds the
# boot_params itself (no kexec-tools, no libc). KEXEC_FILE is x86_64-only, so it is the classic kexec_load.
CONFIG_KEXEC=y
# tinyconfig drops the 32-bit time syscalls: nanosleep (162) is then ENOSYS and every sleep in the puppet and in
# personalities returns at once (found by the P1b spin/cmos tests). gettimeofday is unaffected.
CONFIG_COMPAT_32BIT_TIME=y
