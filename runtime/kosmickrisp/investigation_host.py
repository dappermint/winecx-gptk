import datetime as dt
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import time


TIMESTAMP = re.compile(r'^(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?)\s')
SERIOUS = {'gpu_reset', 'kernel_panic', 'oom_kill'}

CAPTURE_TERMS = {
    'display_timeout': ('display reconfiguration sync timeout',),
    'gpu_preparation_fault': ('preparedbytes != requiredbytes',),
    'kernel_panic': ('panic(cpu', 'kernel panic'),
    'gpu_reset': ('gpu restart', 'gpu reset', 'gpu panic', 'resetting'),
    'idle_exit': ('idle-exit', 'idle exit'),
    'oom_kill': ('killed', 'killing', 'terminated'),
}
CAPTURE_PROCESSES = ('kernel', 'WindowServer', 'ReportCrash', 'memorystatusd')


def capture_predicate():
    processes = ' OR '.join('process == ' + json.dumps(name) for name in CAPTURE_PROCESSES)
    terms = ' OR '.join('eventMessage CONTAINS[c] ' + json.dumps(term)
                        for values in CAPTURE_TERMS.values() for term in values)
    return '(' + processes + ') AND (' + terms + ')'


def timestamp(value):
    if isinstance(value, dt.datetime):
        return value
    return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def classify(line):
    lower = line.lower()
    if 'display reconfiguration sync timeout' in lower:
        return 'display_timeout'
    if 'iogpu' in lower and 'preparedbytes != requiredbytes' in lower:
        return 'gpu_preparation_fault'
    if re.search(r'\bpanic\(cpu\s+\d+\b|\bkernel panic\b', lower):
        return 'kernel_panic'
    if re.search(r'\bgpu (?:restart|reset) (?:occurred|completed|failed|triggered)\b|\bgpu panic\b|\bresetting (?:the )?gpu\b', lower):
        return 'gpu_reset'
    if re.search(r'idle[- ]exit', lower):
        return 'idle_exit'
    if re.search(r'(?:killed|killing|terminated).*(?:out.of.memory|memory pressure|highwater|vm-pageshortage)|(?:out.of.memory|memory pressure|vm-pageshortage).*(?:killed|killing|terminated)', lower):
        return 'oom_kill'
    return None


def analyze_logs(paths, start, end, naive_timezone=None, max_events=1000):
    start, end = timestamp(start), timestamp(end)
    if (start.tzinfo is None) != (end.tzinfo is None) or end < start:
        raise ValueError('Invalid or mixed-timezone interval')
    events, counts, seen = [], {}, set()
    unparsed, outside, timezone_unknown = 0, 0, 0
    for path in paths:
        with Path(path).open(errors='replace') as stream:
            for number, line in enumerate(stream, 1):
                match = TIMESTAMP.match(line)
                if not match:
                    unparsed += 1
                    continue
                when = timestamp(match.group(1))
                if when.tzinfo is None and start.tzinfo is not None:
                    if naive_timezone is None:
                        timezone_unknown += 1
                        continue
                    when = when.replace(tzinfo=naive_timezone)
                if (when.tzinfo is None) != (start.tzinfo is None):
                    timezone_unknown += 1
                    continue
                if not start <= when <= end:
                    outside += 1
                    continue
                kind = classify(line)
                identity = line.rstrip()
                if not kind or identity in seen:
                    continue
                seen.add(identity)
                counts[kind] = counts.get(kind, 0) + 1
                if len(events) < max_events:
                    events.append({'timestamp': when.isoformat(), 'kind': kind,
                                   'severity': 'serious' if kind in SERIOUS else 'lead' if kind != 'idle_exit' else 'informational',
                                   'path': str(path), 'line': number, 'text': identity[:4096]})
    events.sort(key=lambda event: event['timestamp'])
    return {'start': start.isoformat(), 'end': end.isoformat(), 'events': events,
            'counts': counts, 'serious_host_fault': bool(SERIOUS.intersection(counts)),
            'outside_interval': outside, 'unparsed_lines': unparsed,
            'unknown_timezone_lines': timezone_unknown,
            'events_truncated': sum(counts.values()) > len(events),
            'limitations': ['Display timeouts and resource preparation faults are leads, not proof of a fatal GPU hang.',
                            'Host events alone do not identify the game or driver as their cause.',
                            'Missing log events do not exclude a reset, panic, or memory failure.',
                            'Idle-exit terminations are not evidence of out-of-memory kills.']}


def bounded_command(command, timeout=2, max_bytes=65536):
    if timeout <= 0 or max_bytes <= 0:
        raise ValueError('Positive command limits required')
    begin = time.monotonic()
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as error:
        return {'command': command, 'error': str(error), 'output': '', 'returncode': None}
    data = bytearray()
    timed_out = truncated = False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = timeout - (time.monotonic() - begin)
                if remaining <= 0:
                    timed_out = True
                    break
                if not selector.select(min(remaining, 0.1)):
                    continue
                chunk = os.read(process.stdout.fileno(), min(65536, max_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > max_bytes:
                    truncated = True
                    break
        if not timed_out and not truncated:
            try:
                process.wait(timeout=max(0.001, timeout - (time.monotonic() - begin)))
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
    return {'command': command, 'output': bytes(data[:max_bytes]).decode(errors='replace'),
            'returncode': process.returncode, 'timed_out': timed_out,
            'truncated': truncated, 'duration_s': time.monotonic() - begin}


def telemetry(pid=None):
    commands = [['/usr/bin/vm_stat'], ['/usr/sbin/sysctl', 'vm.swapusage']]
    if pid is not None:
        if int(pid) <= 0:
            raise ValueError('Positive process ID required')
        commands.append(['/bin/ps', '-p', str(int(pid)), '-o', 'pid=,ppid=,%cpu=,rss=,etime=,stat='])
    return {'timestamp': dt.datetime.now(dt.timezone.utc).isoformat(),
            'samples': [bounded_command(command) for command in commands]}


def capture_logs(output_dir, start, end, timeout=15, max_bytes=8 * 1024 * 1024):
    start, end = timestamp(start), timestamp(end)
    if start.tzinfo is None or end.tzinfo is None or end < start:
        raise ValueError('Capture requires timezone-aware ordered timestamps')
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    predicate = capture_predicate()
    command = ['/usr/bin/log', 'show', '--style', 'compact', '--timezone', 'UTC',
               '--start', start.astimezone(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S+0000'),
               '--end', end.astimezone(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S+0000'),
               '--predicate', predicate]
    capture = bounded_command(command, timeout=timeout, max_bytes=max_bytes)
    raw = output_dir / 'host-raw.log'
    raw.write_text(capture.pop('output'))
    evidence = analyze_logs([raw], start, end, naive_timezone=dt.timezone.utc)
    result = {'capture': capture, 'raw_log': str(raw), 'evidence': evidence}
    (output_dir / 'host-evidence.json').write_text(json.dumps(result, indent=2) + '\n')
    return result
