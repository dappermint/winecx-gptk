#!/bin/bash
# Build an isolated x86_64 loader + ICD for the Rosetta Wine host.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "$0")" && pwd -P)
source "$script_dir/pins.env"
work=${1:?usage: build.sh BUILD_DIRECTORY OUTPUT_DIRECTORY}
output=${2:?usage: build.sh BUILD_DIRECTORY OUTPUT_DIRECTORY [SOURCE_SNAPSHOT_DIRECTORY]}
snapshot=${3:-}
mkdir -p "$work"
if [ -e "$output" ]; then
    [ -d "$output" ] && [ -z "$(ls -A "$output")" ] || { echo "Driver output must be new or empty: $output" >&2; exit 1; }
else
    mkdir -p "$output"
fi
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
if [ -z "$snapshot" ] && [ -n "${KOSMICKRISP_MESA_REPO:-}" ]; then
    mesa_source="$work/mesa-$KOSMICKRISP_MESA_COMMIT"
    checkout "$KOSMICKRISP_MESA_REPO" "$KOSMICKRISP_MESA_COMMIT" "$mesa_source"
    snapshot="$work/mesa-snapshot-$KOSMICKRISP_MESA_COMMIT"
    if [ ! -e "$snapshot" ]; then
        python3 "$script_dir/source-snapshot.py" export \
            --source "$mesa_source" --commit "$KOSMICKRISP_MESA_COMMIT" --out "$snapshot"
    fi
    python3 "$script_dir/source-snapshot.py" verify "$snapshot" > "$work/pinned-snapshot-verified.json"
    [ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["commit"])' "$work/pinned-snapshot-verified.json")" = "$KOSMICKRISP_MESA_COMMIT" ]
fi
if [ -n "$snapshot" ]; then
    snapshot=$(cd -- "$snapshot" && pwd -P)
    python3 "$script_dir/source-snapshot.py" verify "$snapshot" > "$work/snapshot-verified.json"
    source_key=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["archive_sha256"])' "$work/snapshot-verified.json")
    mesa_commit=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["commit"])' "$work/snapshot-verified.json")
    recipe="$work/recipe-$KOSMICKRISP_BUILD_COMMIT-$source_key"
    checkout https://github.com/shadexternals/mesa-kosmickrisp.git "$KOSMICKRISP_BUILD_COMMIT" "$recipe"
    if [ ! -f "$recipe/externals/mesa/meson.build" ]; then
        rmdir "$recipe/externals/mesa"
        python3 "$script_dir/source-snapshot.py" extract "$snapshot" "$recipe/externals/mesa"
    fi
    python3 "$script_dir/source-snapshot.py" check-source "$snapshot" "$recipe/externals/mesa"
    export MESA_GIT_SHA1_OVERRIDE="$mesa_commit"
else
    recipe="$work/recipe"
    checkout https://github.com/shadexternals/mesa-kosmickrisp.git "$KOSMICKRISP_BUILD_COMMIT" "$recipe"
    git -C "$recipe" submodule update --init --recursive --depth=1
    [ "$(git -C "$recipe/externals/mesa" rev-parse HEAD)" = "$KOSMICKRISP_MESA_COMMIT" ]
    git -C "$recipe/externals/mesa" reset --hard --quiet "$KOSMICKRISP_MESA_COMMIT"
    for patch in "$script_dir"/patches/*.patch; do
        [ -e "$patch" ] || continue
        git -C "$recipe/externals/mesa" apply --index "$patch"
    done
    source_key=$(git -C "$recipe/externals/mesa" write-tree)
    mesa_commit="$KOSMICKRISP_MESA_COMMIT"
    unset MESA_GIT_SHA1_OVERRIDE
fi
driver_build="$work/driver-build-$source_key"
checkout https://github.com/KhronosGroup/Vulkan-Loader.git "$VULKAN_LOADER_COMMIT" "$work/loader"
checkout https://github.com/KhronosGroup/Vulkan-Headers.git "$VULKAN_HEADERS_COMMIT" "$work/headers"

cmake -S "$recipe" -B "$driver_build" \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_OSX_ARCHITECTURES=x86_64 \
    -DCMAKE_OSX_DEPLOYMENT_TARGET=26.0
cmake --build "$driver_build" --parallel "$(sysctl -n hw.ncpu)"
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

cp -L "$driver_build/outputs/libvulkan_kosmickrisp.dylib" "$output/"
cp -L "$work/loader-build/loader/libvulkan.1.dylib" "$output/"
# --prefer-static does not force Mesa's zlib fallback to build statically.
# Bundle the exact target build, never the ARM64 Homebrew copy.
if otool -L "$output/libvulkan_kosmickrisp.dylib" | grep -q '@rpath/libz.1.dylib'; then
    cp -L "$driver_build/externals/mesa/build-target/subprojects/zlib-1.3.1/libz.1.dylib" "$output/"
    chmod u+w "$output/libvulkan_kosmickrisp.dylib"
    install_name_tool -change '@rpath/libz.1.dylib' '@loader_path/libz.1.dylib' "$output/libvulkan_kosmickrisp.dylib"
fi
# Preserve the API version from the actual Mesa build. The ICD path is relative
# to the manifest, not a Mach-O @loader_path expression.
python3 - "$driver_build/outputs/kosmickrisp_mesa_icd.json" "$output/kosmickrisp_icd.json" <<'PY'
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
cp "$recipe/LICENSE.txt" "$output/licenses/build-recipe.txt"
cp "$recipe/externals/mesa/docs/license.rst" "$output/licenses/mesa.rst"
mkdir -p "$output/licenses/mesa" "$output/licenses/vulkan-headers"
cp -R "$recipe/externals/mesa/licenses/." "$output/licenses/mesa/"
cp "$work/loader/LICENSE.txt" "$output/licenses/vulkan-loader.txt"
cp "$work/headers/LICENSE.md" "$output/licenses/vulkan-headers.md"
cp -R "$work/headers/LICENSES/." "$output/licenses/vulkan-headers/"
# Cross builds use Mesa's pinned zlib fallback when no target pkg-config exists.
if [ -f "$recipe/externals/mesa/subprojects/zlib-1.3.1/README" ]; then
    cp "$recipe/externals/mesa/subprojects/zlib-1.3.1/README" "$output/licenses/zlib.txt"
fi
cp "$script_dir/pins.env" "$output/SOURCE.txt"
printf 'MESA_EFFECTIVE_COMMIT=%s\nMESA_SOURCE_KEY=%s\n' "$mesa_commit" "$source_key" >> "$output/SOURCE.txt"
if [ -n "$snapshot" ]; then
    cp "$snapshot/snapshot.json" "$output/SOURCE-SNAPSHOT.json"
    echo 'MESA_SOURCE_PROFILE=committed-snapshot' >> "$output/SOURCE.txt"
else
    echo 'MESA_SOURCE_PROFILE=pinned-with-patches' >> "$output/SOURCE.txt"
fi
for patch in "$script_dir"/patches/*.patch; do
    [ -z "$snapshot" ] || break
    [ -e "$patch" ] || continue
    echo "PATCH=$(basename "$patch") $(shasum -a 256 "$patch" | cut -d' ' -f1)"
done >> "$output/SOURCE.txt"
{
    clang --version
    /usr/bin/clang --version
    xcrun --sdk macosx --show-sdk-version
    cmake --version
    meson --version
    python3 --version
    brew list --versions llvm spirv-tools spirv-llvm-translator libclc python@3.13
    python3 -m pip freeze
} > "$output/BUILD-TOOLS.txt"
cp "$driver_build/externals/mesa/build-target/meson-info/intro-buildoptions.json" "$output/BUILD-OPTIONS.json"
cp "$driver_build/externals/mesa/build-target/meson-info/intro-compilers.json" "$output/BUILD-COMPILERS.json"

(cd "$output" && shasum -a 256 ./*.dylib kosmickrisp_icd.json SOURCE.txt BUILD-TOOLS.txt BUILD-OPTIONS.json BUILD-COMPILERS.json > SHA256SUMS)
