import importlib.util
import json
from pathlib import Path


_spec = importlib.util.spec_from_file_location('investigation_analysis', Path(__file__).with_name('investigation_analysis.py'))
_analysis = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_analysis)
PAIRS = (
    ('cache_mode', 'cold-1', 'warm-control-1'),
    ('cache_mode', 'warm-control-2', 'cold-2'),
    ('archive_enabled', 'archive-on-1', 'archive-off-1'),
    ('archive_enabled', 'archive-off-2', 'archive-on-2'),
)


def read(path):
    return json.loads(path.read_text()) if path.is_file() else {}


def metrics(report):
    menu, start = report.get('menu_observed_ns'), report.get('start_ns')
    return {'startup_ns': menu - start if menu is not None and start is not None else None,
            'shader_compiles': (report.get('compile_counts', {}).get('shaders/create', 0)
                                if 'compile_counts' in report else None),
            'draw_wait_union_ns': sum(p.get('draw_wait_union_ns', 0)
                                      for p in report.get('phases', []) if p.get('phase') == 'startup')}


def improved(slower, faster):
    a, b = slower.get('startup_ns'), faster.get('startup_ns')
    return a is not None and b is not None and a - b >= max(1_000_000_000, a * 0.1)


def evidence_files(out, values):
    if not isinstance(values, list) or not values:
        return False
    return all(isinstance(value, str) and (out / value).is_file()
               and (out / value).resolve().is_relative_to(out.resolve()) for value in values)


def planned_runs(out):
    plan=read(Path(out)/'plan.json')
    if not plan:
        return [name for _,left,right in PAIRS for name in (left,right)]
    cases=['cold-1','warm-control-1','warm-control-2','cold-2',
           'archive-on-1','archive-off-1','archive-off-2','archive-on-2']
    if plan.get('phase','full')=='full':
        cases=['baseline','warm-repeat']+cases
    limit=plan.get('max_runs',len(cases))
    if type(limit) is not int or not 1<=limit<=12:
        raise ValueError('invalid_report_run_limit')
    return cases[:limit]


def capture_summary(run):
    result=run.get('result',{})
    passed=result.get('outcome')=='passed'
    failures=run.get('analysis',{}).get('capture_failures',[])
    invalidating=not passed and bool(failures)
    return {'invalidating':invalidating,'events':failures,
            'classification':'tolerated_diagnostics' if passed else 'failed_run_diagnostics'}


def write_report(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    runs = {}
    for path in sorted((out / 'runs').glob('*')):
        if path.is_dir():
            runs[path.name] = {'analysis': read(path / 'analysis.json'),
                               'result': read(path / 'result.json')}
    expected=planned_runs(out)
    comparisons = []
    for variable, left_name, right_name in PAIRS:
        if left_name not in expected or right_name not in expected:
            continue
        left, right = runs.get(left_name, {}), runs.get(right_name, {})
        a, b = left.get('analysis', {}), right.get('analysis', {})
        comparison = _analysis.compare_runs(a, b, variable)
        if left.get('result', {}).get('outcome') != 'passed' or right.get('result', {}).get('outcome') != 'passed':
            comparison['reasons'].append('normal_exit_and_startup_pass_required')
            comparison['comparable'] = False
        comparison.update({'variable': variable, 'left': left_name, 'right': right_name,
                           'left_metrics': metrics(a), 'right_metrics': metrics(b)})
        prediction = False
        if comparison['comparable']:
            if variable == 'cache_mode':
                cold, warm = (a, b) if a['provenance']['cache_mode'] == 'fresh' else (b, a)
                cm, wm = metrics(cold), metrics(warm)
                prediction = (improved(cm, wm) and cm['shader_compiles'] is not None
                              and wm['shader_compiles'] is not None
                              and cm['shader_compiles'] > wm['shader_compiles'])
            else:
                enabled, disabled = (a, b) if a['provenance']['archive_enabled'] else (b, a)
                prediction = improved(metrics(enabled), metrics(disabled))
        comparison['predicted_effect_observed'] = prediction
        comparisons.append(comparison)
    review = read(out / 'review.json')
    hypotheses = []
    for name, variable, next_step in (
        ('compilation_backlog_contributes_to_startup_stalls', 'cache_mode',
         'Compare fresh and identical seeded cache runs in reversed order, with matched menu progress.'),
        ('archive_path_contributes_to_startup_stalls', 'archive_enabled',
         'Compare enabled and disabled archives using identical seeds; enabled runs must demonstrate hits.'),
    ):
        pairs = [c for c in comparisons if c['variable'] == variable]
        if not pairs:
            continue
        supported = sum(c['comparable'] and c['predicted_effect_observed'] for c in pairs)
        checked = review.get(name, {})
        mechanism = (checked.get('mechanism_checked') is True
                     and evidence_files(out, checked.get('mechanism_evidence')))
        alternative = (checked.get('alternative_checked') is True
                       and evidence_files(out, checked.get('alternative_evidence')))
        observed_overlap = any(any(e.get('hypothesis') == 'compilation_contributes_to_stalls'
                                   and e.get('status') == 'supported' for e in r['analysis'].get('evidence', []))
                               for r in runs.values())
        status = ('probable' if supported == 2 and mechanism and alternative else
                  'supported_candidate' if supported else
                  'supported_contribution' if variable == 'cache_mode' and observed_overlap else 'unestablished')
        hypotheses.append({'hypothesis': name, 'status': status,
                           'matching_pairs_with_predicted_effect': supported,
                           'mechanism_checked_with_evidence': mechanism,
                           'alternative_checked_with_evidence': alternative,
                           'next_discriminator': next_step,
                           'limitation': 'startup_stalls_do_not_establish_cause_of_original_host_freeze'})
    ranks = {'probable': 0, 'supported_candidate': 1, 'supported_contribution': 2, 'unestablished': 3}
    hypotheses.sort(key=lambda h: ranks[h['status']])
    hypotheses.append({'hypothesis': 'display_transition_or_steam_gpu_causes_original_host_freeze',
                       'status': 'unresolved', 'next_discriminator': 'Separate supervised Steam GPU and fullscreen comparisons after a passing windowed baseline.',
                       'limitation': 'windowed_startup_results_cannot_resolve_original_host_freeze'})
    missing = {name: {'missing_logs': r['analysis'].get('missing_logs', ['analysis.json']),
                      'issues': r['analysis'].get('issues', []),
                      'result_missing': not bool(r['result'])}
               for name, r in runs.items() if (not r['analysis'] or not r['result']
                                               or r['analysis'].get('missing_logs') or r['analysis'].get('issues'))}
    run_status = {name: {'outcome': r['result'].get('outcome', 'missing'),
                         'reason': r['result'].get('reason'),
                         'capture_failures': r['analysis'].get('capture_failures', []),
                         'capture_diagnostics':capture_summary(r),
                         'wait_attribution':r['analysis'].get('wait_attribution', {}),
                         'compilation_work': r['analysis'].get('compilation_work', {}),
                         'draw_wait_union_ns': r['analysis'].get('draw_wait_union_ns'),
                         'longest_present_gap_ns': max((g['duration_ns'] for g in r['analysis'].get('gaps', [])), default=0)}
                  for name, r in runs.items()}
    summary = {'run_status': run_status, 'private_marker': 'biblioklept', 'hypotheses': hypotheses,
               'missing_data': missing,
               'planned_runs':expected,
               'missing_runs': sorted(set(expected) - runs.keys()),
               'original_host_freeze': 'unresolved',
               'effect_threshold': 'startup improves by at least 10 percent and one second; cache prediction additionally requires fewer shader compiles'}
    (out / 'comparisons.json').write_text(json.dumps(comparisons, indent=2, sort_keys=True) + '\n')
    (out / 'hypotheses.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
    lines = ['# Private investigation report (biblioklept)', '',
             'The original whole-Mac freeze remains unresolved. Startup stalls are assessed separately.', '',
             '| Run | Outcome | Menu observed |', '|---|---|---|']
    for name, run in runs.items():
        lines.append(f"| {name} | {run['result'].get('outcome', 'missing')} | {run['analysis'].get('menu_observed_ns') is not None} |")
    lines += ['', 'Native library creation, shader deserialization, and shader compilation calls overlap and nest; their summed durations are not additive wall-clock costs. Shader-call duration is not isolated SPIR-V compilation time.', '']
    for name, status in run_status.items():
        lines.append(f"- {name}: reason={status['reason']}; draw-wait union={status['draw_wait_union_ns']} ns; longest present gap={status['longest_present_gap_ns']} ns.")
        for kind, value in status['compilation_work'].items():
            lines.append(f"  - {kind}: {value['count']} intervals, union {value['union_duration_ns']} ns, summed {value['summed_duration_ns']} ns, maximum {value['maximum_duration_ns']} ns.")
        attribution=status['wait_attribution']
        if attribution:
            lines.append(f"  - Exclusive wait attribution: queued={attribution['queued_ns']} ns; active pipeline work={attribution['active_work_ns']} ns; unresolved={attribution['unresolved_ns']} ns. Active work may be archive-backed.")
        for failure in status['capture_failures']:
            detail = failure.get('status', {})
            if status['capture_diagnostics']['invalidating']:
                lines.append(f"  - Capture diagnostic in failed run: {detail.get('failure_reason', 'unknown')}; foreground app {detail.get('frontmost_app', 'unknown')}; target running {detail.get('target_running', 'unknown')}. This run cannot establish a warm-run performance result.")
            else:
                lines.append(f"  - Tolerated capture diagnostic: {detail.get('failure_reason', 'unknown')}; foreground app {detail.get('frontmost_app', 'unknown')}; target running {detail.get('target_running', 'unknown')}. Menu verification and normal exit succeeded.")
    lines += ['', '## Ranked leads', '']
    for hypothesis in hypotheses:
        lines.append(f"- **{hypothesis['hypothesis']}**: {hypothesis['status']}. {hypothesis['next_discriminator']}")
    lines += ['', '## Comparison limits', '',
              'A probable culprit requires two matched comparisons with the predicted effect, mechanism evidence, and an explicitly checked alternative. Timestamp overlap alone does not establish causation.', '',
              summary['effect_threshold'] + '.', '',
              f"Missing planned runs: {', '.join(summary['missing_runs']) or 'none'}.",
              f"Runs with missing or malformed evidence: {', '.join(missing) or 'none'}.", '',
              'Detailed reasons for rejected comparisons are in comparisons.json; evidence status is in hypotheses.json.', '']
    (out / 'REPORT.md').write_text('\n'.join(lines))
    return summary
