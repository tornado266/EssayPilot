"""Keep entry pages independent of full essay/revision downloads."""

import ast
import html
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock

from src.cloud_store import CloudStoreError, CloudUser, SupabaseStore


ROOT = Path(__file__).resolve().parents[1]


class EntryLoadingTests(unittest.TestCase):
    def setUp(self):
        self.st = MagicMock()
        self.st.session_state = {}
        self.st.query_params = {"run_id": "run-bookmarked"}
        self.st.columns.return_value = [MagicMock(), MagicMock(), MagicMock()]
        self.store = Mock(spec=SupabaseStore)
        self.store.enabled = False
        self.user = CloudUser("user-a", "a@example.com", "token")
        self.hydrate = Mock()
        self.namespace = {
            "st": self.st, "html": html, "SupabaseStore": SupabaseStore,
            "CloudUser": CloudUser, "CloudStoreError": CloudStoreError,
            "hydrate_grading_run": self.hydrate,
            "APP_ROUTES": {"home": "学习首页", "write": "写作批改",
                           "report": "批改报告", "training": "专项训练", "growth": "学习档案"},
            "PRODUCTION_MODEL": "test-model", "navigate": Mock(),
            "open_purchase_offer": Mock(), "logout_cloud_user": Mock(),
        }
        names = {"ensure_run_context", "render_app_navigation"}
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        functions = [node for node in tree.body
                     if isinstance(node, ast.FunctionDef) and node.name in names]
        exec(compile(ast.Module(body=functions, type_ignores=[]), "app.py", "exec"), self.namespace)

    def test_bookmarked_home_and_archive_do_not_download_essay_or_revision(self):
        for route in ("home", "growth"):
            with self.subTest(route=route):
                self.st.session_state["page_mode"] = route
                self.namespace["ensure_run_context"](self.store, self.user)
        self.store.get_grading_run.assert_not_called()
        self.store.get_draft_revision.assert_not_called()
        self.hydrate.assert_not_called()
        self.assertEqual(self.st.query_params["run_id"], "run-bookmarked")

    def test_detail_routes_still_restore_the_requested_essay_and_second_draft(self):
        run = {"id": "run-bookmarked", "report_json": {"overall_band": 7}}
        revision = {"grading_run_id": "run-bookmarked", "content": "Saved second draft"}
        self.store.get_grading_run.return_value = run
        self.store.get_draft_revision.return_value = revision
        for route in ("write", "report", "training"):
            with self.subTest(route=route):
                self.st.session_state["page_mode"] = route
                self.namespace["ensure_run_context"](self.store, self.user)
                self.store.get_grading_run.assert_called_with(self.user, "run-bookmarked")
                self.store.get_draft_revision.assert_called_with(self.user, "run-bookmarked")
                self.hydrate.assert_called_with(run, user_id=self.user.id, draft_revision=revision)

    def test_current_run_and_guest_still_skip_cloud_reads(self):
        self.st.session_state.update(page_mode="training", active_run_id="run-bookmarked")
        self.namespace["ensure_run_context"](self.store, self.user)
        self.st.session_state.pop("active_run_id")
        self.namespace["ensure_run_context"](self.store, None)
        self.store.get_grading_run.assert_not_called()

    def test_mobile_navigation_retains_the_unloaded_bookmarked_run(self):
        self.st.session_state.update(page_mode="home", active_run_id="run-previous")
        self.namespace["render_app_navigation"](self.user, store=self.store)
        rendered = "\n".join(str(call.args[0]) for call in self.st.markdown.call_args_list)
        self.assertIn("?page=report&run_id=run-bookmarked", rendered)
        self.assertIn("?page=training&run_id=run-bookmarked", rendered)
        self.assertNotIn("run-previous", rendered)


if __name__ == "__main__":
    unittest.main()
