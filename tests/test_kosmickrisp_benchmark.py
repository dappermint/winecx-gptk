import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('benchmark', Path(__file__).resolve().parents[1] / 'runtime/kosmickrisp/benchmark.py')
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='fixed scene campaign ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runtime = self.root / 'runtime'
        self.bundle = self.root / 'bundle'
        self.bottle = self.root / 'bottle'
        self.output = self.root / 'campaign'
        self.scene = self.root / 'scene.json'
        self.fixture = self.root / 'scene.dem'
        self.fixture.write_bytes(b'recorded scene')
        for path in (self.runtime / 'Wine/bin/wine64', self.runtime / 'DXVK3/x32/d3d9.dll',
                     self.bundle / 'libvulkan_kosmickrisp.dylib', self.bundle / 'libvulkan.1.dylib',
                     self.bottle / 'drive_c/Program Files (x86)/Steam/steamapps/common/Portal 2/portal2.exe'):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(path.name.encode())
        self.scene.write_text(json.dumps({'game': 'portal2-d3d9', 'id': 'fixed-demo',
            'arguments': ['+timedemo', 'scene'], 'reproduction': 'Replay recorded scene without input.',
            'fixtures': [str(self.fixture)]}))

    def create(self, output=None, stage='gpl'):
        return benchmark.make_plan(self.runtime, self.bundle, self.bottle, 'portal2-d3d9',
                                   self.scene, output or self.output, stage)

    def test_balanced_order_and_cache_isolation(self):
        plan = self.create()
        self.assertEqual(len(plan['jobs']), 9)
        self.assertEqual(len({j['work'] for j in plan['jobs']}), 9)
        firsts = [plan['jobs'][i]['condition'] for i in (0, 3, 6)]
        self.assertEqual(len(set(firsts)), 3)
        for job in plan['jobs']:
            self.assertTrue((self.output / job['work'] / 'dxvk.conf').is_file())
        benchmark.verify(plan)

    def test_replaced_shader_or_fixture_invalidates_campaign(self):
        plan = self.create()
        self.fixture.write_bytes(b'changed scene')
        with self.assertRaisesRegex(ValueError, 'input changed'):
            benchmark.verify(plan)

    def test_no_fixture_is_rejected(self):
        self.scene.write_text(json.dumps({'game': 'portal2-d3d9', 'id': 'free-play',
                                         'arguments': [], 'reproduction': 'Uncontrolled input.'}))
        with self.assertRaisesRegex(ValueError, 'fixture'):
            self.create()
        self.assertFalse(self.output.exists())

    def test_stage_keeps_other_settings_fixed(self):
        rows = benchmark.conditions('threads', 'True')
        self.assertEqual([r[1]['dxvk.numCompilerThreads'] for r in rows], ['0', '2', '4'])
        self.assertTrue(all(r[1]['dxvk.enableGraphicsPipelineLibrary'] == 'True' for r in rows))
        self.assertTrue(all('dxvk.numCompilerThreads' not in r[1] for r in benchmark.conditions('gpl')))

    def make_results(self, plan):
        for job in plan['jobs']:
            for label in ('cold', 'warm', 'warm2'):
                path = self.output / job['work'] / 'runs' / label
                path.mkdir(parents=True)
                data = {'frames': {'mean_fps': 60, 'p99_ms': 20, 'hitches': 1, 'one_percent_low_fps': 40},
                        'dxvk': {'backend_verified': True, 'errors': 0, 'rejections': [],
                                 'gpl': True, 'effective_configuration': job['settings']},
                        'meta': {'label': label, 'driver_sha256': plan['hashes'][str(self.bundle / 'libvulkan_kosmickrisp.dylib')],
                                 'scene': {'sha256': plan['hashes'][str(self.scene)]},
                                 'termination': {'reason': 'natural_exit', 'returncode': 0}}}
                (path / 'summary.json').write_text(json.dumps(data))

    def test_summary_requires_all_repetitions_and_review(self):
        plan = self.create()
        report = benchmark.analyze(self.output / 'plan.json')
        self.assertFalse(report['complete'])
        self.make_results(plan)
        report = benchmark.analyze(self.output / 'plan.json')
        self.assertTrue(report['complete'])
        self.assertFalse(report['performance_claim_eligible'])
        self.assertEqual(report['rows'][0]['metrics']['p99_ms']['median'], 20)

    def test_wrong_driver_or_duplicate_results_are_invalid(self):
        plan = self.create()
        self.make_results(plan)
        path = self.output / plan['jobs'][0]['work'] / 'runs/cold/summary.json'
        data = json.loads(path.read_text())
        data['meta']['driver_sha256'] = '0' * 64
        path.write_text(json.dumps(data))
        report = benchmark.analyze(self.output / 'plan.json')
        self.assertFalse(report['complete'])
        self.assertIn('driver differs', str(report['invalid']))

    def test_ignored_configuration_is_rejected(self):
        plan = self.create()
        self.make_results(plan)
        path = self.output / plan['jobs'][0]['work'] / 'runs/cold/summary.json'
        data = json.loads(path.read_text())
        data['dxvk']['effective_configuration'] = {}
        path.write_text(json.dumps(data))
        report = benchmark.analyze(self.output / 'plan.json')
        self.assertFalse(report['complete'])
        self.assertIn('configuration not verified', str(report['invalid']))


if __name__ == '__main__':
    unittest.main()
