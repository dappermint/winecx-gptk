#!/bin/bash
# build dxmt for the arm64 runtime: arm64ec PE frontends + aarch64 winemetal.so,
# against our own wine build tree so winemetal links the winemac we ship.
#
# usage: build-dxmt-arm64.sh [install-dir]
set -euo pipefail
SRC="${SRC:-/Volumes/Wine/src/dxmt}"
WINE_BUILD="${WINE_BUILD:-/Volumes/Wine/localdev/build-arm64-1117}"
LLVM="${LLVM:-/Volumes/Wine/toolchains/llvm-15-darwin-arm64}"
T=/Volumes/Wine/toolchains/llvm-mingw-20260616-ucrt-macos-universal
B="${B:-/Volumes/Wine/localdev/build-dxmt-arm64}"
OUT="${1:-/Volumes/Wine/localdev/dxmt-arm64}"

# the metal compiler ships as a cryptex that command-line-tools xcrun does not
# find, so meson's `xcrun -sdk macosx metal` goes through a shim
METAL_TC=$(ls -d /var/run/com.apple.security.cryptexd/mnt/com.apple.MobileAsset.MetalToolchain-*/Metal.xctoolchain 2>/dev/null | tail -1)
[ -x "$METAL_TC/usr/bin/metal" ] || { echo "no metal toolchain cryptex mounted"; exit 1; }
SDK=$(/usr/bin/xcrun --show-sdk-path)
SHIM=/Volumes/Wine/localdev/metal-shim
mkdir -p "$SHIM"
printf '#!/bin/bash\n[ "$1" = -sdk ] && shift 2\ntool=$1; shift\ncase $tool in metal) exec "%s/usr/bin/metal" -isysroot "%s" "$@";; metallib) exec "%s/usr/bin/metallib" "$@";; *) exec /usr/bin/xcrun "$tool" "$@";; esac\n' \
    "$METAL_TC" "$SDK" "$METAL_TC" > "$SHIM/xcrun"
chmod +x "$SHIM/xcrun"

export PATH="$SHIM:$T/bin:/opt/homebrew/bin:/run/current-system/sw/bin:/usr/bin:/bin:/usr/sbin:/sbin"
rm -rf "$B" "$OUT"
cd "$SRC"
nix shell nixpkgs#meson nixpkgs#ninja --command bash -euc "
  meson setup --cross-file build-arm64ec.txt -Dnative_llvm_path=$LLVM -Dwine_build_path=$WINE_BUILD \
    '$B' --buildtype release --prefix '$OUT' >/dev/null
  meson compile -C '$B'
  meson install -C '$B' >/dev/null
"
find "$OUT" -type f \( -name '*.dll' -o -name '*.so' \) | sort
