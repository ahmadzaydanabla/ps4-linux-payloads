#!/usr/bin/env python3
#
# PS4 Liverpool/Gladius GPU clock and UVD diagnostic reader.
#
# Run on Linux after booting through ps4-linux-payloads:
#   sudo python3 tools/read_ps4_gpu_clocks.py
#
# This intentionally writes only the SMC indirect index register in order to
# read Sony/AMD clock control registers. It does not program clocks.

import argparse
import json
import mmap
import os
import struct
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DEVICE = "0000:00:01.0"
DEFAULT_BAR = 5
DEFAULT_SPLL_MHZ = 800.0
TOOL_VERSION = "2026-04-24.2"

SMC_IND_INDEX_0 = 0x0080
SMC_IND_DATA_0 = 0x0081

PS4_UVD_CLOCK_MAGIC = 0x55564432
PS4_UVD_PROBE_MAGIC = 0x55564450


@dataclass(frozen=True)
class MmioReg:
    name: str
    reg: int
    notes: str = ""


@dataclass(frozen=True)
class SmcReg:
    name: str
    addr: int
    kind: str = "generic"
    notes: str = ""


MMIO_REGS = [
    MmioReg("GRBM_STATUS", 0x2004),
    MmioReg("GRBM_STATUS2", 0x2002),
    MmioReg("UVD_STATUS", 0x3DAF),
    MmioReg("UVD_SOFT_RESET", 0x3D3D),
    MmioReg("UVD_POWER_STATUS", 0x3D4C),
    MmioReg("UVD_PGFSM_STATUS", 0x3D20),
    MmioReg("UVD_PGFSM_CONFIG", 0x3D21),
    MmioReg("UVD_CGC_STATUS", 0x3D2B),
    MmioReg("UVD_CGC_CTRL", 0x3D2C),
    MmioReg("UVD_CGC_GATE", 0x3D2D),
    MmioReg("UVD_LMI_CTRL", 0x3D65),
    MmioReg("UVD_LMI_STATUS", 0x3D66),
    MmioReg("UVD_VCPU_CNTL", 0x3D4A),
    MmioReg("BIOS_SCRATCH_10", 0x05D3, "kexec UVD probe marker"),
    MmioReg("BIOS_SCRATCH_11", 0x05D4, "kexec UVD d-first base probe"),
    MmioReg("BIOS_SCRATCH_12", 0x05D5, "kexec UVD v-first base probe"),
    MmioReg("BIOS_SCRATCH_13", 0x05D6, "kexec UVD v-first high probe"),
    MmioReg("BIOS_SCRATCH_14", 0x05D7, "kexec UVD clock marker"),
    MmioReg("BIOS_SCRATCH_15", 0x05D8, "kexec DCLK/VCLK return codes"),
]

SMC_REGS = [
    SmcReg("CG_SPLL_FUNC_CNTL", 0xC0500140, "pll"),
    SmcReg("CG_SPLL_FUNC_CNTL_2", 0xC050004C, "generic"),
    SmcReg("CG_SPLL_FUNC_CNTL_3", 0xC0500050, "generic"),
    SmcReg("CG_SCLK_CNTL", 0xC050008C, "clock"),
    SmcReg("CG_ACLK_CNTL", 0xC0500094, "clock"),
    SmcReg("CG_DCLK_CNTL", 0xC050009C, "clock", "UVD decode clock control"),
    SmcReg("CG_DCLK_STATUS", 0xC05000A0, "status", "not ECLK"),
    SmcReg("CG_VCLK_CNTL", 0xC05000A4, "clock", "UVD video clock control"),
    SmcReg("CG_VCLK_STATUS", 0xC05000A8, "status"),
    SmcReg("CG_ECLK_CNTL", 0xC05000AC, "clock", "VCE encode clock control"),
    SmcReg("GCK_DFS_BYPASS_CNTL", 0xC0500118, "generic"),
]


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def read_resource_table(device: str):
    path = Path("/sys/bus/pci/devices") / device / "resource"
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        die(f"PCI device {device!r} not found at {path}")

    bars = []
    for index, line in enumerate(lines[:7]):
        parts = line.split()
        if len(parts) < 3:
            continue
        start = int(parts[0], 16)
        end = int(parts[1], 16)
        flags = int(parts[2], 16)
        size = end - start + 1 if end >= start and start else 0
        bars.append((index, start, end, size, flags))
    return bars


def resource_path(device: str, bar: int) -> Path:
    return Path("/sys/bus/pci/devices") / device / f"resource{bar}"


class GpuMmio:
    def __init__(self, path: Path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_SYNC)
        self.size = os.path.getsize(path)
        if self.size <= 0:
            os.close(self.fd)
            die(f"{path} has size 0; wrong BAR?")
        self.map = mmap.mmap(self.fd, self.size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)

    def close(self):
        self.map.close()
        os.close(self.fd)

    def read32_offset(self, offset: int) -> int:
        if offset < 0 or offset + 4 > self.size:
            raise ValueError(f"offset 0x{offset:x} outside mapped BAR size 0x{self.size:x}")
        return struct.unpack_from("<I", self.map, offset)[0]

    def write32_offset(self, offset: int, value: int) -> None:
        if offset < 0 or offset + 4 > self.size:
            raise ValueError(f"offset 0x{offset:x} outside mapped BAR size 0x{self.size:x}")
        struct.pack_into("<I", self.map, offset, value & 0xFFFFFFFF)

    def read_reg(self, reg: int) -> int:
        return self.read32_offset(reg * 4)

    def write_reg(self, reg: int, value: int) -> None:
        self.write32_offset(reg * 4, value)

    def read_smc(self, addr: int) -> int:
        self.write_reg(SMC_IND_INDEX_0, addr)
        return self.read_reg(SMC_IND_DATA_0)


def signed16(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def signed6(value: int) -> int:
    value &= 0x3F
    return value - 0x40 if value & 0x20 else value


def decode_clock_value(value: int, spll_mhz: float) -> dict:
    div = value & 0x7F
    dir_en = bool(value & 0x100)
    dir_div = (value >> 9) & 0x7F
    effective_div = dir_div if dir_en and dir_div else div
    approx_mhz = spll_mhz / effective_div if effective_div else 0.0
    return {
        "divider": div,
        "direct_enabled": dir_en,
        "direct_divider": dir_div,
        "effective_divider": effective_div,
        "approx_mhz": approx_mhz,
    }


def print_bars(bars):
    print("=== GPU PCI BARs ===")
    for index, start, end, size, flags in bars:
        if size:
            print(f"  BAR{index}: 0x{start:012X} size={size // 1024}KB flags=0x{flags:x}")
    print()


def format_clock(value: int, spll_mhz: float) -> str:
    decoded = decode_clock_value(value, spll_mhz)
    return (
        f"DIV={decoded['divider']:2d} "
        f"dir_en={int(decoded['direct_enabled'])} "
        f"dir_div={decoded['direct_divider']:2d} "
        f"~{decoded['approx_mhz']:.1f} MHz"
    )


def decode_kexec_scratch(mmio_values: dict) -> dict:
    marker = mmio_values.get("BIOS_SCRATCH_14")
    packed = mmio_values.get("BIOS_SCRATCH_15")
    if marker is None or packed is None:
        return {}
    return {
        "marker": marker,
        "marker_ok": marker == PS4_UVD_CLOCK_MAGIC,
        "dclk_ret": signed16(packed >> 16),
        "vclk_ret": signed16(packed),
    }


def decode_probe_word(value: int) -> dict:
    return {
        "dclk": (value >> 22) & 0x3FF,
        "vclk": (value >> 12) & 0x3FF,
        "dclk_ret": signed6(value >> 6),
        "vclk_ret": signed6(value),
        "raw": value,
    }


def decode_kexec_probes(mmio_values: dict) -> dict:
    marker = mmio_values.get("BIOS_SCRATCH_10")
    if marker is None:
        return {}

    probe_regs = [
        ("d-first-base", "BIOS_SCRATCH_11"),
        ("v-first-base", "BIOS_SCRATCH_12"),
        ("v-first-high", "BIOS_SCRATCH_13"),
    ]
    probes = []
    for label, name in probe_regs:
        raw = mmio_values.get(name)
        if raw is None:
            continue
        probe = decode_probe_word(raw)
        probe["label"] = label
        probe["accepted"] = probe["dclk_ret"] == 0 and probe["vclk_ret"] == 0
        probes.append(probe)

    return {
        "marker": marker,
        "marker_ok": marker == PS4_UVD_PROBE_MAGIC,
        "probes": probes,
    }


def collect(device: str, bar: int, spll_mhz: float, no_smc: bool) -> dict:
    bars = read_resource_table(device)
    path = resource_path(device, bar)
    if not path.exists():
        die(f"{path} does not exist")

    gpu = GpuMmio(path)
    try:
        mmio = {entry.name: gpu.read_reg(entry.reg) for entry in MMIO_REGS}
        smc = {}
        if not no_smc:
            for entry in SMC_REGS:
                smc[entry.name] = gpu.read_smc(entry.addr)
    finally:
        gpu.close()

    return {
        "device": device,
        "bar": bar,
        "bar_path": str(path),
        "bar_size": os.path.getsize(path),
        "bars": [
            {"index": i, "start": start, "end": end, "size": size, "flags": flags}
            for i, start, end, size, flags in bars
        ],
        "mmio": mmio,
        "smc": smc,
        "kexec": decode_kexec_scratch(mmio),
        "probes": decode_kexec_probes(mmio),
        "spll_mhz": spll_mhz,
    }


def print_report(result: dict) -> None:
    print_bars([(b["index"], b["start"], b["end"], b["size"], b["flags"]) for b in result["bars"]])
    print(f"Mapping {result['bar_path']} (size={result['bar_size']} bytes)")
    print()

    mmio = result["mmio"]
    print("=== MMIO Register Reads ===")
    for entry in MMIO_REGS:
        value = mmio[entry.name]
        suffix = f"  ({entry.notes})" if entry.notes else ""
        print(f"  [{entry.reg:#06x}] {entry.name:<24} = 0x{value:08X}{suffix}")
    print()

    probes = result["probes"]
    print("=== Kexec UVD Clock Probe Table ===")
    if not probes:
        print("  No probe registers found.")
    else:
        print(f"  marker     = 0x{probes['marker']:08X} ({'OK' if probes['marker_ok'] else 'missing/stale'})")
        if probes["marker_ok"]:
            for probe in probes["probes"]:
                verdict = "accepted" if probe["accepted"] else "rejected"
                print(
                    f"  {probe['label']:<12} = DCLK={probe['dclk']:>3} ret={probe['dclk_ret']:>2} "
                    f"VCLK={probe['vclk']:>3} ret={probe['vclk_ret']:>2} -> {verdict} "
                    f"(raw=0x{probe['raw']:08X})"
                )
        else:
            print("  result     = Probe marker missing or stale; boot a payload with probe support.")
    print()

    kexec = result["kexec"]
    print("=== Kexec UVD Clock Request Marker ===")
    if not kexec:
        print("  No marker registers found.")
    else:
        print(f"  marker     = 0x{kexec['marker']:08X} ({'OK' if kexec['marker_ok'] else 'missing/stale'})")
        print(f"  DCLK ret   = {kexec['dclk_ret']}")
        print(f"  VCLK ret   = {kexec['vclk_ret']}")
        if kexec["marker_ok"] and kexec["dclk_ret"] == 0 and kexec["vclk_ret"] == 0:
            print("  result     = Sony accepted both final clock requests.")
        elif kexec["marker_ok"]:
            print("  result     = At least one final set_gpu_freq call returned failure.")
    print()

    smc = result["smc"]
    if smc:
        print("=== SMC Indirect Clock Register Reads ===")
        for entry in SMC_REGS:
            value = smc[entry.name]
            detail = ""
            if entry.kind in ("clock", "pll", "status"):
                detail = format_clock(value, result["spll_mhz"])
            note_text = ""
            if entry.notes:
                note_text = f"  {entry.notes}"
            print(f"  [{entry.addr:#010x}] {entry.name:<24} = 0x{value:08X}  {detail}{note_text}")
        print()

        dclk = decode_clock_value(smc["CG_DCLK_CNTL"], result["spll_mhz"])
        vclk = decode_clock_value(smc["CG_VCLK_CNTL"], result["spll_mhz"])
        eclk = decode_clock_value(smc["CG_ECLK_CNTL"], result["spll_mhz"])
        print("=== Clock Summary ===")
        print(f"  SPLL source = {result['spll_mhz']:.1f} MHz (assumed)")
        print(f"  DCLK        = DIV {dclk['effective_divider']:2d} -> {dclk['approx_mhz']:.1f} MHz raw=0x{smc['CG_DCLK_CNTL']:08X}")
        print(f"  VCLK        = DIV {vclk['effective_divider']:2d} -> {vclk['approx_mhz']:.1f} MHz raw=0x{smc['CG_VCLK_CNTL']:08X}")
        print(f"  ECLK        = DIV {eclk['effective_divider']:2d} -> {eclk['approx_mhz']:.1f} MHz raw=0x{smc['CG_ECLK_CNTL']:08X}")
        print()

    print("=== Notes ===")
    print("  CG_DCLK_STATUS is 0xC05000A0; it is not ECLK control.")
    print("  CG_ECLK_CNTL is 0xC05000AC.")
    print("  kexec ret=0 means Sony's set_gpu_freq path accepted that clock request.")
    print("  kexec ret=1 on DCLK/VCLK means Sony's helper returned failure for that path.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read PS4 Liverpool/Gladius GPU UVD clock diagnostics.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    parser.add_argument("--device", default=DEFAULT_DEVICE, help=f"PCI device id, default {DEFAULT_DEVICE}")
    parser.add_argument("--bar", type=int, default=DEFAULT_BAR, help=f"resource BAR number, default {DEFAULT_BAR}")
    parser.add_argument("--spll-mhz", type=float, default=DEFAULT_SPLL_MHZ, help=f"assumed SPLL MHz, default {DEFAULT_SPLL_MHZ}")
    parser.add_argument("--no-smc", action="store_true", help="skip SMC indirect reads")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text report")
    args = parser.parse_args()

    if os.geteuid() != 0:
        die("run as root so /sys/bus/pci/.../resourceN can be mmaped read/write")

    result = collect(args.device, args.bar, args.spll_mhz, args.no_smc)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print_report(result)


if __name__ == "__main__":
    main()
