#!/bin/bash
# llvm 15.0.7 static libs for dxmt's airconv, arm64 host, same flags as dxmt's ci.
# airconv needs typed pointers, so 15 is both the floor and the ceiling.
set -euo pipefail
T=/Volumes/Wine/toolchains
SRC=$T/llvm-project-15
OUT=$T/llvm-15-darwin-arm64
[ -d "$SRC" ] || git clone -q --depth 1 --branch llvmorg-15.0.7 https://github.com/llvm/llvm-project.git "$SRC"
export PATH="/opt/homebrew/bin:/run/current-system/sw/bin:/usr/bin:/bin:/usr/sbin:/sbin"
nix shell nixpkgs#cmake nixpkgs#ninja nixpkgs#python3 --command bash -c "
  cmake -B $T/llvm-15-build -S $SRC/llvm \
    -DCMAKE_INSTALL_PREFIX=$OUT \
    -DCMAKE_OSX_ARCHITECTURES=arm64 \
    -DLLVM_HOST_TRIPLE=arm64-apple-darwin \
    -DLLVM_ENABLE_ASSERTIONS=On \
    -DLLVM_ENABLE_ZSTD=Off \
    -DCMAKE_BUILD_TYPE=Release \
    -DLLVM_TARGETS_TO_BUILD= \
    -DLLVM_BUILD_TOOLS=Off \
    -DPACKAGE_VENDOR=DXMT \
    -DLLVM_VERSION_PRINTER_SHOW_HOST_TARGET_INFO=Off \
    -G Ninja >/dev/null
  cmake --build $T/llvm-15-build
  cmake --install $T/llvm-15-build >/dev/null
"
ls "$OUT/lib" | wc -l
