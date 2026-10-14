#!/bin/bash
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "$0")" && pwd -P)
source "$script_dir/pins.env"
output=${1:?usage: package-dxvk.sh OUTPUT_DIRECTORY}
if [ -e "$output" ] || [ -L "$output" ]; then
    echo "DXVK output already exists: $output" >&2
    exit 1
fi
work=$(mktemp -d "${TMPDIR:-/tmp}/kosmickrisp-dxvk.XXXXXX")
trap 'rm -rf "$work"' EXIT
url="https://github.com/doitsujin/dxvk/releases/download/v$DXVK_KOSMICKRISP_VERSION/dxvk-$DXVK_KOSMICKRISP_VERSION.tar.gz"
curl -fsSL --retry 3 -o "$work/dxvk.tar.gz" "$url"
echo "$DXVK_KOSMICKRISP_SHA256  $work/dxvk.tar.gz" | shasum -a 256 -c -
tar -xzf "$work/dxvk.tar.gz" -C "$work"
payload="$work/dxvk-$DXVK_KOSMICKRISP_VERSION"
for arch in x32 x64; do
    for dll in d3d8 d3d9 d3d10core d3d11 dxgi; do
        test -s "$payload/$arch/$dll.dll"
    done
done
bundle="$work/bundle"
mkdir -p "$bundle/x32" "$bundle/x64"
for arch in x32 x64; do
    for dll in d3d8 d3d9 d3d10core d3d11 dxgi; do
        cp "$payload/$arch/$dll.dll" "$bundle/$arch/"
    done
done
cp "$script_dir/licenses/dxvk-$DXVK_KOSMICKRISP_VERSION.txt" "$bundle/LICENSE.txt"
printf 'DXVK_VERSION=%s\nDXVK_ARCHIVE_SHA256=%s\nDXVK_URL=%s\n' \
    "$DXVK_KOSMICKRISP_VERSION" "$DXVK_KOSMICKRISP_SHA256" "$url" > "$bundle/SOURCE.txt"
(cd "$bundle" && shasum -a 256 x32/*.dll x64/*.dll LICENSE.txt SOURCE.txt > SHA256SUMS)
mkdir -p "$(dirname -- "$output")"
mv "$bundle" "$output"
