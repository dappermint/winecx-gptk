# kosmickrisp lane: measured results

local runs, not ci. apple a18 pro, macos 27.2, the published 4.7.51 runtime
(wine 11.17) with the lane's bundle installed beside it, disposable prefixes.
driver: recipe `bac93e0`, mesa `063dfae`, plus the carried fillModeNonSolid
patch.

## driver and dxvk

| check | result |
|---|---|
| host probe, x86_64 under rosetta, relocated path with spaces | pass, driver id 28 kosmickrisp, vulkan 1.4.363 |
| windows probe x86_64 and i686: device, surface, swapchain, present | pass |
| compute shader with exact readback, both architectures | pass |
| windowed, resized, borderless-fullscreen, restored swapchains | pass |
| upstream dxvk 3.1.1 d3d9, x86_64 and i686 | pass, device created |
| upstream dxvk 3.1.1 d3d11, x86_64 and i686 | pass, feature level 11_1 |
| same dxvk probes without the fillModeNonSolid patch | fail: `Device does not support required feature 'fillModeNonSolid'` |

`wine64` in the same tree still selects moltenvk; only `wine-kosmickrisp`
selects kosmickrisp.

capabilities that matter to dxvk: geometryShader 1, fillModeNonSolid 1 (with
the patch), tessellationShader 1, VK_EXT_transform_feedback absent. dxvk 3.1.1
treats transform feedback as optional, so it creates devices without it and
d3d10/d3d11 stream output is unavailable.

## games

| game | renderer | result |
|---|---|---|
| hades (steam build 10929685) | dx11 through upstream dxvk 3.1.1 | renders gameplay; fl 11_1 device |
| hades | native vulkan (`x64Vk`) | renders gameplay |
| peak (steam build 25306743, unity 6000.3.15f1) | native vulkan (`-force-vulkan`) | reached gameplay in one of two runs; the other stayed black after steam and eos login. moltenvk reached gameplay in the same setup. cause not yet found |

## d3d9 games

32-bit, so dxvk's x32 dlls under wow64. frame rates come from wine's `fps`
debug channel, which traces vulkan presents (dxvk) and wined3d presents alike,
so both backends are measured the same way. wined3d is the baseline because it
is the only 32-bit d3d9 path whisky has today: d3dmetal has no i386 half, and
dxvk 1.10.3's d3d9 cannot create a device on moltenvk.

| game | kosmickrisp + dxvk 3.1.1 | wined3d |
|---|---|---|
| portal (build 19017868), `testchmb_a_13`, 1280x720, vsync off | renders correctly; mean 72.3 fps, min 27.1, max 99.3 | mean 51.1 fps, min 4.7, max 107.3 |
| sonic adventure dx (build 411939) | d3d9 device created; the game then needs exclusive fullscreen at 1920x1080, the mode change fails and it stays black | same failure |

the portal runs were not scene-matched: there was input during both, so the
views differ and the averages are indicative, not a benchmark. a source
timedemo would make it repeatable. source resolves `d3d9.dll` from `bin/`, not
beside `hl2.exe`; a dll placed only beside the exe silently falls back to
wined3d.

sonic adventure dx's failure is the host's display-mode change, not the
driver: wined3d fails identically, and winemac has no virtual desktop to work
around it. running its `AppLauncher.exe` once to pick windowed mode is the way
past it.

evidence is screenshots and game logs. no comparison with moltenvk yet; moltenvk has no
working 32-bit d3d9 path to compare against.

## known gaps

- transform feedback: not implemented in kosmickrisp, so no d3d10/d3d11 stream
  output.
- fillModeNonSolid is partial: metal has no point fill mode, so
  VK_POLYGON_MODE_POINT draws as wireframe. no test draws wireframe yet, and
  emulated geometry/tessellation draws may draw shared edges twice.
- x86_64 only, for the rosetta wine host; the arm64 lane is not covered.
- the ci probe step needs a metal device. it passes on apple silicon; it has
  not been run on the hosted macos-26 runner.
- no whisky ui to pick the driver yet; the launcher, or the same environment
  variables per program, is the way in.
