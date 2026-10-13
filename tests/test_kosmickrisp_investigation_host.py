import datetime as dt
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('investigation_host', Path(__file__).resolve().parents[1] / 'runtime/kosmickrisp/investigation_host.py')
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class HostTests(unittest.TestCase):
    def test_filters_ignored_log_end_and_duplicate_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'log'
            path.write_text('2026-10-10 11:57:17.836 F WindowServer Display reconfiguration sync timeout\n'
                            '2026-10-10 12:57:17.836 F kernel GPU reset completed\n'
                            'continuation without timestamp\n')
            result = host.analyze_logs([path, path], '2026-10-10 11:57:00', '2026-10-10 11:58:00')
            self.assertEqual(result['counts'], {'display_timeout': 1})
            self.assertFalse(result['serious_host_fault'])
            self.assertEqual(result['outside_interval'], 2)

    def test_accounting_fault_is_lead(self):
        self.assertEqual(host.classify('kernel (IOGPUFamily) prepare steamwebhelper preparedBytes != requiredBytes'), 'gpu_preparation_fault')

    def test_idle_exit_and_paniclog_are_not_fatal(self):
        self.assertEqual(host.classify('memorystatus: killing process due to idle-exit memory pressure'), 'idle_exit')
        self.assertIsNone(host.classify('Extensible paniclog not supported'))
        self.assertIsNone(host.classify('GPU reset support initialized'))

    def test_explicit_faults_are_serious(self):
        self.assertEqual(host.classify('kernel panic(cpu 2 caller 0x123)'), 'kernel_panic')
        self.assertEqual(host.classify('kernel GPU reset triggered'), 'gpu_reset')
        self.assertEqual(host.classify('memorystatus: killing pid 123 reason vm-pageshortage'), 'oom_kill')

    def test_timezone_must_be_known(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'log'
            path.write_text('2026-10-10 14:57:17.836 F WindowServer Display reconfiguration sync timeout\n')
            result = host.analyze_logs([path], '2026-10-10T11:57:00-03:00', '2026-10-10T11:58:00-03:00')
            self.assertEqual(result['unknown_timezone_lines'], 1)
            result = host.analyze_logs([path], '2026-10-10T11:57:00-03:00', '2026-10-10T11:58:00-03:00', naive_timezone=dt.timezone.utc)
            self.assertEqual(result['counts'], {'display_timeout': 1})

    def test_successful_command_exit_status(self):
        result = host.bounded_command([sys.executable, "-c", "print(123)"])
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["output"], "123\n")

    def test_bounds_subprocess_output(self):
        result = host.bounded_command([sys.executable, '-c', 'print("x" * 10000)'], max_bytes=100)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['output']), 100)

    def test_bounds_subprocess_time(self):
        result = host.bounded_command([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.1)
        self.assertTrue(result['timed_out'])
        self.assertLess(result['duration_s'], 2)

    def test_capture_retains_raw_and_filters_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(host, 'bounded_command', return_value={'output': '2026-10-10 15:00:00.000 F kernel GPU reset completed\n', 'returncode': 0}) as call:
                result = host.capture_logs(directory, '2026-10-10T14:00:00Z', '2026-10-10T14:05:00Z')
            self.assertEqual(result['evidence']['counts'], {})
            self.assertTrue(Path(result['raw_log']).exists())
            self.assertEqual(call.call_args.kwargs, {'timeout': 15, 'max_bytes': 8388608})
            self.assertIn('UTC', call.call_args.args[0])
            self.assertEqual(json.loads((Path(directory) / 'host-evidence.json').read_text())['evidence']['outside_interval'], 1)

    def test_capture_predicate_covers_every_classifier_category(self):
        cases = [
            ('display_timeout', 'Display reconfiguration sync timeout'),
            ('gpu_preparation_fault', 'IOGPU preparedBytes != requiredBytes'),
            ('kernel_panic', 'panic(cpu 2 caller 0x123)'),
            ('kernel_panic', 'kernel panic detected'),
            ('gpu_reset', 'GPU reset triggered'),
            ('gpu_reset', 'GPU restart completed'),
            ('gpu_reset', 'GPU panic detected'),
            ('gpu_reset', 'resetting the GPU'),
            ('idle_exit', 'killing process due to idle-exit'),
            ('idle_exit', 'idle exit process'),
            ('oom_kill', 'killed pid 123 out of memory'),
            ('oom_kill', 'memory pressure killing pid 123'),
            ('oom_kill', 'terminated process highwater'),
        ]
        predicate = host.capture_predicate()
        self.assertEqual(set(kind for kind, _ in cases), set(host.CAPTURE_TERMS))
        for kind, message in cases:
            self.assertEqual(host.classify(message), kind)
            terms = [term for term in host.CAPTURE_TERMS[kind] if term in message.lower()]
            self.assertTrue(terms, message)
            self.assertTrue(any('eventMessage CONTAINS[c] ' + json.dumps(term) in predicate for term in terms))
        self.assertIn(') AND (', predicate)
        self.assertFalse(any(term in 'IOGPU ordinary command scheduling' .lower()
                             for values in host.CAPTURE_TERMS.values() for term in values))

    def test_invalid_intervals_rejected(self):
        with self.assertRaises(ValueError):
            host.analyze_logs([], '2026-10-11', '2026-10-10')
        with self.assertRaises(ValueError):
            host.capture_logs('/unused', '2026-10-10', '2026-10-11')


if __name__ == '__main__':
    unittest.main()
