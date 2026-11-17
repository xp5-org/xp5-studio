# fragment applied on top of `make ARCH=i386 tinyconfig`: the card-neutral base of the Linux puppet
# (copied from r128_mesa/linux/kernel.frag without the Rage 128 DRM driver; the stage .frag adds the rest)
CONFIG_M686=n
CONFIG_M586TSC=y
CONFIG_KERNEL_XZ=n
CONFIG_PCI=y
CONFIG_PRINTK=y
CONFIG_TTY=y
CONFIG_VT=y
CONFIG_VT_CONSOLE=y
CONFIG_VGA_CONSOLE=y
CONFIG_SERIAL_8250=y
CONFIG_SERIAL_8250_CONSOLE=y
CONFIG_BINFMT_ELF=y
CONFIG_BLK_DEV_INITRD=y
CONFIG_DEVTMPFS=y
CONFIG_PROC_FS=y
CONFIG_SYSFS=y
CONFIG_MMU=y
CONFIG_MAGIC_SYSRQ=n
CONFIG_PRINTK_TIME=y
CONFIG_ACPI=n
CONFIG_KERNEL_GZIP=y
# tinyconfig links the kernel at 16 MB; the machine has 16 MB RAM total, so link it at 1 MB (where the boot loader puts it).
# CONFIG_RELOCATABLE=y was tried: the decompressor halts with "32-bit relocation outside of kernel!" (gcc 14 / new binutils)
CONFIG_RELOCATABLE=n
CONFIG_PHYSICAL_START=0x100000
