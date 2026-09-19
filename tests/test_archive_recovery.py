"""Exercise failure isolation, lazy archive reads and safe run navigation."""
import ast
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest
from src.cloud_store import CloudStoreError, CloudUser, SupabaseStore
from test_membership_grading_flow import AttrDict, valid_package
import test_membership_grading_flow as grading_fixtures
from test_second_draft_loader import HARNESS as DRAFT_HARNESS, FUNCTION as DRAFT_FUNCTION

SOURCE = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
FUNCTIONS = {n.name: ast.get_source_segment(SOURCE, n) for n in ast.parse(SOURCE).body
             if isinstance(n, ast.FunctionDef)}


def load(name, namespace):
    exec("from __future__ import annotations\n" + FUNCTIONS[name], namespace)
    return namespace[name]


class RunRecoveryTests(unittest.TestCase):
    def test_failed_or_missing_requested_run_stops_before_stale_training(self):
        class Stopped(Exception):
            pass
        for result in (CloudStoreError("offline"), None):
            with self.subTest(result=result):
                state = AttrDict(page_mode="training", active_run_id="A", latest_structured={"overall_band": 6})
                st = Mock(session_state=state, query_params={"run_id": "B"})
                st.button.return_value = False
                st.stop.side_effect = Stopped
                store = Mock()
                if isinstance(result, Exception):
                    store.get_grading_run.side_effect = result
                else:
                    store.get_grading_run.return_value = result
                hydrate = Mock()
                fn = load("ensure_run_context", dict(st=st, CloudStoreError=CloudStoreError,
                                                     hydrate_grading_run=hydrate, navigate=Mock()))
                with self.assertRaises(Stopped):
                    fn(store, SimpleNamespace(id="user"))
                hydrate.assert_not_called()
                store.get_draft_revision.assert_not_called()

    def test_legacy_report_cannot_inherit_previous_draft_baseline(self):
        state = AttrDict(draft_1_snapshot={"grading_run_id": "A"}, draft_2_text="A revision")
        fn = load("hydrate_grading_run", dict(st=SimpleNamespace(session_state=state)))
        fn({"id": "B", "report_markdown": "legacy", "report_json": {}}, user_id="user")
        self.assertEqual(state.active_run_id, "B")
        self.assertNotIn("draft_1_snapshot", state)
        self.assertNotIn("draft_2_text", state)

    def test_local_backup_failure_still_saves_and_settles_first_report(self):
        for writer in ("save_markdown_record", "append_error_book"):
            with self.subTest(writer=writer):
                grader = Mock(return_value=valid_package())
                fn, st, calls = grading_fixtures.MembershipGradingFlowTests().build(grader)
                st.warning = Mock()
                fn.__globals__[writer] = Mock(side_effect=OSError("disk full"))
                store = Mock()
                store.find_cached_grading.return_value = None
                store.find_cached_scoring.return_value = None
                store.save_grading_cycle.return_value = {"grading_run_id": "saved", "essay_id": "essay"}
                complete = Mock()
                fn(store, SimpleNamespace(id="user"), topic="topic", essay="new draft",
                   reserve_model_access=lambda _: {"kind": "membership", "flow_id": "flow"},
                   complete_model_access=complete)
                store.save_grading_cycle.assert_called_once()
                self.assertEqual(complete.call_args.args[1], "saved")
                self.assertEqual(calls["navigate"], 1)
                grader.assert_called_once()

    def test_local_backup_failure_does_not_release_second_draft_reservation(self):
        harness = DRAFT_HARNESS + '''
from types import SimpleNamespace
from src.cloud_store import CloudStoreError
store = Mock()
user = SimpleNamespace(id='test-user')
record_usage_event = Mock()
render_product_feedback = Mock()
reserve_second_draft = Mock(return_value={'flow_id': 'flow'})
release_second_draft = Mock()
save_markdown_record.side_effect = OSError('disk full')
def persist_draft_2_cloud_result(*args, **kwargs):
    st.session_state.cloud_saved = True
    return {'grading_run_id': 'saved-second'}
'''
        script = harness + '\n' + DRAFT_FUNCTION + '''
render_draft_2_training(provider='test', model='test', task_type='Task 2',
                       user_id='test-user', cloud_store=store, cloud_user=user)
st.session_state.released = release_second_draft.call_count
'''
        app = AppTest.from_string(script).run()
        app.text_area(key="draft_2_text").set_value("Revised draft with changes.")
        app.button(key="submit_draft_2").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(app.session_state.cloud_saved)
        self.assertEqual(app.session_state.released, 0)
        self.assertFalse(app.session_state.draft_2_result["settlement_pending"])


ARCHIVE_HARNESS = '''
from __future__ import annotations
import html
from collections import Counter
import streamlit as st
from src.cloud_store import SupabaseStore, CloudStoreError, CloudUser
from src.report_schema import format_overall_band
store = SupabaseStore()
user = CloudUser('user', 'test@example.com', 'test-token')
CATEGORY_LABELS = {}
record_usage_event = lambda *args, **kwargs: None
render_score_trend = lambda runs: None
_criterion_history_scores = lambda run: {k: '-' for k in ['TR','CC','LR','GRA']}
def render_expression_library(store, user, expressions, **kwargs):
    st.write('Expression view')
def navigate(route, run_id=''):
    st.session_state.destination = (route, run_id)
'''


class ArchiveLoadingTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("src.cloud_store.requests.request", side_effect=AssertionError("No network")))
        self.runs = stack.enter_context(patch.object(SupabaseStore, "list_grading_runs", return_value=[
            {"id": "run-1", "overall_band": 7, "criteria": [], "created_at": "2026-09-19",
             "essays": {"question": "Test question", "word_count": 270}}]))
        self.revisions = stack.enter_context(patch.object(SupabaseStore, "list_draft_revisions", return_value=[]))
        self.items = stack.enter_context(patch.object(SupabaseStore, "list_learning_items", return_value=[]))
        self.count = stack.enter_context(patch.object(SupabaseStore, "count_grading_runs", return_value=37))
        self.detail = stack.enter_context(patch.object(SupabaseStore, "get_grading_run", return_value={
            "id": "run-1", "essays": {"content": "Full original essay"}}))
        self.revision_detail = stack.enter_context(patch.object(SupabaseStore, "get_draft_revision", return_value={
            "progress_report": "Saved comparison"}))
        script = ARCHIVE_HARNESS + '\n' + FUNCTIONS['render_correction_history'] + '\n' + FUNCTIONS['render_growth_page']
        self.app = AppTest.from_string(script + '\nrender_growth_page(store, user)')

    def run_app(self, tab=None):
        if tab:
            self.app.session_state['growth_sections'] = tab
        self.app.run()
        self.assertEqual(len(self.app.exception), 0)

    def test_history_survives_revision_failure_and_uses_exact_total(self):
        self.revisions.side_effect = CloudStoreError("offline")
        self.run_app()
        self.assertEqual(self.app.metric[0].value, "37")
        self.app.button(key="history_report_run-1")
        self.items.assert_not_called()
        self.detail.assert_not_called()
        self.runs.assert_called_once_with(unittest.mock.ANY, limit=11, summary=True)
        self.assertTrue(any("二稿关联" in w.value for w in self.app.warning))

    def test_count_failure_does_not_fabricate_total_or_hide_history(self):
        self.count.side_effect = CloudStoreError("offline")
        self.run_app()
        self.assertEqual(len(self.app.metric), 0)
        self.app.button(key="history_report_run-1")

    def test_each_learning_tab_loads_only_its_own_items(self):
        for tab, kind in (("错题本", "error"), ("表达库", "expression")):
            with self.subTest(tab=tab):
                self.items.reset_mock()
                self.run_app(tab)
                self.items.assert_called_once_with(unittest.mock.ANY, item_type=kind)
        self.runs.assert_not_called()
        self.revisions.assert_not_called()
        self.count.assert_not_called()

    def test_expression_deep_link_opens_expression_tab(self):
        self.app.query_params['mode'] = 'expressions-from-report'
        self.run_app()
        self.items.assert_called_once_with(unittest.mock.ANY, item_type='expression')
        self.runs.assert_not_called()
        self.run_app()
        self.runs.assert_not_called()

    def test_original_is_fetched_only_when_expanded(self):
        self.run_app()
        self.detail.assert_not_called()
        self.app.session_state['history_original_run-1'] = True
        self.run_app()
        self.detail.assert_called_once_with(unittest.mock.ANY, 'run-1')
        self.assertTrue(any(t.value == 'Full original essay' for t in self.app.text))

    def test_draft_tab_does_not_load_reports_until_expanded(self):
        self.revisions.return_value = [{'id': 'revision-1', 'grading_run_id': 'run-1',
                                       'grading_runs': {'overall_band': 6},
                                       'score_snapshot': {'Overall Band': 7}}]
        self.run_app('二稿记录')
        self.revision_detail.assert_not_called()
        self.runs.assert_not_called()
        self.items.assert_not_called()
        self.app.session_state['revision_revision-1'] = True
        self.run_app('二稿记录')
        self.revision_detail.assert_called_once_with(unittest.mock.ANY, 'run-1')
        self.assertTrue(any(t.value == 'Saved comparison' for t in self.app.markdown))

    def test_visiting_archive_does_not_replace_active_essay(self):
        self.app.session_state['active_run_id'] = 'older-run'
        self.app.session_state['essay_input'] = 'Unsaved work'
        self.run_app()
        self.assertEqual(self.app.session_state['active_run_id'], 'older-run')
        self.assertEqual(self.app.session_state['essay_input'], 'Unsaved work')


class ArchiveQueryTests(unittest.TestCase):
    def setUp(self):
        self.store = SupabaseStore()
        self.store.url = 'https://example.supabase.co'
        self.store.anon_key = 'anon'
        self.user = CloudUser('user', 'test@example.com', 'token')

    @patch('src.cloud_store.requests.request')
    def test_count_uses_authenticated_head_and_content_range(self, request):
        request.return_value = Mock(status_code=200, content=b'', headers={'Content-Range': '0-0/37'})
        self.assertEqual(self.store.count_grading_runs(self.user), 37)
        self.assertEqual(request.call_args.args[0], 'HEAD')
        self.assertEqual(request.call_args.kwargs['headers']['Prefer'], 'count=exact')
        self.assertEqual(request.call_args.kwargs['headers']['Authorization'], 'Bearer token')
        request.return_value.headers = {'Content-Range': '*/*'}
        with self.assertRaises(CloudStoreError):
            self.store.count_grading_runs(self.user)

    def test_summaries_exclude_full_content_even_on_legacy_schema(self):
        for method in (self.store.list_grading_runs, self.store.list_draft_revisions):
            with self.subTest(method=method.__name__):
                requests = []
                def query(*args, **kwargs):
                    requests.append(dict(kwargs['params']))
                    if len(requests) == 1:
                        raise CloudStoreError('column draft_role parent_run_id revised_grading_run_id does not exist', status_code=400)
                    return []
                with patch.object(self.store, '_authenticated_request', side_effect=query):
                    method(self.user, summary=True)
                self.assertEqual(len(requests), 2)
                for params in requests:
                    for field in ('report_json', 'report_markdown', 'content', 'progress_report'):
                        self.assertNotIn(field, params['select'])

    def test_learning_items_filter_preserves_expression_fields(self):
        with patch.object(self.store, '_authenticated_request', return_value=[]) as request:
            self.store.list_learning_items(self.user, item_type='expression')
        params = request.call_args.kwargs['params']
        self.assertEqual(params['item_type'], 'eq.expression')
        for field in ('source_text', 'target_text', 'explanation'):
            self.assertIn(field, params['select'])
