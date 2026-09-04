"""Guard the shared per-render training read without contacting cloud services."""

import ast
import hashlib
import re
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock

from streamlit.testing.v1 import AppTest

from src.cloud_store import CloudStoreError, CloudUser, SupabaseStore


ROOT = Path(__file__).resolve().parents[1]


class SessionState(dict):
    def __setattr__(self, key, value):
        self[key] = value


class TrainingPageLoadingTests(unittest.TestCase):
    def setUp(self):
        self.st = MagicMock()
        self.st.session_state = SessionState(
            latest_cloud_ids={"grading_run_id": "run-a"},
            latest_structured={
                "sentence_training": [{"original": "Students needs support.",
                                       "goal": "Keep the subject and verb in agreement.",
                                       "reference": "Students need support."}],
                "logic_training": [{"problem": "Explain why", "original": "Education is useful.",
                                    "task": "Explain how education opens opportunities.",
                                    "requirements": ["Connect the cause to its result."]}],
            },
        )
        self.st.query_params = {}
        self.st.tabs.return_value = [MagicMock(), MagicMock(), MagicMock()]
        self.store = Mock(spec=SupabaseStore)
        self.store.list_pending_practice.return_value = []
        self.store.list_practice_attempts_for_run.return_value = []
        self.user = CloudUser("user-a", "a@example.com", "token")
        self.namespace = {
            "st": self.st, "hashlib": hashlib, "re": re,
            "SupabaseStore": SupabaseStore, "CloudUser": CloudUser,
            "CloudStoreError": CloudStoreError, "PRODUCTION_MODEL": "test-model",
            "render_training_stepper": Mock(),
            "render_training_access_gate": Mock(return_value={"read_only": True}),
            "record_usage_event": Mock(), "render_draft_2_training": Mock(),
        }
        names = {
            "_normalize_practice_original_text", "_match_practice_attempt",
            "find_sentence_reference",
            "render_sentence_practice", "render_logic_practice", "render_training_page",
        }
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
        exec(compile(module, str(ROOT / "app.py"), "exec"), self.namespace)
        self.render = self.namespace["render_training_page"]

    def test_both_tabs_share_one_query_and_restore_feedback(self):
        self.store.list_practice_attempts_for_run.return_value = [
            {"task_kind": "sentence", "task_index": 1, "original_text": "Students needs support.",
             "submitted_text": "Students need support.", "feedback": "Saved sentence feedback"},
            {"task_kind": "logic", "task_index": 1, "original_text": "Education is useful.",
             "submitted_text": "Education opens opportunities.", "feedback": "Saved logic feedback"},
        ]

        self.render(self.store, self.user)

        self.store.list_practice_attempts_for_run.assert_called_once_with(self.user, "run-a")
        rendered = [call.args[0] for call in self.st.markdown.call_args_list]
        self.assertIn("Saved sentence feedback", rendered)
        self.assertIn("Saved logic feedback", rendered)

    def test_empty_result_is_shared_without_retrying_in_child_tabs(self):
        self.render(self.store, self.user)
        self.store.list_practice_attempts_for_run.assert_called_once_with(self.user, "run-a")

    def test_failed_read_warns_once_and_does_not_fan_out_retries(self):
        self.store.list_practice_attempts_for_run.side_effect = CloudStoreError("offline")
        self.render(self.store, self.user)
        self.store.list_practice_attempts_for_run.assert_called_once_with(self.user, "run-a")
        warnings = [call.args[0] for call in self.st.caption.call_args_list]
        self.assertEqual(warnings.count("已保存的训练点评暂时无法读取，请稍后刷新。"), 1)

    def test_next_render_reads_fresh_data_for_current_user_and_run(self):
        self.render(self.store, self.user)
        self.st.session_state["latest_cloud_ids"] = {"grading_run_id": "run-b"}
        other_user = CloudUser("user-b", "b@example.com", "other-token")
        self.render(self.store, other_user)
        self.assertEqual(self.store.list_practice_attempts_for_run.call_count, 2)
        self.store.list_practice_attempts_for_run.assert_called_with(other_user, "run-b")

    def test_no_tasks_do_not_load_attempts(self):
        self.st.session_state["latest_structured"] = {"overall_band": 7}
        self.render(self.store, self.user)
        self.store.list_practice_attempts_for_run.assert_not_called()

    def test_standalone_practice_still_loads_its_own_attempts(self):
        self.namespace["render_sentence_practice"](
            ["Students needs support."], "OpenAI", "test-model",
            cloud_store=self.store, cloud_user=self.user, grading_run_id="run-a", read_only=True,
        )
        self.store.list_practice_attempts_for_run.assert_called_once_with(self.user, "run-a")

    def test_training_displays_its_own_goals_and_completion_checks(self):
        self.render(self.store, self.user)
        rendered = "\n".join(str(call.args[0]) for call in self.st.markdown.call_args_list)
        self.assertIn("Keep the subject and verb in agreement.", rendered)
        self.assertIn("Explain how education opens opportunities.", rendered)
        self.assertIn("Connect the cause to its result.", rendered)
        self.assertNotIn("2-4句话", rendered)
        self.assertNotIn("雅思6.5", rendered)

    def test_sentence_references_are_keyed_by_original_including_queued_task(self):
        self.st.session_state["queued_sentence_training"] = {
            "original": "There is many choices.", "reference": "There are many choices.",
        }
        sentence_renderer = Mock()
        self.namespace["render_sentence_practice"] = sentence_renderer
        self.render(self.store, self.user)
        self.assertEqual(sentence_renderer.call_args.kwargs["references"], {
            "Students needs support.": "Students need support.",
            "There is many choices.": "There are many choices.",
        })
        self.assertEqual(sentence_renderer.call_args.kwargs["goals"], {
            "Students needs support.": "Keep the subject and verb in agreement.",
        })

    def test_apptest_renders_task_details_and_handles_missing_reference(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        names = {
            "_normalize_practice_original_text", "_match_practice_attempt",
            "find_sentence_reference", "render_sentence_practice",
            "render_logic_practice", "render_training_page",
        }
        functions = [ast.get_source_segment(source, node) for node in ast.parse(source).body
                     if isinstance(node, ast.FunctionDef) and node.name in names]
        structured = self.st.session_state["latest_structured"]
        for reference in ("Students need support.", ""):
            with self.subTest(reference=reference):
                structured["sentence_training"][0]["reference"] = reference
                script = "\n".join([
                    "import streamlit as st",
                    "import hashlib, re",
                    "from unittest.mock import Mock",
                    "from src.cloud_store import SupabaseStore, CloudStoreError, CloudUser",
                    "PRODUCTION_MODEL = 'test-model'",
                    "render_training_stepper = lambda **kwargs: None",
                    "render_training_access_gate = lambda *args, **kwargs: {'read_only': False}",
                    "record_usage_event = lambda *args, **kwargs: None",
                    "render_draft_2_training = lambda **kwargs: None",
                    *functions,
                    f"st.session_state.latest_structured = {structured!r}",
                    "st.session_state.latest_cloud_ids = {'grading_run_id': 'run-a'}",
                    "store = Mock(spec=SupabaseStore)",
                    "store.list_pending_practice.return_value = []",
                    "store.list_practice_attempts_for_run.return_value = []",
                    "render_training_page(store, CloudUser('user-a', 'a@example.com', 'token'))",
                ])
                app = AppTest.from_string(script).run()
                self.assertEqual(len(app.exception), 0)
                rendered = "\n".join(item.value for item in app.markdown)
                self.assertIn("Keep the subject and verb in agreement.", rendered)
                self.assertIn("Explain how education opens opportunities.", rendered)
                self.assertIn("Connect the cause to its result.", rendered)
                next(button for button in app.button if button.label == "显示参考答案").click().run()
                self.assertEqual(len(app.exception), 0)
                info = "\n".join(item.value for item in app.info)
                self.assertIn(reference or "暂时没有匹配到参考答案", info)


if __name__ == "__main__":
    unittest.main()
