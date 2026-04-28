# PS4 UVD Clock Memory

This file is here so future work does not repeat already-solved confusion.

## Goal

Get PS4 Liverpool/Gladius UVD decode working at a real hardware clock under Linux.

Current bad state: UVD can initialize, but decode is effectively useless because UVD clocks stay around 40 MHz.

The user has already tried direct Linux-side clock pokes. `liverpool_clk.c` style writes are not enough because SAMU/SMC firmware ownership overrides or rejects the clock state. Do not assume simply enabling UVD in the kernel fixes this.

## Hardware Clock Names

- `DCLK` is the UVD decode clock. This is the main throughput clock for decode.
- `VCLK` is the UVD video clock.
- `ECLK` is VCE encode clock, not UVD decode.

Important correction:

- `0xC05000A0` is `CG_DCLK_STATUS`, not ECLK.
- `0xC05000AC` is `CG_ECLK_CNTL`.

## Known SMC Clock Registers

- `CG_SPLL_FUNC_CNTL`: `0xC0500140`
- `CG_SPLL_FUNC_CNTL_2`: `0xC050004C`
- `CG_SPLL_FUNC_CNTL_3`: `0xC0500050`
- `CG_SCLK_CNTL`: `0xC050008C`
- `CG_ACLK_CNTL`: `0xC0500094`
- `CG_DCLK_CNTL`: `0xC050009C`, UVD decode clock control
- `CG_DCLK_STATUS`: `0xC05000A0`, status, not ECLK
- `CG_VCLK_CNTL`: `0xC05000A4`, UVD video clock control
- `CG_VCLK_STATUS`: `0xC05000A8`
- `CG_ECLK_CNTL`: `0xC05000AC`, VCE encode clock control
- `GCK_DFS_BYPASS_CNTL`: `0xC0500118`

The Python reader assumes SPLL source `800 MHz` for rough divider math.

## Known UVD MMIO Registers

- `GRBM_STATUS`: `0x2004`
- `GRBM_STATUS2`: `0x2002`
- `UVD_STATUS`: `0x3DAF`
- `UVD_SOFT_RESET`: `0x3D3D`
- `UVD_POWER_STATUS`: `0x3D4C`
- `UVD_PGFSM_STATUS`: `0x3D20`
- `UVD_PGFSM_CONFIG`: `0x3D21`
- `UVD_CGC_STATUS`: `0x3D2B`
- `UVD_CGC_CTRL`: `0x3D2C`
- `UVD_CGC_GATE`: `0x3D2D`
- `UVD_LMI_CTRL`: `0x3D65`
- `UVD_LMI_STATUS`: `0x3D66`
- `UVD_VCPU_CNTL`: `0x3D4A`

## Scratch Register Protocol Used By Our Payload

The kexec payload writes diagnostics into GPU BIOS scratch registers so Linux can read them through BAR5.

- `BIOS_SCRATCH_10`: `0x05D3`, probe marker
- `BIOS_SCRATCH_11`: `0x05D4`, base probe result
- `BIOS_SCRATCH_12`: `0x05D5`, mid probe result
- `BIOS_SCRATCH_13`: `0x05D6`, high probe result
- `BIOS_SCRATCH_14`: `0x05D7`, final UVD clock marker
- `BIOS_SCRATCH_15`: `0x05D8`, final DCLK/VCLK return codes

Markers:

- Probe marker: `0x55564450`, ASCII-ish `UVDP`
- Final marker: `0x55564432`, ASCII-ish `UVD2`

Probe encoding:

```c
((dclk & 0x3ff) << 22)
| ((vclk & 0x3ff) << 12)
| ((dclk_ret & 0x3f) << 6)
| (vclk_ret & 0x3f)
```

Return fields are decoded as signed 6-bit values in the Python tool.

## Return Value Semantics

Do not get this backwards:

- `ret=0` means Sony's `set_gpu_freq` path accepted the request.
- `ret=1` on DCLK/VCLK means Sony's helper returned failure for that path.

Earlier speculation that a nonzero return might mean “accepted but no visible register change” was wrong for DCLK/VCLK after re-reading the public update PUP disassembly.

## Public Update PUP Reverse-Engineering Notes

The relevant public update PUP code path was reverse-read around Sony's `set_gpu_freq`.

Known function pointer in payload:

- `kern.set_gpu_freq(num, freq)`
- In `kernel.h`, this must be typed as `int (*set_gpu_freq)(unsigned int num, unsigned int freq)` so return codes are not discarded.

Known FW offset context from the public PUP analysis:

- `set_gpu_freq` entry was observed at raw PUP offset `0x631b00`.
- This mapped from kexec magic offset `0x4b0b00 + 0x181000`.
- Jump table was observed at `0x98d6a8`.
- Domain `1` maps to the DCLK path around `0x631cb2`.
- Domain `6` maps to the VCLK path around `0x631d69`.

DCLK path behavior:

- Reads `0xC050009C`.
- Clears bit `0x100` with `and 0xfffffeff`.
- Writes back through Sony's L2/SMC write helper around `0x79c190`.
- Calls helper around `0x633850(freq * 100, 0xC050009C, 0xC05000A0)`.
- After the helper call, code does roughly `cmp eax, -1; sete bl`.
- Therefore it returns `1` when helper `0x633850` returned `-1`.

VCLK path is analogous:

- Uses control `0xC05000A4`.
- Uses status `0xC05000A8`.

Helper `0x633850` behavior:

- Waits for status bit 0 through Sony's L2 read helper around `0x79c070`.
- Calls a table/script interpreter around `0x633c90` for the requested frequency.
- Returns `0xffffffff` on interpreter failure or write failure.
- On success, writes only the low 7 bits of the clock control value, derived from the computed result high byte:
  - read CNTL
  - `old & 0xffffff80 | (computed >> 24)`
  - write through L2/SMC write helper

Important implication:

- If `DCLK_STATUS` bit 0 is set but DCLK still returns `1`, the failure is probably not the simple status-ready wait.
- The likely failure is the table/script interpreter rejecting the request, or the L2/SMC write path refusing DCLK because a precondition is missing.

Sony mediated L2/SMC register access from the public 11.00 PUP:

- Read helper: raw `0x79c070`, magic offset `0x61b070`.
- Write helper: raw `0x79c190`, magic offset `0x61b190`.
- The helpers use command registers around `0x22070`.
- Read command writes command ID `0xa404`.
- Write command writes command ID `0xa505`.
- This path is not normal Linux MMIO. It is the firmware-mediated path Sony uses before touching `0xC050....` clock regs.

AMD uBIOS clock setup also writes clock control/status pairs directly through its own mediated path:

- ACLK: control `0xC0500094`, status `0xC0500098`
- DCLK: control `0xC050009C`, status `0xC05000A0`
- VCLK: control `0xC05000A4`, status `0xC05000A8`
- ECLK: control `0xC05000AC`, status `0xC05000B0`

This suggests a possible workaround if `set_gpu_freq` keeps rejecting DCLK:

- Use Sony's L2 write helper directly to program DCLK/VCLK control dividers.
- This is different from raw Linux MMIO pokes and may satisfy SAMU/SMC ownership better.

## Known Runtime Evidence

Last known output after testing commit `63f2af3` was unchanged from before:

```text
BIOS_SCRATCH_10 = 0x55564450
BIOS_SCRATCH_11 = 0xA86C7040
BIOS_SCRATCH_12 = 0xC8320041
BIOS_SCRATCH_13 = 0xD57D8041
BIOS_SCRATCH_14 = 0x55564432
BIOS_SCRATCH_15 = 0x00010001
```

Decoded:

```text
base = DCLK=673 ret=1 VCLK=711 ret=0
mid  = DCLK=800 ret=1 VCLK=800 ret=1
high = DCLK=853 ret=1 VCLK=984 ret=1
```

Same boot clock state:

```text
CG_DCLK_CNTL   = 0x00000015 -> DIV 21 -> ~38.1 MHz
CG_DCLK_STATUS = 0x00000003
CG_VCLK_CNTL   = 0x00000013 -> DIV 19 -> ~42.1 MHz
CG_VCLK_STATUS = 0x00000003
CG_ECLK_CNTL   = 0x00000016 -> DIV 22 -> ~36.4 MHz
```

Interpretation:

- The probe code definitely ran.
- VCLK base `711` can be accepted.
- DCLK base `673` is rejected.
- Replaying the full GPU clock ladder after the late GPU reset did not change the result.
- VCLK-first ordering also did not change the result:
  - `d-first-base = DCLK=673 ret=1 VCLK=711 ret=0`
  - `v-first-base = DCLK=673 ret=1 VCLK=711 ret=0`
  - `v-first-high = DCLK=853 ret=1 VCLK=984 ret=1`
  - final VCLK returned `0`, final DCLK still returned `1`.
- Therefore the current blocker is DCLK-specific, not “all UVD clocks are impossible.”

Latest result from commit `0ebbf9d Try mediated UVD clock divider writes`:

```text
cgc0-base   = DCLK=673 ret=1  VCLK=711 ret=0
cgc18c-base = DCLK=673 ret=1  VCLK=711 ret=0
l2-div1     = DCLK=1   ret=-13 VCLK=1   ret=-13
final       = DCLK ret=-13 VCLK ret=-13
```

Post-boot registers still:

```text
CG_DCLK_CNTL = 0x00000015 -> DIV 21 -> ~38.1 MHz
CG_VCLK_CNTL = 0x00000013 -> DIV 19 -> ~42.1 MHz
```

Interpretation of `-13`:

- The optional 11.00 helper pointers resolved. If they were missing, the direct path would return `-2`.
- `-13` happened in the read-modify-write direct path before the write attempt, because `direct_smc_clock_div()` returns immediately when `smc_read_reg()` fails.
- Next probe should test write-only through Sony's mediated write helper to find out whether only direct reads are denied or writes are denied too.

Latest result from commit `585fbac` plus build fix `41c92d8`:

```text
cgc0-base   = DCLK=673 ret=1  VCLK=711 ret=0
cgc18c-base = DCLK=673 ret=1  VCLK=711 ret=0
l2-write1   = DCLK=1   ret=-13 VCLK=1   ret=-13
final       = DCLK ret=-13 VCLK ret=-13
```

Interpretation:

- Direct mediated write-only is denied too.
- This means the helper pointer exists, but direct use from our kexec call site is not accepted, or the helper offset/signature is not safe enough to use directly.
- Do not keep finalizing with the direct write path because it produces final `-13` for both clocks.
- Next patch should restore final clock application through `set_gpu_freq` and probe other Sony clock domains instead.

Latest result from commit `3b77656 Probe Sony clock domains for UVD`:

```text
domain-1-6 = DCLK=673 ret=1 VCLK=711 ret=0
domain-2-5 = DCLK=609 ret=0 VCLK=711 ret=0
domain-3-7 = DCLK=800 ret=0 VCLK=673 ret=0
final      = DCLK ret=1 VCLK ret=0
post-boot  = CG_DCLK_CNTL DIV 21 (~38 MHz), CG_VCLK_CNTL DIV 19 (~42 MHz)
```

Interpretation:

- Domains 2/5 and 3/7 accept requests, but they do not move the UVD DCLK/VCLK control registers after Linux boots.
- The only known failing Sony domain that matters for UVD decode clock control is still domain 1.
- The next patch should probe domain 1 across low-to-high DCLK values. If even 38/50 MHz fail, the issue is a DCLK domain state/permission precondition, not just the 673 MHz target.

Latest result from commit `ed7c16e Probe DCLK range for UVD`:

```text
dclk-38-50   = 38 ret=1, 50 ret=1
dclk-100-200 = 100 ret=0, 200 ret=0
dclk-400-673 = 400 ret=0, 673 ret=1
final         = DCLK 673 ret=1, VCLK 711 ret=0
post-boot     = CG_DCLK_CNTL DIV 32 (~25 MHz), CG_VCLK_CNTL DIV 19 (~42 MHz)
```

Interpretation:

- Domain 1 is not globally blocked. It accepts mid-range DCLK values.
- Sony's DCLK path rejects low `38/50` and high stock `673`, but accepts `100/200/400`.
- The next patch should find the upper accepted range between `400` and `673`, then finalize with the highest accepted DCLK instead of known-bad `673`.
- `CG_DCLK_CNTL` moving from DIV 21 to DIV 32 proves domain 1 writes the DCLK control register, but the chosen accepted value can still map to a bad/slow divider.

Latest result from commit `ca9d5fa Probe upper UVD DCLK range`:

```text
dclk-450-500 = 450 ret=0, 500 ret=0
dclk-550-600 = 550 ret=0, 600 ret=0
dclk-625-650 = 625 ret=0, 650 ret=1
final         = DCLK target 625, DCLK ret=0, VCLK ret=0
post-boot     = CG_DCLK_CNTL DIV 21 (~38 MHz), CG_DCLK_STATUS DIV 3 (~266 MHz)
```

Interpretation:

- Highest known accepted DCLK target is `625`; `650` fails.
- Final kexec calls now return success for both DCLK and VCLK.
- Post-boot control regs still look low, while DCLK status changed. Need to know whether kexec had good control/status values before Linux jumped, or whether amdgpu/Linux reset/clock-gate code overwrote the control regs during boot.
- Next patch adds kexec-side final snapshots of `CG_DCLK_CNTL`, `CG_DCLK_STATUS`, `CG_VCLK_CNTL`, `CG_VCLK_STATUS`, and `UVD_CGC_CTRL` into BIOS scratch regs before the kernel jump.

Latest result from commit `7146024 Capture UVD clock handoff state`:

```text
final        = DCLK target 625, DCLK ret=0, VCLK ret=0
kexec DCLK  = CNTL DIV 21 (~38 MHz), STATUS DIV 3 (~266 MHz)
kexec VCLK  = CNTL DIV 19 (~42 MHz), STATUS DIV 1 (~800 MHz)
kexec CGC   = UVD_CGC_CTRL=0x18c, UVD_SOFT_RESET later reads 0
post-boot    = same DCLK/VCLK control/status split
```

Interpretation:

- Linux is not the first clobberer. The control/status split already exists before handoff.
- Sony's accepted clock request updates the status/current divider path, but leaves the control selector at the old low divider.
- Next patch should try a raw SMC indirect write after Sony returns success: preserve the upper control bits and copy the low 7-bit divider from STATUS into CNTL for DCLK and VCLK.

Latest result from commit `1739b20 Sync UVD clock controls to status`:

```text
final        = DCLK target 625, DCLK ret=0, VCLK ret=0
kexec DCLK  = CNTL DIV 21, STATUS DIV 3
kexec VCLK  = CNTL DIV 19, STATUS DIV 1
```

Interpretation:

- Raw SMC indirect writes via `SMC_IND_DATA_0` did not update DCLK/VCLK control regs.
- Next patch should try Sony's mediated `smc_write_reg` helper again, but only after `set_gpu_freq` has accepted the clock, and log the DCLK/VCLK sync return codes in scratch2.

Boot regression after the UVD handoff experiments:

```text
amdgpu: GPU posting now...
amdgpu: gpu post error!
amdgpu: Fatal error during GPU init
```

The same dmesg/readout showed Linux was handed:

```text
UVD_SOFT_RESET=0x00000000
UVD_CGC_CTRL=0x0000018c
UVD_CGC_GATE=0x00007fff
```

Interpretation:

- Do not leave Linux with the experimental UVD gate/reset state.
- The next safety patch must restore the older amdgpu-friendly UVD state before jumping to Linux:
  - `UVD_CGC_GATE=0`
  - `UVD_CGC_CTRL=0x7ffff905`
  - `UVD_SOFT_RESET=0x130`
- Disable the mediated sync write for now and record sync as `-3`, because boot stability comes first.

## Tried So Far

- Captured return codes from `kern.set_gpu_freq` by changing its type to return `int`.
- Moved UVD clock requests to the final handoff point after late GPU reset.
- Added Linux-side reader `tools/read_ps4_gpu_clocks.py`.
- Corrected register labels:
  - `0xC05000A0` is `CG_DCLK_STATUS`.
  - `0xC05000AC` is `CG_ECLK_CNTL`.
- Probed base/mid/high DCLK/VCLK pairs.
- Replayed the full Sony-style GPU pstate/frequency/CU-gate/VDDNP ladder after reset with commit `63f2af3`.
- Tested DCLK-first vs VCLK-first ordering with commit `cc4cb4e`.

Result:

- None of the above made DCLK accept even base `673`.
- VCLK base `711` still accepts.

## Previous Patch Tested

Commit pushed:

- `63f2af3 Reapply GPU clock ladder before UVD probe`

Repo/branch:

- `ahmadzaydanabla/ps4-linux-payloads`
- `master`

What it changed:

- Added `apply_final_gpu_clocks()` in `linux/ps4-kexec-common/linux_boot.c`.
- This replays the full Sony-style pstate/frequency/CU-gate/VDDNP setup after the late GPU reset/audio setup.
- Then it runs the UVD probe table again.

Reason:

- `sys_kexec` applies the GPU clock ladder early, but `linux_boot.c` later resets/reconfigures the GPU.
- That reset may wipe dependencies needed by Sony's DCLK path.
- Re-requesting only DCLK/VCLK after reset may be too narrow.

Result:

- Test output stayed the same:
  - `DCLK=673 ret=1`
  - `VCLK=711 ret=0`
  - DCLK/VCLK control regs stayed around 40 MHz.

## Previous Patch Tested

Commit tested after the unchanged `63f2af3` result:

- `cc4cb4e Probe UVD clock order dependency`

Change:

- `BIOS_SCRATCH_11`: DCLK-first base probe, `DCLK=673` then `VCLK=711`.
- `BIOS_SCRATCH_12`: VCLK-first base probe, `VCLK=711` then `DCLK=673`.
- `BIOS_SCRATCH_13`: VCLK-first high probe, `VCLK=984` then `DCLK=853`.
- Final reapply now also uses VCLK-first order and reports the real final return codes in `BIOS_SCRATCH_15`.

Reason:

- The only accepted UVD-related request observed so far is `VCLK=711`.
- The old probe never retried `DCLK=673` after the accepted base VCLK state.
- If DCLK depends on VCLK being accepted first, `v-first-base` should flip DCLK from `ret=1` to `ret=0`.

Result:

- Ordering was not the missing precondition.
- `v-first-base` still produced `DCLK=673 ret=1 VCLK=711 ret=0`.

## Current Patch Under Test

Commit being prepared after the unchanged order test and public PUP re-check:

- Probe UVD gate/reset preconditions, then directly try Sony's mediated L2 write path for DCLK/VCLK divider 1.

Change:

- `BIOS_SCRATCH_11`: `cgc0-base`, apply `UVD_CGC_GATE=0`, `UVD_CGC_CTRL=0`, `UVD_SOFT_RESET=0`, then VCLK-first base clocks.
- `BIOS_SCRATCH_12`: `cgc18c-base`, apply `UVD_CGC_GATE=0`, `UVD_CGC_CTRL=0x0000018c`, `UVD_SOFT_RESET=0`, then VCLK-first base clocks.
- `BIOS_SCRATCH_13`: `l2-write1`, apply `UVD_CGC_GATE=0`, `UVD_CGC_CTRL=0x0000018c`, `UVD_SOFT_RESET=0`, then use Sony's direct L2 write helper only to set `CG_VCLK_CNTL` and `CG_DCLK_CNTL` to low divider `1`.
- Final reapply uses the direct L2 write-only divider-1 path.

Reason:

- User notes show UVD regs are writable and kernel stop/init leaves a broken state:
  - broken: `UVD_SOFT_RESET` nonzero, `UVD_CGC_CTRL=0x7ffff905`
  - no-UVD-IP working-ish state: `UVD_STATUS=0x3`, `UVD_SOFT_RESET=0`, `UVD_CGC_CTRL=0x1fff018d`
- Notes also say `CGC_CTRL=0`, `CGC_CTRL=0x18c`, and `CGC_GATE=0` read back correctly.
- If Sony's DCLK helper rejects because UVD is gated/reset, one of these profiles should let base DCLK return `0`.
- Direct mediated read/write and write-only attempts returned `-13`, so the direct helper path is no longer the current test.
- Adjacent Sony domains 2/5 and 3/7 accept but do not change UVD DCLK/VCLK control regs, so they are not the missing direct UVD clock path.
- Current probe checks the upper accepted DCLK range:
  - scratch11: DCLK 450/500 MHz
  - scratch12: DCLK 550/600 MHz
  - scratch13: DCLK 625/650 MHz
  - scratch2: DCLK/VCLK sync return codes, `-3` means disabled for boot safety
  - scratch4-8: final kexec-side clock/control snapshots after status-to-control sync
  - scratch9: final selected DCLK target

## Current Theory

DCLK fails because a DCLK-specific precondition is missing at the final handoff point.

Likely preconditions to investigate:

- DCLK domain still power-gated.
- UVD still in reset or partial reset.
- UVD clock gate state blocks DCLK writes.
- Pstate/SCLK/MCLK/voltage/CU gate setup is incomplete after late GPU reset.
- Sony's table/script interpreter refuses DCLK because another domain state is not ready.

Less likely:

- Wrong DCLK register address.
- Wrong domain number for DCLK.
- General SAMU total block of all UVD clocks, because VCLK base accepted once.

## Next Test

After the gate/reset precondition commit builds and boots, run:

```bash
sudo python3 read_ps4_gpu_clockss.py
```

Primary success line:

```text
dclk-450-500, dclk-550-600, or dclk-625-650 has ret=0 and final DCLK target uses the highest accepted value
```

If that happens, DCLK domain 1 is frequency/range/table sensitive, not fully blocked by SAMU. Then compare `CG_DCLK_CNTL` and actual UVD behavior at the selected final target.

## If DCLK Still Returns 1

Do not keep trying random MHz values first. The next work should probe and/or alter DCLK preconditions.

Most useful next diagnostic:

- Record the UVD gate/reset/power registers immediately before and after:
  - final GPU clock ladder
  - DCLK request
  - VCLK request

The current Linux-side reader only sees post-boot final state. If needed, extend kexec scratch logging or add a compact event log in scratch regs to capture intermediate states.

Possible next patch direction:

- Add a kexec-side DCLK precondition probe that reads/writes only known UVD gate/reset state around the DCLK request.
- Keep using Sony's `set_gpu_freq` path for actual clock changes rather than direct raw SMC writes unless the evidence shows Sony's path cannot be made to accept DCLK.

## Files To Remember

- Payload clock patch file: `linux/ps4-kexec-common/linux_boot.c`
- Kernel function table type: `linux/ps4-kexec-common/kernel.h`
- Early clock setup reference: `linux/ps4-kexec-common/kexec.c`
- Diagnostic reader: `tools/read_ps4_gpu_clocks.py`

## Current Download Command For Reader

```bash
wget -O read_ps4_gpu_clocks.py https://raw.githubusercontent.com/ahmadzaydanabla/ps4-linux-payloads/refs/heads/master/tools/read_ps4_gpu_clocks.py
```

The user may have locally named it `read_ps4_gpu_clockss.py`; that typo is okay as long as it is the updated file.
