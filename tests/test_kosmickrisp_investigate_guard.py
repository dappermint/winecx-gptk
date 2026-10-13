import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime/kosmickrisp'))
import investigate

SECOND = 10**9


class GuardTests(unittest.TestCase):
    def test_late_sample_reserved_after_two_early_attempts(self):
        progress=investigate.Progress(0)
        progress.first_present=SECOND
        progress.last_present=300*SECOND
        progress.last_compile=340*SECOND
        records=[{'requested_ns':100*SECOND,'trigger':'present_gap'},
                 {'requested_ns':200*SECOND,'trigger':'present_gap'}]
        self.assertIsNone(investigate.sampling_reason(400*SECOND,0,progress,{},records,'late_title'))
        progress.last_present=420*SECOND
        self.assertEqual(investigate.sampling_reason(420*SECOND,0,progress,{},records,'late_title'),'late_startup')
        records.append({'requested_ns':420*SECOND,'trigger':'late_startup'})
        self.assertIsNone(investigate.sampling_reason(550*SECOND,0,progress,{},records,'late_title'))

    def test_late_sample_busy_fallback_and_menu_exclusion(self):
        progress=investigate.Progress(0)
        progress.first_present=SECOND
        progress.last_present=539*SECOND
        progress.last_compile=530*SECOND
        self.assertIsNone(investigate.sampling_reason(539*SECOND,0,progress,{},[],'late_title'))
        self.assertEqual(investigate.sampling_reason(540*SECOND,0,progress,{},[],'late_title'),'late_startup')
        self.assertIsNone(investigate.sampling_reason(540*SECOND,0,progress,{'menu_verified':True},[],'late_title'))
        self.assertIsNone(investigate.sampling_reason(540*SECOND,0,progress,{},[{'requested_ns':535*SECOND}],'late_title'))

    def test_context_reused_pid_does_not_inspect_other_process(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(investigate,'same_process',return_value=False), \
             patch('investigation_host.bounded_command') as command:
            path=Path(temp)/'context.json'
            investigate.collect_sample_context(path,'/helper',42,'original',{'trigger':'late_startup'})
            command.assert_not_called()
            self.assertEqual(json.loads(path.read_text())['status'],'incomplete')

    def test_context_partial_status_is_not_complete_evidence(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(investigate,'same_process',return_value=True), \
             patch('investigation_host.bounded_command',return_value={'returncode':-9,'timed_out':True}), \
             patch('investigation_host.telemetry',return_value={'samples':[{'returncode':0}]}), \
             patch.object(investigate.gameplay,'clocks',return_value=(10,10)):
            path=Path(temp)/'context.json'
            investigate.collect_sample_context(path,'/helper',42,'original',{'trigger':'late_startup'})
            result=json.loads(path.read_text())
            self.assertEqual(result['status'],'incomplete')
            self.assertTrue(result['identity_valid_after'])

    def test_sample_request_is_not_collected_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.txt'
            process = MagicMock()
            process.poll.return_value = -9
            record = {'process':process,'output':str(path),'timed_out':True,'requested_ns':1}
            self.assertFalse(investigate.sample_evidence([record])[0]['collected'])
            path.write_text('stack')
            self.assertFalse(investigate.sample_evidence([record])[0]['collected'])
            record['timed_out'] = False
            process.poll.return_value = 0
            self.assertTrue(investigate.sample_evidence([record])[0]['collected'])

    def test_ui_group_cleanup_rejects_reused_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'ui-owner.json').write_text(json.dumps({'pid':123,'identity':'original'}))
            with patch.object(investigate,'same_process',return_value=False), patch.object(investigate.os,'killpg') as kill:
                investigate.stop_ui_group(root)
                kill.assert_not_called()
            with patch.object(investigate,'same_process',return_value=True), patch.object(investigate.os,'getpgid',return_value=123), patch.object(investigate.os,'killpg') as kill:
                investigate.stop_ui_group(root)
                kill.assert_called_once_with(123,investigate.signal.SIGKILL)

    def test_sampler_stop_never_waits(self):
        process = MagicMock()
        process.poll.return_value = None
        investigate.stop_owned(process, wait=False)
        process.kill.assert_called_once()
        process.wait.assert_not_called()
        process.terminate.assert_not_called()

    def test_reaped_process_is_not_signalled(self):
        process = MagicMock()
        process.poll.return_value = 0
        investigate.stop_owned(process, wait=False)
        process.kill.assert_not_called()

    def test_progress_counts_complete_rows_and_completed_compiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'present.csv').write_text('time,frame\n110,1\n130,')
            (root / 'compile.csv').write_text('shaders,new,110,120\n')
            progress = investigate.Progress(100)
            progress.update(root, 150)
            self.assertEqual(progress.first_present, 110)
            self.assertEqual(progress.last_present, 110)
            self.assertEqual(progress.last_compile, 120)
            with (root / 'present.csv').open('a') as stream:
                stream.write('2\n')
            progress.update(root, 150)
            self.assertEqual(progress.last_present, 130)

    def test_progress_ignores_stale_future_and_handles_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / 'present.csv'
            log.write_text('90,1\n999,2\n140,3\n')
            progress = investigate.Progress(100)
            progress.update(root, 150)
            self.assertEqual(progress.first_present, 140)
            log.write_text('160,4\n')
            progress.update(root, 170)
            self.assertEqual(progress.last_present, 160)
            self.assertFalse(progress.stalled(160 + 44 * SECOND))
            self.assertTrue(progress.stalled(160 + 45 * SECOND))

    def test_prepare_exception_restores_and_releases_inherited_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            transaction = MagicMock()
            transaction.prepare.side_effect = RuntimeError('prepare_failed')
            with patch.object(investigate, 'ProfileTransaction', return_value=transaction), \
                 patch.object(investigate, 'BottleLease') as lease, \
                 patch.object(investigate, 'game_processes', return_value=[]), \
                 patch.object(investigate.gameplay, 'clocks', return_value=(SECOND, SECOND)), \
                 patch.object(investigate.os, 'fstat'), \
                 patch.object(investigate.os, 'close') as close:
                result = investigate.guard_run({'bottle': '/bottle', 'game_executable': '/game.exe'}, run, '/helper', lease_fd=99)
            lease.assert_not_called()
            transaction.restore.assert_called_once()
            close.assert_any_call(99)
            self.assertEqual(sum(call.args == (99,) for call in close.call_args_list), 1)
            self.assertEqual(result['outcome'], 'invalid')
            self.assertTrue(result['restored'])
            self.assertIn('start_wall', result)
            self.assertEqual(json.loads((run / 'result.json').read_text()), result)

    def test_deadline_kills_child_before_reaping_and_cannot_pass_after_kill(self):
        self.check_guard_stop(controller_exit=None, now_seconds=601,
                              expected='timeout_progressing')

    def test_controller_death_kills_game_and_restores_profile(self):
        self.check_guard_stop(controller_exit=1, now_seconds=1,
                              expected='invalid')

    def check_guard_stop(self, controller_exit, now_seconds, expected):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / 'compile.csv').write_text('shaders,new,0,' + str(now_seconds * SECOND) + '\n')
            events = []
            child, controller = MagicMock(), MagicMock()
            child.pid = 42
            child.returncode = None
            child.poll.side_effect = lambda: child.returncode
            controller.returncode = controller_exit
            controller.poll.side_effect = lambda: controller.returncode
            def kill(process, label):
                events.append('kill_' + label)
                process.returncode = -9
            child.kill.side_effect = lambda: kill(child, 'game')
            controller.kill.side_effect = lambda: kill(controller, 'controller')
            child.wait.side_effect = lambda **kwargs: events.append('wait_game')
            controller.wait.side_effect = lambda **kwargs: events.append('wait_controller')
            transaction = MagicMock()
            transaction.restore.side_effect = lambda: events.append('restore')
            plan = {'bottle': '/bottle', 'game_executable': '/game.exe', 'runtime': '/runtime'}
            clock = [(0, 0), (now_seconds * SECOND, now_seconds * SECOND),
                     ((now_seconds + 1) * SECOND, (now_seconds + 1) * SECOND)]
            with patch.object(investigate, 'ProfileTransaction', return_value=transaction), \
                 patch.object(investigate, 'BottleLease'), \
                 patch.object(investigate, 'game_processes', return_value=[]), \
                 patch.object(investigate, 'clean_environment', return_value={}), \
                 patch.object(investigate, 'process_identity', return_value='identity'), \
                 patch.object(investigate.gameplay, 'clocks', side_effect=clock), \
                 patch.object(investigate.subprocess, 'Popen', side_effect=[child, controller]):
                result = investigate.guard_run(plan, run, '/helper')
            self.assertEqual(result['outcome'], expected)
            self.assertTrue(result['restored'])
            self.assertLess(events.index('kill_game'), events.index('wait_game'))
            self.assertLess(events.index('wait_game'), events.index('restore'))

    def test_pid_reuse_is_not_same_owned_process(self):
        with patch.object(investigate, 'process_identity', return_value='new-start'):
            self.assertFalse(investigate.same_process(42, 'old-start'))
            self.assertFalse(investigate.same_process(42, None))
            self.assertTrue(investigate.same_process(42, 'new-start'))


if __name__ == '__main__':
    unittest.main()
