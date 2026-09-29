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

evidence is screenshots and game logs. no frame-time or performance comparison
with moltenvk has been made yet.

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
