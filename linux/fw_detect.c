/*
 * fw_detect.c — firmware version detection via libc.sprx SCE header
 *
 * Reads bytes 4-5 of the SCE header fw_version field from libc.sprx.
 * This is the exact firmware the console is running, not the SDK target.
 *
 * Adapted from Al-Azif/ps4-skeleton (MIT).
 */

#include "fw_detect.h"

/* FreeBSD O_RDONLY / SEEK_SET come from the payload SDK's fcntl/unistd */
#ifndef O_RDONLY
#define O_RDONLY 0
#endif
#ifndef SEEK_SET
#define SEEK_SET 0
#endif

/* ------------------------------------------------------------------ *
 * Minimal SELF / ELF / SCE structs — no Sony SDK dependency          *
 * Source: https://www.psdevwiki.com/ps4/SELF_File_Format             *
 * ------------------------------------------------------------------ */

typedef struct {
    u32 props;
    u32 reserved;
    u64 offset;
    u64 file_size;
    u64 memory_size;
} SelfEntry;

typedef struct {
    u32 magic;          /* 0x1D3D154F                    */
    u8  version;
    u8  mode;
    u8  endian;
    u8  attr;
    u8  content_type;
    u8  program_type;
    u16 padding;
    u16 header_size;
    u16 signature_size;
    u64 self_size;
    u16 num_of_segments;
    u16 flags;
    u32 reserved2;
} SelfHeader;

typedef struct {
    unsigned char e_ident[16];
    u16      e_type;
    u16      e_machine;
    u32      e_version;
    u64      e_entry;
    u64      e_phoff;
    u64      e_shoff;
    u32      e_flags;
    u16      e_ehsize;
    u16      e_phentsize;
    u16      e_phnum;
    u16      e_shentsize;
    u16      e_shnum;
    u16      e_shstrndx;
} Elf64_Ehdr;

typedef struct {
    u64      program_authority_id;
    u64      program_type;
    u64      app_version;
    u64      fw_version;   /* target field: bytes 4-5 = major.minor */
    unsigned char digest[0x20];
} SceHeader;

/* Syscall wrappers — provided by the ps4-payload-sdk libc layer */
extern int      open(const char *path, int flags, ...);
extern int      close(int fd);
extern long     read(int fd, void *buf, unsigned long nbytes);
extern long     lseek(int fd, long offset, int whence);

/* ------------------------------------------------------------------ *
 * Implementation                                                      *
 * ------------------------------------------------------------------ */

static u16 g_firmware = 0;

u16 get_firmware(void)
{
    if (g_firmware)
        return g_firmware;

    int fd = open("/system/common/lib/libc.sprx", O_RDONLY, 0);
    if (fd < 0)
        return 0;

    /* --- Read SELF header ----------------------------------------- */
    SelfHeader self_hdr;
    lseek(fd, 0, SEEK_SET);
    if (read(fd, &self_hdr, sizeof(self_hdr)) != (long)sizeof(self_hdr))
        goto fail;

    /* --- Calculate ELF header offset ------------------------------ */
    u64 elf_off = sizeof(self_hdr)
                     + (u64)self_hdr.num_of_segments * sizeof(SelfEntry);

    /* --- Read ELF header ------------------------------------------ */
    Elf64_Ehdr elf_hdr;
    lseek(fd, (long)elf_off, SEEK_SET);
    if (read(fd, &elf_hdr, sizeof(elf_hdr)) != (long)sizeof(elf_hdr))
        goto fail;

    /* --- Calculate SCE header offset (16-byte aligned) ------------ */
    u64 sce_off = elf_off + elf_hdr.e_ehsize
                     + (u64)elf_hdr.e_phnum * elf_hdr.e_phentsize;
    while (sce_off % 0x10 != 0)
        sce_off++;

    /* --- Read SCE header ------------------------------------------ */
    SceHeader sce_hdr;
    lseek(fd, (long)sce_off, SEEK_SET);
    if (read(fd, &sce_hdr, sizeof(sce_hdr)) != (long)sizeof(sce_hdr))
        goto fail;

    close(fd);

    /*
     * fw_version layout (big-endian within the 64-bit field):
     *   byte 5 (shift 40) = major   e.g. 0x09 for 9.xx
     *   byte 4 (shift 32) = minor   e.g. 0x60 for x.60
     *
     * Decode BCD nibbles → integer: major * 100 + minor  (e.g. 960).
     */
    u8 major = (u8)((sce_hdr.fw_version >> 40) & 0xFF);
    u8 minor = (u8)((sce_hdr.fw_version >> 32) & 0xFF);

    u32 maj_dec = ((major >> 4) * 10) + (major & 0xF);
    u32 min_dec = ((minor >> 4) * 10) + (minor & 0xF);

    u16 ret = (u16)(maj_dec * 100 + min_dec);
    g_firmware = ret;
    return ret;

fail:
    close(fd);
    return 0;
}

/*
 * normalize_fw_ver — map BCD alias versions to their canonical base.
 *
 * The offset table is keyed on the canonical base version.  Alias
 * versions (e.g. 7.01, 7.02) share identical kernel offsets with the
 * base (7.00) and therefore map to it.
 *
 * Note: 13.02 is a DISTINCT firmware with its own offset entry and is
 * NOT aliased to 1300 — it passes through unchanged.
 */
u16 normalize_fw_ver(u16 raw_fw)
{
    switch (raw_fw) {
        case 701:
        case 702:
            return 700;
        case 751:
        case 755:
            return 750;
        case 801:
        case 803:
            return 800;
        case 852:
            return 850;
        case 904:
            return 903;
        case 950:
        case 951:
            return 960;
        case 1001:
            return 1000;
        case 1070:
        case 1071:
            return 1050;
        case 1152:
            return 1150;
        case 1202:
            return 1200;
        case 1252:
            return 1250;
        default:
            return raw_fw;
    }
}
