# ARM64 Wine/FEX, DXVK and KosmicKrisp investigation

Source audit and isolated build experiment: 2026-10-13. No ARM64 Wine runtime
is installed locally, and no end-to-end Wine/FEX/DXVK test was run. Native KK
and loader tests and an unmodified upstream ARM64EC DXVK build have now passed.

## Finding

Dappermint already provides much of the native-host foundation. Extending it
to KosmicKrisp should start as an isolated integration experiment rather than
a new CPU translator or a custom cross-process graphics protocol. Native DXVK
is a second milestone, not a prerequisite to testing native KK.

The native lane uses FEX rather than Rosetta. Apple’s restriction on directly
mixing ARM64 and x86_64 Mach-O libraries still applies to our Rosetta lane;
the native lane implements the Windows guest boundary through Wine and FEX.

## Sources examined

| Component | Revision or source |
|---|---|
| Runtime recipes | [winecx-gptk `dad361ad`](https://github.com/dappermint/winecx-gptk/tree/dad361ad7fe7744d36c4e8378d55c507dc58789c/arm64) |
| Native Wine branch | [winecx `arm64-1117`, `c8ef4872`](https://github.com/dappermint/winecx/tree/c8ef4872c1d53120b80aa92185fc7e295d94d092) |
| FEX macOS fork | [FEX `4efc3abc`](https://github.com/dappermint/FEX/tree/4efc3abc8aca8a0d3a8aca1f153ef6c4299f4426) |
| Current DXVK payload | [Upstream DXVK 3.1.1 release](https://github.com/doitsujin/dxvk/releases/tag/v3.1.1), x32/x64 |
| ARM64EC DLL interoperability | [Microsoft ARM64EC documentation](https://learn.microsoft.com/en-us/windows/arm/arm64ec) |

The native Wine branch is separate from the Wine 11.19 pin in the ordinary
runtime workflow. Do not compare lanes as if they already use identical Wine
revisions or dependency sets.

## What already exists

- `arm64/build-arm64-unix.sh` configures Wine with
  `--enable-archs=arm64ec,aarch64` and requires an ARM64EC-capable LLVM-MinGW
  toolchain. The native Unix host is ARM64.
- `arm64/package-arm64.sh` carries FEX's `libarm64ecfex.so`,
  `libwow64fex.so`, `xtajit64.dll` and `xtajit.dll` from an existing native
  runtime. It also carries CrossOver's signed `wine.app` loader.
- Native graphics are already exercised through ARM64EC DXMT and its ARM64
  `winemetal.so`, with build/install scripts in the same directory.
- The native Wine branch still implements `CX_LIBVULKAN` in
  `dlls/win32u/vulkan.c`. With `CX_ACTIVE_GRAPHICS_BACKEND=wined3d`, it
  `dlopen`s the selected host Vulkan library. This is the mechanism used by
  our existing KK launcher.
- Wine provides the Windows Vulkan entry points and Unix-call bridge through
  `winevulkan`. Consequently, using that bridge is the first approach to
  investigate; porting FEX's Linux Vulkan thunk library is not automatically
  a prerequisite. This is an architectural inference, not an executed test.
- Our native ARM64 KK build exists and passed standalone GPU probes. That
  establishes the native driver independently, not its Wine/FEX integration.

## What is missing

### Native loader and driver packaging

The current KosmicKrisp build recipe hardcodes x86_64 for both its driver and
Vulkan loader and rejects other architectures. It cannot be reused unchanged
for the ARM64 lane. Build and package matching ARM64 libraries, dependency
closure, relative ICD manifest, signing and provenance in a separate artifact.
The launcher’s environment-selection mechanism is promising, but must be
tested against the native loader and the native Wine branch.

### DXVK architecture

The current upstream DXVK release payload supplies x86/x64 Windows DLLs.
Those can provide an initial translated-DXVK control condition if FEX/Wine
Vulkan interoperability works.

For native DXVK callable by an x64 Windows game, target ARM64EC PE DLLs,
not ordinary ARM64 PE DLLs and not macOS Mach-O libraries. Microsoft documents
ARM64EC interoperability with x64 code. DXVK Native is a separate application
porting configuration and is not a replacement for these Wine DLLs.

Try a source build of the pinned upstream DXVK release with an ARM64EC
LLVM-MinGW cross file before assuming a permanent DXVK fork is needed.
Upstream's checked-in cross files are x86/x64. Third-party ARM64EC build recipes
exist, but they are leads to reproduce, not proof of support on this macOS lane.
The experiment below installs the ARM64EC toolchain in its own working
directory without changing the system toolchain or existing runtime.

### 32-bit D3D9

Do not infer 32-bit support from a successful x64 ARM64EC run. The native Wine
build recipe names only ARM64EC and AArch64, while packaging carries a WOW64
FEX bridge. The documented verification script tests .NET and one DXMT game,
not our x86 Vulkan/D3D9 probes or the required i386 builtin-DLL inventory.

An ARM64EC DXVK DLL is a 64-bit interface, so it does not directly replace the
32-bit DXVK DLL in a 32-bit game. Initially expect that DLL to remain x86 code
executed by FEX, with Vulkan calls crossing Wine's 32/64-bit bridge to native
KK. Validate this path separately. This matters because much of the project's
D3D9 motivation is 32-bit software.

### Bootstrapping and distribution

The packaging script is a local development recipe with hardcoded toolchain
paths and an existing ARM64 runtime as input. It does not build a complete
redistributable native runtime from pinned sources.

The Wine branch's `free_pagezero()` implementation releases ARM64's low-address
reservation and reports failure without
`com.apple.developer.cross-architecture-support`. The packaging script preserves
CrossOver's signed loader instead of replacing it. The project documents this
entitlement as a distribution constraint. Reusing the recipe locally does not
establish that we can distribute a self-built loader or the copied components.

### Performance and compatibility

Dappermint's FEX macOS Unix helpers report hardware TSO and kernel unaligned
atomic controls as unsupported. Its commit describes fallback to software
handling. Moving graphics code native may reduce translation overhead, but
changing game-code translation from Rosetta to FEX can offset those gains.
Benchmark the complete stack; do not promise a net speedup from architecture
alone.

## Executed experiment

Artifacts are isolated under
`/Users/niltonperimneto/Projects/kk-shader-build/arm64-native-graphics/`.
The current Rosetta runtime and live bottles were not modified.

| Check | Result |
|---|---|
| ARM64 Khronos loader | Built from pinned `32fcb949`, against headers `b5c8f996`, macOS deployment target 26.0 |
| Native KK bundle | ARM64-only KK, loader and SPIRV-Tools dependency staged in `bundle relocated/`; non-system dependency rewritten to `@loader_path`; ad-hoc signatures verified |
| Host loader/ICD probe | Passed enumeration and device creation using the relative ICD in the path with spaces |
| Native GPU rendering probe | Passed the existing point-mode rendering suite, including flat/provoking cases |
| Windows probes | Built Vulkan, D3D9 and D3D11 probes for ARM64EC, x64 and x86; Vulkan probes compiled with warnings treated as errors |
| Upstream DXVK 3.1.1 | All five DLLs built from `b1a1c99a` without source patches; verified `COFF-ARM64EC`, machine `0xA641` and CHPE metadata using LLVM |
| Wine/FEX execution | Not run: no native runtime, FEX installation or entitled CrossOver launcher was found locally |

The toolchain is LLVM-MinGW `20260616`, matching the native recipe's release.
Its macOS universal archive was verified against SHA-256
`2cab02a2e964bd4aae981150a45985d07c657cfa8d244959eb9e2dcc5eedd7b1`.
The local `arm64ec.ini` selects its ARM64EC Windows compilers and identifies
the Meson host as Windows/AArch64. No custom DXVK C/C++ patch was required.
The build emitted compiler warnings, so this is not a warning-free-build claim.

The installed native DLL payload is in `dxvk-arm64ec/bin/`, with its license
and `SOURCE.json`. `experiment.json` records source/toolchain identities,
DLL and bundle hashes, probe architectures and the untested Wine/FEX status.
Stock DXVK x32/x64 control DLLs are staged separately in `dxvk-stock-control/`.
Do not install the ARM64EC DLLs in a Rosetta Wine bottle.

The KK bundle was assembled from the existing local development build. The
recorded checkout was `ec28f2abc7c6ba74d76b51a2d70991e4313beb9b`; bundle hashes
identify the actual binaries. This does not replace a clean, reproducible
native driver build or establish a distribution artifact. Native probe output
also reports absent transform-feedback support, so device creation is not a
claim that KK meets every upstream DXVK supported-driver requirement.

The D3D probes compiled but were not executed. `file` labels the ARM64EC
probe as x86-64, so use LLVM's COFF header and CHPE output to establish its
actual ARM64EC code rather than relying on `file` alone.

Runtime acquisition was also checked. CodeWeavers announced native ARM64
Wine/FEX in [CrossOver Preview](https://www.codeweavers.com/blog/mjohnson/2026/7/31/crossover-preview-the-right-to-bear-arm64-on-mac).
The [official Preview download](https://www.codeweavers.com/preview/download)
redirects unauthenticated access to the CodeWeavers sign-in page. No native
runtime or entitled launcher was found in installed applications, runtime
directories or downloads. The inspected public runtime releases supply the
existing x86_64 lane; the native recipe needs a previously working ARM64 runtime.
Compiling Wine/FEX locally does not supply Apple's restricted launcher
entitlement. However, that does not prevent source-building the components:
the follow-up experiment below does so and tests the self-built launcher.

### Follow-up: source-building Wine and FEX

Following the [FEX ARM64EC guide](https://wiki.fex-emu.com/index.php/Development:ARM64EC),
both Windows emulation modules were built from dappermint/FEX `4efc3abc`
without source patches: `libarm64ecfex.dll` and `libwow64fex.dll`. LLVM confirms
ARM64EC and ARM64 machine identifiers respectively. Both native ARM64 Unix
helper libraries also compiled from the fork's existing Darwin implementation.
The helper libraries were compiled directly on macOS because the UnixLib
CMake recipe links Linux's `rt` library. No FEX source changes were made or
published; its AGENTS.md forbids AI-generated code contributions.

Wine `arm64-1117` at `c8ef4872` configured for ARM64EC, AArch64 and i386 with
the pinned ARM64 Vulkan headers/loader detected. Its ARM64 loader, Unix ntdll,
Windows ntdll and wineserver built successfully. The launcher runs locally and
prints `wine-11.17`. A full Wine installation has not been built: startup was
tested at the core stage before spending time on the remaining modules.

The self-built launcher's guest startup fails before executing the probe:

```text
free_pagezero released the pagezero, 0x100000000 bytes at 0
NtAllocateVirtualMemory ... 0x7ffe0000 00001000 ...
map_fixed_area out of memory for 0x7ffe0000-0x7ffe1000
wine: failed to map the shared user data: c0000017
```

The first attempt also lacked NLS files. Those were then built, and the same
low-address failure remained. Contrary to the earlier assumption, releasing
pagezero reports success; the observed blocker is the subsequent mapping of
Windows shared user data. The self-built launcher has no cross-architecture
entitlement. This is consistent with the documented native address-space
constraint, but an entitled-loader comparison is still needed to establish
that signing alone resolves this specific failure.

Logs and hashes are recorded under the isolated experiment directory in
`guest-startup-with-nls.log` and `experiment.json`. These results establish
that the guide can build the Wine/FEX components on this machine with the
macOS adaptations. They do not establish FEX guest execution, Vulkan through
Wine, DXVK device creation or gameplay. No live bottle or installed runtime
was modified.

## Smallest validation sequence

1. Obtain a working, appropriately licensed native Wine/FEX installation with
   the entitled loader. Record the Wine, FEX, toolchain and loader identities.
   Use a disposable prefix, leaving the Rosetta runtime and real bottles alone.
2. The isolated loader/KK build and host creation/rendering checks now pass.
   Add a clean reproducible native-driver build, compute readback and host
   presentation coverage before treating this stage as release validation.
3. Run the existing x64 Windows Vulkan probe through FEX/native Wine, selecting
   the native loader with the existing environment override. Verify KK is the
   selected ICD. Repeat for x86 and inventory missing WOW64 components.
4. Run stock x64 DXVK 3.1.1 device probes as an integration control. Separately
   run x86 D3D9 probes; do not treat either result as gameplay validation.
5. The pinned upstream ARM64EC DXVK build and PE metadata checks now pass.
   Verify Wine module loading. Repeat D3D9/D3D11 device creation, callbacks, resource
   creation, rendering, resize and teardown checks.
6. Compare identical gameplay scenes across Rosetta+x64 DXVK+x64 KK,
   FEX+x64 DXVK+ARM64 KK, and FEX+ARM64EC DXVK+ARM64 KK. Record revision
   differences and cache state; measure CPU time and frame-time distributions.

## Maintenance decision

Native KK integration is a bounded experiment using an existing Wine host
bridge. Native DXVK adds a toolchain/build matrix and DLL compatibility work.
Owning a new Wine/FEX fork or an independent entitled loader would be the much
larger commitment. Prefer dappermint's existing lane and upstream components
where feasible; keep reproducible builds, 32-bit coverage, distribution and
performance as explicit gates before making this a supported runtime.

This research track is separate from completing the current Rosetta runtime.
