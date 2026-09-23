#!/bin/bash
# boot a runtime on a fresh clone of the arm64 bottle: prefix upgrade, mono, then casualties unknown
set -u
ID="${1:-whisky-arm64-5.1.0}"
RT="$HOME/Library/Application Support/com.dappermint.WhiskyPreview/Runtimes/$ID/Wine"
SRC=~/Library/Containers/com.franke.Whisky/Bottles/780CB9F9-8423-423D-A0A6-E526B02AA446
PFX=/Volumes/Wine/scratch/arm64-${2:-rt510}
GAME="/Users/dappy/Games/SteamWindows/steamapps/common/Casualties Unknown Demo"
LOG=/Volumes/Wine/scratch/arm64-${2:-rt510}

rm -rf "$PFX"; cp -Rc "$SRC" "$PFX"
eval "$('/Applications/Whisky Preview.app/Contents/Resources/WhiskyCmd' shellenv arm64 2>/dev/null | grep '^export' | grep -v 'export PATH\|WINEPREFIX\|WINEDEBUG\|WINEDLLOVERRIDES')"
export WINEPREFIX="$PFX" WINESERVER="$RT/bin/wineserver" WINEDEBUG="err+all,fixme-all"
"$RT/bin/wineserver" -k 2>/dev/null

echo "== wineboot -u"
/usr/bin/timeout 180 "$RT/bin/wine" wineboot -u > "$LOG-boot.log" 2>&1; echo "boot exit=$?"
"$RT/bin/wineserver" -w
echo "mono in prefix: $(ls "$PFX/drive_c/windows/mono" 2>/dev/null | tr '\n' ' ')"
echo "mscoree: $(strings -a "$PFX/drive_c/windows/system32/mscoree.dll" 2>/dev/null | grep -m1 -o 'Wine builtin DLL')"
for f in xtajit64.dll winemetal.dll lsteamclient.dll; do printf "%s " "$f"; [ -e "$PFX/drive_c/windows/system32/$f" ] && echo ok || echo MISSING; done

echo "== .net"
printf 'class P{static void Main(){System.Console.WriteLine("dotnet ok "+System.Environment.Version+" "+System.Runtime.InteropServices.RuntimeInformation.ProcessArchitecture);}}' > "$PFX/drive_c/hello.cs"
cd "$PFX/drive_c"
MONOD='C:\windows\mono\mono-2.0\lib\mono\4.5'
/usr/bin/timeout 120 "$RT/bin/wine" "$MONOD\\mcs.exe" /out:hi.exe hello.cs > "$LOG-csc.log" 2>&1; echo "csc exit=$?"
/usr/bin/timeout 60 "$RT/bin/wine" hi.exe 2>&1 | grep -v 'err:\|fixme:' | tail -2

echo "== casualties unknown"
export WINEDLLOVERRIDES="mscoree,mshtml=;d3d11,d3d10core,dxgi=n,b;winemetal=b"
P="$PFX/drive_c/users/crossover/AppData/LocalLow/Orsoniks/CasualtiesUnknown/Player.log"
rm -f "$P"
cd "$GAME"
/usr/bin/timeout 60 "$RT/bin/wine" ./CasualtiesUnknown.exe > "$LOG-cu.log" 2>&1; echo "cu exit=$? (124 = still running at timeout)"
"$RT/bin/wineserver" -k 2>/dev/null
grep -iE 'Direct3D:|Version:|Renderer|d3d11:|Failed|Input initialized' "$P" 2>/dev/null | head -8
grep -iE 'err:|metal view' "$LOG-cu.log" | grep -v 'ZwLoadDriver\|wineusb\|winebth' | cut -c1-200 | head -8
