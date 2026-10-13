import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('investigation_analysis', Path(__file__).resolve().parents[1] / 'runtime/kosmickrisp/investigation_analysis.py')
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)
SECOND = 10**9


class InvestigationAnalysisTests(unittest.TestCase):
    def event(self, phase, left, right=None, key='r', parent='r', origin='prewarm', thread=1):
        return {'phase':phase,'start_ns':left,'end_ns':left if right is None else right,
                'request_id':key,'parent_id':parent,'origin':origin,'thread_id':thread}

    def test_wait_attribution_nested_work_and_queued_time_reconcile(self):
        events=[self.event('enqueue',0),self.event('worker_start',3),
                self.event('complete',9),self.event('pso_create',3,9,'native','r'),
                self.event('archive_load',4,6,'archive','native'),
                self.event('native_create',6,9,'native','r'),
                self.event('pso_wait',1,10,origin='draw')]
        result=analysis.attribute_waits(events,0,10)
        self.assertEqual(result['draw_wait_union_ns'],9)
        self.assertEqual(result['queued_ns'],2)
        self.assertEqual(result['active_work_ns'],6)
        self.assertEqual(result['unresolved_ns'],1)
        self.assertEqual(result['exclusive_phase_ns']['archive_load'],2)
        self.assertEqual(sum(result['exclusive_phase_ns'].values()),9)

    def test_reused_ids_do_not_join_previous_completed_episode(self):
        events=[self.event('enqueue',0),self.event('worker_start',1),self.event('complete',2),
                self.event('enqueue',10),self.event('worker_start',13),self.event('complete',19),
                self.event('pso_wait',11,13,origin='draw')]
        result=analysis.attribute_waits(events,0,20)
        self.assertEqual(result['queued_ns'],2)
        self.assertEqual(result['unresolved_ns'],0)

    def test_overlapping_draw_waits_count_union_once(self):
        events=[self.event('pso_create',0,10,'native','r'),
                self.event('pso_wait',1,8,origin='draw'),
                self.event('pso_wait',5,10,origin='draw')]
        result=analysis.attribute_waits(events,0,10)
        self.assertEqual(result['active_work_ns'],9)
        self.assertEqual(result['draw_wait_union_ns'],9)
        self.assertEqual(result['top_requests'][0]['wait_union_ns'],9)

    def test_ambiguous_parallel_work_remains_unresolved(self):
        events=[self.event('native_create',0,10,'native','r',thread=1),
                self.event('native_create',0,10,'native','r',thread=2),
                self.event('pso_wait',1,8,origin='draw')]
        result=analysis.attribute_waits(events,0,10)
        self.assertEqual(result['unresolved_ns'],7)
        self.assertEqual(result['active_work_ns'],0)

    def test_missing_id_and_conflicting_family_cannot_attribute_work(self):
        wait=self.event('pso_wait',1,8,origin='draw');wait['family_id']='one'
        work=self.event('pso_create',0,10,'native','r');work['family_id']='two'
        self.assertEqual(analysis.attribute_waits([wait,work],0,10)['unresolved_ns'],7)
        del wait['request_id']
        self.assertEqual(analysis.attribute_waits([wait,work],0,10)['unresolved_ns'],7)

    def test_parallel_waits_with_different_categories_are_unresolved(self):
        events=[self.event('archive_load',0,10,'a','r'),self.event('native_create',0,10,'b','s'),
                self.event('pso_wait',1,8,origin='draw'),
                self.event('pso_wait',4,10,key='s',parent='s',origin='draw')]
        result=analysis.attribute_waits(events,0,10)
        self.assertEqual(result['draw_wait_union_ns'],9)
        self.assertEqual(result['exclusive_phase_ns']['archive_load'],3)
        self.assertEqual(result['exclusive_phase_ns']['native_create'],2)
        self.assertEqual(result['unresolved_ns'],4)

    def test_queued_and_active_work_overlap_is_not_arbitrarily_assigned(self):
        events=[self.event('enqueue',0),self.event('worker_start',9),
                self.event('native_create',0,10,'native','r'),
                self.event('pso_wait',1,8,origin='draw')]
        result=analysis.attribute_waits(events,0,10)
        self.assertEqual(result['unresolved_ns'],7)
        self.assertEqual(result['waits'][0]['unresolved_reasons_ns']['queue_and_active_work_overlap'],7)

    def test_reused_child_id_from_unrelated_parent_does_not_add_native_work(self):
        events=[self.event('pso_create',0,10,'native','r'),
                self.event('native_create',0,10,'native','other'),
                self.event('pso_wait',1,8,origin='draw')]
        result=analysis.attribute_waits(events,0,10)
        self.assertEqual(result['exclusive_phase_ns']['pso_create'],7)
        self.assertNotIn('native_create',result['exclusive_phase_ns'])

    def test_pipeline_outcomes_are_key_and_time_correlated(self):
        events=[self.event('pso_create',0,10,'native','r'),
                self.event('pso_wait',1,8,origin='draw')]
        cache=[{'event':'pso','key':'native','time_ns':0,'outcome':'archive_hit'},
               {'event':'pso','key':'native','time_ns':7,'outcome':'compiler_fallback'},
               {'event':'pso','key':'other','time_ns':7,'outcome':'archive_hit'}]
        result=analysis.attribute_waits(events,0,10,cache)
        self.assertEqual(result['top_requests'][0]['pipeline_outcomes_during_wait'],['compiler_fallback'])

    def test_explicit_shared_cache_wait_can_join_another_recipe_producer(self):
        wait=self.event('pso_wait',1,8,origin='draw');wait['family_id']='one'
        dependency=self.event('pso_wait',0,8,'native','r');dependency['family_id']='one'
        producer=self.event('pso_create',0,10,'native','other');producer['family_id']='two'
        result=analysis.attribute_waits([wait,dependency,producer],0,10)
        self.assertEqual(result['exclusive_phase_ns']['pso_create'],7)
        self.assertEqual(result['unresolved_ns'],0)

    def test_wait_union_clips_overlap_and_counts_completed_metal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'present.csv').write_text('1000000000,1,0,1\n7000000000,1,0,1\n')
            waits = [{'origin': 'draw', 'phase': 'pso_wait', 'start_ns': 0, 'end_ns': 3*SECOND},
                     {'origin': 'draw', 'phase': 'fs_wait', 'start_ns': 2*SECOND, 'end_ns': 4*SECOND},
                     {'origin': 'prewarm', 'phase': 'pso_wait', 'start_ns': 0, 'end_ns': 8*SECOND}]
            (root / 'variants.jsonl').write_text('\n'.join(map(json.dumps, waits)))
            (root / 'metal.jsonl').write_text(json.dumps({'start_ns': 0, 'end_ns': 5*SECOND, 'success': True})+'\n')
            result = analysis.analyze_run(root, SECOND)
            self.assertEqual(result['draw_wait_union_ns'], 3*SECOND)
            gap = result['gaps'][0]
            self.assertEqual(gap['draw_wait_overlap_ns'], 3*SECOND)
            self.assertEqual(gap['outside_logged_draw_waits_ns'], 3*SECOND)
            self.assertEqual(gap['metal_completed'], 1)
            self.assertEqual(gap['causality'], 'temporal_overlap_only')
            self.assertFalse(any(e['status'] == 'probable' for e in result['evidence']))

    def test_recognition_requires_consecutive_recent_menu_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [{'time_ns': SECOND, 'text': 'CONTINUE GAME SELECT'},
                       {'time_ns': 2*SECOND, 'text': 'LOADING CONTINUE GAME SELECT'},
                       {'time_ns': 3*SECOND, 'text': 'CONTINUE GAME BACK'},
                       {'time_ns': 4*SECOND, 'text': 'CONTINUE GAME SELECT'}]
            (root / 'observations.jsonl').write_text('\n'.join(map(json.dumps, records)))
            result = analysis.analyze_run(root, 0)
            self.assertEqual(result['menu_observed_ns'], 4*SECOND)
            self.assertEqual(result['phases'][0]['end_ns'], 4*SECOND)

    def test_malformed_tail_is_disclosed_and_missing_logs_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'metal.jsonl').write_text('{"end_ns": 3}\n{"start_ns":')
            result = analysis.analyze_run(root)
            self.assertEqual(result['issues'][0]['line'], 2)
            self.assertIn('present.csv', result['missing_logs'])
            self.assertIsNone(result['menu_observed_ns'])
            self.assertFalse(analysis.compare_runs(result, result)['comparable'])

    def matched(self):
        provenance = {key: 'same' for key in analysis.PROVENANCE_FIELDS}
        provenance['archive_enabled'] = True
        return {'provenance': provenance, 'menu_observed_ns': 2*SECOND,
                'native_archive_hits': 3, 'missing_logs': [], 'issues': []}

    def test_different_seed_cannot_validate_archive_toggle(self):
        left, right = self.matched(), self.matched()
        right['provenance']['archive_enabled'] = False
        right['provenance']['cache_seed_sha256'] = 'warmed-by-left'
        result = analysis.compare_runs(left, right, 'archive_enabled')
        self.assertFalse(result['comparable'])
        self.assertEqual(result['unexpected_differences'], ['cache_seed_sha256'])

    def test_archive_comparison_requires_enabled_hits_and_menu_progress(self):
        left, right = self.matched(), self.matched()
        right['provenance']['archive_enabled'] = False
        self.assertTrue(analysis.compare_runs(left, right, 'archive_enabled')['comparable'])
        left['native_archive_hits'] = 0
        self.assertIn('enabled_case_has_no_archive_hits', analysis.compare_runs(left, right, 'archive_enabled')['reasons'])
        left['native_archive_hits'] = 3
        right['menu_observed_ns'] = None
        self.assertIn('matching_menu_progress_unverified', analysis.compare_runs(left, right, 'archive_enabled')['reasons'])

    def test_missing_identity_and_changed_driver_block_comparison(self):
        left, right = self.matched(), self.matched()
        del right['provenance']['loader_sha256']
        right['provenance']['driver_sha256'] = 'other'
        result = analysis.compare_runs(left, right)
        self.assertFalse(result['comparable'])
        self.assertIn('loader_sha256', result['missing_fields'])
        self.assertIn('driver_sha256', result['unexpected_differences'])

    def test_cache_mode_comparison_allows_only_seed_and_mode_to_change(self):
        left, right = self.matched(), self.matched()
        right['provenance']['cache_mode'] = 'fresh'
        right['provenance']['cache_seed_sha256'] = 'empty-seed'
        self.assertTrue(analysis.compare_runs(left, right, 'cache_mode')['comparable'])
        right['provenance']['driver_sha256'] = 'other'
        self.assertFalse(analysis.compare_runs(left, right, 'cache_mode')['comparable'])

    def test_native_library_and_deserialization_remain_separate_nested_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'compile.csv').write_text('deserialize,create,0,9000000000\nshaders,create,0,1000000000\n')
            (root / 'metal.jsonl').write_text('\n'.join(json.dumps(e) for e in [
                {'operation': 'library', 'start_ns': SECOND, 'end_ns': 5*SECOND},
                {'operation': 'library', 'start_ns': 3*SECOND, 'end_ns': 8*SECOND}]))
            value = analysis.analyze_run(root, 2*SECOND)['compilation_work']
            self.assertEqual(value['native_library']['summed_duration_ns'], 8*SECOND)
            self.assertEqual(value['native_library']['union_duration_ns'], 6*SECOND)
            self.assertEqual(value['shader_deserialization']['union_duration_ns'], 7*SECOND)
            self.assertEqual(value['shader_compile_calls']['count'], 0)

    def test_capture_mode_difference_rejects_otherwise_matched_runs(self):
        left, right = self.matched(), self.matched()
        left['provenance']['capture_mode'] = 'foreground_screen'
        right['provenance']['capture_mode'] = 'pid_window'
        result = analysis.compare_runs(left, right)
        self.assertFalse(result['comparable'])
        self.assertIn('capture_mode', result['unexpected_differences'])

    def test_focus_policy_difference_and_missing_policy_reject_comparison(self):
        left, right = self.matched(), self.matched()
        left['provenance']['focus_policy'] = 'foreground_required'
        right['provenance']['focus_policy'] = 'background_allowed'
        result = analysis.compare_runs(left, right)
        self.assertFalse(result['comparable'])
        self.assertIn('focus_policy', result['unexpected_differences'])
        del right['provenance']['focus_policy']
        result = analysis.compare_runs(left, right)
        self.assertFalse(result['comparable'])
        self.assertIn('focus_policy', result['missing_fields'])

    def test_unknown_variable_rejected(self):
        with self.assertRaises(ValueError):
            analysis.compare_runs({}, {}, 'anything')

    def test_cinematic_and_pause_do_not_become_menu(self):
        for text in ('NEW GAME', 'CONTINUE GAME', 'PAUSED CONTINUE GAME BACK', 'LOADING CONTINUE GAME SELECT'):
            self.assertFalse(analysis.menu_observation(text))
        self.assertTrue(analysis.menu_observation('Continue Game Select'))


if __name__ == '__main__':
    unittest.main()
