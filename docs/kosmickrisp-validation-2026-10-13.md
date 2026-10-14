# Local KosmicKrisp integration and experiment results

Validation on Apple A18 Pro, macOS 27.2. The local Wine fixture reports
`wine-11.18`; this is not a full rebuild or validation of the repository's
Wine 11.19 CI lane. Source, binaries and logs are under
`work/kosmickrisp-improvements/`, which is excluded from Git.

## Integration delivered

- Pinned upstream DXVK 3.1.1 is packaged in the experimental lane with licensing,
  release provenance and file checksums. The real release archive and all ten
  DLL hashes/PE architectures were verified locally.
- The prefix helper provides install/status/restore, stopped-prefix checks,
  original DLL/symlink and targeted registry backup, atomic replacement, a
  durable write-ahead journal, interrupted-operation recovery and conflict
  detection. It supports game-local DLLs, including Source's `bin/` directory.
- The launcher validates the managed prefix, selects the isolated loader/ICD,
  enforces native DXVK, preserves other DLL overrides, and sets persistent
  per-bottle Mesa and Wine-accessible DXVK cache defaults. Restoration works
  without the payload or driver. Explicit cache overrides remain effective.
- Committed Mesa source snapshots are checksummed, deterministically compressed,
  checked against archive commit metadata and rechecked before reuse. The build
  records effective source identity and binary hashes. The original CI source
  pin is retained; the candidate is an explicit local build option.
- CI now exercises the packaged prefix helper and restores its changes. Native
  x86/x64 device probes also require DXVK version and KosmicKrisp driver evidence.
  A D3D11 constant-array render/readback probe supplements device creation.
- The comparison harness freezes scene/fixture/binary/configuration inputs,
  separates conditions and repetitions, distinguishes GPL support from activation,
  and summarizes variability. Visibility, scene execution and correct gameplay
  still require review before selecting settings.

## Frozen driver and rendered checks

| Input/check | Result |
| --- | --- |
| Mesa commit | `91518988cc7a9876a30934b1952adadee4e0a8cd` |
| Committed tree | `51f1f09aecd324760c2aeb7dbe78cee951e782ab` |
| Source archive SHA-256 | `4179929b916c4c58d843393358dea90b90ace59bef5dd01db4ca3d9a12fc9957` |
| Packaged driver SHA-256 | `925efc21bb0937bf3d80dd8370576e63ddbfdbc6ddb7900cb137daea955e0f8c` |
| Clean isolated driver/loader build | Pass; x86_64, relocatable dependencies and signing checked |
| Extracted committed source recheck and reused build | Pass; the reused build produces the identical driver SHA-256 |
| Host probe | Driver ID 28, Vulkan 1.4.363; reports the frozen source revision |
| x64 and x86 Vulkan compute/readback and presentation | Pass on initialized prefixes; resize, borderless and restored presentation pass |
| x64 and x86 DXVK 3.1.1 D3D9/D3D11 | Device creation passes through the managed launcher |
| x64 and x86 constant-array probe | 20,736 pixels checked per run; all four integer channels match |
| Driver PSO cache and Metal specialization executables | Both pass when run directly under Rosetta |
| Mesa bridge Python tests | 20 pass; Meson's cross-test runner skips the two executable tests, so direct execution was required |
| Application cache identity across old/new driver binaries | Different pipeline-cache UUIDs observed; not a proof of every compatibility transition |

The existing out-of-tree variant replay produced identical image hash
`4d9136b22554b5eb` in cold, warm and warm2 processes. Cold recording had 12 draws
above 5 ms; warm and warm2 had none, with maximum draw times of 0.1 and 2.0 ms.
This validates the focused replay and cache reuse, not game frame-rate gains
or full Vulkan conformance.

A fresh Wine 11.18 fixture prefix initially lacked
`syswow64/kernel32.dll` and failed before reaching Vulkan. The successful x86
checks used a disposable clone of an initialized prefix. The helper now refuses
incomplete prefixes instead of reporting a successful setup. Fresh-prefix
acceptance on the actual Wine 11.19 CI artifact remains necessary.

The x86 trace explicitly records `win32u_vkMapMemory2KHR Using placed mapping`.
Readback passes through that path. This confirms existing Wine mapping support
in the fixture and does not justify implementing a replacement mapping path.

## Constant-array experiment

The candidate adds KosmicKrisp to DXVK's driver list that lets the driver handle
large constant arrays. Both x64 builds use DXVK v3.1.1 revision
`b1a1c99ab52b687cf950d62c88bc2fa316b41663`, the same submodule revisions, compiler
and release settings; the candidate differs by the one-line experimental patch.
The patch is not applied to the packaged release.

The first experiment with Wine's default HLSL compiler produced identical SPIR-V
for control/candidate: it did not exercise immediate-constant-buffer lowering.
Those timings cannot establish the patch's effect. A separately supplied native
compiler produced different SPIR-V for all three fragment shaders and unchanged
vertex SPIR-V. GPU readback passed for both paths.

The matched native-compiler x64 campaign completed 18 runs: three repetitions
of each control/candidate with separate cold/warm/warm2 application caches.
Median elapsed milliseconds for shader creation, drawing, synchronization and
readback are shown below. These are not GPU-only times or FPS measurements.

| Cache state | 64 entries control/candidate | 256 entries control/candidate | 1024 entries control/candidate |
| --- | --- | --- | --- |
| Cold | 145.536 / 152.834 | 50.879 / 52.370 | 95.399 / 106.118 |
| Warm | 101.625 / 89.738 | 23.078 / 20.443 | 60.833 / 60.068 |
| Warm2 | 82.360 / 95.101 | 21.744 / 17.406 | 66.220 / 63.106 |

All runs pass readback. Results are mixed across sizes and cache states, and
there is no controlled game comparison yet. Retain stock DXVK. The x86 candidate
also passes native-compiler readback and changes the intended fragment shaders;
that check uses the packaged release as control and is correctness evidence only.

Raw reports: `constants-native-comparison/report.json`,
`constants-native-diagnostic/report.json`,
`constants-native-x32-correctness/report.json`, and
`constant-array-decision.json`. Both local native compilers remain test inputs
outside Git and outside the packaged runtime.

## Final checks

The repository suite runs 176 tests: 175 pass and one is skipped. Python compile
checks, ShellCheck, actionlint and whitespace checks pass. The final helper was
also exercised with real DLLs through install/status/restore in the disposable
initialized prefix; restoration succeeds. The earlier real restore additionally
verified every original DLL/symlink against the journal.

The helper stages and verifies release files before replacement and detects
changes to originals/registry during backup and installation. Regression tests
cover those races, interrupted operations, corrupt backups, changed managed
files and preservation of unrelated registry edits.

## Open acceptance gates

Portal 2 was launched only in a disposable bottle/game copy. It could not start
the recording because the copied Steam session was not authenticated; the console
reported that Steam was not running. No timedemo or gameplay performance result
was accepted. Steam sign-in is pending before recording the fixed scene and
running the GPL/thread campaigns.

Still open: full experimental Wine 11.19 CI, macOS 26 validation, matched gameplay
and visibility review, fresh-prefix startup, complete point-fill correctness,
and the Skywalker level-loading regression. GPL/cache work remains experimental.
Shader-layout changes, Wine conversion optimizations, transform feedback and
new descriptor models require their planned reproducer/profile/compatibility
measurements; no speculative implementation or performance default was selected.
