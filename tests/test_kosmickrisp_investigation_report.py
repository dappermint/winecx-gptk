import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('investigation_report', Path(__file__).resolve().parents[1] / 'runtime/kosmickrisp/investigation_report.py')
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


class InvestigationReportTests(unittest.TestCase):
    def create_run(self, root, name, fresh=False, outcome='passed'):
        path = root / 'runs' / name
        path.mkdir(parents=True)
        provenance = {key: 'same' for key in report._analysis.PROVENANCE_FIELDS}
        provenance.update(cache_mode='fresh' if fresh else 'seeded', cache_seed_sha256='empty' if fresh else 'seed', archive_enabled=True)
        value = {'start_ns': 0, 'menu_observed_ns': (100 if fresh else 50)*10**9,
                 'provenance': provenance, 'compile_counts': {'shaders/create': 100 if fresh else 5},
                 'missing_logs': [], 'issues': [], 'native_archive_hits': 10}
        (path / 'analysis.json').write_text(json.dumps(value))
        (path / 'result.json').write_text(json.dumps({'outcome': outcome}))

    def test_empty_batch_reports_missing_evidence_without_culprit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = report.write_report(root)
            self.assertEqual(len(result['missing_runs']), 8)
            self.assertTrue(all(h['status'] != 'probable' for h in result['hypotheses']))
            self.assertIn('biblioklept', (root / 'REPORT.md').read_text())

    def test_two_predictions_still_need_mechanism_and_alternative(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('cold-1', 'warm-control-1', 'warm-control-2', 'cold-2'):
                self.create_run(root, name, name.startswith('cold'))
            result = report.write_report(root)
            lead = result['hypotheses'][0]
            self.assertEqual(lead['status'], 'supported_candidate')
            self.assertEqual(lead['matching_pairs_with_predicted_effect'], 2)
            (root / 'evidence.txt').write_text('reviewed stacks and alternative comparison')
            (root / 'review.json').write_text(json.dumps({lead['hypothesis']: {
                'mechanism_checked': True, 'alternative_checked': True,
                'mechanism_evidence': ['evidence.txt'], 'alternative_evidence': ['evidence.txt']}}))
            self.assertEqual(report.write_report(root)['hypotheses'][0]['status'], 'probable')

    def test_failed_exit_does_not_validate_performance_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_run(root, 'cold-1', True)
            self.create_run(root, 'warm-control-1', False, 'timeout_progressing')
            report.write_report(root)
            pair = json.loads((root / 'comparisons.json').read_text())[0]
            self.assertFalse(pair['comparable'])
            self.assertFalse(pair['predicted_effect_observed'])
            self.assertIn('normal_exit_and_startup_pass_required', pair['reasons'])

    def test_invalid_foreground_run_reports_actual_app(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_run(root, 'warm-repeat', outcome='invalid')
            path = root / 'runs/warm-repeat/analysis.json'
            value = json.loads(path.read_text())
            value['capture_failures'] = [{'event': 'capture_failure', 'status': {
                'failure_reason': 'not_foreground', 'frontmost_app': 'Zed', 'target_running': True}}]
            path.write_text(json.dumps(value))
            report.write_report(root)
            text = (root / 'REPORT.md').read_text()
            self.assertIn('foreground app Zed', text)
            self.assertIn('cannot establish a warm-run performance result', text)

    def test_passing_run_tolerates_startup_and_exit_capture_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.create_run(root,'warm-control-1')
            path=root/'runs/warm-control-1/analysis.json';value=json.loads(path.read_text())
            value['capture_failures']=[{'status':{'failure_reason':'target_window_missing'}},
                                       {'status':{'failure_reason':'target_not_running'}}]
            path.write_text(json.dumps(value));result=report.write_report(root)
            self.assertFalse(result['run_status']['warm-control-1']['capture_diagnostics']['invalidating'])
            text=(root/'REPORT.md').read_text()
            self.assertIn('Tolerated capture diagnostic',text)
            self.assertNotIn('cannot establish a warm-run performance result',text)

    def test_four_case_campaign_does_not_require_archive_experiments(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'plan.json').write_text(json.dumps({'phase':'comparisons','max_runs':4}))
            for name in ['cold-1','warm-control-1','warm-control-2','cold-2']:
                self.create_run(root,name,name.startswith('cold'))
            result=report.write_report(root)
            self.assertEqual(result['missing_runs'],[])
            pairs=json.loads((root/'comparisons.json').read_text())
            self.assertEqual(len(pairs),2)
            self.assertTrue(all(p['variable']=='cache_mode' for p in pairs))

    def test_external_evidence_cannot_satisfy_review_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(report.evidence_files(root, ['/etc/hosts']))
            self.assertFalse(report.evidence_files(root, []))


if __name__ == '__main__':
    unittest.main()
