# winecx-gptk

ci build of a gptk-capable wine runtime for the [frankea/Whisky](https://github.com/frankea/Whisky) fork: codeweavers' crossover 26.3 wine changes, rebased onto upstream wine 11.15.

why: apple's game porting toolkit / d3dmetal payload only executes on crossover-derived wine builds, it patches their unixcall internals at load time. details in [frankea/Whisky#163](https://github.com/frankea/Whisky/issues/163), importer app-side in [frankea/Whisky#164](https://github.com/frankea/Whisky/pull/164).

the source tree is the [`wine1115` branch of dappermint/winecx](https://github.com/dappermint/winecx/tree/wine1115): crossover 26.3's diff (221 files against its wine 11.0 base) merged onto wine 11.15 via a synthetic three-way, with every local patch committed in the tree itself. `patches/` in this repo is empty on purpose; the apply step is a guarded no-op. the crossover diff turned out compact enough that tracking upstream wine releases is sustainable, most of the rebase churn was mechanical.

what runs on it, measured on an m5: steam's ui end to end, d3d12 through d3dmetal at feature level 12_2 (binding tier 3, sm 6.6), dxvk d3d11, msync, and the media stack.

what the workflow does:

- clones winecx at the pinned commit, builds the unix half for x86_64 under rosetta
- PE half via mingw-w64 gcc (not llvm-mingw: an llvm-built `kernelbase.dll` stalls steam's CM login, found by module bisection), `--enable-archs=i386,x86_64`
- freetype, gnutls, gstreamer and friends come from pinned nixpkgs x86_64-darwin and are bundled flat into `Wine/lib` with `@loader_path` rewrites, so the tree relocates; moltenvk from khronos' own release
- wine-mono and wine-gecko go in extracted, the same form and the same place the stock whisky engine puts them
- packages a whisky `Libraries.tar.gz` with `gptkCapable` set in the version plist

gates that refuse to ship a bad tree, each one added after that exact thing shipped silently:

- **relocatability.** every Mach-O file is swept for absolute non-system references. a dylib whose own id is a `/nix/store` path cannot be dlopened off the builder, which cost fonts, vulkan and tls for seven builds without a single error message.
- **every bundled library dlopens.** the closure is loaded file by file on the builder with the store paths masked; this caught the libiconv split (`_iconv` vs `_libiconv`) that had silently killed the whole media stack.
- **the runtime opens a window and media foundation has decoders.** `wine --version` passes on runtimes that cannot create a window.
- **the i386 half is non-empty.** a 64-bit-only tree cannot load `syswow64\ntdll.dll`, so every 32-bit program dies with `c0000135`.
- **the PE half is stripped.** gcc emits DWARF and nothing removes it; `ntdll.dll` is 3.0MB unstripped against the stock engine's 0.7MB, and everything still runs.

## reproducibility

everything the build consumes is pinned in-tree:

| input | pinned by |
|---|---|
| winecx sources | `WINECX_COMMIT` in the workflow env |
| nixpkgs | `NIXPKGS_REV` in the workflow env, a rev not a branch |
| moltenvk, dxvk, dxmt | version + sha256 in the workflow |
| wine-mono, wine-gecko | sha256 table in the workflow, checked after download; the versions are read out of winecx's `dlls/appwiz.cpl/addons.c` and the build stops if an unpinned version appears |

builds run on a self-hosted runner by default (warm ccache, ~15 min); the `hosted` dispatch input is the clean-room check and what releases should come from when provenance matters more than turnaround.

## notable changes carried in the tree

**ntdll: don't call a foreign personality routine as an SEH handler.** `virtual_unwind()` leaves `LDR_DATA_TABLE_ENTRY *module` uninitialised and `LdrFindEntryForAddress` does not touch it on failure, so for a fault in Mach-O code the "personality routine in system library" guard reads garbage and never fires; wine then calls a libunwind personality routine as a windows exception handler and recurses to stack death. still present upstream as of wine 11.15, applied at the moved location there.

**winemac: host cross-process metal layers over CAContext.** wine 11.x grew CALayerHost cross-process swapchains upstream; what it still lacks is child windows and win32 state mirroring for hosted layers, which is what steam's chromium needs. the gpu process renders into another process's child windows; the owner hosts the published tree and re-derives hidden state and z-order from the win32 windows on every WindowPosChanged. without the mirroring, chromium's hidden standby surface covers the live one with one stale black frame, which shows up as a fully rendered steam library under a black layer.

**d3dkmt: adapter identity and segment sizes.** five more KMTQAITYPEs answered honestly from vulkan (adapter type, physical adapter count, pci address, adapter guid, segment sizes), placed above crossover's WDDM 2.7 hack so its fallthrough keeps reaching `default` for non-d3dmetal backends.

**upstream fixes taken ahead of their release.** eight commits from wine master after the 11.15 tag, kept as cherry-picks with their trailers: winemac hiding a stale `client_view` behind gdi drawing on a window that used to be a d3d target, the win32u client-surface refcount race, core audio honouring the period frame size a game asks for instead of the device default, the simd exception codes, and two input fixes. all of them land in 11.16, so drop them at that rebase rather than carrying them forward.

## history

the wine 10 line (crossover 25.1, series 4.3) proved d3dmetal executes on a self-built runtime and carried the first version of the cross-process bridge as four patches; the wine 11.0 line (crossover 26.3) collapsed them to one. both era tips are tagged, [`lane/wine10-cx25`](../../tree/lane/wine10-cx25) and [`lane/wine11.0-cx26`](../../tree/lane/wine11.0-cx26), and the old `patches/` files are browsable in those trees.
