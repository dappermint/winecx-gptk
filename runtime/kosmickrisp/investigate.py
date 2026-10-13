import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import threading
import queue

import gameplay
import stutter


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(path)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def inventory(root):
    root = Path(root)
    return {str(p.relative_to(root)): sha(p) for p in sorted(root.rglob('*')) if p.is_file()}


def inventory_hash(root):
    return hashlib.sha256(json.dumps(inventory(root), sort_keys=True).encode()).hexdigest()


class BottleLease:
    def __init__(self, bottle, directory=None):
        root = Path(directory) if directory else Path.home() / 'Library/Caches/kk-gameplay-tools/locks'
        root.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(str(Path(bottle).resolve()).encode()).hexdigest()
        self.stream = (root / (key + '.lock')).open('a+')
        try:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.stream.close()
            raise RuntimeError('bottle_investigation_already_running')

    def close(self):
        self.stream.close()


def process_identity(pid):
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='],
                            capture_output=True, text=True, timeout=3)
    return result.stdout.strip() if result.returncode == 0 else None


def same_process(pid, identity):
    return identity is not None and process_identity(pid) == identity


def stop_owned(process, *, wait=True):
    if process is None or process.poll() is not None:
        return
    if not wait:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def stop_ui_group(run):
    owner=Path(run)/'ui-owner.json'
    if not owner.exists():
        return
    record=json.loads(owner.read_text())
    pid=int(record['pid'])
    if pid <= 1 or not same_process(pid,record['identity']):
        return
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid,signal.SIGKILL)
    except ProcessLookupError:
        pass


def game_processes(executable):
    name = Path(executable).name.lower()
    listing = subprocess.run(['ps', '-ww', '-axo', 'pid=,args='],
                             capture_output=True, text=True, timeout=5, check=True)
    found = []
    for line in listing.stdout.splitlines():
        match = re.match(r'^\s*(\d+)\s+("?)(.+?\.exe)\2(?:\s|$)', line, re.IGNORECASE)
        if match and match[3].replace('\\', '/').rsplit('/', 1)[-1].lower() == name:
            found.append(int(match[1]))
    return found


class ProfileTransaction:
    def __init__(self, plan, run):
        self.plan = plan
        self.profile = Path(plan['profile'])
        self.appid = Path(plan['game_executable']).with_name('steam_appid.txt')
        self.run = Path(run)
        self.journal = self.run / 'transaction.json'

    def prepare(self):
        if self.journal.exists():
            raise RuntimeError('existing_transaction_requires_recovery')
        dlls = {}
        source_dir = self.plan.get('dxvk_directory')
        if source_dir:
            for name in ('d3d11.dll', 'dxgi.dll'):
                source = Path(source_dir) / name
                expected = self.plan.get('dxvk_sha256', {}).get(name)
                if not expected or not source.is_file() or sha(source) != expected:
                    raise RuntimeError('dxvk_source_hash_mismatch_' + name)
                target = self.appid.with_name(name)
                if target.is_symlink() or (target.exists() and not target.is_file()):
                    raise RuntimeError('unsupported_local_dll_' + name)
                dlls[name] = {'path': str(target), 'existed': target.exists(),
                              'sha256': sha(target) if target.exists() else None,
                              'installed_sha256': expected}
        backup = self.run / 'backup'
        backup.mkdir()
        shutil.copy2(self.profile / 'PCCONFIG.TXT', backup / 'PCCONFIG.TXT')
        shutil.copytree(self.profile / 'SAVEDGAMES', backup / 'SAVEDGAMES')
        appid_existed = self.appid.exists()
        if appid_existed:
            shutil.copy2(self.appid, backup / 'steam_appid.txt')
        for name, entry in dlls.items():
            if entry['existed']:
                shutil.copy2(entry['path'], backup / name)
        atomic_json(self.journal, {'dlls': dlls, 'state': 'prepared', 'profile': str(self.profile),
                                  'appid': str(self.appid), 'appid_existed': appid_existed,
                                  'save_hash': inventory_hash(backup / 'SAVEDGAMES')})
        text = (backup / 'PCCONFIG.TXT').read_text()
        for key, value in {'Windowed': 1, 'BorderlessWindow': 0, 'FullscreenWindow': 0, 'WindowTop': 0}.items():
            text, count = re.subn(r'(?m)^(' + re.escape(key) + r'[ \t]+)[^\r\n]+',
                                  lambda m: m[1] + str(value), text)
            if count != 1:
                raise RuntimeError('ambiguous_configuration_' + key)
        (self.profile / 'PCCONFIG.TXT').write_text(text)
        self.appid.write_text('920210\n')
        for name, entry in dlls.items():
            self.install_file(Path(source_dir) / name, Path(entry['path']))
            if sha(entry['path']) != entry['installed_sha256']:
                raise RuntimeError('dxvk_install_hash_mismatch_' + name)

    @staticmethod
    def install_file(source, target):
        import tempfile
        descriptor, name = tempfile.mkstemp(prefix='.kk-dll-', dir=target.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            shutil.copy2(source, temporary)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def restore(self):
        if not self.journal.exists():
            return
        state = json.loads(self.journal.read_text())
        if Path(state['profile']).resolve() != self.profile.resolve() or Path(state['appid']).resolve() != self.appid.resolve():
            raise RuntimeError('transaction_target_mismatch')
        if state['state'] == 'restored':
            return
        backup = self.run / 'backup'
        for name, entry in state.get('dlls', {}).items():
            if name not in ('d3d11.dll', 'dxgi.dll') or Path(entry['path']).absolute() != self.appid.with_name(name).absolute():
                raise RuntimeError('dll_transaction_target_mismatch')
            if entry['existed'] and sha(backup / name) != entry['sha256']:
                raise RuntimeError('dll_backup_corrupt_' + name)
        if inventory_hash(backup / 'SAVEDGAMES') != state['save_hash']:
            raise RuntimeError('save_backup_corrupt')
        shutil.copy2(backup / 'PCCONFIG.TXT', self.profile / 'PCCONFIG.TXT')
        gameplay.replace_save(backup / 'SAVEDGAMES', self.profile / 'SAVEDGAMES')
        if state['appid_existed']:
            shutil.copy2(backup / 'steam_appid.txt', self.appid)
        else:
            self.appid.unlink(missing_ok=True)
        if inventory_hash(self.profile / 'SAVEDGAMES') != state['save_hash']:
            raise RuntimeError('save_restoration_failed')
        if sha(self.profile / 'PCCONFIG.TXT') != sha(backup / 'PCCONFIG.TXT'):
            raise RuntimeError('configuration_restoration_failed')
        if state['appid_existed'] and sha(self.appid) != sha(backup / 'steam_appid.txt'):
            raise RuntimeError('appid_restoration_failed')
        for name, entry in state.get('dlls', {}).items():
            target = self.appid.with_name(name)
            if entry['existed']:
                self.install_file(backup / name, target)
                if sha(target) != entry['sha256']:
                    raise RuntimeError('dll_restoration_failed_' + name)
            else:
                target.unlink(missing_ok=True)
        state['state'] = 'restored'
        atomic_json(self.journal, state)


class Progress:
    def __init__(self, start_ns):
        self.start_ns = start_ns
        self.last_present = start_ns
        self.last_compile = start_ns
        self.first_present = None
        self.offsets = {}

    def update(self, directory, now_ns=None):
        for name, column, attribute in [('present.csv', 0, 'last_present'), ('compile.csv', 3, 'last_compile')]:
            path = Path(directory) / name
            if not path.exists():
                continue
            offset = self.offsets.get(name, 0)
            if path.stat().st_size < offset:
                offset = 0
            with path.open() as stream:
                stream.seek(offset)
                while True:
                    position = stream.tell()
                    line = stream.readline()
                    if not line or not line.endswith('\n'):
                        self.offsets[name] = position
                        break
                    row = line.strip().split(',')
                    if len(row) > column and row[column].isdigit():
                        value = int(row[column])
                        if value < self.start_ns or (now_ns is not None and value > now_ns):
                            continue
                        setattr(self, attribute, max(getattr(self, attribute), value))
                        if name == 'present.csv' and self.first_present is None:
                            self.first_present = value

    def stalled(self, now_ns):
        return now_ns - max(self.last_present, self.last_compile) >= 45 * 10**9


def clean_environment(plan, run, archive_enabled):
    env = stutter.wine_env(Path(plan['runtime']), Path(plan['bundle']), Path(plan['bottle']))
    for key in list(env):
        if key.startswith(('MESA_KK_', 'MESA_SHADER_CACHE', 'DXVK_', 'VK_LAYER_', 'VK_INSTANCE_LAYERS', 'KK_TEST_IDLE')):
            env.pop(key)
    env.update({'WINEMSYNC': '1', 'WINEESYNC': '1', 'WINEDLLOVERRIDES': 'd3d11,dxgi=n,b',
                'SteamAppId': '920210', 'SteamGameId': '920210', 'DXVK_ASYNC': '0',
                'MESA_SHADER_CACHE_DIR': str(run / 'cache/mesa-cache'),
                'DXVK_SHADER_CACHE_PATH': str(run / 'cache/dxvk-shader-cache'),
                'DXVK_STATE_CACHE_PATH': str(run / 'cache/dxvk-state-cache')})
    for key, filename in {'MESA_KK_PRESENT_LOG': 'present.csv', 'MESA_KK_COMPILE_LOG': 'compile.csv',
                          'MESA_KK_CACHE_LOG': 'cache.jsonl', 'MESA_KK_VARIANT_LOG': 'variants.jsonl',
                          'MESA_KK_METAL_CONCURRENCY_LOG': 'metal.jsonl'}.items():
        env[key] = str(run / filename)
    if not archive_enabled:
        env['MESA_KK_PSO_CACHE_DISABLE'] = '1'
    return env


def sampling_reason(now, start, progress, controller, records, policy='gaps'):
    if controller.get('menu_verified') or len(records) >= 3 or progress.first_present is None:
        return None
    if records and now-records[-1]['requested_ns'] <= 30*10**9:
        return None
    if policy == 'late_title' and not any(r.get('trigger') == 'late_startup' for r in records):
        quiet_since = progress.last_compile if progress.last_compile is not None else start
        if now-start >= 420*10**9 and (now-quiet_since >= 60*10**9 or now-start >= 540*10**9):
            return 'late_startup'
        if len(records) >= 2:
            return None
    if progress.last_present is not None and now-progress.last_present > 2*10**9:
        return 'present_gap'
    return None


def collect_sample_context(path, helper, pid, identity, metadata):
    import investigation_host as host
    record={**metadata, 'status':'collecting'}
    atomic_json(path,record)
    try:
        if not same_process(pid,identity):
            raise RuntimeError('owned_process_identity_changed')
        record['foreground']=host.bounded_command([str(helper),'status',str(pid)],timeout=3)
        record['memory']=host.telemetry(pid)
        record['identity_valid_after']=same_process(pid,identity)
        record['finished_ns']=gameplay.clocks()[0]
        commands=[record['foreground'],*record['memory']['samples']]
        complete=all(c.get('returncode') == 0 and not c.get('timed_out') and not c.get('truncated')
                     and not c.get('error') for c in commands)
        record['status']=('identity_changed' if not record['identity_valid_after'] else
                          'complete' if complete else 'incomplete')
    except Exception as error:
        record.update(status='incomplete',error=str(error))
    atomic_json(path,record)


def sample_evidence(records):
    result=[]
    for record in records:
        path=Path(record['output'])
        code=record['process'].poll()
        available=path.is_file() and path.stat().st_size > 0
        result.append({k:v for k,v in record.items() if k != 'process'})
        result[-1].update(returncode=code, output_available=available,
                          collected=code == 0 and available and not record['timed_out'])
    return result


def host_evidence_valid(record):
    capture = record.get('capture', {})
    evidence = record.get('evidence', {})
    return (capture.get('returncode') == 0 and capture.get('timed_out') is False
            and capture.get('truncated') is False and not capture.get('error')
            and evidence.get('unknown_timezone_lines') == 0
            and isinstance(evidence.get('serious_host_fault'), bool)
            and isinstance(evidence.get('counts'), dict)
            and isinstance(evidence.get('events'), list))


def selected_host_evidence(run, result):
    primary=Path(run)/'host-result.json'
    original=json.loads(primary.read_text()) if primary.exists() else {}
    if host_evidence_valid(original):
        return original
    supplemental=Path(run)/'host-result-supplemental.json'
    if not supplemental.exists():
        return original
    record=json.loads(supplemental.read_text())
    if not host_evidence_valid(record):
        return original
    try:
        raw=Path(record['raw_log']).resolve()
        raw.relative_to(Path(run).resolve())
        if sha(raw) != record['raw_sha256']:
            return original
        for key in ('start','end'):
            if datetime.datetime.fromisoformat(record['evidence'][key]) != datetime.datetime.fromisoformat(result[key+'_wall']):
                return original
    except (KeyError,ValueError,OSError):
        return original
    return record


class HostMonitor:
    def __init__(self, run, start_wall):
        self.run = Path(run)
        self.start_wall = start_wall
        self.stop = threading.Event()
        self.serious = threading.Event()
        self.incomplete = threading.Event()
        self.thread = threading.Thread(target=self.watch, daemon=True)
        self.thread.start()

    def inspect(self, directory, start, end):
        import investigation_host as host
        try:
            evidence = host.capture_logs(directory, start, end, timeout=15)
            if not host_evidence_valid(evidence):
                self.incomplete.set()
            elif evidence['evidence']['serious_host_fault']:
                self.serious.set()
        except Exception:
            self.incomplete.set()

    def watch(self):
        start = datetime.datetime.fromisoformat(self.start_wall)
        iteration = 0
        while not self.stop.wait(30):
            end = datetime.datetime.now().astimezone()
            iteration += 1
            self.inspect(self.run / 'host-live' / str(iteration), start, end)
            if self.serious.is_set():
                return
            start = end - datetime.timedelta(seconds=5)

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=16)
        if self.thread.is_alive():
            self.incomplete.set()


def guard_run(plan, run, helper, archive_enabled=True, lease_fd=None):
    run = Path(run)
    lease = None if lease_fd is not None else BottleLease(plan['bottle'])
    if lease_fd is not None:
        os.fstat(lease_fd)
    transaction = ProfileTransaction(plan, run)
    child = controller = sampler = None
    sample_records = []
    context_threads = []
    result = {'outcome': 'invalid', 'reason': 'not_launched', 'private_marker': 'biblioklept'}
    start_wall = datetime.datetime.now().astimezone().isoformat()
    result['start_wall'] = start_wall
    stop_reason = None
    host_monitor = None
    try:
        existing = game_processes(plan['game_executable'])
        if existing:
            raise RuntimeError('game_already_running')
        transaction.prepare()
        env = clean_environment(plan, run, archive_enabled)
        start_ns, start_uptime = gameplay.clocks()
        progress = Progress(start_ns)
        atomic_json(run / 'launch.json', {'start_ns': start_ns, 'start_wall': start_wall,
                    'command': [stutter.wine(plan['runtime'], 'wine64'), plan['game_executable']],
                    'environment': {k:v for k,v in env.items() if k.startswith(('MESA_', 'DXVK_', 'VK_', 'CX_', 'WINE', 'Steam'))}})
        with (run / 'wine.log').open('w') as log:
            child = subprocess.Popen([stutter.wine(plan['runtime'], 'wine64'), plan['game_executable']],
                                      env=env, cwd=str(Path(plan['game_executable']).parent),
                                      stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        identity = process_identity(child.pid)
        if identity is None:
            raise RuntimeError('launch_identity_unavailable')
        atomic_json(run / 'owner.json', {'guardian_pid': os.getpid(), 'guardian_identity': process_identity(os.getpid()),
                                         'pid': child.pid, 'identity': identity, 'start_ns': start_ns})
        with (run / 'controller.log').open('w') as log:
            controller = subprocess.Popen([sys.executable, str(Path(__file__).with_name('gameplay.py')),
                           '--startup-controller', str(run), '--helper', str(helper), '--pid', str(child.pid),
                           '--start-ns', str(start_ns), '--timeout', str(plan.get('startup_seconds', 600)),
                           '--focus-policy', plan.get('focus_policy', 'manual'), '--bottle', plan['bottle']],
                           stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        host_monitor = HostMonitor(run, start_wall)
        samples = 0
        sample_start = 0
        last_inspection = 0
        loaded = False
        controller_state = {}
        inspections = queue.Queue()
        inspection_thread = None

        def inspect_process():
            try:
                current_identity = process_identity(child.pid)
                unexpected = set(game_processes(plan['game_executable'])) - {child.pid}
                files = subprocess.run(['/usr/sbin/lsof', '-a', '-p', str(child.pid), '-d', 'txt', '-Fn'],
                                       capture_output=True, text=True, timeout=3)
                paths = [line[1:] for line in files.stdout.splitlines() if line.startswith('n') and
                         any(name in line.lower() for name in ('kosmickrisp', 'libvulkan', 'd3d11.dll', 'dxgi.dll'))]
                inspections.put((current_identity, unexpected, paths, None))
            except Exception as error:
                inspections.put((None, set(), [], str(error)))

        while child.poll() is None:
            now_ns, uptime = gameplay.clocks()
            progress.update(run, now_ns)
            state_path = run / 'controller-state.json'
            if state_path.exists():
                controller_state = json.loads(state_path.read_text())
            elapsed = now_ns - start_ns
            reason = None
            if abs(elapsed - (uptime - start_uptime)) > 2 * 10**9:
                result.update(outcome='invalid');reason='system_sleep'
            elif host_monitor.serious.is_set():
                result.update(outcome='invalid');reason='serious_host_fault'
            elif (run / 'stop-request').exists():
                result.update(outcome='invalid');reason='stop_requested'
            elif controller_state.get('outcome') in ('invalid', 'crashed', 'stalled'):
                result.update(outcome=controller_state['outcome']);reason=controller_state.get('failure', controller_state.get('reason', 'controller_failed'))
            elif controller.poll() is not None and not controller_state.get('quit_requested_ns'):
                result.update(outcome='invalid');reason='controller_exited'
            elif controller_state.get('quit_requested_ns') and now_ns - controller_state['quit_requested_ns'] >= 60 * 10**9:
                result.update(outcome='stalled');reason='normal_exit_timeout'
            elif progress.stalled(now_ns):
                result.update(outcome='invalid' if progress.first_present is None else 'stalled')
                reason='instrumentation_progress_missing' if progress.first_present is None else 'no_present_or_compile_progress'
            elif elapsed >= plan.get('startup_seconds', 600) * 10**9:
                result.update(outcome='invalid' if controller_state.get('input_blocked_since_ns') is not None else 'timeout_progressing')
                reason='invalid_input_starvation' if controller_state.get('input_blocked_since_ns') is not None else 'absolute_deadline'
            if reason:
                result['reason'] = reason
                stop_reason = reason
                stop_owned(child, wait=False)
                break
            if sampler is not None and sampler.poll() is not None and 'finished_ns' not in sample_records[-1]:
                sample_records[-1]['finished_ns'] = now_ns
            if sampler is not None and sampler.poll() is None and now_ns - sample_start >= 20 * 10**9:
                sample_records[-1]['timed_out'] = True
                stop_owned(sampler, wait=False)
            trigger=sampling_reason(now_ns,start_ns,progress,controller_state,sample_records,
                                    plan.get('sampling_policy','gaps'))
            if trigger and (sampler is None or sampler.poll() is not None):
                samples += 1
                sample_start = now_ns
                with (run / f'sample-{samples}.log').open('w') as log:
                    sampler = subprocess.Popen(['/usr/bin/sample', str(child.pid), '1', '10', '-file',
                                  str(run / f'sample-{samples}.txt')], stdout=log, stderr=subprocess.STDOUT)
                sample_records.append({'process':sampler,'requested_ns':now_ns,'timed_out':False,'trigger':trigger,
                                       'output':str(run / f'sample-{samples}.txt'),'pid':sampler.pid,'budget_seconds':20})
                context=threading.Thread(target=collect_sample_context,args=(
                    run/f'sample-{samples}-context.json',helper,child.pid,identity,
                    {'requested_ns':now_ns,'trigger':trigger,'controller':dict(controller_state),
                     'last_present_ns':progress.last_present,'last_compile_ns':progress.last_compile,
                     'elapsed_ns':elapsed}),daemon=True)
                context_threads.append(context)
                context.start()
            if not inspections.empty():
                current_identity, unexpected, paths, inspection_error = inspections.get_nowait()
                if inspection_error:
                    result.update(outcome='invalid', reason='process_inspection_failed', inspection_error=inspection_error)
                    stop_reason = result['reason']
                    break
                if current_identity != identity:
                    result.update(outcome='invalid', reason='owned_process_identity_changed')
                    stop_reason = result['reason']
                    break
                if unexpected:
                    result.update(outcome='invalid', reason='unexpected_game_process', unexpected_pids=sorted(unexpected))
                    stop_reason = result['reason']
                    break
                if any('libvulkan_kosmickrisp.dylib' in path for path in paths):
                    (run / 'loaded-images.txt').write_text('\n'.join(paths) + '\n')
                    loaded = str(Path(plan['bundle']) / 'libvulkan_kosmickrisp.dylib') in paths
                    if 'dxvk_directory' in plan:
                        loaded = loaded and all(str(Path(plan['game_executable']).parent/name) in paths for name in ('d3d11.dll','dxgi.dll'))
                    if not loaded:
                        result.update(outcome='invalid', reason='wrong_driver_loaded')
                        stop_reason = result['reason']
                        break
            if (now_ns - last_inspection > 5 * 10**9 and
                    (inspection_thread is None or not inspection_thread.is_alive())):
                last_inspection = now_ns
                inspection_thread = threading.Thread(target=inspect_process, daemon=True)
                inspection_thread.start()
            time.sleep(.2)
        if child.poll() is not None and stop_reason is None:
            end_ns = gameplay.clocks()[0]
            progress.update(run, end_ns)
            state_path = run / 'controller-state.json'
            if state_path.exists():
                controller_state = json.loads(state_path.read_text())
            quit_ns = controller_state.get('quit_requested_ns')
            menu_ns = controller_state.get('menu_verified_ns')
            within_deadline = (end_ns - start_ns < plan.get('startup_seconds', 600) * 10**9
                               and quit_ns is not None and 0 <= end_ns - quit_ns < 60 * 10**9)
            verified = (controller_state.get('menu_verified') and menu_ns is not None
                        and quit_ns is not None and start_ns <= menu_ns <= quit_ns <= end_ns
                        and controller_state.get('outcome') is None)
            if child.returncode == 0 and verified and loaded and progress.first_present and within_deadline:
                result.update(outcome='passed', reason='menu_and_normal_exit')
            elif not within_deadline and verified:
                result.update(outcome='stalled' if progress.stalled(end_ns) else 'timeout_progressing',
                              reason='exit_after_deadline')
            else:
                result.update(outcome='crashed' if child.returncode else 'invalid', reason='exit_before_verified_menu')
        if result['outcome'] == 'stalled' and progress.first_present is None:
            result.update(outcome='invalid',reason='instrumentation_progress_missing')
        result.update(pid=child.pid, start_ns=start_ns, menu_verified=controller_state.get('menu_verified', False),
                      menu_verified_ns=controller_state.get('menu_verified_ns'),
                      first_present_ns=progress.first_present, last_present_ns=progress.last_present,
                      last_compile_ns=progress.last_compile, samples_requested=samples, driver_verified=loaded,
                      input_blocked_since_ns=controller_state.get('input_blocked_since_ns'),
                      total_input_blocked_ns=controller_state.get('total_input_blocked_ns',0) +
                          (max(0,gameplay.clocks()[0]-controller_state['input_blocked_since_ns'])
                           if controller_state.get('input_blocked_since_ns') is not None else 0),
                      focus_recovery_count=controller_state.get('focus_recovery_count',0))
    except BaseException as error:
        result.update(outcome='invalid', reason=type(error).__name__ + ': ' + str(error))
    finally:
        cleanup_errors = []
        try:
            (run / 'guardian-stop').touch()
        except Exception as error:
            cleanup_errors.append('sentinel: ' + str(error))
        for process in (child, controller, sampler):
            try:
                stop_owned(process, wait=False)
            except Exception as error:
                cleanup_errors.append('signal: ' + str(error))
        try:
            stop_ui_group(run)
        except Exception as error:
            cleanup_errors.append('ui_group: ' + str(error))
        for process in (child, controller, sampler):
            if process is not None:
                try:
                    process.wait(timeout=5)
                except Exception as error:
                    cleanup_errors.append('reap: ' + str(error))
        if child is not None:
            result['returncode'] = child.returncode
        try:
            if child is not None and child.poll() is None:
                raise RuntimeError('live_game_blocks_restoration')
            transaction.restore()
            result['restored'] = True
        except Exception as error:
            result.update(outcome='invalid', restored=False, restoration_error=str(error))
        if host_monitor is not None:
            host_monitor.finish()
            if host_monitor.serious.is_set():
                result.update(outcome='invalid', reason='serious_host_fault')
            elif host_monitor.incomplete.is_set():
                result['host_monitor_incomplete'] = True
                if result.get('outcome') == 'passed':
                    result.update(outcome='invalid', reason='host_evidence_incomplete')
        if cleanup_errors:
            result.update(outcome='invalid', cleanup_errors=cleanup_errors)
        for context in context_threads:
            context.join(timeout=0.1)
        result['samples'] = sample_evidence(sample_records)
        result['end_wall'] = datetime.datetime.now().astimezone().isoformat()
        result['end_ns'] = gameplay.clocks()[0]
        try:
            atomic_json(run / 'result.json', result)
        finally:
            if lease is not None:
                lease.close()
            elif lease_fd is not None:
                os.close(lease_fd)
    return result


def campaign_sequence(out, snapshot, phase='full'):
    comparisons=[('cold-1',None,True),('warm-control-1',snapshot,True),
                 ('warm-control-2',snapshot,True),('cold-2',None,True),
                 ('archive-on-1',snapshot,True),('archive-off-1',snapshot,False),
                 ('archive-off-2',snapshot,False),('archive-on-2',snapshot,True)]
    if phase == 'comparisons':
        return comparisons
    if phase != 'full':
        raise ValueError('invalid_campaign_phase')
    return [('baseline',snapshot,True),('warm-repeat',Path(out)/'runs/baseline/cache',True)] + comparisons


def validate_plan(plan):
    for key in ('runtime','bottle','bundle','profile','game_executable','cache_seed'):
        if not Path(plan[key]).exists():
            raise ValueError('missing_' + key)
    if plan.get('startup_seconds',600) != 600 or not 1 <= plan.get('max_runs',12) <= 12:
        raise ValueError('unapproved_limits')
    if plan.get('windowed', True) is not True or plan.get('steam_gpu', False) is not False:
        raise ValueError('supervised_conditions_not_enabled')
    if plan.get('focus_policy','manual') not in ('manual','steam_only'):
        raise ValueError('invalid_focus_policy')
    if plan.get('sampling_policy','gaps') not in ('gaps','late_title'):
        raise ValueError('invalid_sampling_policy')
    if plan.get('interaction_mode','automated') not in ('automated','supervised_foreground'):
        raise ValueError('invalid_interaction_mode')
    if plan.get('phase','full') not in ('full','comparisons'):
        raise ValueError('invalid_campaign_phase')
    driver = Path(plan['bundle'])/'libvulkan_kosmickrisp.dylib'
    if sha(driver) != plan['driver_sha256']:
        raise ValueError('driver_hash_mismatch')
    if 'dxvk_directory' in plan:
        for name in ('d3d11.dll','dxgi.dll'):
            if sha(Path(plan['dxvk_directory'])/name) != plan['dxvk_sha256'][name]:
                raise ValueError('dxvk_hash_mismatch_'+name)


def steam_process_records(bottle):
    result = subprocess.run(['ps', '-ww', '-axo', 'pid=,args='], capture_output=True,
                            text=True, timeout=5, check=True)
    root = (Path(bottle) / 'drive_c/Program Files (x86)/Steam').resolve()
    records = []
    for line in result.stdout.splitlines():
        match = re.match(r'^\s*(\d+)\s+("?)(.+?\.exe)\2(?:\s|$)', line, re.IGNORECASE)
        if not match:
            continue
        name = match[3].replace('\\', '/').rsplit('/', 1)[-1].lower()
        if name not in ('steam.exe', 'steamwebhelper.exe'):
            continue
        pid = int(match[1])
        files = subprocess.run(['/usr/sbin/lsof', '-a', '-p', str(pid), '-d', 'txt', '-Fn'],
                               capture_output=True, text=True, timeout=3)
        paths = [Path(value[1:]).resolve() for value in files.stdout.splitlines() if value.startswith('n')]
        mapped = any(path == root / 'steam.exe' if name == 'steam.exe' else
                     path.name.lower() == name and root in path.parents for path in paths)
        if files.returncode == 0 and mapped:
            records.append({'pid': pid, 'name': name, 'args': line.strip().split(None, 1)[1]})
    return records


def steam_arguments(bottle):
    records = steam_process_records(bottle)
    if not any(record['name'] == 'steam.exe' for record in records):
        return []
    return [record['args'] for record in records]


def steam_gpu_disabled(args):
    if any(re.search(r'(?:^|\s)-cef-force-gpu(?:\s|$)', line) for line in args):
        raise RuntimeError('steam_forced_gpu_requires_supervision')
    helpers = [line for line in args if re.match(r'^"?.*?steamwebhelper\.exe(?:"?\s|$)', line, re.IGNORECASE)]
    browsers = [line for line in helpers if not re.search(r'(?:^|\s)--type=', line)]
    if any(re.search(r'(?:^|\s)--use-angle=metal(?:\s|$)', line) for line in helpers):
        raise RuntimeError('steam_hardware_gpu_requires_supervision')
    gpu_children = [line for line in helpers if re.search(r'(?:^|\s)--type=gpu-process(?:\s|$)', line)]
    return bool(browsers) and all(re.search(r'(?:^|\s)--disable-gpu(?:\s|$)', line) for line in browsers) and all(
        re.search(r'(?:^|\s)--use-angle=swiftshader-webgl(?:\s|$)', line) for line in gpu_children)


def ensure_steam(plan, helper, out):
    if not steam_arguments(plan['bottle']):
        env = stutter.wine_env(Path(plan['runtime']), Path(plan['bundle']), Path(plan['bottle']))
        env.update({'WINEMSYNC':'1','WINEESYNC':'1'})
        with (out / 'steam.log').open('w') as log:
            process = stutter.steam_start(plan['runtime'], env, ['-cef-disable-gpu'], log)
        atomic_json(out / 'steam-owner.json', {'pid': process.pid, 'identity': process_identity(process.pid)})
    deadline = gameplay.clocks()[0] + 90 * 10**9
    while gameplay.clocks()[0] < deadline:
        args = steam_arguments(plan['bottle'])
        if steam_gpu_disabled(args):
            return args
        time.sleep(1)
    raise RuntimeError('steam_gpu_off_not_verified')


def cleanup_steam(plan, helper, out):
    owner_path = Path(out) / 'steam-owner.json'
    if not owner_path.exists():
        return {'owned': False}
    owner = json.loads(owner_path.read_text())
    pid, identity = owner['pid'], owner['identity']
    if not same_process(pid, identity):
        return {'owned': True, 'exited': True}
    if pid not in {record['pid'] for record in steam_process_records(plan['bottle']) if record['name'] == 'steam.exe'}:
        return {'owned': True, 'exited': False, 'reason': 'steam_ownership_not_verified'}
    request = subprocess.run([str(helper), 'quit', str(pid)], capture_output=True, text=True, timeout=5)
    deadline = gameplay.clocks()[0] + 15 * 10**9
    while gameplay.clocks()[0] < deadline:
        if not same_process(pid, identity):
            return {'owned': True, 'exited': True}
        time.sleep(0.5)
    return {'owned': True, 'exited': False, 'reason': 'normal_steam_exit_timeout', 'request_returncode': request.returncode}


def prepare_run(plan, run, seed, archive_enabled, steam_args):
    needed = sum(p.stat().st_size for p in Path(seed).rglob('*') if p.is_file()) if seed else 0
    if shutil.disk_usage(run.parent).free < needed + 2 * 1024**3:
        raise RuntimeError('insufficient_disk_space')
    run.mkdir()
    atomic_json(run/'steam-arguments.json',steam_args)
    if seed is None:
        (run/'cache').mkdir()
    else:
        shutil.copytree(seed,run/'cache')
    for name in ('mesa-cache','dxvk-state-cache','dxvk-shader-cache'):
        (run/'cache'/name).mkdir(exist_ok=True)
    seed_hash=inventory_hash(run/'cache')
    profile=Path(plan['profile'])
    config=(profile/'PCCONFIG.TXT').read_text()
    for key,value in {'Windowed':1,'BorderlessWindow':0,'FullscreenWindow':0,'WindowTop':0}.items():
        config=re.sub(r'(?m)^('+key+r'[ \t]+)[^\r\n]+',lambda m:m[1]+str(value),config)
    system=Path(plan['dxvk_directory']) if 'dxvk_directory' in plan else Path(plan['bottle'])/'drive_c/windows/system32'
    atomic_json(run/'provenance.json',{'driver_sha256':plan['driver_sha256'],
                'loader_sha256':sha(Path(plan['bundle'])/'libvulkan.1.dylib'),
                'game_sha256':sha(plan['game_executable']),
                'dxvk_sha256':{n:sha(system/n) for n in ('d3d11.dll','dxgi.dll')},
                'settings_sha256':hashlib.sha256(config.encode()).hexdigest(),
                'steam_arguments':{'gpu_disabled':True,'cef_force_gpu':False},'environment':{k:v.replace(str(run),'<RUN>') for k,v in clean_environment(plan,run,True).items() if k.startswith(('MESA_', 'DXVK_', 'VK_', 'CX_', 'WINE', 'Steam'))},
                'cache_seed_sha256':seed_hash,'cache_mode':'fresh' if seed is None else 'seeded',
                'capture_mode':plan.get('capture_mode','pid_window'),
                'capture_backend':'ScreenCaptureKit',
                'activation_backend':'verified_app_and_ax_window',
                'focus_policy':plan.get('focus_policy','manual'),
                'sampling_policy':plan.get('sampling_policy','gaps'),
                'interaction_mode':plan.get('interaction_mode','automated'),
                'archive_enabled':archive_enabled})


def recover(plan, out, lease=None):
    owned = lease is None
    lease = lease or BottleLease(plan['bottle'])
    try:
        if game_processes(plan['game_executable']):
            raise RuntimeError('running_game_blocks_recovery')
        for journal in sorted(out.glob('runs/*/transaction.json')):
            owner=journal.with_name('owner.json')
            if owner.exists():
                identity=json.loads(owner.read_text())
                if same_process(identity['guardian_pid'],identity['guardian_identity']):
                    raise RuntimeError('guardian_still_active')
            stop_ui_group(journal.parent)
            ProfileTransaction(plan,journal.parent).restore()
    finally:
        if owned:
            lease.close()


def run_from_gameplay(config,out,runs):
    plan_path=Path(config['investigation_plan'])
    return main(['--plan',str(plan_path),'--out',str(out),'--max-runs',str(runs)])


def event(out, kind, **fields):
    with (out/'events.jsonl').open('a') as stream:
        stream.write(json.dumps({'time':datetime.datetime.now().astimezone().isoformat(),
                                 'event':kind, **fields})+'\n')
        stream.flush()
        os.fsync(stream.fileno())


def campaign(plan, args, lease):
    import investigation_analysis as analysis
    import investigation_host as host
    import investigation_report as report
    if args.resume:
        recover(plan,args.out,lease)
        saved=json.loads((args.out/'plan.json').read_text())
        if saved != plan:
            raise RuntimeError('resume_plan_changed')
    else:
        atomic_json(args.out/'plan.json',plan)
    (args.out/'runs').mkdir(exist_ok=True)
    helper=gameplay.build_helper(Path(__file__).with_name('gameplay_ui.swift'),Path.home()/'Library/Caches/kk-gameplay-tools')
    permissions=json.loads(subprocess.check_output([str(helper),'preflight'],text=True,timeout=10))
    error=gameplay.permission_error(permissions,helper)
    if error:
        raise RuntimeError(error)
    if permissions.get('desktop_interactive') is False:
        raise RuntimeError('desktop_not_interactive')
    steam_args=ensure_steam(plan,helper,args.out)
    probe_env=clean_environment(plan,args.out,True)
    probe_env['WINEDEBUG']='err+all'
    probe=subprocess.run([stutter.wine(plan['runtime'],'wine64'),'cmd.exe','/c','exit','0'],env=probe_env,capture_output=True,text=True,timeout=15)
    atomic_json(args.out/'wine-preflight.json',{'returncode':probe.returncode,'stdout':probe.stdout,'stderr':probe.stderr})
    if probe.returncode:
        raise RuntimeError('wine_process_preflight_failed')
    snapshot=args.out/'cache-seed'
    if not snapshot.exists():
        temp=args.out/'cache-seed.partial'
        if temp.exists():
            shutil.rmtree(temp)
        shutil.copytree(plan['cache_seed'],temp)
        temp.rename(snapshot)
    seed_hash=inventory_hash(snapshot)
    seed_record=args.out/'cache-seed.json'
    if seed_record.exists() and json.loads(seed_record.read_text())['sha256'] != seed_hash:
        raise RuntimeError('immutable_seed_changed')
    atomic_json(seed_record,{'sha256':seed_hash})
    sequence=campaign_sequence(args.out,snapshot,plan.get('phase','full'))
    results=[]
    reason='batch_complete'
    for label,seed,archive in sequence[:plan.get('max_runs',12)]:
        run=args.out/'runs'/label
        if (run/'result.json').exists():
            result=json.loads((run/'result.json').read_text());results.append(result)
            previous_host = selected_host_evidence(run,result)
            if (result['outcome'] != 'passed' or not host_evidence_valid(previous_host)
                    or previous_host.get('evidence', {}).get('serious_host_fault')):
                reason='previous_run_requires_review';break
            continue
        if label.startswith('archive-'):
            controls=[args.out/'runs'/name/'analysis.json' for name in ('warm-control-1','warm-control-2')]
            if not all(p.exists() and json.loads(p.read_text()).get('native_archive_hits',0)>0 for p in controls):
                reason='archive_comparison_has_no_enabled_hits';break
        if run.exists():
            reason='incomplete_run_requires_review';break
        steam_args=ensure_steam(plan,helper,args.out)
        prepare_run(plan,run,seed,archive,steam_args)
        atomic_json(run/'host-before.json',host.telemetry())
        event(args.out,'launch',label=label,archive_enabled=archive)
        with (run/'guardian.log').open('w') as log:
            command=[sys.executable,str(Path(__file__).resolve()),'--guard','--plan',str(args.out/'plan.json'),'--out',str(run),'--helper',str(helper),'--lease-fd',str(lease.stream.fileno())]
            if not archive:command.append('--archive-disabled')
            guardian=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,pass_fds=(lease.stream.fileno(),))
        guardian.wait()
        if not (run/'result.json').exists():
            reason='guardian_missing_result';event(args.out,reason,label=label);break
        result=json.loads((run/'result.json').read_text());result['label']=label
        atomic_json(run/'result.json',result)
        atomic_json(run/'analysis.json',analysis.analyze_run(run,result.get('start_ns')))
        evidence=host.capture_logs(run/'host',result['start_wall'],result['end_wall'])
        atomic_json(run/'host-result.json',evidence)
        atomic_json(run/'host-after.json',host.telemetry())
        results.append(result)
        atomic_json(args.out/'results.json',results)
        event(args.out,'completed',label=label,outcome=result['outcome'])
        report.write_report(args.out)
        print(json.dumps(result),flush=True)
        if not host_evidence_valid(evidence):
            reason='host_evidence_incomplete';break
        if evidence.get('evidence',{}).get('serious_host_fault'):
            reason='serious_host_fault';break
        if result['outcome']!='passed':
            reason='run_requires_review';break
    atomic_json(args.out/'status.json',{'state':'evidence_review','reason':reason,'runs':len(results),'probable_culprit':None,
                'original_host_freeze':'unresolved_supervised_comparison_required','private_marker':'biblioklept'})
    report.write_report(args.out)
    return 0


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--analyze-only',action='store_true')
    parser.add_argument('--guard',action='store_true')
    parser.add_argument('--helper',type=Path)
    parser.add_argument('--archive-disabled',action='store_true')
    parser.add_argument('--lease-fd',type=int)
    parser.add_argument('--max-runs',type=int)
    args=parser.parse_args(argv)
    plan=json.loads(args.plan.read_text())
    if args.max_runs is not None:
        plan['max_runs']=min(plan.get('max_runs',12),args.max_runs)
    if args.guard:
        validate_plan(plan)
        result=guard_run(plan,args.out,args.helper,not args.archive_disabled,lease_fd=args.lease_fd)
        return 0 if result['outcome']=='passed' else 1
    if args.analyze_only:
        import investigation_analysis as analysis
        import investigation_report as report
        for run in sorted((args.out/'runs').glob('*')):
            if (run/'result.json').exists():
                atomic_json(run/'analysis.json',analysis.analyze_run(run,json.loads((run/'result.json').read_text()).get('start_ns')))
        report.write_report(args.out)
        return 0
    validate_plan(plan)
    if args.out.exists() and not args.resume:
        parser.error('output_exists_use_resume')
    args.out.mkdir(parents=True,exist_ok=True)
    lease=BottleLease(plan['bottle'])
    awake=None
    try:
        awake=subprocess.Popen(['/usr/bin/caffeinate','-d','-i','-w',str(os.getpid())])
        return campaign(plan,args,lease)
    except BaseException as error:
        event(args.out,'coordinator_error',reason=str(error))
        raise
    finally:
        try:
            helper=Path.home()/'Library/Caches/kk-gameplay-tools/gameplay-ui'
            atomic_json(args.out/'steam-cleanup.json',cleanup_steam(plan,helper,args.out))
        finally:
            stop_owned(awake)
            lease.close()


if __name__=='__main__':
    raise SystemExit(main())
