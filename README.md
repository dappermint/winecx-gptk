# winecx-gptk

ci build of a gptk-capable wine runtime for the [frankea/Whisky](https://github.com/frankea/Whisky) fork, from [codeweavers' crossover 25 wine sources](https://github.com/PhoenicisOrg/winecx) (wine 10.0).

why: apple's game porting toolkit / d3dmetal payload only executes on crossover-derived wine builds, it patches their unixcall internals at load time. the fork's current wine 11 runtime kills every process that runs d3dmetal code. details in [frankea/Whisky#163](https://github.com/frankea/Whisky/issues/163), importer app-side in [frankea/Whisky#164](https://github.com/frankea/Whisky/pull/164).

what the workflow does:

- clones winecx, builds the unix half for x86_64 under rosetta on a macos-15 runner
- PE half via llvm-mingw, `--enable-archs=i386,x86_64` (new wow64, 32-bit steam.exe works without crossover's 32on64 toolchain)
- first cut is minimal: no vulkan (no dxvk), no gstreamer, freetype + gnutls from x86_64 homebrew
- packages a whisky `Libraries.tar.gz` with `gptkCapable` set in the version plist

## patches

`patches/` is applied to the winecx tree after cloning.

**0001, ntdll: don't call a foreign personality routine as an SEH handler.** `virtual_unwind()` leaves `LDR_DATA_TABLE_ENTRY *module` uninitialised, and `LdrFindEntryForAddress` does not touch it when it fails. so for a fault in Mach-O code with no PE module, the existing `!module` guard ("calling personality routine in system library not supported yet") reads garbage and never fires. wine then calls the dylib's libunwind personality routine as a windows exception handler. it returns 3, which wine reads as `ExceptionCollidedUnwind` (under the itanium unwind ABI 3 is `_URC_FATAL_PHASE1_ERROR`, the two enums simply do not correspond), and `call_seh_handlers` re-enters `RtlVirtualUnwind` with `FunctionEntry == NULL` **and** `handler_data == NULL`. `RtlVirtualUnwind2`'s leaf path writes `*data` unconditionally, so it faults at `RtlVirtualUnwind2+0x602`, which raises another exception on the same path and recurses ~0xfc0 of stack per pass until the thread dies of stack overflow. initialising `module` to NULL makes the guard work; the second hunk guards the `*data` store as well. measured on steam's webhelper: 255 collided unwinds and 775 access violations per 70s run on this build, versus 0 and 12 on stock wine 11.

**0002, advapi32: report the real username by default.** CrossOver Hack 12735 pins `GetUserNameA/W` to the literal `"crossover"` unless `CX_REPORT_REAL_USERNAME` is set. wine salts its DPAPI key with `GetUserNameA` (`crypt32/protectdata.c`), so a blob sealed by a stock wine prefix cannot be opened here even though the user SID is identical. that breaks any bottle shared with a stock wine runtime, including chromium's `os_crypt` key and therefore steam's saved login token. the hack is flipped to opt-in via `CX_CONSISTENT_USERNAME`.

**0003, winemac: render into another process's window.** backport of upstream wine [52e03c61e4](https://gitlab.winehq.org/wine/wine/-/commit/52e03c61e4) and [1a63b0d7c4](https://gitlab.winehq.org/wine/wine/-/commit/1a63b0d7c4) by marc-aurel zent, adapted to the older winemac.drv here (no `client_surface`, so the swapchain hangs off `wine_vk_surface` and the offscreen branch lives in `macdrv_vulkan_surface_create`). upstream landed these after wine 11.0, so neither wine 11.0 nor winecx 25 has them. without it `macdrv_vulkan_surface_create` gives up whenever `get_win_data` fails, which it always does for a window owned by another process, so `CreateSwapChainForHwnd` returns `E_INVALIDARG` and chromium's gpu process dies with `eglCreateWindowSurface: EGL_BAD_ALLOC`. that is what makes steam's window black here. the fix renders into an offscreen `CAMetalLayer`, publishes it with a `CAContext`, and posts the context id to the owning process, which hosts it with a `CALayerHost` on its content view. verified against wine 11.0, which creates the surface and then never parents it, so the window is black with no error at all.

the apple payload is never bundled. import your own gptk evaluation environment dmg, the whisky importer from #164 deploys it onto capable runtimes automatically.
