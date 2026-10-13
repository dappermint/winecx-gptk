import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import tempfile
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('gameplay', Path(__file__).resolve().parents[1] / 'runtime/kosmickrisp/gameplay.py')
gameplay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gameplay)
SECOND = 10**9


class GameplayTests(unittest.TestCase):
    def test_startup_replays_successful_menu_without_new_game(self):
        session = gameplay.StartupSession(0, 0)
        for second in range(1, 178):
            session.observe(second * SECOND, second * SECOND, 'THE SKYWALKER SAGA',
                            present_ns=second * SECOND)
        texts = ('rAIL THE SKYWALKER SAGA •* +t4OPN SAVING... 14BACK CONTINUE GAME SELECT',
                 'rAIL THE SKYWALKER SAGA 4V 1*1 +t4OPN.J 14BACK CONTINUE GAME SELECT')
        self.assertFalse(session.observe(179 * SECOND, 179 * SECOND, texts[0], present_ns=179 * SECOND))
        self.assertTrue(session.observe(182 * SECOND, 182 * SECOND, texts[1], present_ns=182 * SECOND))
        self.assertIsNone(session.outcome(182 * SECOND))
        self.assertFalse(session.valid_exit(0))
        session.request_exit(182 * SECOND)
        self.assertEqual(session.outcome(184 * SECOND, 0), 'passed')
        self.assertEqual(session.outcome(184 * SECOND, 1), 'crashed')

    def test_startup_requires_consecutive_unambiguous_observations(self):
        session = gameplay.StartupSession(0, 0)
        for second, text in enumerate(('CONTINUE GAME SELECT', 'LOADING CONTINUE GAME SELECT',
                                      'CONTINUE GAME BACK', 'PAUSED CONTINUE GAME BACK',
                                      'CONTINUE GAME', 'SELECT', 'SKIP'), 1):
            self.assertFalse(session.observe(second * SECOND, second * SECOND, text))
        self.assertIsNone(gameplay.startup_action('CONTINUE GAME SELECT'))
        self.assertEqual(gameplay.startup_action('PRESS ANY BUTTON'), ('key', 49))
        self.assertEqual(gameplay.startup_action('SKIP'), ('key', 3, 3))

    def test_startup_completed_compiles_prevent_false_stall_until_absolute_deadline(self):
        session = gameplay.StartupSession(0, 0)
        for second in range(1, 601):
            session.observe(second * SECOND, second * SECOND, compile_ns=second * SECOND)
            self.assertEqual(session.outcome(second * SECOND), 'timeout_progressing' if second == 600 else None)
        silent = gameplay.StartupSession(0, 0)
        silent.observe(45 * SECOND, 45 * SECOND)
        self.assertEqual(silent.outcome(45 * SECOND), 'stalled')

    def test_startup_ignores_old_and_future_progress(self):
        session = gameplay.StartupSession(100 * SECOND, 100 * SECOND)
        session.observe(145 * SECOND, 145 * SECOND, present_ns=99 * SECOND, compile_ns=999 * SECOND)
        self.assertEqual(session.outcome(145 * SECOND), 'stalled')

    def test_startup_sleep_and_early_clean_exit_are_invalid(self):
        session = gameplay.StartupSession(0, 0)
        self.assertEqual(session.outcome(SECOND, 0), 'invalid')
        session.observe(10 * SECOND, SECOND)
        self.assertEqual(session.outcome(10 * SECOND), 'invalid')
        with self.assertRaisesRegex(ValueError, 'menu_not_verified'):
            session.request_exit(10 * SECOND)

    def test_startup_exit_deadline_cannot_extend_absolute_deadline(self):
        session = gameplay.StartupSession(0, 0)
        for second in (590, 591):
            session.observe(second * SECOND, second * SECOND, 'CONTINUE GAME SELECT',
                            present_ns=second * SECOND)
        session.request_exit(591 * SECOND)
        session.request_exit(599 * SECOND)
        self.assertEqual(session.exit_requested_ns, 591 * SECOND)
        self.assertEqual(session.outcome(600 * SECOND), 'timeout_progressing')

    def test_startup_controller_stops_without_signalling_when_guardian_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'guardian-stop').touch()
            with patch.object(gameplay, 'controller_process_owned', return_value=True), \
                 patch.object(gameplay, 'clocks', return_value=(SECOND, SECOND)), \
                 patch.object(gameplay, 'alive', return_value=True), \
                 patch.object(gameplay.time, 'sleep'), \
                 patch.object(gameplay, 'run_ui_command', return_value=CompletedProcess([], 0)) as ui:
                state = gameplay.startup_controller('/helper', root, 42, SECOND)
            self.assertIsNone(state['outcome'])
            self.assertFalse(state['menu_verified'])
            self.assertEqual(ui.call_count, 1)
            self.assertEqual(json.loads((root / 'controller-state.json').read_text()), state)

    def test_startup_controller_requests_quit_once_without_recapturing_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            count = 0
            def clock():
                nonlocal count
                count += 1
                return count * SECOND, count * SECOND
            def command(args, **kwargs):
                if args[1] == 'ocr':
                    return CompletedProcess(args, 0, json.dumps(['CONTINUE GAME SELECT']))
                return CompletedProcess(args, 0, '')
            with patch.object(gameplay, 'controller_process_owned', return_value=True), \
                 patch.object(gameplay, 'clocks', side_effect=clock), \
                 patch.object(gameplay, 'alive', side_effect=[True, True, True, False]), \
                 patch.object(gameplay.time, 'sleep'), \
                 patch.object(gameplay, 'run_ui_command', side_effect=command) as ui:
                state = gameplay.startup_controller('/helper', root, 42, SECOND)
            operations = [call.args[0][1] for call in ui.call_args_list]
            self.assertEqual(operations, ['activate', 'capture-window', 'ocr', 'capture-window', 'ocr', 'quit'])
            self.assertTrue(state['menu_verified'])
            self.assertIsNotNone(state['quit_requested_ns'])
            self.assertIsNone(state['outcome'])

    def test_startup_controller_focus_loss_does_not_send_input(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(gameplay, 'controller_process_owned', return_value=True), \
                 patch.object(gameplay, 'clocks', return_value=(SECOND, SECOND)), \
                 patch.object(gameplay, 'alive', return_value=True), \
                 patch.object(gameplay.time, 'sleep'), \
                 patch.object(gameplay, 'run_ui_command', side_effect=[CompletedProcess([], 0), CompletedProcess([], 0), CompletedProcess([], 0, '[]'), CompletedProcess([], 2, 'capture details', 'capture stderr'), CompletedProcess([], 0, json.dumps({'failure_reason': 'not_foreground', 'frontmost_pid': 99, 'frontmost_app': 'Terminal', 'target_window_present': True}))]) as ui:
                state = gameplay.startup_controller('/helper', directory, 42, SECOND)
            self.assertEqual(state['outcome'], 'invalid')
            self.assertEqual(state['failure'], 'window_capture_lost')
            self.assertEqual(ui.call_count, 5)
            self.assertEqual(state['capture_failure']['stdout'], 'capture details')
            self.assertEqual(state['capture_failure']['stderr'], 'capture stderr')
            self.assertEqual(state['capture_failure']['status']['frontmost_pid'], 99)

    def test_startup_initial_window_retries_are_bounded(self):
        for end, expected in ((4, None), (17, 'initial_window_timeout')):
            with self.subTest(end=end), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                times = iter([(SECOND, SECOND), (2 * SECOND, 2 * SECOND),
                              (end * SECOND, end * SECOND)])
                with patch.object(gameplay, 'controller_process_owned', return_value=True), \
                     patch.object(gameplay, 'clocks', side_effect=lambda: next(times)), \
                     patch.object(gameplay, 'alive', side_effect=[True, False]), \
                     patch.object(gameplay.time, 'sleep'), \
                     patch.object(gameplay, 'run_ui_command', return_value=CompletedProcess([], 2)) as ui:
                    state = gameplay.startup_controller('/helper', root, 42, SECOND)
                self.assertEqual(state.get('failure'), expected)
                self.assertEqual(ui.call_count, 3)

    def test_locked_desktop_is_invalid_without_capture_retries_or_input(self):
        with tempfile.TemporaryDirectory() as temp:
            def command(args, **kwargs):
                return CompletedProcess(args,2 if args[1]=='capture-window' else 0,
                                        json.dumps({'failure_reason':'desktop_not_interactive'}))
            with patch.object(gameplay,'controller_process_owned',return_value=True), \
                 patch.object(gameplay,'clocks',return_value=(SECOND,SECOND)), \
                 patch.object(gameplay,'alive',return_value=True), \
                 patch.object(gameplay.time,'sleep'), \
                 patch.object(gameplay,'run_ui_command',side_effect=command) as ui:
                state=gameplay.startup_controller('/helper',Path(temp),42,SECOND)
            self.assertEqual(state['outcome'],'invalid')
            self.assertEqual(state['failure'],'desktop_not_interactive')
            operations=[call.args[0][1] for call in ui.call_args_list]
            self.assertEqual(operations.count('capture-window'),1)
            self.assertNotIn('key',operations)

    def test_window_capture_continues_background_and_defers_keyboard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            texts = iter(['PRESS ANY BUTTON', 'CONTINUE GAME SELECT', 'CONTINUE GAME BACK'])
            count = 5
            def clock():
                nonlocal count
                count += 1
                return count * SECOND, count * SECOND
            def command(args, **kwargs):
                if args[1] == 'ocr':
                    return CompletedProcess(args, 0, json.dumps([next(texts)]))
                if args[1] == 'status':
                    return CompletedProcess(args, 0, json.dumps({'frontmost_pid': 99, 'frontmost_app': 'Terminal'}))
                return CompletedProcess(args, 0, '')
            with patch.object(gameplay, 'controller_process_owned', return_value=True), \
                 patch.object(gameplay, 'clocks', side_effect=clock), \
                 patch.object(gameplay, 'alive', side_effect=[True, True, True, False]), \
                 patch.object(gameplay.time, 'sleep'), \
                 patch.object(gameplay, 'run_ui_command', side_effect=command) as ui:
                state = gameplay.startup_controller('/helper', root, 42, 6 * SECOND)
            operations = [call.args[0][1] for call in ui.call_args_list]
            self.assertEqual(operations.count('activate'), 1)
            self.assertEqual(operations.count('capture-window'), 3)
            self.assertNotIn('key', operations)
            self.assertTrue(state['menu_verified'])
            events = gameplay.read_events(root / 'controller.jsonl')
            self.assertEqual([event['event'] for event in events].count('input_deferred'), 1)

    def test_controller_rejects_reused_pid_before_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'owner.json').write_text(json.dumps({'pid': 42, 'identity': 'old start'}))
            with patch.object(gameplay.subprocess, 'run', return_value=CompletedProcess([], 0, 'new start\n')):
                self.assertFalse(gameplay.controller_process_owned(root, 42))
                self.assertFalse(gameplay.controller_process_owned(root, 43))
            with patch.object(gameplay, 'clocks', return_value=(SECOND, SECOND)), \
                 patch.object(gameplay, 'controller_process_owned', return_value=False), \
                 patch.object(gameplay, 'run_ui_command') as ui:
                state = gameplay.startup_controller('/helper', root, 42, SECOND)
            ui.assert_not_called()
            self.assertEqual(state['failure'], 'owned_process_identity_changed')

    def test_ui_timeout_kills_helper_and_spawned_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / 'child.pid'
            command = [sys.executable, '-c',
                       'import subprocess,sys,time,os; '
                       'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
                       'open(sys.argv[1],"w").write(str(p.pid)); '
                       'print(os.getpid(),flush=True); time.sleep(60)', str(pidfile)]
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                gameplay.run_ui_command(command, timeout=1)
            parent = int(raised.exception.output.strip())
            child = int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(parent, 0)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                status = subprocess.run(['ps', '-p', str(child), '-o', 'stat='], capture_output=True, text=True)
                if status.returncode or status.stdout.strip().startswith('Z'):
                    break
                time.sleep(.05)
            else:
                self.fail('helper descendant survived timeout')

    def test_ui_owner_survives_abrupt_controller_death(self):
        with tempfile.TemporaryDirectory() as directory:
            ownerpath = Path(directory) / 'ui-owner.json'
            source = str(Path(gameplay.__file__).parent)
            command = [sys.executable, '-c',
                       'import sys; sys.path.insert(0,sys.argv[1]); import gameplay; '
                       'gameplay.run_ui_command([sys.executable,"-c","import time; time.sleep(60)"],timeout=60,ownerpath=sys.argv[2])',
                       source, str(ownerpath)]
            controller = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            owner = None
            try:
                deadline = time.monotonic() + 3
                while not ownerpath.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(ownerpath.exists())
                owner = json.loads(ownerpath.read_text())
                controller.kill()
                controller.wait(timeout=3)
                self.assertEqual(json.loads(ownerpath.read_text()), owner)
                identity = subprocess.run(['ps', '-p', str(owner['pid']), '-o', 'lstart='],
                                          capture_output=True, text=True, check=True)
                self.assertEqual(identity.stdout.strip(), owner['identity'])
                self.assertEqual(os.getpgid(owner['pid']), owner['pgid'])
                self.assertEqual(owner['pid'], owner['pgid'])
            finally:
                if controller.poll() is None:
                    controller.kill()
                    controller.wait(timeout=3)
                if owner is not None:
                    try:
                        os.killpg(owner['pgid'], signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_ui_owner_cleared_only_if_it_matches_completed_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            ownerpath = Path(directory) / 'ui-owner.json'
            gameplay.run_ui_command([sys.executable, '-c', 'import time; time.sleep(.1)'],
                                    ownerpath=ownerpath)
            self.assertFalse(ownerpath.exists())
            replacement = {'pid': 123, 'pgid': 123, 'identity': 'replacement'}
            command = [sys.executable, '-c',
                       'import json,sys,time; time.sleep(.1); open(sys.argv[1],"w").write(sys.argv[2])',
                       str(ownerpath), json.dumps(replacement)]
            gameplay.run_ui_command(command, ownerpath=ownerpath)
            self.assertEqual(json.loads(ownerpath.read_text()), replacement)

    def test_controller_timeout_is_invalid_and_records_command(self):
        with tempfile.TemporaryDirectory() as directory:
            command = ['/helper', 'capture-window', '42', 'frame.png']
            with patch.object(gameplay, 'controller_process_owned', return_value=True), \
                 patch.object(gameplay, 'clocks', return_value=(SECOND, SECOND)), \
                 patch.object(gameplay, 'alive', return_value=True), \
                 patch.object(gameplay.time, 'sleep'), \
                 patch.object(gameplay, 'run_ui_command', side_effect=[CompletedProcess([], 0), subprocess.TimeoutExpired(command, 20, output='partial', stderr='capture hung')]) as ui:
                state = gameplay.startup_controller('/helper', directory, 42, SECOND)
            self.assertEqual(state['outcome'], 'invalid')
            self.assertEqual(state['ui_failure']['command'][1], 'capture-window')
            self.assertEqual(state['ui_failure']['stderr'], 'capture hung')
            self.assertEqual(state['ui_failure']['timeout_seconds'], 20)
            self.assertEqual(ui.call_args_list[0].kwargs['timeout'], 10)
            self.assertEqual(ui.call_args_list[1].kwargs['timeout'], 20)
            self.assertIn('capture-window', state['failure'])
            events = gameplay.read_events(Path(directory) / 'controller.jsonl')
            self.assertEqual(events[-1]['event'], 'ui_timeout')

    @unittest.skipUnless(os.environ.get('KK_GAMEPLAY_TEST_HELPER'), 'native helper not supplied')
    def test_native_helper_identifies_unbundled_process_without_app_launch_date(self):
        helper = os.environ['KK_GAMEPLAY_TEST_HELPER']
        def identity(pid):
            result = subprocess.run([helper, 'process-identity', str(pid)],
                                    capture_output=True, text=True, timeout=5, check=True)
            return json.loads(result.stdout)
        first = identity(os.getpid())
        self.assertFalse(first['app_launch_date_available'])
        self.assertTrue(first['identity'].startswith('kernel:'))
        self.assertEqual(first, identity(os.getpid()))
        self.assertIsNone(identity(2147483647)['identity'])

    def test_focus_recovery_budget_and_spacing(self):
        recovery = gameplay.FocusRecovery()
        self.assertTrue(recovery.attempt(0))
        self.assertFalse(recovery.attempt(9 * SECOND))
        self.assertTrue(recovery.attempt(10 * SECOND))
        self.assertTrue(recovery.attempt(20 * SECOND))
        self.assertFalse(recovery.attempt(60 * SECOND))
        self.assertEqual(recovery.count, 3)

    def test_steam_focus_requires_mapped_bottle_executable_and_stable_identity(self):
        cases = [('/bottle/drive_c/Program Files (x86)/Steam/steam.exe', 'old', True),
                 ('/bottle/drive_c/Program Files (x86)/Steam/bin/cef/cef.win7x64/steamwebhelper.exe', 'old', True),
                 ('/other/drive_c/Program Files (x86)/Steam/steam.exe', 'old', False),
                 ('/bottle/drive_c/Program Files (x86)/Steam/unknown.exe', 'old', False),
                 ('/bottle/drive_c/Program Files (x86)/Steam/steam.exe', 'new', False)]
        for executable, second_identity, accepted in cases:
            with self.subTest(executable=executable, identity=second_identity), \
                 patch.object(gameplay, 'focus_process_identity', side_effect=['old', second_identity]), \
                 patch.object(gameplay.subprocess, 'run', side_effect=[CompletedProcess([], 0, 'steam.exe'), CompletedProcess([], 0, 'n' + executable + '\n')]):
                self.assertEqual(gameplay.steam_focus_identity(99, '/bottle') is not None, accepted)
        with patch.object(gameplay, 'focus_process_identity', return_value='old'), \
             patch.object(gameplay.subprocess, 'run', return_value=CompletedProcess([], 0, 'steamwebhelper.exe --type=renderer')) as command:
            self.assertIsNone(gameplay.steam_focus_identity(99, '/bottle'))
            self.assertEqual(command.call_count, 1)

    def test_steam_focus_verification_timeout_defers(self):
        with patch.object(gameplay.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['ps'], 3)):
            self.assertIsNone(gameplay.focus_process_identity(99))
            self.assertIsNone(gameplay.steam_focus_identity(99, '/bottle'))
        with patch.object(gameplay, 'focus_process_identity', return_value='old'), \
             patch.object(gameplay.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['lsof'], 3)):
            self.assertIsNone(gameplay.steam_focus_identity(99, '/bottle'))

    def test_steam_recovery_success_failed_activation_and_input_race(self):
        for failure in (None, 'activation', 'input_race', 'pid_reused', 'unknown'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                count = 5
                activations = 0
                def clock():
                    nonlocal count
                    count += 1
                    return count * SECOND, count * SECOND
                def command(args, **kwargs):
                    nonlocal activations
                    if args[1] == 'ocr':
                        return CompletedProcess(args, 0, '["PRESS ANY BUTTON"]')
                    if args[1] == 'activate':
                        activations += 1
                        return CompletedProcess(args, 2 if failure == 'activation' and activations > 1 else 0, '')
                    if args[1] == 'status':
                        return CompletedProcess(args, 0, json.dumps({'frontmost_pid': 42 if activations > 1 else 99}))
                    if args[1] == 'key' and failure == 'input_race':
                        return CompletedProcess(args, 2, '{"failure_reason":"not_foreground"}')
                    return CompletedProcess(args, 0, '')
                owner = None if failure == 'unknown' else {'pid': 99, 'identity': 'old', 'executable': '/bottle/steam.exe'}
                with patch.object(gameplay, 'controller_process_owned', return_value=True), \
                     patch.object(gameplay, 'clocks', side_effect=clock), \
                     patch.object(gameplay, 'alive', side_effect=[True, False]), \
                     patch.object(gameplay.time, 'sleep'), \
                     patch.object(gameplay, 'steam_focus_identity', return_value=owner), \
                     patch.object(gameplay, 'focus_process_identity', return_value='new' if failure == 'pid_reused' else 'old'), \
                     patch.object(gameplay, 'run_ui_command', side_effect=command) as ui:
                    state = gameplay.startup_controller('/helper', root, 42, 6 * SECOND,
                                                        focus_policy='steam_only', bottle='/bottle')
                self.assertIsNone(state['outcome'])
                self.assertEqual(state['input_blocked_since_ns'] is None, failure is None)
                self.assertEqual(state['focus_recovery_count'], 0 if failure in ('unknown', 'pid_reused') else 1)
                keys = [call for call in ui.call_args_list if call.args[0][1] == 'key']
                self.assertEqual(len(keys), 1 if failure in (None, 'input_race') else 0)

    def test_persistent_prompt_starvation_is_invalid_only_at_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'present.csv').write_text(str(606 * SECOND) + ',1\n')
            times = [(6 * SECOND, 6 * SECOND), (7 * SECOND, 7 * SECOND),
                     (8 * SECOND, 8 * SECOND), (606 * SECOND, 606 * SECOND)]
            def command(args, **kwargs):
                if args[1] == 'ocr':
                    return CompletedProcess(args, 0, '["PRESS ANY BUTTON"]')
                if args[1] == 'status':
                    return CompletedProcess(args, 0, '{"frontmost_pid":99}')
                return CompletedProcess(args, 0, '')
            with patch.object(gameplay, 'controller_process_owned', return_value=True), \
                 patch.object(gameplay, 'clocks', side_effect=times), \
                 patch.object(gameplay, 'alive', return_value=True), \
                 patch.object(gameplay.time, 'sleep'), \
                 patch.object(gameplay, 'run_ui_command', side_effect=command):
                state = gameplay.startup_controller('/helper', root, 42, 6 * SECOND)
            self.assertEqual(state['outcome'], 'invalid')
            self.assertEqual(state['failure'], 'invalid_input_starvation')
            self.assertEqual(state['input_blocked_since_ns'], 8 * SECOND)

    def test_windows_steam_command_identified_by_mapped_bottle_executable(self):
        processes = '25049 C:\\Program Files (x86)\\Steam\\steam.exe -cef-force-gpu\n25056 C:\\Steam\\steamwebhelper.exe -steampath=C:\\Steam\\steam.exe\n'
        files = 'p25049\nftxt\nn/bottle/drive_c/Program Files (x86)/Steam/steam.exe\n'
        with patch.object(gameplay.subprocess, 'run', side_effect=[CompletedProcess([], 0, processes), CompletedProcess([], 0, files)]) as calls:
            self.assertEqual(gameplay.steam_processes('/bottle'), [25049])
            self.assertEqual(calls.call_count, 2)
            self.assertIn('-ww', calls.call_args_list[0].args[0])

    def test_steam_in_other_bottle_does_not_pass_preflight(self):
        processes = '25049 C:\\Program Files (x86)\\Steam\\steam.exe\n'
        files = 'p25049\nftxt\nn/other/drive_c/Program Files (x86)/Steam/steam.exe\n'
        with patch.object(gameplay.subprocess, 'run', side_effect=[CompletedProcess([], 0, processes), CompletedProcess([], 0, files)]):
            self.assertEqual(gameplay.steam_processes('/bottle'), [])

    def test_disappearing_steam_process_does_not_pass_preflight(self):
        processes = '25049 C:\\Program Files (x86)\\Steam\\steam.exe\n'
        with patch.object(gameplay.subprocess, 'run', side_effect=[CompletedProcess([], 0, processes), CompletedProcess([], 1, '')]):
            self.assertEqual(gameplay.steam_processes('/bottle'), [])

    def test_permission_error_reports_only_missing_access(self):
        message = gameplay.permission_error({'accessibility': True, 'screen_recording': False}, Path('/stable/gameplay-ui'))
        self.assertIn('Missing macOS access: Screen Recording.', message)
        self.assertIn('/stable/gameplay-ui', message)
        self.assertIsNone(gameplay.permission_error({'accessibility': True, 'screen_recording': True}, Path('/stable/gameplay-ui')))

    def test_helper_reused_and_failed_rebuild_preserves_previous_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache = root / 'helper.swift', root / 'cache'
            source.write_text('original source')
            def compile_binary(command, **kwargs):
                Path(command[-1]).write_bytes(b'original binary')
            with patch.object(gameplay.subprocess, 'run', side_effect=compile_binary) as compile_call:
                helper = gameplay.build_helper(source, cache)
                self.assertEqual(gameplay.build_helper(source, cache), helper)
                self.assertEqual(compile_call.call_count, 1)
            source.write_text('updated source')
            with patch.object(gameplay.subprocess, 'run', side_effect=RuntimeError('compiler failed')):
                with self.assertRaisesRegex(RuntimeError, 'compiler failed'):
                    gameplay.build_helper(source, cache)
            self.assertEqual(helper.read_bytes(), b'original binary')
            self.assertEqual(list(cache.glob('build-*')), [])

    def test_exit_after_first_gameplay_frame_is_not_success(self):
        session = gameplay.Session(0, 0)
        session.observe(SECOND, SECOND, 'BOONTA LAP 1/3')
        self.assertFalse(session.valid_exit(0, True))

    def test_full_window_requires_repeated_scene_and_checkpoint(self):
        session = gameplay.Session(0, 0)
        for seconds in (1, 50, 100, 122):
            session.observe(seconds * SECOND, seconds * SECOND, 'BOONTA LAP 1/3')
        self.assertTrue(session.valid_exit(0, True))
        self.assertFalse(session.valid_exit(0, False))
        self.assertFalse(session.valid_exit(1, True))

    def test_sleep_invalidates_window(self):
        session = gameplay.Session(0, 0)
        with self.assertRaisesRegex(RuntimeError, 'system_sleep'):
            session.observe(10 * SECOND, SECOND, '')

    def test_pausing_before_window_end_is_invalid(self):
        session = gameplay.Session(0, 0)
        session.observe(SECOND, SECOND, 'BOONTA LAP 1/3')
        with self.assertRaisesRegex(RuntimeError, 'gameplay_paused'):
            session.observe(2 * SECOND, 2 * SECOND, 'PAUSED')

    def test_menu_and_autosave_actions(self):
        self.assertEqual(gameplay.action('AUT05AVE NOTICE OK', False), ('key', 49))
        self.assertIsNone(gameplay.action('NEW GAME', False))
        self.assertEqual(gameplay.action('PAUSED QUIT TO MAIN MENU', True), ('key', 49))
        self.assertEqual(gameplay.action('CONTINUE GAME', True)[0], 'move')

    def test_stale_trigger_paths_are_replaced_and_steam_restart_rejected(self):
        base = ['python', 'stutter.py', 'run', '--runs', 'old', '--seconds', '300',
                '--env', 'KK_TEST_IDLE_TRIGGER=/old/trigger']
        result = gameplay.command_for(base, 'warm', Path('/new'), 1200)
        self.assertIn('KK_TEST_IDLE_TRIGGER=/new/idle-trigger', result)
        self.assertNotIn('KK_TEST_IDLE_TRIGGER=/old/trigger', result)
        self.assertIn('--cache-log', result)
        with self.assertRaises(ValueError):
            gameplay.command_for(base + ['--via-steam'], 'warm', Path('/new'), 1200)

    def test_queue_idle_without_new_capture_needs_no_serialization(self):
        log = 'KK_TEST_IDLE_LAYER checkpoint start_ns=10 end_ns=20 result=0'
        self.assertIsNotNone(gameplay.confirmed_checkpoint(log, 5, [], []))
        self.assertIsNone(gameplay.confirmed_checkpoint(log, 30, [], []))

    def test_pending_pso_requires_successful_serialization(self):
        log = 'KK_TEST_IDLE_LAYER checkpoint start_ns=10 end_ns=30 result=0'
        cache = [{'outcome': 'compiler_fallback', 'time_ns': 15}]
        self.assertIsNone(gameplay.confirmed_checkpoint(log, 5, [], cache))
        serial = {'operation': 'serialize', 'start_ns': 20, 'end_ns': 25, 'success': True}
        self.assertIsNotNone(gameplay.confirmed_checkpoint(log, 5, [serial], cache))
        with self.assertRaisesRegex(RuntimeError, 'serialization_failed'):
            gameplay.confirmed_checkpoint(log, 5, [{**serial, 'success': False}], cache)

    def test_queue_idle_failure_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, 'queue_idle_failed'):
            gameplay.confirmed_checkpoint('KK_TEST_IDLE_LAYER checkpoint start_ns=10 end_ns=20 result=-4', 5, [], [])

    def test_failed_save_swap_keeps_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, source = root / 'target', root / 'source'
            target.mkdir(); source.mkdir()
            (target / 'original').write_text('original')
            (source / 'new').write_text('new')
            rename = Path.rename
            def fail_install(path, destination):
                if path.name.startswith('.kk-save-') and not path.name.endswith('-previous'):
                    raise OSError('simulated rename failure')
                return rename(path, destination)
            with patch.object(Path, 'rename', fail_install):
                with self.assertRaises(OSError):
                    gameplay.replace_save(source, target)
            self.assertEqual((target / 'original').read_text(), 'original')
            self.assertEqual({p.name for p in root.iterdir()}, {'source', 'target'})

    def test_startup_failure_restores_original_save_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile, starting = root / 'profile', root / 'starting'
            profile.mkdir(); starting.mkdir()
            (profile / 'original').write_text('original')
            (starting / 'replacement').write_text('replacement')
            report = gameplay.run_one({}, ['--via-steam'], root / 'unused', root / 'run', 'warm', profile, starting)
            self.assertFalse(report['valid'])
            self.assertEqual({p.name for p in profile.iterdir()}, {'original'})
            self.assertEqual((profile / 'original').read_text(), 'original')


if __name__ == '__main__':
    unittest.main()
