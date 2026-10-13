# Bounded KosmicKrisp investigation

These opt-in tools collect startup, compile, present and host evidence for
LEGO Star Wars: The Skywalker Saga. They require an externally built experimental
driver with the logging hooks. They are not run by runtime packaging or CI.

A plan JSON supplies `runtime`, `bottle`, `bundle`, `profile`, `game_executable`,
`cache_seed`, and `driver_sha256`. Paths must exist. `profile` is the game's
Warner Bros. profile containing `PCCONFIG.TXT` and `SAVEDGAMES`; `bundle` contains
the driver, Vulkan loader and ICD manifest. Optional `dxvk_directory` requires
`dxvk_sha256` entries for `d3d11.dll` and `dxgi.dll`.

```sh
python3 runtime/kosmickrisp/investigate.py \
  --plan /path/to/plan.json --out /tmp/kk-investigation --max-runs 2
python3 runtime/kosmickrisp/investigate.py \
  --plan /tmp/kk-investigation/plan.json --out /tmp/kk-investigation --analyze-only
```

The startup deadline is 600 seconds. Run with the desktop unlocked and the game
window visible; Accessibility and Screen Recording permissions are required.
The coordinator isolates caches, verifies driver identity, records bounded
samples and host logs, and stops the campaign for an invalid run or serious
host fault. It journals and restores save/configuration and local DXVK DLL
changes. A minimized or missing game window invalidates capture.

Use `--resume` only with the same saved plan; it attempts transaction recovery
before continuing. Reports distinguish missing evidence, supported hypotheses
and reviewed mechanisms. Reaching the menu establishes startup progress, but
does not verify level-loading or subsequent gameplay correctness.
