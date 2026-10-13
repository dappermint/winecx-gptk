import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime/kosmickrisp'))
import investigate


class CoordinatorTests(unittest.TestCase):
    def test_analyze_only_does_not_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = root / 'plan.json'
            plan.write_text('{}')
            with patch.object(investigate.subprocess, 'Popen', side_effect=AssertionError('launch')):
                self.assertEqual(investigate.main(['--plan', str(plan), '--out', str(root), '--analyze-only']), 0)
            self.assertTrue((root / 'REPORT.md').exists())

    def test_gameplay_adapter_preserves_run_limit(self):
        with patch.object(investigate, 'main', return_value=0) as main:
            self.assertEqual(investigate.run_from_gameplay({'investigation_plan':'plan.json'},'output',2),0)
            self.assertEqual(main.call_args[0][0][-2:],['--max-runs','2'])

    def test_supplement_requires_matching_interval_and_raw_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            raw=root/'host.log';raw.write_text('evidence')
            result={'start_wall':'2026-10-11T01:00:00+00:00','end_wall':'2026-10-11T01:01:00+00:00'}
            record={'capture':{'returncode':0,'timed_out':False,'truncated':False},
                    'evidence':{'unknown_timezone_lines':0,'serious_host_fault':False,'counts':{},'events':[],
                                'start':result['start_wall'],'end':result['end_wall']},
                    'raw_log':str(raw),'raw_sha256':investigate.sha(raw)}
            path=root/'host-result-supplemental.json';path.write_text(json.dumps(record))
            self.assertEqual(investigate.selected_host_evidence(root,result),record)
            raw.write_text('changed')
            self.assertEqual(investigate.selected_host_evidence(root,result),{})
            raw.write_text('evidence');record['evidence']['end']=result['start_wall']
            path.write_text(json.dumps(record))
            self.assertEqual(investigate.selected_host_evidence(root,result),{})

    def test_comparisons_phase_preserves_reverse_order_and_seed(self):
        seed=Path('/immutable/seed')
        sequence=investigate.campaign_sequence(Path('/out'),seed,'comparisons')
        self.assertEqual([case[0] for case in sequence[:4]],
                         ['cold-1','warm-control-1','warm-control-2','cold-2'])
        self.assertEqual([case[1] for case in sequence[:4]],[None,seed,seed,None])
        self.assertEqual([case[2] for case in sequence[4:]],[True,False,False,True])
        with self.assertRaises(ValueError):
            investigate.campaign_sequence(Path('/out'),seed,'unknown')

    def test_ledger_appends(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            investigate.event(root, 'launch', label='baseline')
            investigate.event(root, 'completed', outcome='invalid')
            events = [json.loads(line) for line in (root / 'events.jsonl').read_text().splitlines()]
            self.assertEqual([event['event'] for event in events], ['launch', 'completed'])

    def test_reject_supervised_and_invalid_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            plan = dict.fromkeys(('runtime', 'bottle', 'bundle', 'profile', 'game_executable', 'cache_seed'), temp)
            for extra in ({'max_runs': 13}, {'max_runs': 0}, {'startup_seconds': 601}, {'windowed': False}, {'steam_gpu': True}, {'focus_policy':'unknown'}, {'phase':'unknown'}):
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    investigate.validate_plan({**plan, **extra})


if __name__ == '__main__':
    unittest.main()
