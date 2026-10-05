# kosmickrisp lane: performance notes

what limits kosmickrisp + upstream dxvk here, what can be tuned without patching
either, and what needs driver work. each item says whether it was measured or is
still a hypothesis. numbers are from an apple a18 pro, macos 27.2, the 4.7.51
runtime, x86_64 under rosetta.

## fixed: memory budget reported at a quarter of the real value

measured. `VK_EXT_memory_budget` comes from mesa's
`os_get_available_system_memory`, which on macos multiplies the free and
inactive page counts from `host_statistics64` by `PAGE_SIZE`. those counts are
in the kernel's 16 KiB pages, but an x86_64 process under rosetta sees
`PAGE_SIZE` and `vm_page_size` as 4 KiB, so the result was a quarter of the
truth. dxvk 3.1.1 logged `Budget: 249 MiB` on a 5.33 GiB heap.

`patches/0002-util-use-the-host-page-size-...` uses `host_page_size()`, which
returns the kernel's page size in x86_64 and arm64 processes alike; dxvk then
logs `Budget: 1.5 GiB` with about 1.1 GiB free plus inactive on the host.
dxvk uses the budget for eviction and allocation decisions, so memory-hungry
games should benefit most; the effect on frame rate has not been measured.
the bug is in mesa's shared util code and affects every mesa driver built for
x86_64 on apple silicon, so it belongs upstream in mesa.

## no graphics pipeline libraries: pipelines compile at draw time

measured that it applies, not how much it costs. kosmickrisp does not expose
`VK_EXT_graphics_pipeline_library`, and dxvk logs `Graphics pipeline
libraries not supported`. dxvk then compiles each full pipeline the first time
a render state appears, on its compiler threads (6 here), which shows up as
stutter. the low minimums (portal 27 fps against a 72 fps mean, sonic
adventure dx 9 fps) fit this, but nothing yet ties those dips to compiles.

without patching, the only lever is caching below dxvk, which dropped its
on-disk state cache in 2.0. mesa's shader cache is compiled into kosmickrisp
and metal keeps its own per-process cache, so a second run of the same scene
should dip less. to check: run it twice and compare the minimums, and keep
`MESA_SHADER_CACHE_DIR` somewhere persistent per bottle.

the real fix is implementing `VK_EXT_graphics_pipeline_library` in kosmickrisp,
which is a driver feature, not a tuning change.

## emulated paths split metal render passes

hypothesis from the source. geometry shaders, tessellation, triangle fans,
primitive restart and 8-bit indices run as compute passes before the draw, and
the driver notes that these end the current render pass
(`TODO_KOSMICKRISP ... split render pass` in `kk_cmd_draw.c`). every extra pass
costs load/store bandwidth on a tile-based gpu. no setting avoids it; a metal
system trace would show how often it happens per frame in a given game.

## the whole stack runs under rosetta

structural. wine, the driver's cpu work (turning vulkan commands into metal)
and dxvk all run as translated x86_64 code. the arm64 lane is the long-term
fix; this lane is x86_64 only.

## settings to try without patches

- frame pacing: kosmickrisp exposes `VK_KHR_present_wait` and `present_id`,
  which dxvk's latency features need. `dxvk.latencySleep = True`, or
  `d3d9.maxFrameLatency = 1` / `dxgi.maxFrameLatency = 1`, in `dxvk.conf` should
  smooth frame times; they are not expected to raise the average.
- keep msync on, as the runtime already does.
- keep `MESA_SHADER_CACHE_DIR` persistent per bottle, see above.

## measuring before optimizing

1. scene-matched runs: a source timedemo for portal, a fixed save for sonic
   adventure dx. the results in `kosmickrisp-results.md` had player input.
2. `DXVK_HUD=fps,frametimes,gpuload,submissions,pipelines`: shows whether dips
   line up with pipeline compiles, and whether the gpu is busy or waiting.
3. metal system trace (instruments) on the wine process: render passes per
   frame and gpu utilisation.
4. wine's `fps` debug channel, as used for the results, for numbers that are
   comparable across dxvk and wined3d.

## larger work, by expected impact

| change | size | expected effect |
|---|---|---|
| `VK_EXT_graphics_pipeline_library` in kosmickrisp | large driver feature | removes compile stutter |
| descriptor buffer / descriptor heap support | large | lower cpu cost per draw in dxvk 3.x |
| fewer render pass splits for emulated paths | medium to large | less bandwidth per frame in games that hit them |
| arm64 lane | structural | no rosetta for driver and wine |
