import argparse
import ctypes
import json
import hashlib
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]


def build_helper(source, cache):
    cache.mkdir(parents=True, exist_ok=True)
    helper, stamp = cache / 'gameplay-ui', cache / 'source.sha256'
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if helper.is_file() and stamp.is_file() and stamp.read_text() == digest:
        return helper
    with tempfile.TemporaryDirectory(prefix='build-', dir=cache) as staging:
        binary = Path(staging) / 'gameplay-ui'
        subprocess.run(['swiftc', '-warnings-as-errors', '-target',
                        os.uname().machine + '-apple-macos14.0', str(source),
                        '-o', str(binary)], check=True)
        binary.replace(helper)
        (Path(staging) / 'source.sha256').write_text(digest)
        (Path(staging) / 'source.sha256').replace(stamp)
    return helper


def permission_error(permissions, helper):
    names = {'accessibility': 'Accessibility', 'screen_recording': 'Screen Recording'}
    missing = [label for key, label in names.items() if not permissions.get(key, False)]
    if not missing:
        return None
    return ('Missing macOS access: ' + ', '.join(missing) + '. Open System Settings > '
            'Privacy & Security and enable the app macOS identifies in the permission prompts '
            '(normally Terminal when launching this .command file). If gameplay-ui is listed, '
            f'enable it too; its stable path is {helper}. Quit and reopen Terminal after '
            'granting access, start Steam in the test bottle, then rerun the launcher.')


def steam_processes(bottle):
    processes = subprocess.run(['ps', '-ww', '-axo', 'pid=,args='],
                               capture_output=True, text=True, check=True)
    executable = (Path(bottle) / 'drive_c/Program Files (x86)/Steam/steam.exe').resolve()
    matches = []
    for line in processes.stdout.splitlines():
        match = re.match(r'^\s*(\d+)\s+("?)(.+?\.exe)\2(?:\s|$)', line, re.IGNORECASE)
        if not match or not match[3].replace('\\', '/').lower().endswith('/steam.exe'):
            continue
        pid = int(match[1])
        files = subprocess.run(['/usr/sbin/lsof', '-a', '-p', str(pid), '-d', 'txt', '-Fn'],
                               capture_output=True, text=True)
        if files.returncode == 0 and any(
                Path(name[1:]).resolve() == executable
                for name in files.stdout.splitlines() if name.startswith('n')):
            matches.append(pid)
    return matches


def read_events(path):
    if not path.exists():
        return []
    result = []
    for line in path.read_text().splitlines():
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return result


def clocks():
    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    lib.clock_gettime_nsec_np.restype = ctypes.c_uint64
    return lib.clock_gettime_nsec_np(6), lib.clock_gettime_nsec_np(8)


class Session:
    def __init__(self, now, uptime, seconds=120, timeout=1200):
        self.previous = now, uptime
        self.deadline = now + timeout * 10**9
        self.seconds = seconds
        self.playing = None
        self.finished = False
        self.observations = []

    def observe(self, now, uptime, text):
        delta = (now - self.previous[0]) - (uptime - self.previous[1])
        self.previous = now, uptime
        if delta > 2 * 10**9:
            raise RuntimeError('system_sleep')
        if now >= self.deadline:
            raise RuntimeError('run_time_limit')
        text = text.upper()
        if 'BOONTA' in text and 'LAP' in text and 'PAUSED' not in text:
            self.observations.append(now)
            if self.playing is None:
                self.playing = now
        if self.playing is not None and not self.finished and 'PAUSED' in text:
            raise RuntimeError('gameplay_paused')
        if self.playing is not None and now >= self.playing + self.seconds * 10**9:
            self.finished = True
        return self.finished

    def valid_exit(self, code, checkpoint):
        return (code == 0 and self.finished and checkpoint and
                len(self.observations) >= 3 and
                self.observations[-1] - self.playing >= 90 * 10**9)


def startup_menu(text):
    text = ' '.join(text.upper().split())
    return ('CONTINUE GAME' in text and ('SELECT' in text or 'BACK' in text)
            and not any(word in text for word in ('PAUSED', 'LOADING', 'QUIT TO MAIN MENU')))


def startup_action(text):
    if 'CONTINUE GAME' in text.upper():
        return None
    return action(text, False)


class StartupSession:
    def __init__(self, now, uptime, timeout=600, no_progress_seconds=45, exit_seconds=60):
        self.started = now
        self.previous = now, uptime
        self.deadline = now + timeout * 10**9
        self.no_progress_ns = no_progress_seconds * 10**9
        self.exit_ns = exit_seconds * 10**9
        self.last_progress = now
        self.last_present = None
        self.last_compile = None
        self.menu_observations = 0
        self.menu_verified_ns = None
        self.exit_requested_ns = None
        self.failure = None

    def observe(self, now, uptime, text=None, present_ns=None, compile_ns=None):
        if (now - self.previous[0]) - (uptime - self.previous[1]) > 2 * 10**9:
            self.failure = 'system_sleep'
        self.previous = now, uptime
        for field, stamp in (('last_present', present_ns), ('last_compile', compile_ns)):
            previous = getattr(self, field)
            if stamp is not None and self.started <= stamp <= now and (previous is None or stamp > previous):
                setattr(self, field, stamp)
                self.last_progress = max(self.last_progress, stamp)
        if text is not None:
            self.menu_observations = self.menu_observations + 1 if startup_menu(text) else 0
            if self.menu_observations >= 2 and self.menu_verified_ns is None:
                self.menu_verified_ns = now
        return self.menu_verified_ns is not None

    def request_exit(self, now):
        if self.menu_verified_ns is None:
            raise ValueError('menu_not_verified')
        if self.exit_requested_ns is None:
            self.exit_requested_ns = now

    def valid_exit(self, code):
        return (code == 0 and self.menu_verified_ns is not None
                and self.exit_requested_ns is not None and self.failure is None)

    def outcome(self, now, code=None):
        if self.failure:
            return 'invalid'
        if code is not None:
            if self.valid_exit(code):
                return 'passed'
            return 'crashed' if code != 0 else 'invalid'
        if self.exit_requested_ns is not None and now >= min(self.deadline, self.exit_requested_ns + self.exit_ns):
            return 'timeout_progressing' if now - self.last_progress < self.no_progress_ns else 'stalled'
        if now - self.last_progress >= self.no_progress_ns:
            return 'stalled'
        if now >= self.deadline:
            return 'timeout_progressing'
        return None


def controller_process_owned(directory, pid):
    owner = json.loads((Path(directory) / 'owner.json').read_text())
    if owner.get('pid') != pid or not owner.get('identity'):
        return False
    current = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='],
                             capture_output=True, text=True, timeout=3)
    return current.returncode == 0 and current.stdout.strip() == owner['identity']


def run_ui_command(command, timeout=10, ownerpath=None):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    owner = None
    try:
        if ownerpath is not None:
            ownerpath = Path(ownerpath)
            identity = subprocess.run(['ps', '-p', str(process.pid), '-o', 'lstart='],
                                      capture_output=True, text=True, timeout=3)
            if identity.returncode == 0 and identity.stdout.strip():
                owner = {'pid': process.pid, 'pgid': process.pid,
                         'identity': identity.stdout.strip()}
                temporary = ownerpath.with_suffix('.tmp')
                temporary.write_text(json.dumps(owner) + '\n')
                temporary.replace(ownerpath)
            elif process.poll() is None:
                raise RuntimeError('ui_identity_unavailable')
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=2)
            if isinstance(error, subprocess.TimeoutExpired):
                error.output, error.stderr = stdout, stderr
        except subprocess.TimeoutExpired:
            process.stdout.close()
            process.stderr.close()
        raise
    finally:
        if owner is not None and process.poll() is not None and ownerpath.exists():
            try:
                if json.loads(ownerpath.read_text()) == owner:
                    ownerpath.unlink()
            except (FileNotFoundError, json.JSONDecodeError):
                pass
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def focus_process_identity(pid):
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='],
                                capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def steam_focus_identity(pid, bottle):
    try:
        return mapped_steam_focus_identity(pid, bottle)
    except (OSError, subprocess.TimeoutExpired):
        return None


def mapped_steam_focus_identity(pid, bottle):
    if not bottle:
        return None
    identity = focus_process_identity(pid)
    if not identity:
        return None
    args = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'args='],
                          capture_output=True, text=True, timeout=3)
    if args.returncode or re.search(r'(?:^|\s)--type(?:=|\s)', args.stdout):
        return None
    files = subprocess.run(['/usr/sbin/lsof', '-a', '-p', str(pid), '-d', 'txt', '-Fn'],
                           capture_output=True, text=True, timeout=3)
    if files.returncode:
        return None
    root = (Path(bottle) / 'drive_c/Program Files (x86)/Steam').resolve()
    for line in files.stdout.splitlines():
        if not line.startswith('n'):
            continue
        mapped = Path(line[1:]).resolve()
        valid = mapped == root / 'steam.exe' or (
            mapped.name == 'steamwebhelper.exe' and mapped.is_relative_to(root / 'bin/cef'))
        if valid and focus_process_identity(pid) == identity:
            return {'pid': pid, 'identity': identity, 'executable': str(mapped)}
    return None


class FocusRecovery:
    def __init__(self):
        self.count = 0
        self.last_ns = None

    def allowed(self, now):
        return self.count < 3 and (self.last_ns is None or now - self.last_ns >= 10 * 10**9)

    def attempt(self, now):
        if not self.allowed(now):
            return False
        self.count += 1
        self.last_ns = now
        return True


def startup_controller(helper, directory, pid, start_ns, *, timeout=600, focus_policy="manual", bottle=None):
    directory = Path(directory)
    now, uptime = clocks()
    session = StartupSession(start_ns, uptime - (now - start_ns), timeout=timeout)
    state = {'menu_verified': False, 'outcome': None, 'last_observation_ns': None,
             'quit_requested_ns': None, 'pid': pid}
    sequence = 0
    last_action = 0
    recovery = FocusRecovery()
    state.update(input_blocked_since_ns=None, total_input_blocked_ns=0, focus_recovery_count=0)

    def ui(*args):
        if args[0] != 'ocr' and not controller_process_owned(directory, pid):
            raise RuntimeError('owned_process_identity_changed')
        command = [str(helper), *map(str, args)]
        try:
            return run_ui_command(command, timeout=20 if args[0] == 'capture-window' else 10,
                                  ownerpath=directory / 'ui-owner.json')
        except subprocess.TimeoutExpired as error:
            diagnostic = {'command': command, 'timeout_seconds': error.timeout,
                          'stdout': error.output, 'stderr': error.stderr}
            state['ui_failure'] = diagnostic
            with (directory / 'controller.jsonl').open('a') as stream:
                stream.write(json.dumps({'event': 'ui_timeout', **diagnostic}) + '\n')
            raise RuntimeError('ui_command_timeout: ' + json.dumps(command)) from error

    def record(event, **values):
        with (directory / 'controller.jsonl').open('a') as stream:
            stream.write(json.dumps({'event': event, **values}) + '\n')

    def blocked(deferred, now):
        begin = state['input_blocked_since_ns']
        if deferred and begin is None:
            state['input_blocked_since_ns'] = now
            record('input_blocked', clock_ns=now)
        elif not deferred and begin is not None:
            state['total_input_blocked_ns'] += now - begin
            state['input_blocked_since_ns'] = None
            record('input_unblocked', clock_ns=now, start_ns=begin, end_ns=now)

    def save():
        temporary = directory / 'controller-state.tmp'
        temporary.write_text(json.dumps(state, indent=2) + '\n')
        temporary.replace(directory / 'controller-state.json')

    window_seen = False
    acquisition_deadline = now + 15 * 10**9
    try:
        ui('activate', pid)
        time.sleep(1)
        while alive(pid) and not (directory / 'guardian-stop').exists():
            now, uptime = clocks()
            presents, compiles = [], []
            for name, stamps in (('present.csv', presents), ('compile.csv', compiles)):
                path = directory / name
                if not path.exists():
                    continue
                for line in path.read_text().splitlines():
                    values = line.split(',')
                    candidate = values[0] if name == 'present.csv' else values[-1]
                    if candidate.isdigit():
                        stamps.append(int(candidate))
            session.observe(now, uptime, present_ns=max(presents, default=None),
                            compile_ns=max(compiles, default=None))
            state['outcome'] = session.outcome(now)
            if state['outcome'] is not None:
                if now >= session.deadline and state['input_blocked_since_ns'] is not None:
                    state.update(outcome='invalid', failure='invalid_input_starvation')
                break
            if session.exit_requested_ns is not None:
                save()
                time.sleep(.5)
                continue
            image = directory / f'startup-frame-{sequence:04}.png'
            capture = ui('capture-window', pid, image)
            if capture.returncode:
                diagnostic = {'returncode': capture.returncode, 'stdout': capture.stdout,
                              'stderr': capture.stderr}
                try:
                    status = run_ui_command([str(helper), 'status', str(pid)], timeout=3,
                                            ownerpath=directory / 'ui-owner.json')
                    diagnostic['status'] = json.loads(status.stdout) if status.returncode == 0 else {
                        'returncode': status.returncode, 'stdout': status.stdout, 'stderr': status.stderr}
                except Exception as error:
                    diagnostic['status_error'] = str(error)
                state['capture_failure'] = diagnostic
                try:
                    capture_status=json.loads(capture.stdout)
                except (TypeError,json.JSONDecodeError):
                    capture_status={}
                if capture_status.get('failure_reason') == 'desktop_not_interactive':
                    raise RuntimeError('desktop_not_interactive')
                with (directory / 'controller.jsonl').open('a') as stream:
                    stream.write(json.dumps({'event': 'capture_failure', 'clock_ns': now,
                                             **diagnostic}) + '\n')
                if not window_seen and clocks()[0] < acquisition_deadline:
                    save()
                    time.sleep(.5)
                    continue
                raise RuntimeError('window_capture_lost' if window_seen else 'initial_window_timeout')
            window_seen = True
            ocr = ui('ocr', image)
            if ocr.returncode:
                raise RuntimeError('ocr_failed')
            text = ' '.join(json.loads(ocr.stdout))
            now, uptime = clocks()
            session.observe(now, uptime, text)
            state.update(menu_verified=session.menu_verified_ns is not None,
                         menu_verified_ns=session.menu_verified_ns,
                         last_observation_ns=now)
            with (directory / 'controller.jsonl').open('a') as stream:
                stream.write(json.dumps({'event': 'screen', 'clock_ns': now,
                                         'text': text, 'image': str(image)}) + '\n')
            if startup_action(text) is None:
                blocked(False, now)
            if session.menu_verified_ns is not None:
                session.request_exit(now)
                state['quit_requested_ns'] = now
                save()
                if ui('quit', pid).returncode and alive(pid):
                    raise RuntimeError('normal_exit_request_failed')
            elif now - last_action >= 5 * 10**9:
                next_action = startup_action(text)
                if next_action:
                    status_result = ui('status', pid)
                    if status_result.returncode:
                        raise RuntimeError('input_status_failed')
                    input_status = json.loads(status_result.stdout)
                    deferred = input_status.get('frontmost_pid') != pid
                    if deferred and focus_policy == 'steam_only' and recovery.allowed(now):
                        owner = steam_focus_identity(input_status.get('frontmost_pid'), bottle)
                        if owner is not None:
                            fresh = ui('status', pid)
                            current = json.loads(fresh.stdout) if fresh.returncode == 0 else {}
                            if (current.get('frontmost_pid') == owner['pid'] and
                                    focus_process_identity(owner['pid']) == owner['identity'] and recovery.attempt(now)):
                                state['focus_recovery_count'] = recovery.count
                                activated = ui('activate', pid)
                                checked = ui('status', pid)
                                input_status = json.loads(checked.stdout) if checked.returncode == 0 else {}
                                deferred = activated.returncode != 0 or input_status.get('frontmost_pid') != pid
                                record('focus_recovery', clock_ns=now, steam_owner=owner,
                                       attempt=recovery.count, activation_returncode=activated.returncode,
                                       recovered=not deferred, status=input_status)
                    if not deferred:
                        response = ui(next_action[0], pid, *next_action[1:])
                        if response.returncode:
                            try:
                                input_status = json.loads(response.stdout)
                            except (TypeError, json.JSONDecodeError):
                                raise RuntimeError('input_failed')
                            if input_status.get('failure_reason') != 'not_foreground':
                                raise RuntimeError('input_failed')
                            deferred = True
                    with (directory / 'controller.jsonl').open('a') as stream:
                        stream.write(json.dumps({'event': 'input_deferred' if deferred else 'input',
                                                 'clock_ns': now, 'action': next_action,
                                                 'status': input_status}) + '\n')
                    blocked(deferred, now)
                    if not deferred:
                        last_action = now
            sequence += 1
            save()
            time.sleep(2)
    except (Exception, KeyboardInterrupt) as error:
        state.update(outcome='invalid', failure=str(error) or type(error).__name__)
    finally:
        save()
    return state


def action(text, finished):
    text = text.upper()
    if finished:
        if 'PAUSED' in text:
            return ('key', 49) if 'QUIT TO MAIN MENU' in text else ('move', .701, .878)
        if 'QUIT GAME' in text:
            return 'key', 49
        if 'CONTINUE GAME' in text:
            return 'move', .665, .878
        if 'PRESS ANY' in text:
            return 'key', 49
        return None
    if 'BOONTA' in text and 'LAP' in text:
        return None
    if ('PRESS ANY' in text or 'CONTINUE GAME' in text or
            (('AUTOSAVE' in text or 'AUT05AVE' in text) and 'OK' in text) or
            ('SEIZURE WARNING' in text and 'CONTINUE' in text)):
        return 'key', 49
    if 'SKIP' in text:
        return 'key', 3, 3
    return None


def command_for(base, label, directory, timeout):
    command = []
    index = 0
    replacements = {
        'MESA_KK_METAL_CONCURRENCY_LOG': str(directory / 'metal.jsonl'),
        'MESA_KK_VARIANT_LOG': str(directory / 'variants.jsonl'),
        'KK_TEST_IDLE_TRIGGER': str(directory / 'idle-trigger'),
    }
    while index < len(base):
        item = base[index]
        if item == '--via-steam':
            raise ValueError('via-steam would restart the existing Steam process')
        if item in ('--runs', '--seconds'):
            command += [item, label if item == '--runs' else str(timeout)]
            index += 2
        elif item == '--env':
            name, _, value = base[index + 1].partition('=')
            command += [item, name + '=' + replacements.get(name, value)]
            index += 2
        else:
            command.append(item)
            index += 1
    if '--cache-log' not in command:
        command.append('--cache-log')
    for name, value in replacements.items():
        if not any(s.startswith(name + '=') for s in command):
            command += ['--env', name + '=' + value]
    return command


def confirmed_checkpoint(log, requested, metal, cache):
    pattern = r"KK_TEST_IDLE_LAYER checkpoint start_ns=(\d+) end_ns=(\d+) result=(-?\d+)"
    matches = [tuple(map(int, match)) for match in re.findall(pattern, log)
               if int(match[0]) >= requested]
    if not matches:
        return None
    start, end, result = matches[-1]
    if result != 0:
        raise RuntimeError('queue_idle_failed')
    if any(e.get('operation') == 'serialize' and not e.get('success')
           and start <= e['start_ns'] <= end for e in metal):
        raise RuntimeError('serialization_failed')
    serializations = [e for e in metal if e.get('operation') == 'serialize'
                      and e.get('success') and e['end_ns'] <= end]
    previous_end = max((e['end_ns'] for e in serializations), default=0)
    pending = [e for e in cache if e.get('outcome') in
               ('compiler_fallback', 'compute_fallback') and
               previous_end < e.get('time_ns', 0) <= end]
    if pending:
        return None
    return {'start_ns': start, 'end_ns': end, 'result': result,
            'serialization': serializations[-1] if serializations else None}


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def stop_owned_game(pid):
    if pid is None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(.2)


def replace_save(source, target):
    stage = Path(tempfile.mkdtemp(prefix='.kk-save-', dir=target.parent))
    old = stage.with_name(stage.name + '-previous')
    moved = False
    try:
        shutil.copytree(source, stage, dirs_exist_ok=True)
        if target.exists():
            target.rename(old)
            moved = True
        try:
            stage.rename(target)
        except BaseException:
            if moved:
                old.rename(target)
                moved = False
            raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if moved:
            shutil.rmtree(old)


def run_one(config, base, helper, directory, label, profile, starting):
    directory.mkdir()
    shutil.copytree(profile, directory / 'save-before')
    harness = None
    pid = None
    report = {'label': label, 'valid': False, 'private_marker': 'biblioklept'}
    def record(event, **values):
        with (directory / 'controller.jsonl').open('a') as stream:
            stream.write(json.dumps({'event': event, 'clock_ns': clocks()[0], **values}) + '\n')
    def ui(*args):
        return run_ui_command([str(helper), *map(str, args)], timeout=10)
    try:
        replace_save(starting, profile)
        command = command_for(base, label, directory, config.get('timeout', 1200))
        (directory / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
        work = Path(command[command.index('--work') + 1])
        before = set((work / 'runs').glob('*'))
        with (directory / 'harness.log').open('w') as log:
            harness = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            initial = clocks()
            session = Session(*initial, seconds=config.get('gameplay_seconds', 120),
                              timeout=config.get('timeout', 1200))
            while clocks()[0] - initial[0] < 90 * 10**9:
                events = read_events(directory / 'metal.jsonl')
                if events:
                    pid = events[0]['pid']
                    break
                if harness.poll() is not None:
                    raise RuntimeError('launch_failed')
                session.observe(*clocks(), '')
                time.sleep(.5)
            if pid is None:
                raise RuntimeError('launch_pid_timeout')
            run_dirs = set((work / 'runs').glob('*')) - before
            if len(run_dirs) != 1:
                raise RuntimeError('ambiguous_run_directory')
            run = run_dirs.pop()
            report['run'] = str(run)
            report['pid'] = pid
            if ui('activate', pid).returncode:
                raise RuntimeError('activation_failed')
            time.sleep(1)
            foreground_seen = False
            foreground_deadline = clocks()[0] + 15 * 10**9
            checkpoint = False
            checkpoint_requested = None
            last_action = 0
            progress = clocks()[0]
            previous_mtime = 0
            for sequence in range(600):
                now, uptime = clocks()
                session.observe(now, uptime, '')
                if harness.poll() is not None or not alive(pid):
                    harness.wait(timeout=10)
                    meta = json.loads((run / 'run.json').read_text())
                    code = meta.get('termination', {}).get('returncode')
                    report['termination'] = meta.get('termination')
                    report['valid'] = session.valid_exit(code, checkpoint)
                    if not report['valid']:
                        raise RuntimeError('exit_before_validation_completed')
                    break
                image = directory / f'frame-{sequence:04}.png'
                capture = ui('capture', pid, image)
                if capture.returncode:
                    if not foreground_seen and now < foreground_deadline:
                        time.sleep(.5)
                        ui('activate', pid)
                        continue
                    if session.finished and checkpoint:
                        deadline = time.time() + 3
                        while alive(pid) and time.time() < deadline:
                            time.sleep(.2)
                    if not alive(pid):
                        continue
                    raise RuntimeError('foreground_or_capture_lost')
                foreground_seen = True
                ocr = ui('ocr', image)
                if ocr.returncode:
                    raise RuntimeError('ocr_failed')
                text = ' '.join(json.loads(ocr.stdout))
                was_playing = session.playing
                finished = session.observe(*clocks(), text)
                record('screen', text=text, image=str(image))
                if was_playing is None and session.playing is not None:
                    report['window_start_ns'] = session.playing
                    report['window_end_ns'] = session.playing + session.seconds * 10**9
                    record('gameplay_start', **{k: report[k] for k in ('window_start_ns', 'window_end_ns')})
                files = [run / 'present.csv', run / 'compile.csv', directory / 'variants.jsonl']
                mtime = max((p.stat().st_mtime_ns for p in files if p.exists()), default=0)
                if mtime != previous_mtime:
                    previous_mtime, progress = mtime, now
                if now - progress > 60 * 10**9:
                    raise RuntimeError('no_runtime_progress')
                if finished and checkpoint_requested is None:
                    checkpoint_requested = now
                    (directory / 'idle-trigger').touch()
                    record('checkpoint_requested')
                if checkpoint_requested is not None and not checkpoint:
                    wine_log = (run / 'wine.log').read_text(errors='replace')
                    confirmed = confirmed_checkpoint(
                        wine_log, checkpoint_requested,
                        read_events(directory / 'metal.jsonl'),
                        read_events(run / 'cache.jsonl'))
                    if confirmed:
                        checkpoint = True
                        report['checkpoint'] = confirmed
                        record('checkpoint_completed')
                        if ui('key', pid, 53).returncode:
                            raise RuntimeError('foreground_lost_before_pause')
                        last_action = now
                    elif now - checkpoint_requested > 60 * 10**9:
                        raise RuntimeError('checkpoint_not_confirmed')
                    time.sleep(2)
                    continue
                if now - last_action > 5 * 10**9:
                    next_action = action(text, finished)
                    if next_action:
                        if ui(next_action[0], pid, *next_action[1:]).returncode:
                            raise RuntimeError('foreground_lost_before_input')
                        record('input', action=next_action)
                        last_action = now
                time.sleep(2)
            else:
                raise RuntimeError('controller_iteration_limit')
    except (Exception, KeyboardInterrupt) as error:
        report['failure'] = str(error) or type(error).__name__
        if pid is not None and harness is not None and harness.poll() is None:
            try:
                subprocess.run(['/usr/bin/sample', str(pid), '5', '-file',
                                str(directory / 'failure-sample.txt')],
                               capture_output=True, timeout=15)
            except subprocess.TimeoutExpired:
                pass
    finally:
        if harness is not None and harness.poll() is None:
            stop_owned_game(pid)
        if harness is not None and harness.poll() is None:
            harness.terminate()
            try:
                harness.wait(timeout=10)
            except subprocess.TimeoutExpired:
                harness.kill()
                harness.wait()
        if profile.exists():
            shutil.copytree(profile, directory / 'save-after')
        replace_save(directory / 'save-before', profile)
        (directory / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def analyze_result(result, directory, previous, capture_interval):
    import importlib.util
    spec = importlib.util.spec_from_file_location('stutter', Path(__file__).with_name('stutter.py'))
    stutter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stutter)
    run = Path(result['run'])
    command = [sys.executable, str(ROOT / 'tests/analyze_kosmickrisp_warm.py'),
               '--cache', str(run / 'cache.jsonl'), '--variants', str(directory / 'variants.jsonl'),
               '--start-ns', str(result['window_start_ns']),
               '--end-ns', str(result['window_end_ns']), '--out', str(directory / 'ledger.json')]
    for path in previous:
        command += ['--previous-cache', str(path)]
    if capture_interval is not None:
        command += ['--persisted-from-ns', str(capture_interval[0]),
                    '--persisted-through-ns', str(capture_interval[1])]
    subprocess.run(command, check=True)
    ledger = json.loads((directory / 'ledger.json').read_text())
    begin, end = result['window_start_ns'], result['window_end_ns']
    starts = [t for t in stutter.load_presents(run / 'present.csv') if begin <= t <= end]
    frames = stutter.analyze_frames(starts, stutter.load_compiles(run / 'compile.csv'))
    (directory / 'frames.json').write_text(json.dumps(frames, indent=2) + '\n')
    waits = ledger['window_draw_waits']
    intervals = sorted((e['start_ns'], e['end_ns']) for e in waits)
    total, covered = 0, begin
    for left, right in intervals:
        if right > covered:
            total += right - max(left, covered)
            covered = right
    result['draw_wait_count'] = len(waits)
    result['draw_wait_union_ms'] = total / 10**6
    result['native_compilations'] = ledger['native_compilations']['counts']
    result['shader_compilations'] = ledger['shader_compilations']['counts']
    result['present_count'] = len(starts)
    if (len(starts) < 3 or starts[0] > begin + 5 * 10**9 or
            starts[-1] < end - 5 * 10**9 or
            not read_events(directory / 'variants.jsonl') or
            ledger['shader_compilations']['failed_events']):
        raise RuntimeError('insufficient_frames_or_shader_failure')
    return run / 'cache.jsonl'


def main():
    if '--startup-controller' in sys.argv[1:]:
        controller_parser = argparse.ArgumentParser()
        controller_parser.add_argument('--startup-controller', type=Path, required=True)
        controller_parser.add_argument('--helper', type=Path, required=True)
        controller_parser.add_argument('--pid', type=int, required=True)
        controller_parser.add_argument('--start-ns', type=int, required=True)
        controller_parser.add_argument('--timeout', type=int, default=600)
        controller_parser.add_argument('--focus-policy', choices=('manual', 'steam_only'), default='manual')
        controller_parser.add_argument('--bottle', type=Path)
        controller_args = controller_parser.parse_args()
        state = startup_controller(controller_args.helper, controller_args.startup_controller,
                                   controller_args.pid, controller_args.start_ns,
                                   timeout=controller_args.timeout, focus_policy=controller_args.focus_policy,
                                   bottle=controller_args.bottle)
        return 1 if state['outcome'] else 0
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--mode', choices=('startup', 'boonta'), default='boonta')
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--request-permissions', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if args.mode == 'startup':
        import investigate
        return investigate.run_from_gameplay(config, args.out, args.runs)

    base = json.loads(Path(config['command_file']).read_text())
    if '--via-steam' in base or 'skywalker-dx11' not in base:
        parser.error('This controller requires a direct Skywalker DX11 launch')
    profile = Path(config['save_directory'])
    starting = Path(config['starting_save'])
    for path in (profile, starting):
        if not path.is_dir():
            parser.error(f'missing save directory: {path}')
    if args.runs < 1:
        parser.error('--runs must be positive')
    if profile.resolve().is_relative_to(starting.resolve()) or starting.resolve().is_relative_to(profile.resolve()):
        parser.error('test profile and starting save must be separate directories')
    if not 120 <= config.get('timeout', 1200) <= 1200:
        parser.error('timeout must be between 120 and 1200 seconds')
    if config.get('gameplay_seconds', 120) != 120:
        parser.error('acceptance requires a 120-second gameplay window')
    args.out.mkdir(parents=True, exist_ok=False)
    helper = build_helper(Path(__file__).with_name('gameplay_ui.swift'),
                          Path.home() / 'Library/Caches/kk-gameplay-tools')
    permissions = subprocess.run([str(helper), 'request-permissions' if args.request_permissions else 'preflight'], capture_output=True,
                                 text=True, check=True)
    bottle = base[base.index('--bottle') + 1]
    steam_pids = steam_processes(bottle)
    steam_running = bool(steam_pids)
    bundle = Path(base[base.index('--bundle') + 1])
    hashes = {name: hashlib.sha256((bundle / name).read_bytes()).hexdigest()
              for name in ('libvulkan_kosmickrisp.dylib', 'libvulkan.1.dylib')}
    preflight = {'platform': sys.platform, 'steam_running': steam_running,
                 'steam_pids': steam_pids,
                 'helper': str(helper),
                 'bundle_sha256': hashes, 'permissions': json.loads(permissions.stdout),
                 'config': config,
                 'private_marker': 'biblioklept'}
    (args.out / 'preflight.json').write_text(json.dumps(preflight, indent=2) + '\n')
    if args.preflight or args.request_permissions:
        print(json.dumps(preflight, indent=2))
        error = permission_error(preflight['permissions'], helper)
        if error:
            print(error, file=sys.stderr)
            return 2
        return 0
    error = permission_error(preflight['permissions'], helper)
    if error:
        subprocess.run([str(helper), 'request-permissions'], check=True)
        parser.error(error)
    if not steam_running:
        parser.error('Start Steam in the test bottle before running gameplay validation')
    caffeinate = subprocess.Popen(['/usr/bin/caffeinate', '-di', '-w', str(os.getpid())])
    results = []
    previous = [Path(p) for p in config.get('previous_caches', [])]
    capture_interval = config.get('previous_capture_interval')
    try:
        for index in range(1, args.runs + 1):
            label = 'automated-warm-' + str(index)
            result = run_one(config, base, helper, args.out / label, label, profile, starting)
            if result['valid']:
                try:
                    previous.append(analyze_result(result, args.out / label, previous, capture_interval))
                    serial = result['checkpoint']['serialization']
                    if serial:
                        events = read_events(args.out / label / 'metal.jsonl')
                        capture_interval = [min(e['start_ns'] for e in events), serial['start_ns']]
                except Exception as error:
                    result['valid'] = False
                    result['failure'] = 'analysis: ' + str(error)
                (args.out / label / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
            results.append(result)
            (args.out / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
            print(json.dumps(result), flush=True)
            if not result['valid']:
                return 1
    finally:
        caffeinate.terminate()
        caffeinate.wait()
        acceptance = {'attempted_runs': len(results),
                      'valid_runs': sum(r['valid'] for r in results),
                      'zero_draw_waits_verified': len(results) >= 3 and
                      all(r['valid'] and r.get('draw_wait_count') == 0 for r in results)}
        (args.out / 'acceptance.json').write_text(json.dumps(acceptance, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
