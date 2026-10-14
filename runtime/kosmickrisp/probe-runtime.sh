#!/bin/bash
set -euo pipefail
export DYLD_FALLBACK_LIBRARY_PATH="$PWD/Libraries/Wine/lib"
# same shape as the window check: build the probe, run it in a fresh
# prefix, refuse the tree if the driver cannot create a device. the
# probes refuse a fallback icd themselves, so this tests kosmickrisp
# and nothing else. the host probe runs from a copy in a path with
# spaces, the way a relocated runtime is found in a bottle.
headers=${1:?usage: probe-runtime.sh VULKAN_HEADERS_DIRECTORY}
mkdir -p kosmickrisp-probes
relocated="$RUNNER_TEMP/kosmickrisp relocated"
rm -rf "$relocated"; mkdir -p "$relocated"
cp -R Libraries/Wine/lib/kosmickrisp "$relocated/driver"
/usr/bin/clang -arch x86_64 -O2 -Wall -Wextra -Werror -I"$headers" \
  tests/kosmickrisp_probe.c -o kosmickrisp-probes/host-probe
VK_DRIVER_FILES="$relocated/driver/kosmickrisp_icd.json" \
  kosmickrisp-probes/host-probe "$relocated/driver/libvulkan.1.dylib" \
  > kosmickrisp-probes/host.txt 2>&1 || { cat kosmickrisp-probes/host.txt; exit 1; }

export PATH="${NIX_TOOL_PATH:-}$PATH"
export WINEPREFIX="$PWD/kosmickrisp-prefix"
export WINEDLLOVERRIDES="mscoree=d;mshtml=d"
WINEDEBUG=-all ./Libraries/Wine/bin/wine64 wineboot -u >/dev/null 2>&1 || true
for target in x86_64 i686; do
  "$target-w64-mingw32-gcc" -O2 -Wall -Wextra -Werror -I"$headers" \
    tests/kosmickrisp_probe.c -o "kosmickrisp-probes/$target.exe"
  WINEDEBUG=-all ./Libraries/Wine/bin/wine-kosmickrisp \
    "kosmickrisp-probes/$target.exe" --present > "kosmickrisp-probes/$target.txt" 2>&1 || {
    echo "::error::kosmickrisp $target probe failed"
    tail -20 "kosmickrisp-probes/$target.txt"
    exit 1
  }
done
grep -hE '^(device|api|geometryShader)=' kosmickrisp-probes/x86_64.txt | tee -a "$GITHUB_STEP_SUMMARY"
echo "kosmickrisp: host, x86_64 and i686 probes passed" | tee -a "$GITHUB_STEP_SUMMARY"

# Use the managed prefix so the probe verifies the packaged helper,
# native overrides and DLL lookup as well as DXVK and driver identity.
source runtime/kosmickrisp/pins.env
(cd Libraries/DXVK3 && shasum -a 256 -c SHA256SUMS)
./Libraries/Wine/bin/wineserver -k || test "$?" -eq 1
./Libraries/Wine/bin/wineserver -w
./Libraries/Wine/bin/wine-kosmickrisp-dxvk --install-prefix > kosmickrisp-probes/install.json
for target in x86_64 i686; do
  dir="kosmickrisp-probes/dxvk-$target"
  mkdir -p "$dir"
  "$target-w64-mingw32-gcc" -O2 tests/d3d11feat.c -ld3d11 -ldxgi -ldxguid -luuid -o "$dir/d3d11.exe"
  "$target-w64-mingw32-gcc" -O2 tests/d3d9feat.c -ld3d9 -o "$dir/d3d9.exe"
  "$target-w64-mingw32-gcc" -O2 tests/d3d11_constants.c -ld3d11 -ld3dcompiler -ldxgi -o "$dir/d3d11constants.exe"
  for api in d3d9 d3d11 d3d11constants; do
    marker="[ ok ] D3D11CreateDevice"
    [ "$api" = d3d9 ] && marker="[ ok ] CreateDevice D3DDEVTYPE_HAL"
    [ "$api" = d3d11constants ] && marker="RESULT: constant-array readback passed"
    status=0
    ( cd "$dir" && WINEDLLOVERRIDES="d3d9,d3d11,dxgi=n;mscoree=d;mshtml=d" \
        DXVK_LOG_LEVEL=info DXVK_LOG_PATH="$PWD" WINEDEBUG=-all \
        "$GITHUB_WORKSPACE/Libraries/Wine/bin/wine-kosmickrisp-dxvk" "./$api.exe" > "$api.txt" 2>&1 ) || status=$?
    if [ "$status" -eq 0 ] && grep -qF "$marker" "$dir/$api.txt" && grep -qF "DXVK: v$DXVK_KOSMICKRISP_VERSION" "$dir/${api}_"*.log && grep -qE 'Driver[[:space:]]*: KosmicKrisp' "$dir/${api}_"*.log; then
      echo "dxvk $DXVK_KOSMICKRISP_VERSION $api $target: $(grep -F "$marker" "$dir/$api.txt")" | tee -a "$GITHUB_STEP_SUMMARY"
    else
      echo "::error::dxvk $DXVK_KOSMICKRISP_VERSION $api $target did not create a device on kosmickrisp"
      cat "$dir/${api}_"*.log 2>/dev/null | grep -E 'Skipping|err:|warn:' | head -10
      exit 1
    fi
  done
done
./Libraries/Wine/bin/wineserver -k || test "$?" -eq 1
./Libraries/Wine/bin/wineserver -w
./Libraries/Wine/bin/wine-kosmickrisp-dxvk --restore-prefix > kosmickrisp-probes/restore.json
