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

`runtime/kosmickrisp/pins.env` pins the shadexternals cross-build recipe, the
mesa commit its submodule must resolve to, and the khronos loader and headers.
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
