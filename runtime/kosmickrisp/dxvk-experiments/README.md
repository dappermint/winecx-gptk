# Constant-array experiment

`0001-kosmickrisp-native-constant-arrays.patch` is an experimental one-line
change against DXVK v3.1.1. It lets KosmicKrisp handle constant arrays through
its compiler rather than enabling DXVK's `LowerConstantArrays` path.

It is not applied to the packaged DXVK release. Build both control and candidate
from the same source revision, submodule pins, compiler and flags. Apply the
patch in an isolated source checkout. Compare the resulting DLLs with
`tests/d3d11_constants.c` and controlled game scenes before considering a
runtime default change.

The probe dynamically indexes 64-, 256- and 1024-element constant arrays,
renders into an integer texture and verifies all four components of 20,736
pixels by GPU readback. Its elapsed time includes shader creation, draws,
synchronization and readback; it is not a GPU-only performance measurement.
Record driver compilation telemetry and shader dumps in separate diagnostic
runs. The Windows shader compiler is also required at runtime.

To verify the intended lowering path, supply a native shader compiler that emits
DXBC immediate constant buffers. Wine's default compiler can emit temporary
arrays instead; those pass readback but do not exercise this DXVK flag.
Use a legally installed compiler locally; it is not included in the runtime.

```sh
python3 runtime/kosmickrisp/dxvk-experiments/compare.py \
  --runtime /path/to/Libraries --prefix /path/to/disposable-installed-prefix \
  --probe /path/to/d3d11-constants.exe \
  --control /path/to/matched-control-dlls \
  --candidate /path/to/matched-candidate-dlls \
  --compiler /path/to/native/d3dcompiler_47.dll \
  --out /tmp/kk-constant-arrays
```

Each DLL directory needs `d3d11.dll` and `dxgi.dll` matching the probe architecture.
Use `--diagnostic --repeats 1` in a separate output directory to collect MSL,
compilation telemetry and SPIR-V dumps. The diagnostic report compares shaders
and records whether the patch changed the shader path. Performance comparisons
use three repetitions and separate cold/warm/warm2 caches, with alternating
control/candidate order. OS/Metal caches are not reset.

The 2026-10-13 local experiment verified the changed path and correct readback
on x64 and x86. The matched x64 timing comparison was mixed, so the patch remains
unselected. See `docs/kosmickrisp-validation-2026-10-13.md` for the evidence and
limitations.
