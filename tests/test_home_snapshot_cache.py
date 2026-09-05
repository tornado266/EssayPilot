"""Keep browser-component reruns from repeating the signed-in home queries."""

import ast
import time
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import urlencode

from streamlit.testing.v1 import AppTest

from src import auth_session
from src.cloud_store import CloudStoreError, CloudUser, SupabaseStore
from src.home_dashboard import build_home_summary


ROOT = Path(__file__).resolve().parents[1]


class SessionState(dict):
    def __setattr__(self, key, value):
        self[key] = value


def snapshot(run_id="run-a", *, pending=False):
    runs = [{"id": run_id, "overall_band": 7, "criteria": [],
             "created_at": "2026-09-04"}]
    tasks = ([{"grading_run_id": run_id, "task_kind": "logic",
               "original_text": "Explain why."}] if pending else [])
    return runs, tasks


class HomeSnapshotCacheTests(unittest.TestCase):
    def setUp(self):
        self.st = MagicMock()
        self.st.session_state = SessionState()
        self.st.query_params = {}
        self.clock = Mock(return_value=100.0)
        self.store = Mock(spec=SupabaseStore)
        self.store.get_home_snapshot.return_value = snapshot()
        self.user = CloudUser("user-a", "a@example.com", "token-a")
        self.card = Mock()
        self.namespace = {
            "st": self.st,
            "time": SimpleNamespace(monotonic=self.clock),
            "uuid": uuid,
            "urlencode": urlencode,
            "CloudUser": CloudUser,
            "SupabaseStore": SupabaseStore,
            "CloudStoreError": CloudStoreError,
            "build_home_summary": build_home_summary,
            "render_home_heading": Mock(),
            "render_home_action_card": self.card,
            "render_home_loading": Mock(),
        }
        self.namespace.update({name: value for name, value in vars(auth_session).items()
                               if name.startswith("AUTH_")})
        names = {"render_learning_dashboard", "clear_account_private_learning_state",
                 "finish_local_logout"}
        assignments = {"_ACCOUNT_PRIVATE_LEARNING_KEYS", "_ACCOUNT_PRIVATE_LEARNING_PREFIXES"}
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        nodes = [node for node in tree.body
                 if (isinstance(node, ast.FunctionDef) and node.name in names)
                 or (isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id in assignments
                             for target in node.targets))]
        module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
        exec(compile(module, str(ROOT / "app.py"), "exec"), self.namespace)
        self.render = self.namespace["render_learning_dashboard"]

    def test_same_account_rerun_reuses_successful_snapshot(self):
        self.assertTrue(self.render(self.store, self.user))
        self.clock.return_value = 114.9
        self.assertTrue(self.render(self.store, self.user))
        self.store.get_home_snapshot.assert_called_once_with(self.user)
        self.assertEqual(self.card.call_count, 2)
        self.assertEqual(self.st.session_state["latest_home_snapshot"]["user_id"], "user-a")

    def test_ttl_expiry_loads_current_pending_task(self):
        self.render(self.store, self.user)
        self.clock.return_value = 115.0
        self.store.get_home_snapshot.return_value = snapshot("run-new", pending=True)
        self.render(self.store, self.user)
        self.assertEqual(self.store.get_home_snapshot.call_count, 2)
        self.assertEqual(self.card.call_args.kwargs["primary_label"], "继续这项训练")
        self.assertIn("run_id=run-new", self.card.call_args.kwargs["primary_href"])

    def test_empty_success_is_cached_too(self):
        self.store.get_home_snapshot.return_value = ([], [])
        self.assertFalse(self.render(self.store, self.user))
        self.assertFalse(self.render(self.store, self.user))
        self.store.get_home_snapshot.assert_called_once_with(self.user)
        self.card.assert_not_called()

    def test_different_account_cannot_reuse_snapshot(self):
        self.render(self.store, self.user)
        other = CloudUser("user-b", "b@example.com", "token-b")
        self.store.get_home_snapshot.return_value = snapshot("run-b", pending=True)
        self.render(self.store, other)
        self.assertEqual([call.args[0].id for call in self.store.get_home_snapshot.call_args_list],
                         ["user-a", "user-b"])
        self.assertEqual(self.st.session_state["latest_home_snapshot"]["user_id"], "user-b")
        self.assertIn("run_id=run-b", self.card.call_args.kwargs["primary_href"])

    def test_failed_snapshot_is_not_cached_and_next_render_retries(self):
        self.store.get_home_snapshot.side_effect = [CloudStoreError("offline"), snapshot()]
        self.render(self.store, self.user)
        self.assertNotIn("latest_home_snapshot", self.st.session_state)
        self.render(self.store, self.user)
        self.assertEqual(self.store.get_home_snapshot.call_count, 2)
        self.assertIn("latest_home_snapshot", self.st.session_state)
        self.st.warning.assert_called_once()

    def test_failed_refresh_never_falls_back_to_expired_pending_task(self):
        self.store.get_home_snapshot.return_value = snapshot("run-stale", pending=True)
        self.render(self.store, self.user)
        self.clock.return_value = 116.0
        self.store.get_home_snapshot.side_effect = [CloudStoreError("offline"), snapshot("run-new")]
        self.render(self.store, self.user)
        self.assertNotIn("latest_home_snapshot", self.st.session_state)
        self.assertEqual(self.card.call_args.kwargs["primary_label"], "从剑雅真题开始")
        self.assertNotIn("run-stale", str(self.card.call_args))
        self.render(self.store, self.user)
        self.assertEqual(self.store.get_home_snapshot.call_count, 3)
        self.assertIn("run-new", str(self.card.call_args))

    def test_logout_clears_home_snapshot_with_other_account_data(self):
        self.render(self.store, self.user)
        self.st.session_state["cloud_user"] = {"id": "user-a"}
        self.st.session_state["page_mode"] = "home"
        self.namespace["finish_local_logout"](reason="user")
        self.assertNotIn("latest_home_snapshot", self.st.session_state)
        self.assertNotIn("cloud_user", self.st.session_state)
        self.assertEqual(self.st.session_state["page_mode"], "home")


class HomeSnapshotAppTests(unittest.TestCase):
    def signed_in_app(self, *, visitor_values):
        now = time.time()
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        app.query_params["page"] = "home"
        app.session_state["cloud_user"] = {
            "id": "user-a", "email": "a@example.com",
            "access_token": "access-test", "refresh_token": "refresh-test",
            "expires_at": int(now + 3600), "expires_in": 3600,
            "expiry_source": "expires_at",
        }
        app.session_state["auth_user_version"] = 10
        browser_value = SimpleNamespace(auth_session={
            "status": "loaded", "refresh_token": "refresh-test",
            "saved_at": now, "version": 10,
        }, auth_wake=0)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("src.auth_session._AUTH_COMPONENT", return_value=browser_value))
        stack.enter_context(patch("src.cloud_store._setting", return_value=""))
        stack.enter_context(patch("src.visitor_identity.browser_visitor_id", side_effect=visitor_values))
        stack.enter_context(patch("src.product_analytics.record_event_safely", return_value=None))
        stack.enter_context(patch("src.cloud_store.requests.request",
                                  side_effect=AssertionError("Unexpected live cloud request")))
        stack.enter_context(patch("src.ai_grader.grade_essay_package",
                                  side_effect=AssertionError("Unexpected model call")))
        stack.enter_context(patch("src.ai_grader.grade_scoring_decision",
                                  side_effect=AssertionError("Unexpected model call")))
        load = stack.enter_context(patch.object(SupabaseStore, "get_home_snapshot", return_value=snapshot()))
        return app, load

    def test_visitor_component_value_rerun_does_not_repeat_home_read(self):
        app, load = self.signed_in_app(visitor_values=["", "00000000-0000-0000-0000-000000000000"])
        app.run()
        self.assertEqual(len(app.exception), 0)
        app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(app.session_state["visitor_hash"])
        self.assertEqual(load.call_count, 1)

    def test_leaving_for_write_then_returning_loads_new_pending_task(self):
        app, load = self.signed_in_app(visitor_values=["", "", ""])
        app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertIn("latest_home_snapshot", app.session_state)
        app.query_params["page"] = "write"
        app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertNotIn("latest_home_snapshot", app.session_state)
        self.assertEqual(load.call_count, 1)
        load.return_value = snapshot("run-new", pending=True)
        app.query_params["page"] = "home"
        app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(load.call_count, 2)
        html = "\n".join(str(item.proto.body) for item in app.get("html"))
        self.assertIn("继续这项训练", html)
        self.assertIn("?page=training&amp;run_id=run-new", html)


if __name__ == "__main__":
    unittest.main()
