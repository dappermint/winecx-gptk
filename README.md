# winecx-gptk

ci build of a gptk-capable wine runtime for the [frankea/Whisky](https://github.com/frankea/Whisky) fork, from codeweavers' crossover wine sources.

why: apple's game porting toolkit / d3dmetal payload only executes on crossover-derived wine builds, it patches their unixcall internals at load time. details in [frankea/Whisky#163](https://github.com/frankea/Whisky/issues/163), importer app-side in [frankea/Whisky#164](https://github.com/frankea/Whisky/pull/164).

## lanes

| branch | base | series | state |
|---|---|---|---|
| `main` | crossover 25.1 (wine 10.0), [PhoenicisOrg mirror](https://github.com/PhoenicisOrg/winecx) | 4.3 | frozen, first line that proved d3dmetal on a self-built runtime |
| `feat/wine-11-patches` | crossover 26.3 (wine 11.0), [our import](https://github.com/dappermint/winecx) | 4.3 | steam ui, d3d12 12_2, the cross-process metal bridge as `patches/0003` |
| `feat/wine-11.15` | crossover 26.3 diff rebased onto wine 11.15, [`wine1115` branch](https://github.com/dappermint/winecx/tree/wine1115) | 4.5 | current release lane; patches live in the winecx tree, `patches/` is empty here |

the 11.15 lane is the interesting one: the 26.3 diff is 221 files against wine 11.0, and rebasing it onto upstream's current dev release turned out mostly mechanical. wine 11.x grew its own CALayerHost cross-process swapchain machinery, so our bridge shrank to the two halves upstream still lacks, child windows and win32 visibility/z mirroring for hosted layers. tracking upstream wine looks sustainable.

what the workflow does:

- clones winecx at the pinned commit, builds the unix half for x86_64 under rosetta
- PE half via mingw-w64 gcc (not llvm-mingw: an llvm-built `kernelbase.dll` stalls steam's CM login, found by module bisection), `--enable-archs=i386,x86_64`
- freetype, gnutls, gstreamer and friends come from pinned nixpkgs x86_64-darwin and are bundled flat into `Wine/lib` with `@loader_path` rewrites, so the tree relocates; moltenvk from khronos' own release
- wine-mono and wine-gecko go in extracted, the same form and the same place the stock whisky engine puts them
- packages a whisky `Libraries.tar.gz` with `gptkCapable` set in the version plist

gates that refuse to ship a bad tree, each one added after that exact thing shipped silently:

- **relocatability.** every Mach-O file is swept for absolute non-system references. a dylib whose own id is a `/nix/store` path cannot be dlopened off the builder, which cost fonts, vulkan and tls for seven builds without a single error message.
- **every bundled library dlopens.** the closure is loaded file by file on the builder with the store paths masked; this is what caught the libiconv split (`_iconv` vs `_libiconv`) that had silently killed the whole media stack.
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

## patches

on `main` and `feat/wine-11-patches`, `patches/` is applied to the winecx tree after cloning; descriptions live in each patch header. on `feat/wine-11.15` the same changes are commits in the winecx `wine1115` branch itself.

the two worth knowing about:

**ntdll: don't call a foreign personality routine as an SEH handler.** `virtual_unwind()` leaves `LDR_DATA_TABLE_ENTRY *module` uninitialised and `LdrFindEntryForAddress` does not touch it on failure, so for a fault in Mach-O code the "personality routine in system library" guard reads garbage and never fires; wine then calls a libunwind personality routine as a windows exception handler and recurses to stack death. still present upstream as of wine 11.15, re-applied at the moved location there.

**winemac: host cross-process metal layers over CAContext.** chromium's gpu process renders into another process's child windows; the owner hosts the published layer tree and mirrors win32 visibility and z-order onto it. without the mirroring, chromium's hidden standby surface covers the live one with one stale black frame, which is a fully rendered steam library under a black layer.
