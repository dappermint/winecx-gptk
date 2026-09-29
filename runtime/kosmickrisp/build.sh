#!/bin/bash
# Build an isolated x86_64 loader + ICD for the Rosetta Wine host.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "$0")" && pwd -P)
source "$script_dir/pins.env"
work=${1:?usage: build.sh BUILD_DIRECTORY OUTPUT_DIRECTORY}
output=${2:?usage: build.sh BUILD_DIRECTORY OUTPUT_DIRECTORY}
mkdir -p "$work" "$output"
work=$(cd "$work" && pwd -P)
output=$(cd "$output" && pwd -P)
export MACOSX_DEPLOYMENT_TARGET=26.0
python3 -c 'import sys; assert sys.version_info >= (3, 10), "Mesa requires Python >= 3.10; activate the build venv first"'

checkout() {
    local url=$1 revision=$2 directory=$3
    if [ ! -d "$directory/.git" ]; then
        git init "$directory"
        git -C "$directory" remote add origin "$url"
    fi
    git -C "$directory" fetch --depth=1 origin "$revision"
    git -C "$directory" checkout --detach FETCH_HEAD
    [ "$(git -C "$directory" rev-parse HEAD)" = "$revision" ]
}
checkout https://github.com/shadexternals/mesa-kosmickrisp.git "$KOSMICKRISP_BUILD_COMMIT" "$work/recipe"
git -C "$work/recipe" submodule update --init --recursive --depth=1
[ "$(git -C "$work/recipe/externals/mesa" rev-parse HEAD)" = "$KOSMICKRISP_MESA_COMMIT" ]
checkout https://github.com/KhronosGroup/Vulkan-Loader.git "$VULKAN_LOADER_COMMIT" "$work/loader"
checkout https://github.com/KhronosGroup/Vulkan-Headers.git "$VULKAN_HEADERS_COMMIT" "$work/headers"

cmake -S "$work/recipe" -B "$work/driver-build" \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_OSX_ARCHITECTURES=x86_64 \
    -DCMAKE_OSX_DEPLOYMENT_TARGET=26.0
cmake --build "$work/driver-build" --parallel "$(sysctl -n hw.ncpu)"
cmake -S "$work/headers" -B "$work/headers-build" \
    -DCMAKE_INSTALL_PREFIX="$work/headers-install"
cmake --install "$work/headers-build"
cmake --fresh -S "$work/loader" -B "$work/loader-build" \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_OSX_ARCHITECTURES=x86_64 \
    -DCMAKE_SYSTEM_NAME=Darwin -DCMAKE_SYSTEM_PROCESSOR=x86_64 \
    -DCMAKE_OSX_DEPLOYMENT_TARGET=26.0 -DBUILD_TESTS=OFF \
    -DCMAKE_PREFIX_PATH="$work/headers-install" \
    -DVULKAN_HEADERS_INSTALL_DIR="$work/headers-install"
cmake --build "$work/loader-build" --parallel "$(sysctl -n hw.ncpu)"

cp -L "$work/driver-build/outputs/libvulkan_kosmickrisp.dylib" "$output/"
cp -L "$work/loader-build/loader/libvulkan.1.dylib" "$output/"
# --prefer-static does not force Mesa's zlib fallback to build statically.
# Bundle the exact target build, never the ARM64 Homebrew copy.
if otool -L "$output/libvulkan_kosmickrisp.dylib" | grep -q '@rpath/libz.1.dylib'; then
    cp -L "$work/driver-build/externals/mesa/build-target/subprojects/zlib-1.3.1/libz.1.dylib" "$output/"
    chmod u+w "$output/libvulkan_kosmickrisp.dylib"
    install_name_tool -change '@rpath/libz.1.dylib' '@loader_path/libz.1.dylib' "$output/libvulkan_kosmickrisp.dylib"
fi
# Preserve the API version from the actual Mesa build. The ICD path is relative
# to the manifest, not a Mach-O @loader_path expression.
python3 - "$work/driver-build/outputs/kosmickrisp_mesa_icd.json" "$output/kosmickrisp_icd.json" <<'PY'
import json, sys
with open(sys.argv[1]) as stream:
    manifest = json.load(stream)
manifest['ICD']['library_path'] = './libvulkan_kosmickrisp.dylib'
with open(sys.argv[2], 'w') as stream:
    json.dump(manifest, stream, indent=2)
    stream.write('\n')
PY

# Fail rather than silently shipping an arm64 Homebrew library or an unresolved
# @rpath dependency. zlib is the only explicitly bundled target dependency.
for library in "$output"/*.dylib; do
    [ "$(lipo -archs "$library")" = x86_64 ]
    chmod u+w "$library"
    install_name_tool -id "@loader_path/$(basename "$library")" "$library"
    otool -L "$library" | tail -n +3 | while read -r dependency rest; do
        case "$dependency" in
            /usr/lib/*|/System/Library/*) ;;
            @loader_path/libz.1.dylib) test -f "$output/libz.1.dylib" ;;
            *) echo "Unbundled dependency in $library: $dependency" >&2; exit 1 ;;
        esac
    done
    codesign --force --sign - --timestamp=none "$library"
    codesign --verify --strict "$library"
done
mkdir -p "$output/licenses"
cp "$work/recipe/LICENSE.txt" "$output/licenses/build-recipe.txt"
cp "$work/recipe/externals/mesa/docs/license.rst" "$output/licenses/mesa.rst"
mkdir -p "$output/licenses/mesa" "$output/licenses/vulkan-headers"
cp -R "$work/recipe/externals/mesa/licenses/." "$output/licenses/mesa/"
cp "$work/loader/LICENSE.txt" "$output/licenses/vulkan-loader.txt"
cp "$work/headers/LICENSE.md" "$output/licenses/vulkan-headers.md"
cp -R "$work/headers/LICENSES/." "$output/licenses/vulkan-headers/"
# Cross builds use Mesa's pinned zlib fallback when no target pkg-config exists.
if [ -f "$work/recipe/externals/mesa/subprojects/zlib-1.3.1/README" ]; then
    cp "$work/recipe/externals/mesa/subprojects/zlib-1.3.1/README" "$output/licenses/zlib.txt"
fi
cp "$script_dir/pins.env" "$output/SOURCE.txt"
{
    /usr/bin/clang --version
    cmake --version
    meson --version
    python3 --version
    brew list --versions llvm spirv-tools spirv-llvm-translator libclc python@3.13
    python3 -m pip freeze
} > "$output/BUILD-TOOLS.txt"
