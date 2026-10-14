# kosmickrisp lane (experimental)

an optional second vulkan driver for the same rosetta wine host:
[kosmickrisp](https://docs.mesa3d.org/drivers/kosmickrisp.html), mesa's
conformant vulkan 1.4 driver on metal, in place of moltenvk.

why: moltenvk does not expose the vulkan 1.3 features dxvk 2.x and 3.x require,
which is why whisky keeps dxvk frozen at the 1.10.x macos fork. kosmickrisp does
expose them, so the goal is kosmickrisp + upstream dxvk, and native vulkan games
without moltenvk's translation gaps. nothing changes for a runtime built without
this lane, and `wine`/`wine64` keep moltenvk even in a runtime built with it.

## build

dispatch `build.yml` with the `kosmickrisp` box ticked. the artifact is named
`whiskywine-gptk-libraries-kosmickrisp-experimental`, and publish refuses it, on
`main` too; a push never builds this lane.

Hosted builds validate compilation, relocation, windows and media on macOS 26.
They require `gpu_runner_label` to run the complete GPU suite as a second job
on a physical Metal 4 GPU, using the exact uploaded runtime and its checksums.
The dispatch must come from the repository owner. Self-hosted builds run the
same GPU suite in the build job.

`runtime/kosmickrisp/pins.env` pins the shadexternals cross-build recipe, the
complete Mesa tree from `niltonperimneto/mesa`, and the Khronos loader and headers.
The default build exports a verified snapshot of the pinned fork commit and
replaces the recipe's older Mesa submodule. The fork already contains the carried
driver fixes, so the old patches are not reapplied. Updating the fork requires an
explicit pin update here; CI never follows a moving branch tip.
`build.sh` builds them for x86_64, because an arm64 icd cannot load into this
wine host. the build fails if a library is not x86_64-only, links anything
outside `/usr/lib` and `/System`, or fails codesign verification. mesa's zlib
fallback stays dynamic despite `--prefer-static`, so it is bundled and relinked
through `@loader_path`. homebrew and xcode are not pinned; `BUILD-TOOLS.txt` in
the bundle records what built it.

## layout and use

`Wine/lib/kosmickrisp/` holds `libvulkan.1.dylib` (khronos loader),
`libvulkan_kosmickrisp.dylib`, `kosmickrisp_icd.json` (relative library path),
zlib when mesa needs it, licenses, and `SOURCE.txt` with the pins.

`DXVK3/x32/` and `DXVK3/x64/` contain upstream DXVK 3.1.1's `d3d8.dll`,
`d3d9.dll`, `d3d10core.dll`, `d3d11.dll`, and `dxgi.dll`. The payload includes
`LICENSE.txt`, `SOURCE.txt` with the release URL and verified archive hash,
and `SHA256SUMS` for the packaged files. The experimental CI probes use these
same DLLs. This payload is separate from the existing `DXVK/` backend; bottle
installation and an app backend selector are follow-up work.

```sh
WINEPREFIX=/path/to/prefix Libraries/Wine/bin/wine-kosmickrisp program.exe
```

the launcher needs no wine change. crossover's win32u (cw hack 25909) already
loads `CX_LIBVULKAN` as the host vulkan library while
`CX_ACTIVE_GRAPHICS_BACKEND` is `wined3d`, so the launcher sets both, points
`VK_DRIVER_FILES` at the bundled icd, and clears inherited
`VK_ICD_FILENAMES`/`VK_LOADER_DRIVERS_*`. `wined3d` here selects no direct3d
implementation for dxvk or a native vulkan program; it only unlocks the
override. whisky can set the same variables per program.

## Managed DXVK setup

Experimental artifacts include `wine-kosmickrisp-dxvk` and a prefix helper.
They require Python 3.9 or newer on PATH. Close applications in the selected
prefix and wait for its wineserver to exit before installation or restoration.
The helper checks that both Windows architectures are initialized, verifies
payload checksums and PE architectures, backs up existing DLLs and registry
values, and writes a recovery journal before making changes.

```sh
export WINEPREFIX=/absolute/path/to/initialized/prefix
Libraries/Wine/bin/wine-kosmickrisp-dxvk --install-prefix
Libraries/Wine/bin/wine-kosmickrisp-dxvk --status-prefix
Libraries/Wine/bin/wine-kosmickrisp-dxvk game.exe
Libraries/Wine/bin/wine-kosmickrisp-dxvk --restore-prefix
```

Installation changes DLLs and native overrides for the selected prefix. Use the
KosmicKrisp launcher for its D3D programs; restore the installation before
selecting another graphics backend. Status reports modified DLLs or overrides;
restoration refuses to overwrite changes made outside the transaction. Interrupted
installation or restoration can be recovered with `--restore-prefix`. Keep the
prefix at its original path until restoration is complete. Restore does not
require the DXVK payload or driver to remain installed.

For a game that loads a DLL from its own directory, include that directory in
installation so it participates in the same backup and restoration transaction:

```sh
Libraries/Wine/bin/wine-kosmickrisp-dxvk --install-prefix \
  --game-dir '/absolute/path/to/Portal 2/bin' --arch x32 --dll d3d9
```

Default caches live under the prefix's `drive_c/ShaderCaches/kosmickrisp/`.
DXVK receives a Windows `C:\ShaderCaches\kosmickrisp\dxvk` path; Mesa receives
its host path. Explicit `DXVK_SHADER_CACHE_PATH` and `MESA_SHADER_CACHE_DIR`
overrides are preserved. Installation and restoration do not delete caches.

## Building a committed candidate

The default CI pin remains separate from the experimental GPL/cache candidate.
Freeze a complete committed Mesa tree, including its existing driver changes,
without editing the working checkout:

```sh
python3 runtime/kosmickrisp/source-snapshot.py export \
  --source ../mesa --commit FULL_COMMIT_HASH --out /tmp/mesa-snapshot
bash runtime/kosmickrisp/build.sh /tmp/kk-build /tmp/kk-driver /tmp/mesa-snapshot
```

The optional third argument replaces the recipe's Mesa submodule with the
verified archive. It does not apply the default lane's patches again. Source
archives have deterministic compression, a commit/tree identity and SHA-256.
Builds use separate source-specific directories and recheck extracted source
contents on reuse. Use a fresh build root when changing build tools or SDKs.
Bundles record effective source identity, build tools and binary checksums.
The candidate remains experimental until rendering and gameplay gates pass.
