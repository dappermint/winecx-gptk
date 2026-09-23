#!/bin/bash
# swap a dxmt build into an arm64 runtime.
#
# winemetal stays a builtin (it is unixlib-backed): .so + .dll into the wine
# tree, marker kept. d3d11/dxgi/d3d10core are loaded as native from the
# runtime's DXMT/x64, so the "Wine builtin DLL" marker at 0x40 is replaced
# with the standard DOS stub bytes it overwrote, or wine refuses them under a
# native-only override.
#
# usage: install-dxmt-arm64.sh <runtime-dir> [dxmt-build]
set -euo pipefail
RT="${1:?runtime dir}"
D="${2:-/Volumes/Wine/localdev/dxmt-arm64}"

cp -c "$D/aarch64-unix/winemetal.so" "$RT/Wine/lib/wine/aarch64-unix/winemetal.so"
cp -c "$D/aarch64-windows/winemetal.dll" "$RT/Wine/lib/wine/aarch64-windows/winemetal.dll"
cp -c "$D/aarch64-windows/winemetal.dll" "$RT/DXMT/x64/winemetal.dll"
for f in d3d11 dxgi d3d10core; do
    cp -c "$D/aarch64-windows/$f.dll" "$RT/DXMT/x64/$f.dll"
    printf '\x0e\x1f\xba\x0e\x00\xb4\x09\xcd\x21\xb8\x01\x4c\xcd\x21\x90\x90' \
        | dd of="$RT/DXMT/x64/$f.dll" bs=1 seek=64 count=16 conv=notrunc 2>/dev/null
    ! strings -a "$RT/DXMT/x64/$f.dll" | grep -q 'Wine builtin DLL' || { echo "marker still in $f"; exit 1; }
done
codesign -f -s - "$RT/Wine/lib/wine/aarch64-unix/winemetal.so" 2>/dev/null
echo "dxmt from $D -> $RT"
