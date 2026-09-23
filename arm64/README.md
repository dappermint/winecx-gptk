# arm64 runtime

local recipe for `whisky-arm64-<ver>`: wine arm64 (arm64ec + aarch64, native
unix half), our FEX, DXMT arm64ec, wine-mono, on CrossOver's entitled
`wine.app` until we have our own `cross-architecture-support` entitlement.

sources, all pushed:

| piece | repo | branch |
|---|---|---|
| wine | dappermint/winecx | `arm64-<wine>` (e.g. `arm64-1117`) |
| DXMT | dappermint/dxmt | `arm64-1117` |
| FEX | dappermint/FEX | `main` |

order:

1. `build-arm64-unix.sh all` builds wine (SRC/B env to override)
2. `build-llvm15.sh` once, airconv needs llvm 15 exactly
3. `build-dxmt-arm64.sh` builds DXMT against the wine build tree
4. `package-arm64.sh <ver> <base-runtime>` installs wine, carries FEX, the
   loader and dylibs from the base runtime, adds wine-mono
5. `install-dxmt-arm64.sh <runtime> [dxmt-build]` swaps in the fresh DXMT
6. `verify-runtime.sh <runtime-id> <label>` boots a clone of the arm64 bottle,
   compiles and runs a .NET exe, runs casualties unknown

wine-mono comes from the upstream `wine-mono-<ver>-arm64.tar.xz` release, the
version in `dlls/mscoree/mscoree_private.h`.
