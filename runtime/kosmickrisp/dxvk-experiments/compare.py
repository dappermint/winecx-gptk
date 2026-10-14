#!/usr/bin/env python3
"""Compare matched DXVK builds using the constant-array GPU readback probe."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import time


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare(runtime, prefix, probe, control, candidate, output, repeats=3, diagnostic=False, compiler=None):
    if repeats < 3 and not diagnostic:
        raise ValueError('performance comparisons need at least three repetitions')
    if output.exists():
        raise ValueError('output already exists; use a new campaign directory')
    launcher = runtime / 'Wine/bin/wine-kosmickrisp-dxvk'
    driver = runtime / 'Wine/lib/kosmickrisp/libvulkan_kosmickrisp.dylib'
    inputs = [probe, driver, launcher, runtime / 'Wine/bin/wine64']
    for payload in (control, candidate):
        inputs += [payload / 'd3d11.dll', payload / 'dxgi.dll']
    if compiler:
        inputs.append(compiler)
    hashes = {str(p): sha(p) for p in inputs}
    check = subprocess.run([str(launcher), '--status-prefix'], env=dict(os.environ, WINEPREFIX=str(prefix)),
                           capture_output=True, text=True, check=True)
    receipt = json.loads(check.stdout)
    output.mkdir(parents=True)
    campaign = hashlib.sha256(str(output).encode()).hexdigest()[:16]
    results = []
    for repetition in range(repeats):
        order = [('control', control), ('candidate', candidate)]
        if repetition % 2:
            order.reverse()
        for name, payload in order:
            root = output / name / f'repeat-{repetition + 1}'
            root.mkdir(parents=True)
            for dll in ('d3d11.dll', 'dxgi.dll'):
                shutil.copy2(payload / dll, root / dll)
            shutil.copy2(probe, root / 'constants.exe')
            if compiler:
                shutil.copy2(compiler, root / 'd3dcompiler_47.dll')
            cache = prefix / 'drive_c/ShaderCaches/kosmickrisp/constant-arrays' / campaign / name / f'repeat-{repetition + 1}'
            if cache.exists():
                raise ValueError('campaign cache already exists; refusing a false cold run')
            cache.mkdir(parents=True)
            for label in (('cold',) if diagnostic else ('cold', 'warm', 'warm2')):
                for p, digest in hashes.items():
                    if sha(Path(p)) != digest:
                        raise ValueError(f'frozen experiment input changed: {p}')
                run = root / label
                run.mkdir()
                env = {k: v for k, v in os.environ.items() if not k.startswith(('MESA_KK_', 'DXVK_'))}
                env.update(WINEPREFIX=str(prefix), WINEDEBUG='-all',
                           MESA_SHADER_CACHE_DIR=str(root / 'mesa-cache'),
                           DXVK_SHADER_CACHE_PATH='C:\\ShaderCaches\\kosmickrisp\\constant-arrays\\' + campaign + '\\' + name + f'\\repeat-{repetition + 1}',
                           DXVK_LOG_PATH=str(run), DXVK_LOG_LEVEL='info', DXVK_CONFIG_FILE='',
                           DXVK_CONFIG='dxvk.enableGraphicsPipelineLibrary = Auto; dxvk.numCompilerThreads = 2')
                if compiler:
                    env['WINEDLLOVERRIDES'] = 'd3dcompiler_47=n'
                if diagnostic:
                    env.update(MESA_KK_COMPILE_LOG=str(run / 'compile.csv'),
                               MESA_KK_DEBUG='msl', DXVK_SHADER_DUMP_PATH=str(run / 'shaders'))
                    (run / 'shaders').mkdir()
                started = time.monotonic()
                with (run / 'probe.txt').open('w') as log:
                    process = subprocess.run([str(launcher), str(root / 'constants.exe')], env=env, cwd=root,
                                             stdout=log, stderr=subprocess.STDOUT, timeout=180)
                text = (run / 'probe.txt').read_text(errors='replace')
                logs = '\n'.join(p.read_text(errors='replace') for p in run.glob('*_d3d11.log'))
                valid = process.returncode == 0 and 'RESULT: constant-array readback passed (20736 pixels)' in text and bool(re.search(r'Driver\s*:\s*KosmicKrisp', logs))
                times = {int(size): float(ms) for size, ms in re.findall(r'array=(\d+) elapsed_ms=([0-9.]+)', text)}
                valid &= set(times) == {64, 256, 1024}
                record = {'condition': name, 'repetition': repetition + 1, 'cache_state': label,
                          'valid': valid, 'returncode': process.returncode, 'array_ms': times,
                          'process_seconds': round(time.monotonic() - started, 3)}
                results.append(record)
                (run / 'result.json').write_text(json.dumps(record, indent=2) + '\n')
                print(json.dumps(record), flush=True)
                if not valid:
                    (output / 'failed.json').write_text(json.dumps(results, indent=2) + '\n')
                    raise ValueError(f'GPU readback or driver verification failed: {run}')
    rows = []
    for name in ('control', 'candidate'):
        for label in (('cold',) if diagnostic else ('cold', 'warm', 'warm2')):
            samples = [r for r in results if r['condition'] == name and r['cache_state'] == label]
            rows.append({'condition': name, 'cache_state': label,
                         'array_ms': {size: {'median': statistics.median(r['array_ms'][size] for r in samples),
                                            'minimum': min(r['array_ms'][size] for r in samples),
                                            'maximum': max(r['array_ms'][size] for r in samples)} for size in (64, 256, 1024)}})
    shaders = []
    if diagnostic:
        for path in (output / 'control/repeat-1/cold/shaders').glob('*.spv'):
            other = output / 'candidate/repeat-1/cold/shaders' / path.name
            if other.is_file():
                shaders.append({'shader': path.name, 'control_sha256': sha(path),
                                'candidate_sha256': sha(other), 'different': sha(path) != sha(other)})
    report = {'hashes': hashes, 'managed_prefix_version': receipt['version'], 'diagnostic': diagnostic,
              'compiler': str(compiler) if compiler else 'Wine default',
              'cache_scope': 'Mesa and DXVK application caches; OS/Metal caches are not reset', 'shader_comparison': shaders,
              'changed_shader_path_observed': any(s['different'] for s in shaders) if diagnostic else None,
              'repetitions': repeats, 'results': results, 'rows': rows,
              'limitation': 'Elapsed times include shader creation, GPU work, synchronization and readback. This is a focused correctness/workload comparison, not a game benchmark.'}
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('runtime', 'prefix', 'probe', 'control', 'candidate', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--diagnostic', action='store_true')
    parser.add_argument('--compiler', type=Path, help='optional native d3dcompiler_47.dll matching the probe architecture')
    args = parser.parse_args()
    compare(args.runtime.resolve(), args.prefix.resolve(), args.probe.resolve(), args.control.resolve(),
            args.candidate.resolve(), args.out.resolve(), args.repeats, args.diagnostic,
            args.compiler.resolve() if args.compiler else None)


if __name__ == '__main__':
    main()
