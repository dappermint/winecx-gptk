#!/usr/bin/env python3
"""Plan, run and summarize isolated GPL, compiler-thread and latency comparisons."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import statistics
import subprocess
import sys

HERE = Path(__file__).resolve().parent


def sha(path):
    with path.open('rb') as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def conditions(stage, gpl='Auto'):
    if stage == 'gpl':
        return [(f'gpl-{value.lower()}', {'dxvk.enableGraphicsPipelineLibrary': value}) for value in ('Auto', 'True', 'False')]
    if stage == 'threads':
        return [(f'threads-{value}', {'dxvk.enableGraphicsPipelineLibrary': gpl,
                                    'dxvk.numCompilerThreads': str(value)}) for value in (0, 2, 4)]
    if stage == 'latency':
        return [(f'latency-{value}', {'dxvk.enableGraphicsPipelineLibrary': gpl,
                                    'dxgi.maxFrameLatency': str(value), 'd3d9.maxFrameLatency': str(value)}) for value in (0, 1, 2)]
    if stage == 'sleep':
        return [(f'sleep-{value.lower()}', {'dxvk.enableGraphicsPipelineLibrary': gpl,
                                           'dxvk.latencySleep': value}) for value in ('Auto', 'True', 'False')]
    raise ValueError('unknown comparison stage')


def make_plan(runtime, bundle, bottle, game, scene_path, output, stage, repeats=3, seed=0, gpl='Auto', seconds=None):
    if repeats < 3:
        raise ValueError('at least three repetitions are required')
    games = json.loads((HERE / 'stutter-games.json').read_text())
    if game not in games:
        raise ValueError('unknown game')
    scene = json.loads(scene_path.read_text())
    if scene.get('game') != game or not scene.get('id') or not scene.get('reproduction'):
        raise ValueError('scene needs the matching game, an id and a reproduction description')
    if not isinstance(scene.get('arguments'), list) or not all(isinstance(x, str) for x in scene['arguments']):
        raise ValueError('scene needs fixed string arguments')
    fixtures = scene.get('fixtures', [])
    if not fixtures:
        raise ValueError('scene needs at least one recorded demo, save or input script fixture')
    files = {str(scene_path): sha(scene_path),
             str(bundle / 'libvulkan_kosmickrisp.dylib'): sha(bundle / 'libvulkan_kosmickrisp.dylib'),
             str(bundle / 'libvulkan.1.dylib'): sha(bundle / 'libvulkan.1.dylib'),
             str(runtime / 'Wine/bin/wine64'): sha(runtime / 'Wine/bin/wine64'),
             str(HERE / 'stutter.py'): sha(HERE / 'stutter.py')}
    for relative in ('Wine/bin/wine-kosmickrisp', 'Wine/bin/wine-kosmickrisp-dxvk', 'Wine/share/kosmickrisp/dxvk-prefix.py'):
        path = runtime / relative
        if path.is_file():
            files[str(path)] = sha(path)
    for item in fixtures:
        path = Path(item).expanduser().resolve()
        files[str(path)] = sha(path)
    game_spec = games[game]
    executable = bottle / 'drive_c' / game_spec['bottle_path'] / game_spec['executable']
    files[str(executable)] = sha(executable)
    for name in game_spec.get('dxvk_dlls', ['d3d9.dll']):
        path = runtime / 'DXVK3' / game_spec.get('dxvk_arch', 'x32') / name
        files[str(path)] = sha(path)
    if output.exists():
        raise ValueError('campaign output already exists; use a new directory')
    comparisons = conditions(stage, gpl)
    rng = random.Random(seed)
    rng.shuffle(comparisons)
    jobs = []
    for repetition in range(repeats):
        order = comparisons[repetition % len(comparisons):] + comparisons[:repetition % len(comparisons)]
        for name, settings in order:
            relative = f'{name}/repeat-{repetition + 1}'
            jobs.append({'condition': name, 'repetition': repetition + 1,
                         'work': relative, 'settings': settings})
    plan = {'schema': 1, 'stage': stage, 'game': game, 'scene': str(scene_path),
            'runtime': str(runtime), 'bundle': str(bundle), 'bottle': str(bottle),
            'repetitions': repeats, 'seed': seed, 'seconds': seconds or game_spec['seconds'],
            'hashes': files, 'jobs': jobs, 'measurement': 'frame-times',
            'cache_scope': 'Mesa and DXVK application caches; OS/Metal caches are not reset',
            'gameplay_verification': 'requires review of scene execution and visible rendering'}
    output.mkdir(parents=True)
    for job in jobs:
        work = output / job['work']
        work.mkdir(parents=True)
        config = work / 'dxvk.conf'
        config.write_text(''.join(f'{key} = {value}\n' for key, value in job['settings'].items()))
        plan['hashes'][str(config.resolve())] = sha(config)
    (output / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    return plan


def verify(plan):
    if plan.get('schema') != 1 or plan.get('measurement') != 'frame-times':
        raise ValueError('unsupported benchmark plan')
    for filename, digest in plan['hashes'].items():
        path = Path(filename)
        if not path.is_file() or sha(path) != digest:
            raise ValueError(f'frozen benchmark input changed: {path}')


def run(plan_path):
    output = plan_path.parent.resolve()
    plan = json.loads(plan_path.read_text())
    verify(plan)
    results = []
    for job in plan['jobs']:
        verify(plan)
        work = output / job['work']
        if list((work / 'runs').glob('*/summary.json')):
            raise ValueError(f'campaign already has results for {job["work"]}; analyze or create a new campaign')
        command = [sys.executable, str(HERE / 'stutter.py'), 'run',
                   '--runtime', plan['runtime'], '--bundle', plan['bundle'], '--bottle', plan['bottle'],
                   '--game', plan['game'], '--scene', plan['scene'], '--work', str(work),
                   '--seconds', str(plan['seconds']), '--runs', 'cold,warm,warm2', '--no-diagnostics',
                   '--env', 'DXVK_CONFIG_FILE=Z:' + str(work / 'dxvk.conf'), '--env', 'DXVK_CONFIG=',
                   '--env', 'DXVK_SHADER_CACHE=1']
        with (work / 'runner.log').open('w') as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=3 * plan['seconds'] + 1800)
        record = dict(job, returncode=result.returncode)
        results.append(record)
        (output / 'execution.json').write_text(json.dumps(results, indent=2) + '\n')
        if result.returncode:
            raise ValueError(f'benchmark failed in {job["work"]}; inspect runner.log')
        for path in (work / 'runs').glob('*/summary.json'):
            summary = json.loads(path.read_text())
            if summary['frames'].get('error') or not summary['dxvk']['backend_verified']:
                raise ValueError(f'missing frame or KosmicKrisp evidence: {path}')
    return analyze(plan_path)


def analyze(plan_path):
    plan = json.loads(plan_path.read_text())
    output = plan_path.parent
    groups = {}
    invalid = []
    driver = str(Path(plan['bundle']) / 'libvulkan_kosmickrisp.dylib')
    for job in plan['jobs']:
        paths = list((output / job['work'] / 'runs').glob('*/summary.json'))
        seen = set()
        for path in paths:
            data = json.loads(path.read_text())
            meta = data.get('meta', {})
            label = meta.get('label')
            if label not in ('cold', 'warm', 'warm2') or label in seen:
                invalid.append({'run': str(path), 'reason': 'missing or duplicate cache-state label'})
                continue
            seen.add(label)
            reasons = []
            if data['frames'].get('error') or not data['dxvk'].get('backend_verified'):
                reasons.append('missing frame or KosmicKrisp evidence')
            if data['dxvk'].get('errors') or data['dxvk'].get('rejections'):
                reasons.append('DXVK errors or device rejection')
            effective = data['dxvk'].get('effective_configuration', {})
            if any(effective.get(key) != value for key, value in job['settings'].items()):
                reasons.append('requested DXVK configuration not verified in its log')
            if job['settings'].get('dxvk.enableGraphicsPipelineLibrary') != 'False' and data['dxvk'].get('gpl') is not True:
                reasons.append('GPL condition is unsupported by this driver')
            if meta.get('driver_sha256') != plan['hashes'][driver]:
                reasons.append('driver differs from frozen input')
            if (meta.get('scene') or {}).get('sha256') != plan['hashes'][plan['scene']]:
                reasons.append('scene differs from frozen input')
            termination = meta.get('termination', {})
            if termination.get('reason') == 'natural_exit' and termination.get('returncode'):
                reasons.append('game exited with an error')
            if reasons:
                invalid.append({'run': str(path), 'reason': '; '.join(reasons)})
                continue
            groups.setdefault((job['condition'], label), []).append(data['frames'])
        if seen != {'cold', 'warm', 'warm2'}:
            invalid.append({'job': job['work'], 'reason': 'incomplete cold/warm/warm2 sequence'})
    rows = []
    for (condition, label), frames in sorted(groups.items()):
        values = {key: [f[key] for f in frames] for key in ('mean_fps', 'p99_ms', 'hitches', 'one_percent_low_fps')}
        rows.append({'condition': condition, 'cache_state': label, 'samples': len(frames),
                     'metrics': {key: {'median': statistics.median(v), 'minimum': min(v), 'maximum': max(v)}
                                 for key, v in values.items()}})
    report = {'stage': plan['stage'], 'scene': plan['scene'], 'rows': rows, 'invalid': invalid,
              'complete': not invalid and len(rows) == 9 and all(r['samples'] == plan['repetitions'] for r in rows),
              'performance_claim_eligible': False,
              'limitation': 'Scene execution, window visibility and rendering correctness need review before selecting defaults.'}
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    create = sub.add_parser('plan')
    for name in ('runtime', 'bundle', 'bottle', 'scene', 'out'):
        create.add_argument('--' + name, type=Path, required=True)
    create.add_argument('--game', required=True)
    create.add_argument('--stage', choices=('gpl', 'threads', 'latency', 'sleep'), default='gpl')
    create.add_argument('--gpl', choices=('Auto', 'True'), default='Auto')
    create.add_argument('--repeats', type=int, default=3)
    create.add_argument('--seed', type=int, default=0)
    create.add_argument('--seconds', type=int)
    for name in ('run', 'analyze'):
        sub.add_parser(name).add_argument('plan', type=Path)
    args = parser.parse_args()
    if args.operation == 'plan':
        report = make_plan(args.runtime.resolve(), args.bundle.resolve(), args.bottle.resolve(), args.game,
                           args.scene.resolve(), args.out.resolve(), args.stage, args.repeats, args.seed, args.gpl, args.seconds)
        print(json.dumps({'plan': str(args.out / 'plan.json'), 'jobs': len(report['jobs'])}, indent=2))
    else:
        print(json.dumps({'run': run, 'analyze': analyze}[args.operation](args.plan.resolve()), indent=2))


if __name__ == '__main__':
    main()
