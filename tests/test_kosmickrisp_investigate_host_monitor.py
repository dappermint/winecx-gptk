import importlib.util
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

module_dir = Path(__file__).resolve().parents[1] / 'runtime/kosmickrisp'
sys.path.insert(0, str(module_dir))
try:
    spec = importlib.util.spec_from_file_location('investigate_host_monitor_test', module_dir / 'investigate.py')
    investigate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(investigate)
finally:
    sys.path.pop(0)


def evidence(serious=False):
    return {'capture': {'returncode': 0, 'timed_out': False, 'truncated': False},
            'evidence': {'unknown_timezone_lines': 0, 'serious_host_fault': serious,
                         'events': [], 'counts': {}}}


class HostMonitorTests(unittest.TestCase):
    def monitor(self):
        monitor = investigate.HostMonitor.__new__(investigate.HostMonitor)
        monitor.stop = threading.Event()
        monitor.serious = threading.Event()
        monitor.incomplete = threading.Event()
        return monitor

    def test_incomplete_evidence_never_passes_gate(self):
        self.assertTrue(investigate.host_evidence_valid(evidence()))
        for field, value in [('returncode', 1), ('timed_out', True), ('truncated', True), ('error', 'failure')]:
            record = evidence()
            record['capture'][field] = value
            self.assertFalse(investigate.host_evidence_valid(record), field)
        record = evidence()
        record['evidence']['unknown_timezone_lines'] = 1
        self.assertFalse(investigate.host_evidence_valid(record))
        for record in [{}, {'capture': {'returncode': 0}}, {'capture': {}, 'evidence': {}}]:
            self.assertFalse(investigate.host_evidence_valid(record))

    def test_serious_complete_capture_sets_alert(self):
        monitor = self.monitor()
        host = Mock()
        host.capture_logs.return_value = evidence(True)
        with patch.dict(sys.modules, {'investigation_host': host}):
            monitor.inspect('/unused', 'start', 'end')
        self.assertTrue(monitor.serious.is_set())
        self.assertFalse(monitor.incomplete.is_set())
        host.capture_logs.assert_called_once_with('/unused', 'start', 'end', timeout=15)

    def test_partial_capture_marks_incomplete_without_serious(self):
        monitor = self.monitor()
        record = evidence(True)
        record['capture']['truncated'] = True
        host = Mock()
        host.capture_logs.return_value = record
        with patch.dict(sys.modules, {'investigation_host': host}):
            monitor.inspect('/unused', 'start', 'end')
        self.assertTrue(monitor.incomplete.is_set())
        self.assertFalse(monitor.serious.is_set())

    def test_capture_exception_marks_incomplete(self):
        monitor = self.monitor()
        host = Mock()
        host.capture_logs.side_effect = OSError('capture failed')
        with patch.dict(sys.modules, {'investigation_host': host}):
            monitor.inspect('/unused', 'start', 'end')
        self.assertTrue(monitor.incomplete.is_set())
        self.assertFalse(monitor.serious.is_set())

    def test_finish_bounds_join(self):
        monitor = self.monitor()
        monitor.thread = Mock()
        monitor.thread.is_alive.return_value = True
        monitor.finish()
        self.assertTrue(monitor.stop.is_set())
        self.assertTrue(monitor.incomplete.is_set())
        monitor.thread.join.assert_called_once_with(timeout=16)


if __name__ == '__main__':
    unittest.main()
