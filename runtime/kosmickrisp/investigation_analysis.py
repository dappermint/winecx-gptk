import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path


WAIT_PHASES = {'pso_wait', 'fs_wait', 'raster_wait'}
PROVENANCE_FIELDS = (
    'driver_sha256', 'loader_sha256', 'game_sha256', 'dxvk_sha256',
    'settings_sha256', 'steam_arguments', 'environment',
    'cache_seed_sha256', 'cache_mode', 'archive_enabled', 'capture_mode', 'focus_policy', 'sampling_policy', 'interaction_mode', 'capture_backend', 'activation_backend',
)


def union_ns(intervals, start, end):
    cursor = start
    total = 0
    for left, right in sorted(intervals):
        left, right = max(left, start, cursor), min(right, end)
        if right > left:
            total += right - left
            cursor = right
    return total


def duration_summary(intervals, start, end):
    clipped = [(max(left, start), min(right, end)) for left, right in intervals
               if right > start and left < end and right > left]
    durations = [right - left for left, right in clipped]
    return {'count': len(clipped), 'summed_duration_ns': sum(durations),
            'union_duration_ns': union_ns(clipped, start, end),
            'maximum_duration_ns': max(durations, default=0)}


def read_jsonl(path, issues):
    if not path.exists():
        return []
    events = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError('event is not an object')
            events.append(value)
        except (ValueError, TypeError):
            issues.append({'file': path.name, 'line': number, 'error': 'malformed_event'})
    return events


def read_object(path):
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f'{path}: expected object')
    return value


def stamp(event):
    return event.get('time_ns', event.get('clock_ns', event.get('start_ns')))


def interval(event):
    left, right = event.get('start_ns'), event.get('end_ns')
    if isinstance(left, int) and isinstance(right, int) and right >= left:
        return left, right
    return None


def menu_observation(text):
    text = text.upper()
    return ('CONTINUE GAME' in text and ('SELECT' in text or 'BACK' in text)
            and not any(word in text for word in ('PAUSED', 'LOADING')))


WORK_PHASES = ('archive_load', 'archive_lookup', 'native_create', 'fs_prepare', 'pso_create')


def exclusive_segments(segments):
    points=sorted({x for left,right,_ in segments for x in (left,right)})
    result=[]
    for left,right in zip(points,points[1:]):
        labels={label for begin,end,label in segments if begin <= left and end >= right}
        if labels:
            result.append((left,right,next(iter(labels)) if len(labels)==1 else 'unresolved'))
    return result


def attribute_waits(events, start, end, cache_events=()):
    by_id=defaultdict(list)
    children=defaultdict(list)
    for number,event in enumerate(events):
        if interval(event) is None:
            continue
        by_id[event.get('request_id')].append((number,event))
        if event.get('parent_id') and event.get('parent_id') != event.get('request_id'):
            children[event['parent_id']].append((number,event))
    rows=[]
    all_segments=[]
    ranked=defaultdict(list)
    counts=Counter()
    for wait in events:
        if wait.get('origin') != 'draw' or wait.get('phase') not in WAIT_PHASES or not interval(wait):
            continue
        left,right=max(start,wait['start_ns']),min(end,wait['end_ns'])
        if right <= left:
            continue
        key=wait.get('request_id')
        related={}
        pending=[key] if key else []
        visited=set()
        shared_wait_ids=set()
        while pending:
            parent=pending.pop()
            if parent in visited:
                continue
            visited.add(parent)
            for number,event in by_id[parent]+children[parent]:
                if event['end_ns'] < left or event['start_ns'] > right:
                    continue
                if parent != key and parent not in shared_wait_ids and event.get('parent_id') not in visited:
                    continue
                if (parent not in shared_wait_ids and wait.get('family_id') and event.get('family_id') and
                        wait['family_id'] != event['family_id']):
                    continue
                related[number]=event
                if event.get('phase') in WAIT_PHASES and event.get('request_id') != key:
                    shared_wait_ids.add(event.get('request_id'))
                if event.get('parent_id') == parent and event.get('request_id') and event.get('request_id') != parent:
                    pending.append(event['request_id'])
        work=[(max(left,e['start_ns']),min(right,e['end_ns']),e['phase'],number)
              for number,e in related.items() if e.get('phase') in WORK_PHASES
              and e['end_ns'] > left and e['start_ns'] < right]
        queued=[]
        own=by_id.get(key,[]) if key else []
        enqueues=sorted({e['start_ns'] for _,e in own if e.get('phase')=='enqueue'})
        for i,queued_at in enumerate(enqueues):
            next_enqueue=enqueues[i+1] if i+1<len(enqueues) else end+1
            completes=[e['start_ns'] for _,e in own if e.get('phase')=='complete'
                       and queued_at <= e['start_ns'] < next_enqueue]
            boundary=min(completes,default=next_enqueue)
            workers={e['start_ns'] for _,e in own if e.get('phase')=='worker_start'
                     and queued_at <= e['start_ns'] <= boundary}
            if len(workers)==1:
                dispatched=next(iter(workers))
                if dispatched>left and queued_at<right:
                    queued.append((max(left,queued_at),min(right,dispatched)))
        points=sorted({left,right,*[x for a,b,_,_ in work for x in (a,b)],
                       *[x for a,b in queued for x in (a,b)]})
        segments=[]
        reasons=Counter()
        for begin,finish in zip(points,points[1:]):
            active=[(phase,number) for a,b,phase,number in work if a<=begin and b>=finish]
            in_queue=any(a<=begin and b>=finish for a,b in queued)
            label='unresolved'
            if active and in_queue:
                reasons['queue_and_active_work_overlap']+=finish-begin
            elif active:
                best=min(WORK_PHASES.index(phase) for phase,_ in active)
                candidates=[number for phase,number in active if WORK_PHASES.index(phase)==best]
                signatures={(related[n]['request_id'],related[n]['start_ns'],related[n]['end_ns'],
                             related[n].get('thread_id')) for n in candidates}
                if len(signatures)==1:
                    label=WORK_PHASES[best]
                else:
                    reasons['ambiguous_active_events']+=finish-begin
            elif in_queue:
                label='queued'
            else:
                reasons['no_unique_correlated_interval']+=finish-begin
            segments.append((begin,finish,label))
        durations=Counter()
        for begin,finish,label in segments:
            durations[label]+=finish-begin
        row={'request_id':key,'family_id':wait.get('family_id'),'start_ns':left,'end_ns':right,
             'duration_ns':right-left,'exclusive_ns':dict(durations),'unresolved_reasons_ns':dict(reasons),
             'evidence_event_indices':sorted(related)}
        rows.append(row)
        all_segments.extend(segments)
        ranked[key or '<missing-request-id>'].extend(segments)
        counts[key or '<missing-request-id>']+=1
    global_segments=exclusive_segments(all_segments)
    durations=Counter()
    for begin,finish,label in global_segments:
        durations[label]+=finish-begin
    ranking=[]
    for key,segments in ranked.items():
        merged=exclusive_segments(segments)
        values=Counter()
        for begin,finish,label in merged:
            values[label]+=finish-begin
        outcomes=set()
        for row in rows:
            if (row['request_id'] or '<missing-request-id>') != key:
                continue
            native_keys={events[n].get('request_id') for n in row['evidence_event_indices']
                         if events[n].get('phase')=='pso_create'}
            for event in cache_events:
                if (event.get('event')=='pso' and event.get('key') in native_keys and
                        event.get('outcome') in ('archive_hit','compiler_fallback') and
                        isinstance(event.get('time_ns'),int) and
                        row['start_ns']<=event['time_ns']<=row['end_ns']):
                    outcomes.add(event['outcome'])
        ranking.append({'request_id':key,'wait_count':counts[key],
                        'pipeline_outcomes_during_wait':sorted(outcomes),
                        'wait_union_ns':sum(values.values()),'exclusive_ns':dict(values)})
    total=sum(durations.values())
    queued=durations.get('queued',0)
    unknown=durations.get('unresolved',0)
    return {'draw_wait_union_ns':total,'queued_ns':queued,'active_work_ns':total-queued-unknown,
            'unresolved_ns':unknown,'exclusive_phase_ns':dict(durations),
            'top_requests':sorted(ranking,key=lambda r:r['wait_union_ns'],reverse=True)[:10],
            'waits':rows,'semantics':'ID_and_parent_correlated_exclusive_time_not_proof_of_fresh_compilation'}


def analyze_run(run_dir, start_ns=None):
    root = Path(run_dir)
    issues = []
    files = {}
    for name in ('present.csv', 'compile.csv', 'metal.jsonl', 'variants.jsonl',
                 'cache.jsonl', 'observations.jsonl', 'controller.jsonl', 'provenance.json'):
        path = root / name
        if path.is_file():
            files[name] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                           'bytes': path.stat().st_size}
    present = []
    if (root / 'present.csv').exists():
        with (root / 'present.csv').open(newline='') as stream:
            for row in csv.reader(stream):
                if len(row) >= 2 and row[0].isdigit():
                    present.append(int(row[0]))
    present = sorted(set(present))
    compiles = []
    if (root / 'compile.csv').exists():
        with (root / 'compile.csv').open(newline='') as stream:
            for row in csv.reader(stream):
                if len(row) == 4 and row[2].isdigit() and row[3].isdigit():
                    compiles.append({'kind': row[0], 'origin': row[1],
                                     'start_ns': int(row[2]), 'end_ns': int(row[3])})
    metal = read_jsonl(root / 'metal.jsonl', issues)
    variants = read_jsonl(root / 'variants.jsonl', issues)
    cache = read_jsonl(root / 'cache.jsonl', issues)
    observations = read_jsonl(root / 'observations.jsonl', issues)
    controller = read_jsonl(root / 'controller.jsonl', issues)
    observations += [e for e in controller
                     if e.get('event') == 'screen']
    observations = sorted((e for e in observations if isinstance(stamp(e), int)), key=stamp)
    all_events = compiles + metal + variants + cache + observations
    timestamps = present + [stamp(e) for e in all_events if isinstance(stamp(e), int)]
    timestamps += [e['end_ns'] for e in all_events if isinstance(e.get('end_ns'), int)]
    if start_ns is None:
        start_ns = min(timestamps) if timestamps else 0
        start_source = 'first_logged_event'
    else:
        start_source = 'caller'
    finish = max(timestamps + [start_ns])
    waits = [interval(e) for e in variants if e.get('origin') == 'draw'
             and e.get('phase') in WAIT_PHASES and interval(e)]
    metal_intervals = [interval(e) for e in metal if interval(e)]
    draw_compiles = [interval(e) for e in compiles if e.get('origin') == 'draw' and interval(e)]
    native_libraries = [interval(e) for e in metal if e.get('operation') == 'library' and interval(e)]
    deserializations = [interval(e) for e in compiles if e.get('kind') == 'deserialize' and interval(e)]
    shader_calls = [interval(e) for e in compiles if e.get('kind') == 'shaders' and interval(e)]
    work = {'native_library': native_libraries, 'shader_deserialization': deserializations,
            'shader_compile_calls': shader_calls}
    menu = None
    for previous, current in zip(observations, observations[1:]):
        if (menu_observation(previous.get('text', '')) and
                menu_observation(current.get('text', '')) and
                0 < stamp(current) - stamp(previous) <= 10_000_000_000):
            menu = stamp(current)
            break
    gameplay = next((stamp(e) for e in observations
                     if 'BOONTA' in e.get('text', '').upper()
                     and 'LAP' in e.get('text', '').upper()
                     and 'PAUSED' not in e.get('text', '').upper()), None)
    boundaries = [('startup', start_ns)]
    if menu is not None and menu >= start_ns:
        boundaries.append(('menu_observed', menu))
    if gameplay is not None and gameplay >= start_ns:
        boundaries.append(('gameplay_observed', gameplay))
    boundaries.sort(key=lambda item: item[1])
    phases = []
    for index, (name, begin) in enumerate(boundaries):
        end = boundaries[index + 1][1] if index + 1 < len(boundaries) else finish
        phases.append({'phase': name, 'start_ns': begin, 'end_ns': end,
                       'present_count': sum(begin <= p < end for p in present),
                       'completed_compiles': sum(begin <= e['end_ns'] < end for e in compiles),
                       'draw_wait_union_ns': union_ns(waits, begin, end),
                       'compilation_work': {key: duration_summary(values, begin, end)
                                            for key, values in work.items()}})
    gaps = []
    for left, right in zip(present, present[1:]):
        left = max(left, start_ns)
        if right - left < 2_000_000_000:
            continue
        covered = union_ns(waits, left, right)
        gaps.append({'start_ns': left, 'end_ns': right, 'duration_ns': right - left,
                     'draw_wait_overlap_ns': covered,
                     'outside_logged_draw_waits_ns': right - left - covered,
                     'draw_compile_overlap_ns': union_ns(draw_compiles, left, right),
                     'metal_overlap_ns': union_ns(metal_intervals, left, right),
                     'metal_completed': sum(left < e.get('end_ns', 0) <= right for e in metal),
                     'compilation_work': {key: duration_summary(values, left, right)
                                          for key, values in work.items()},
                     'causality': 'temporal_overlap_only'})
    outcomes = dict(Counter(f"{e.get('event')}/{e.get('outcome')}" for e in cache
                            if e.get('event') in ('shader', 'pso')))
    hits = sum(e.get('outcome') in ('archive_hit', 'compute_archive_hit') for e in cache)
    evidence = []
    experiments = []
    if gaps and any(g['draw_wait_overlap_ns'] for g in gaps):
        evidence.append({'hypothesis': 'compilation_contributes_to_stalls', 'status': 'supported',
                         'basis': 'logged_draw_waits_overlap_present_gaps',
                         'limitation': 'remaining_gap_and_host_freeze_unexplained'})
        experiments.append({'variable': 'cache_mode', 'comparison': 'fresh_vs_identical_seed',
                            'prediction': 'warming_reduces_compiles_and_present_gaps',
                            'falsification': 'matched_progress_stalls_persist_without_compile_reduction'})
    if not hits:
        evidence.append({'hypothesis': 'archive_serialization_causes_hang', 'status': 'unestablished',
                         'basis': 'no_native_archive_hits_observed',
                         'limitation': 'archive_disable_comparison_requires_matching_seed_and_enabled_hits'})
    return {'schema_version': 2, 'run_dir': str(root), 'files': files, 'issues': issues,
            'missing_logs': [n for n in ('present.csv', 'compile.csv', 'metal.jsonl',
                                        'variants.jsonl', 'cache.jsonl') if n not in files],
            'start_ns': start_ns, 'start_source': start_source, 'end_ns': finish,
            'first_present_ns': next((p for p in present if p >= start_ns), None),
            'menu_observed_ns': menu, 'gameplay_observed_ns': gameplay, 'phases': phases,
            'present_count': sum(p >= start_ns for p in present),
            'draw_wait_count': sum(e > start_ns for s, e in waits),
            'draw_wait_union_ns': union_ns(waits, start_ns, finish),
            'wait_attribution': attribute_waits(variants,start_ns,finish,cache),
            'compilation_work': {key: duration_summary(values, start_ns, finish)
                                 for key, values in work.items()},
            'compilation_work_semantics': 'nested_intervals_not_additive_shader_call_not_spirv_only',
            'capture_failures': [e for e in controller if e.get('event') == 'capture_failure'],
            'compile_counts': dict(Counter(f"{e['kind']}/{e['origin']}" for e in compiles
                                           if e['start_ns'] >= start_ns)),
            'gaps': sorted(gaps, key=lambda g: g['duration_ns'], reverse=True),
            'cache_outcomes': outcomes, 'native_archive_hits': hits,
            'metal_failures': [e for e in metal if e.get('success') is False],
            'provenance': read_object(root / 'provenance.json'),
            'evidence': evidence, 'proposed_experiments': experiments,
            'probable_criterion': 'two_matched_comparisons_with_predicted_effect_mechanism_and_alternative_checked'}


def compare_runs(left, right, changed_variable=None):
    if changed_variable is not None and changed_variable not in PROVENANCE_FIELDS:
        raise ValueError('unknown experiment variable')
    a, b = left.get('provenance', {}), right.get('provenance', {})
    missing = [key for key in PROVENANCE_FIELDS
               if key not in a or key not in b or a[key] is None or b[key] is None]
    differences = [key for key in PROVENANCE_FIELDS
                   if key not in missing and a[key] != b[key]]
    allowed = {changed_variable}
    if changed_variable == 'cache_mode':
        allowed.add('cache_seed_sha256')
    unexpected = [key for key in differences if key not in allowed]
    reasons = []
    if missing:
        reasons.append('missing_provenance')
    if unexpected:
        reasons.append('uncontrolled_differences')
    if changed_variable and changed_variable not in differences:
        reasons.append('experiment_variable_unchanged_or_unknown')
    if not (left.get('menu_observed_ns') and right.get('menu_observed_ns')):
        reasons.append('matching_menu_progress_unverified')
    if left.get('issues') or right.get('issues') or left.get('missing_logs') or right.get('missing_logs'):
        reasons.append('incomplete_evidence')
    if changed_variable == 'archive_enabled':
        enabled = left if a.get('archive_enabled') else right
        if not enabled.get('native_archive_hits'):
            reasons.append('enabled_case_has_no_archive_hits')
    return {'comparable': not reasons, 'reasons': reasons, 'missing_fields': missing,
            'differences': differences, 'unexpected_differences': unexpected,
            'causal_status': 'requires_repeated_matched_experiment'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--start-ns', type=int)
    args = parser.parse_args()
    print(json.dumps(analyze_run(args.run_dir, args.start_ns), indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
