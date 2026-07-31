# winecx-gptk

ci build of a gptk-capable wine runtime for the [frankea/Whisky](https://github.com/frankea/Whisky) fork, from [codeweavers' crossover 25 wine sources](https://github.com/PhoenicisOrg/winecx) (wine 10.0).

why: apple's game porting toolkit / d3dmetal payload only executes on crossover-derived wine builds, it patches their unixcall internals at load time. the fork's current wine 11 runtime kills every process that runs d3dmetal code. details in [frankea/Whisky#163](https://github.com/frankea/Whisky/issues/163), importer app-side in [frankea/Whisky#164](https://github.com/frankea/Whisky/pull/164).

what the workflow does:

- clones winecx, builds the unix half for x86_64 under rosetta on a macos-15 runner
- PE half via llvm-mingw, `--enable-archs=i386,x86_64` (new wow64, 32-bit steam.exe works without crossover's 32on64 toolchain)
- first cut is minimal: no vulkan (no dxvk), no gstreamer, freetype + gnutls from x86_64 homebrew
- packages a whisky `Libraries.tar.gz` with `gptkCapable` set in the version plist

the apple payload is never bundled. import your own gptk evaluation environment dmg, the whisky importer from #164 deploys it onto capable runtimes automatically.
