"""Original-site navigation retains sessions and avoids redundant asset writes."""
import ast
import time
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.cloud_store import CloudStoreError, CloudUser, SupabaseStore
from src.internal_navigation import parse_navigation
from tests.test_home_snapshot_cache import HomeSnapshotAppTests, SessionState

ROOT = Path(__file__).resolve().parents[1]


class NavigationTests(HomeSnapshotAppTests):
    def test_link_to_another_report_loads_the_requested_record(self):
        state = SessionState(page_mode='home', active_run_id='run-old')
        st = SimpleNamespace(session_state=state, query_params={})
        store = Mock(spec=SupabaseStore)
        user = CloudUser('user-a', 'test@example.test', 'test-token')
        run = {'id': 'run-new'}
        store.get_grading_run.return_value = run
        store.get_draft_revision.return_value = None
        hydrate = Mock()
        env = dict(st=st, SupabaseStore=SupabaseStore, CloudUser=CloudUser,
                   CloudStoreError=CloudStoreError, APP_ROUTES={'home', 'report'},
                   hydrate_grading_run=hydrate)
        tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name in {'navigate', 'ensure_run_context'}]
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'app.py', 'exec'), env)
        env['navigate']('report', 'run-new')
        self.assertEqual(state['active_run_id'], 'run-old')
        env['ensure_run_context'](store, user)
        store.get_grading_run.assert_called_once_with(user, 'run-new')
        hydrate.assert_called_once_with(run, user_id=user.id, draft_revision=None)

    def test_html_link_uses_existing_session_and_keeps_draft_and_home_cache(self):
        app, load = self.signed_in_app(visitor_values=[''] * 6)
        app.run()
        session_id = app.session_state['flow_id']
        with patch('src.internal_navigation.internal_navigation_event', return_value=('write', '', 'topics')):
            app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.query_params['mode'], ['topics'])
        app.text_area(key='essay_input').set_value('An unfinished essay.')
        app.button(key='mobile_nav_home').click().run()
        with patch('src.internal_navigation.internal_navigation_event', return_value=('write', '', '')):
            app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state['flow_id'], session_id)
        self.assertEqual(app.text_area(key='essay_input').value, 'An unfinished essay.')
        self.assertEqual(load.call_count, 1)

    def test_only_internal_known_destinations_are_accepted(self):
        self.assertEqual(parse_navigation('?page=training&run_id=run-a&mode=draft'), ('training', 'run-a', 'draft'))
        for value in ('https://outside.test/?page=write', '?page=admin', '?page=home&admin=1',
                      '?page=write&page=home', '?page=write#script', None):
            self.assertIsNone(parse_navigation(value))


class LearningAssetSyncTests(unittest.TestCase):
    def setUp(self):
        self.state = SessionState(latest_structured={'value': 'first'},
                                  latest_cloud_ids={'grading_run_id': 'run-a'})
        self.store = Mock(spec=SupabaseStore)
        self.user = CloudUser('user-a', 'test@example.test', 'test-token')
        tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'ensure_learning_assets')
        env = dict(st=SimpleNamespace(session_state=self.state), SupabaseStore=SupabaseStore,
                   CloudUser=CloudUser, CloudStoreError=CloudStoreError, deepcopy=deepcopy,
                   build_learning_items=lambda report, **kw: [{'value': report['value']}])
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'app.py', 'exec'), env)
        self.sync = env['ensure_learning_assets']

    def test_revisit_skips_successful_batch_but_changed_report_or_account_does_not(self):
        self.sync(self.store, self.user)
        self.sync(self.store, self.user)
        self.store.upsert_learning_items.assert_called_once()
        self.state['latest_structured']['value'] = 'changed'
        self.sync(self.store, self.user)
        self.sync(self.store, CloudUser('user-b', 'b@example.test', 'other-token'))
        self.assertEqual(self.store.upsert_learning_items.call_count, 3)

    def test_failed_sync_retries_and_does_not_claim_success(self):
        self.store.upsert_learning_items.side_effect = [CloudStoreError('offline'), None]
        self.sync(self.store, self.user)
        self.assertFalse(self.state['learning_assets_ready'])
        self.assertNotIn('learning_assets_synced_batch', self.state)
        self.sync(self.store, self.user)
        self.assertTrue(self.state['learning_assets_ready'])
        self.assertEqual(self.store.upsert_learning_items.call_count, 2)

    def test_revisit_does_not_wait_for_slow_cloud_write(self):
        self.store.upsert_learning_items.side_effect = lambda *args: time.sleep(.15)
        first = time.perf_counter()
        self.sync(self.store, self.user)
        first = time.perf_counter() - first
        revisit = time.perf_counter()
        self.sync(self.store, self.user)
        revisit = time.perf_counter() - revisit
        self.assertGreaterEqual(first, .15)
        self.assertLess(revisit, .05)


if __name__ == '__main__':
    unittest.main()
