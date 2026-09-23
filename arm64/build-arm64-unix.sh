#!/bin/bash
# configure and build the arm64 winecx lane (branch `arm64`) out of tree.
#
# Defaults to the two unix libraries we hot-swap into the installed runtime;
# pass targets to build something else, or `all` for the whole thing.
#
#   bison/flex/pkg-config come from nix: macOS ships bison 2.3 and wine wants 3.0+.
#   freetype and gnutls come from nix too, resolved at run time rather than
#   pinned, since `nix shell` only puts binaries on PATH and configure needs the
#   headers.
#   the PE half needs llvm-mingw for arm64ec; homebrew's mingw-w64 is x86 only.
#   MoltenVK is unpacked beside the toolchain: without it win32u fails to
#   compile on an undefined SONAME_LIBVULKAN, and winemac.so links against
#   win32u.
set -euo pipefail
SRC="${SRC:-/Volumes/Wine/src/winecx-arm64-1117}"
B="${B:-/Volumes/Wine/localdev/build-arm64-1117}"
T=/Volumes/Wine/toolchains/llvm-mingw-20260616-ucrt-macos-universal

TARGETS=${*:-"dlls/ntdll/ntdll.so dlls/winemac.drv/winemac.so"}

export PATH="$T/bin:/opt/homebrew/bin:/run/current-system/sw/bin:/usr/bin:/bin:/usr/sbin:/sbin"
mkdir -p "$B"
cd "$B"

DEPS=$(nix build --no-link --print-out-paths nixpkgs#freetype.dev nixpkgs#gnutls.dev)
PKG_CONFIG_PATH=$(echo "$DEPS" | sed 's|$|/lib/pkgconfig|' | tr '\n' ':')
export PKG_CONFIG_PATH
export LDFLAGS="-L/Volumes/Wine/toolchains/MoltenVK/MoltenVK/dynamic/dylib/macOS${LDFLAGS:+ $LDFLAGS}"

nix shell nixpkgs#bison nixpkgs#flex nixpkgs#pkg-config --command bash -c '
  set -eu
  if [ ! -f Makefile ]; then
    echo "== configure"
    "'"$SRC"'/configure" --enable-archs=arm64ec,aarch64 --disable-tests \
      --without-x --without-wayland --without-oss --without-alsa --without-pulse \
      --without-sane --without-usb --without-v4l2 --without-pcap --without-capi \
      --without-opencl --without-cups --prefix=/opt/whiskywine > configure.out 2>&1 || {
        tail -30 configure.out; exit 1; }
    echo "   configured"
  fi
  echo "== build '"$TARGETS"'"
  make -o config.status -o Makefile -j10 '"$TARGETS"' ${MAKEFLAGS_EXTRA:-}
'
ls -la $(echo "$TARGETS" | tr " " "\n" | grep "\.so$") 2>/dev/null || true
