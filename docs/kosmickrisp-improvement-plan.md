# KosmicKrisp gaming improvement plan

Status: implementation and measurement plan, updated 2026-10-13 after
[squidbus's feedback](https://github.com/shadexternals/mesa/pull/2#issuecomment-6141082195).
The memory-budget calculation and DXVK packaging have new focused local
validation. No new gameplay benchmarks were run to update this plan.

Improve D3D9/D3D11 compatibility, compilation latency, frame pacing and CPU
cost through winecx-gptk, DXVK and the existing KosmicKrisp work. Start with
reproducible integration, then use controlled experiments to choose changes.
Keep the lane opt-in throughout this plan. Each numbered task should produce
an independently reviewable change or experiment report.

## Starting point

- `runtime/kosmickrisp/pins.env` pins Mesa `063dfae` and DXVK 3.1.1. The build
  applies two local patches, for non-solid fill and host page size.
- The neighboring Mesa checkout is clean at `91518988cc7`; the published
  fork's main branch remains at `0fa70084064`. The local candidate includes
  GPL, independent interpolation, fast linking, shader objects, variant history
  and Metal archive/concurrency mechanisms. Feature advertisement does not
  establish conformance or game reliability. Use explicit revisions when
  requesting tests; the newest local changes are not yet on published main.
- CI on upstream main packages the ICD and launcher. DXVK payload packaging
  is implemented separately in
  [winecx-gptk PR #20](https://github.com/dappermint/winecx-gptk/pull/20), currently
  open for review. It adds `DXVK3/x32` and `DXVK3/x64`, licensing, release
  provenance and file checksums; the existing device probes use those DLLs.
  All eight focused tests and real archive/hash/PE architecture checks pass.
  A complete runtime build and GPU device probes remain to be run for that PR.
- `stutter.py` expects a `DXVK3` payload and a `dxvk-prefix.py` helper that the
  current workflow does not install. It already isolates Mesa and DXVK caches
  and produces frame/compile summaries.
- DXVK 3.1.1 already recognizes KosmicKrisp as a tiler. Adding that recognition
  or forcing tiler mode is not a new optimization.
- Existing reports show warm-run shader churn, but do not prove shader
  equivalence or identify every cause. Historical runs are diagnostic evidence,
  not controlled proof of a performance improvement.

## Upstream coordination and immediate priorities

`shadexternals/mesa` is maintained for shadPS4, not as a home for DXVK support.
Squidbus does not intend to maintain the broader downstream work there. Its
PRs #1 and #2 are closed; keep them as historical context rather than reopening
them or requesting that this fork carry our project.

1. Resolve Freedesktop account verification/fork permissions through the
   user's own communication, then submit focused Mesa MRs. The Vulkan Discord
   Apple channel is another suggested place for the user to contact developers.
2. Start upstream review with the standalone macOS memory-budget fix. Squidbus
   said the change seems good, but did not give a formal `Reviewed-by` tag.
   The exact PR function passed isolated ARM64 and Rosetta validation with
   warnings treated as errors. Under Rosetta, `PAGE_SIZE` is 4096 while
   `host_page_size()` is 16384; the old calculation returns one quarter of
   the expected result. Full Mesa testing and upstream acceptance remain open.
3. Complete review and experimental CI validation of winecx-gptk PR #20.
   Then implement the reversible prefix helper as its own change. Packaging
   alone does not complete task 2 or make the backend selectable in Whisky.
4. Make fill-mode support correct before proposing it as complete upstream.
   `VK_POLYGON_MODE_POINT` must have point emulation, not wireframe fallback.
   Squidbus reports separately checking the line case through CTS; this is
   not CTS coverage of our whole patch. Use upstream geometry-shader support
   when available, investigate Dozen's generated geometry shader as a design
   reference, and account for pipelines that already have a geometry shader.
   Validate point/line output and relevant conformance cases, including
   geometry/tessellation paths, before claiming `fillModeNonSolid` is complete.
5. Keep the GPL/cache candidate separate and experimental. Reproduce and
   isolate the Skywalker Saga level-loading hang before selecting it as a
   stable snapshot. Cache mock tests and successful menu startup do not
   establish correct level loading or gameplay.

Preserve human authorship and required AI disclosure when preparing upstream
patches. Authorship, submission assistance and formal review are separate;
credit only the contributions and review tags actually provided.

## Phase 1 — reproduce and integrate the driver

### 1. Freeze a tested driver snapshot

Select an explicit Mesa commit plus a checksummed patch series. Preserve the
working tree; do not silently include or discard its uncommitted edits. Use a
separate build checkout. For CI, either carry patches against the existing
recipe's pinned Mesa, or update the recipe and its submodule pin together; the
current equality check means changing only `KOSMICKRISP_MESA_COMMIT` is not enough.

Record Mesa/recipe/loader/header revisions, patch hashes, compiler versions,
driver hash and cache compatibility identity. Check which existing patches
are already included before applying them again. Refresh the capability and
performance documents to distinguish the old bundle from the candidate.

Acceptance: clean and reused build directories reproduce the selected source;
the existing x86_64, relocation, linkage and signing checks pass; host and
x86/x64 Wine probes pass device creation, compute readback and presentation.
Add focused GPL/linking/rendering checks, including interpolation correctness,
before treating the new advertisement as usable. Complete point polygon-mode
emulation and its correctness checks before treating non-solid fill as fully
supported; the carried partial patch is an experimental compatibility aid.
Run the relevant existing
Mesa cache/concurrency tests for the exact selected snapshot.

### 2. Package DXVK and make bottle setup reversible

The payload portion is implemented in PR #20 and awaits review and full
experimental CI validation. Package the pinned, verified DXVK x32/x64 DLLs and
license in the experimental artifact. Implement the prefix helper expected by
the harness as the next separate change, with an explicit
install/status/restore lifecycle. Journal previous files and DLL overrides;
detect conflicting backend installations and interrupted transactions. Handle
game-local DLL lookup, including Source's `bin/` directory.

Set persistent, writable per-bottle defaults for Mesa and DXVK shader caches,
respecting explicit user overrides. Use Wine-accessible paths for the PE DXVK
cache. Keep benchmark cache directories separate. Rely on existing cache
compatibility mechanisms where sufficient; verify invalidation after a driver
change and never reuse incompatible Metal archives.

Extend diagnostics to record the driver actually selected, DXVK DLL hashes and
version, binding model, GPL capability and effective configuration. Capability
support alone must not be reported as proof that DXVK used GPL.

Acceptance: x32 D3D9 and x64 D3D11 load the intended DLLs and driver; a second
process reuses caches; explicit cache overrides work; restore recovers original
files/settings, including after interrupted installation. Backend mismatch is
visible. Ordinary runtime launch and packaging checks continue to pass.

## Phase 2 — establish reliable experiments

### 3. Extend the existing harness and define a baseline

Extend `stutter.py`, `stutter-games.json` and the bounded investigation tools.
Use a recorded Portal 2 timedemo, a repeatable Hades encounter, and a fixed
Skywalker save/progression sequence. Sonic Adventure DX remains a capped
32-bit compatibility case; PEAK remains a native-Vulkan startup case. Inspect
their actual availability and automation before selecting the final workload set.

Record exact scene, resolution, game build, save/config, runtime and driver
hashes, DXVK settings, cache state, OS, GPU, visibility and termination reason.
Report startup/loading separately from gameplay. Capture median/p95/p99 frame
times, 1% lows, hitches over 50 ms, CPU/GPU time, process memory, compilation
counts and durations, archive hits/misses, queue delay and presentation waits.
Treat temporal overlap with compilation as correlation, not proof of causation.

Use separate caches per condition, then cold/warm/warm2 runs within each
condition. Include archived pipeline/history caches in the inventory; deleting
only the obvious shader directory may not establish a cold run. Perform at
least three repetitions and vary condition order. Keep detailed diagnostic
runs separate from minimally instrumented performance runs.

Acceptance: repeated control runs establish variability; reports identify
invalid/minimized runs and missing evidence; saves/configurations are restored;
startup success is distinguishable from loading a level and completing gameplay.

### 4. Compare GPL behavior and compiler concurrency

Screen `dxvk.enableGraphicsPipelineLibrary = Auto`, `True`, and `False`, holding
compiler settings fixed. `True` uses GPL when supported but skips DXVK's
background optimized-pipeline compilation; `False` is the diagnostic control.
Then compare default, 2 and 4 compiler threads for the promising GPL conditions.
Avoid a full Cartesian matrix until the screening identifies a useful question.

Measure driver prewarm/recapture activity alongside DXVK workers. If speculative
work competes with demanded work, evaluate bounded admission, deduplication and
demand promotion in the driver's existing queues. A worker-count reduction is
an experiment, not a universal default.

Acceptance: any selected per-game setting improves frame-time results beyond
control variability without unacceptable loading, GPU-time, memory or rendering
regressions. Report tradeoffs and inconclusive results. A provisional decision
rule is at least 10% lower gameplay p99 or 20% fewer hitches, with mean FPS no
more than 5% worse; refine it after measuring baseline noise.

## Phase 3 — targeted DXVK/compiler experiments

### 5. Test constant-array lowering

Build a small DXVK candidate that lets KosmicKrisp handle large constant arrays
instead of enabling DXVK's `LowerConstantArrays` path. KosmicKrisp already calls
`nir_opt_large_constants`, but its Metal output still needs validation.

Compare stock and candidate DXVK on a focused dynamically indexed constant-array
workload and the fixed game scenes. Inspect SPIR-V/MSL, compile time, GPU time,
code size and resource usage. Verify indexing and numerical output.

Acceptance: correct results and a repeatable benefit on representative workloads.
If the driver path expands or slows shaders, retain the existing DXVK behavior.
Deliver the experiment independently of the driver snapshot and other changes.

### 6. Trace and reduce proven shader/layout churn

Add diagnostic correlation from D3D shader identity through emitted SPIR-V,
pipeline layout, Mesa variant key and Metal PSO. Track descriptor mappings,
specialization constants, interpolation, vertex inputs and attachment state.
Measure why warm2 creates new entries and how many are legitimate new variants.

Choose the narrowest repair supported by evidence: stabilize equivalent DXVK
binding layouts, normalize inactive state in Mesa, or move a proven source of
variation out of generated shader code. Do not reuse shaders merely because
their generated source looks similar, and preserve Vulkan layout compatibility.

Acceptance: a reproducer explains duplicate compilation; equivalent work reuses
the cache across processes while meaningful shader/layout differences remain
distinct; output and concurrency checks pass; warm-run compilation counts and
frame-time results improve. Avoid hard-coded game-specific identities.

## Phase 4 — Wine CPU cost and frame pacing

### 7. Profile warm-cache Vulkan calls and 32-bit mapping

Measure x64 and WOW64 separately. Attribute CPU time to DXVK, Wine's Vulkan
conversion, Mesa command recording and Metal. Inspect descriptor updates,
command frequency, allocations/copies, resource mapping and waits.

Confirm whether `VK_EXT_map_memory_placed` or external host memory is actually
used by 32-bit games; both paths already exist in Wine. If descriptor conversion
is significant, test DXVK update templates before changing Wine. Make a focused
Wine patch only for a measured conversion/allocation bottleneck.

Acceptance: lower CPU/frame time on a workload with warm caches, correct handles
and mappings on both architectures, unchanged Vulkan synchronization semantics,
and no regressions in resize, teardown or device creation. A driver compile
stall is not evidence that Wine's call conversion needs rewriting.

### 8. Validate presentation and latency controls

Measure the existing present-wait/id and swapchain-maintenance paths through
Wine. Test windowed, borderless, resize, minimize/restore, Vsync changes and
supported OS versions. Compare default frame latency with 1 and 2; test
`dxvk.latencySleep` independently, since it can worsen pacing in some games.

Acceptance: stable presentation without hangs or black windows; lower measured
latency or better pacing where applicable, with throughput tradeoffs recorded.
Store supported settings per game. Fix Wine or driver WSI only where traces
identify the responsible layer. Reuse the bounded Skywalker investigation for
level-loading failures rather than treating latency settings as a hang fix.

## Phase 5 — expand capabilities after the measured improvements

### 9. D3D10/D3D11 stream output

Plan `VK_EXT_transform_feedback` as a separate driver feature. Inventory the
existing geometry emulation path and partial property declarations; property
values alone do not implement the extension. Cover stream selection, counters,
queries, overflow, indirect draws and rasterization semantics. Wine/DXVK
integration follows a working driver implementation.

Acceptance: relevant conformance coverage, focused D3D stream-output readback
tests, and a real affected game's progression/rendering. Do not advertise support
to bypass device checks before implementing the behavior.

### 10. Descriptor model feasibility

Use phase 4's measurements to decide whether descriptor buffer or descriptor
heap support warrants the larger implementation. Examine Metal representation,
descriptor indexing, alignment, robustness, mutable descriptors and DXVK's
driver eligibility rules. Prototype one path, not both simultaneously.

Acceptance: correct binding and descriptor lifetime behavior plus meaningful
CPU savings in descriptor-heavy scenes. Include DXVK activation in the prototype;
advertising a driver extension does not guarantee its automatic selection.

Native ARM64 Wine/driver hosting and D3D12 through vkd3d-proton are separate
projects with additional runtime/platform requirements. They are not dependencies
for completing this D3D9/D3D11 plan.

The [ARM64 Wine/FEX source investigation](arm64-kosmickrisp-investigation.md)
finds that dappermint already has the native-host foundation. The first isolated
experiment should test native KK through that Wine branch's existing Vulkan
bridge using stock x64 DXVK as a control; ARM64EC DXVK follows separately.
An entitled loader, reproducible native packaging, x86 D3D9 compatibility and
measured full-stack performance remain gates. No end-to-end native Wine test
has been run locally. The isolated ARM64 loader/KK host and rendering probes
now pass, and unmodified upstream DXVK 3.1.1 builds as verified ARM64EC DLLs.
The next dependency is a working native Wine/FEX runtime with its entitled
launcher; these build results do not establish DLL interoperability or gameplay.

## Execution order and completion

Pursue upstream access and the standalone memory fix alongside PR #20 review.
Follow packaging with reversible setup and a reproducible baseline. Resolve
point-fill correctness and the GPL loading regression before promoting the
candidate driver. Implement tasks 1–3 first. Run task 4, then task 5. Use their
results and the
new traces to scope task 6; perform tasks 7–8 on the selected stable candidate.
Scope tasks 9–10 only after identifying the affected games or measured CPU cost.
Do not combine independent experimental changes in the baseline.

The first milestone is an opt-in artifact with a reproducible driver, reversible
DXVK setup, persistent caches, verified backend identity and a repeatable report
for x32 D3D9 and x64 D3D11. The second milestone is a measured improvement with
its individual source/configuration change, correctness results and rollback.
Larger capability work has separate acceptance gates.

Before recommending wider use, exercise macOS 26 and 27, Steam UI/popups, game
startup and actual gameplay, and backend switching with and without an imported
GPTK payload. A local improvement does not remove the experimental publish guard.

## Execution update — 2026-10-13

This checkout now includes the DXVK payload change, reversible prefix setup,
persistent caches, backend diagnostics, committed-source snapshot builds, a
controlled comparison harness and the constant-array experiment. The snapshot
of `91518988cc7` builds and passes focused rendered/cache checks locally.
Matched x64 constant-array measurements are mixed, so stock DXVK remains selected.

See [the local validation report](kosmickrisp-validation-2026-10-13.md) for source
identities, rendering evidence, the Wine 11.18 fixture limitation and open gates.
Controlled gameplay comparison is waiting for authentication of Steam in the
new disposable bottle. The default CI source pin and experimental publish guard
remain in place; the full Wine 11.19 build, macOS 26 acceptance, point-fill
correctness and Skywalker progression are not complete.
