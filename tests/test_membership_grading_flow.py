import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]


class AttrDict(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class FakeStreamlit:
    def __init__(self):
        self.session_state = AttrDict(user_id="visitor")


class CloudStoreError(RuntimeError):
    pass


class GradingSettlementError(RuntimeError):
    pass


def load_grade_submission(namespace):
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    names = {
        "first_report_actor_key",
        "first_report_cache_key",
        "grade_submission",
    }
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(ROOT / "app.py"), "exec"), namespace)
    return namespace["grade_submission"]


def load_guest_claim(namespace):
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    names = {
        "first_report_actor_key",
        "first_report_cache_key",
        "claim_guest_result",
    }
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(ROOT / "app.py"), "exec"), namespace)
    return namespace["claim_guest_result"]


def valid_package():
    return {
        "report": "complete report",
        "structured": {"overall_band": 6.5},
        "prompt_version": "report-v",
        "skill_version": "skill-v",
        "schema_version": "2.0",
        "graded_at": "2026-09-01T00:00:00Z",
        "model": "test-model",
        "usage": {},
    }


class MembershipGradingFlowTests(unittest.TestCase):
    cache_key = ("guest", "visitor", "f" * 64)

    def build(self, grader):
        st = FakeStreamlit()
        calls = {"navigate": 0, "events": 0}
        namespace = {
            "Callable": Callable,
            "SupabaseStore": object,
            "CloudUser": object,
            "CloudStoreError": CloudStoreError,
            "GradingSettlementError": GradingSettlementError,
            "st": st,
            "REPORT_PROMPT_VERSION": "report-v",
            "SCORING_PROMPT_VERSION": "scoring-v",
            "SCORING_SKILL_VERSION": "skill-v",
            "PRODUCTION_MODEL": "test-model",
            "count_words": lambda essay: len(essay.split()),
            "submission_hash": lambda topic, essay: "f" * 64,
            "score_snapshot": lambda structured: {"Overall Band": structured["overall_band"]},
            "grade_essay_package": grader,
            "save_markdown_record": lambda **kwargs: None,
            "append_error_book": lambda **kwargs: None,
            "record_grading_event": lambda **kwargs: calls.__setitem__("events", calls["events"] + 1),
            "ensure_learning_assets": lambda store, user: None,
            "navigate": lambda *args: calls.__setitem__("navigate", calls["navigate"] + 1),
            "clear_membership_cache": lambda: None,
        }
        return load_grade_submission(namespace), st, calls

    def test_cache_hit_neither_reserves_nor_calls_model(self):
        def should_not_grade(**kwargs):
            raise AssertionError("model should not run")

        grade_submission, st, _ = self.build(should_not_grade)
        st.session_state.grading_cache = {
            self.cache_key: {"package": valid_package(), "cloud_ids": {}}
        }
        reserved = []

        grade_submission(
            object(),
            None,
            topic="topic",
            essay="one two",
            reserve_model_access=lambda fingerprint: reserved.append(fingerprint) or {},
        )

        self.assertEqual(reserved, [])
        self.assertTrue(st.session_state.reused_result_notice)

    def test_model_failure_releases_the_reservation(self):
        def failing_grader(**kwargs):
            raise ValueError("provider failed")

        grade_submission, st, _ = self.build(failing_grader)
        released = []
        with self.assertRaisesRegex(ValueError, "provider failed"):
            grade_submission(
                object(),
                None,
                topic="topic",
                essay="one two",
                reserve_model_access=lambda fingerprint: {
                    "kind": "guest", "flow_id": "flow"
                },
                release_model_access=lambda ticket: released.append(ticket),
            )

        self.assertEqual(released[0]["flow_id"], "flow")
        self.assertEqual(st.session_state.pending_first_report_access, {})

    def test_success_completes_once_after_model_result_is_cached(self):
        grade_submission, st, _ = self.build(lambda **kwargs: valid_package())
        completed = []

        grade_submission(
            object(),
            None,
            topic="topic",
            essay="one two",
            reserve_model_access=lambda fingerprint: {
                "kind": "guest", "flow_id": "flow"
            },
            complete_model_access=lambda ticket, run_id: completed.append((ticket, run_id)),
        )

        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0][0]["flow_id"], "flow")
        self.assertIn(self.cache_key, st.session_state.grading_cache)
        self.assertEqual(st.session_state.pending_first_report_access, {})

    def test_uncertain_completion_retries_settlement_without_second_model_call(self):
        model_calls = []

        def grader(**kwargs):
            model_calls.append(1)
            return valid_package()

        grade_submission, st, _ = self.build(grader)
        ticket = {"kind": "guest", "flow_id": "flow"}
        with self.assertRaises(GradingSettlementError):
            grade_submission(
                object(),
                None,
                topic="topic",
                essay="one two",
                reserve_model_access=lambda fingerprint: ticket,
                complete_model_access=lambda access, run_id: (_ for _ in ()).throw(
                    CloudStoreError("timeout")
                ),
            )

        self.assertEqual(len(model_calls), 1)
        self.assertEqual(
            st.session_state.pending_first_report_access[self.cache_key]["flow_id"],
            "flow",
        )
        completions = []
        grade_submission(
            object(),
            None,
            topic="topic",
            essay="one two",
            reserve_model_access=lambda fingerprint: (_ for _ in ()).throw(
                AssertionError("must reuse the pending reservation")
            ),
            complete_model_access=lambda access, run_id: completions.append(access),
        )

        self.assertEqual(len(model_calls), 1)
        self.assertEqual(completions[0]["flow_id"], "flow")
        self.assertEqual(st.session_state.pending_first_report_access, {})

    def test_other_users_cache_and_cloud_ids_are_not_reused(self):
        model_calls = []
        grade_submission, st, _ = self.build(
            lambda **kwargs: model_calls.append(1) or valid_package()
        )
        st.session_state.grading_cache = {
            ("user", "user-a", "f" * 64): {
                "package": valid_package(),
                "cloud_ids": {"grading_run_id": "run-a"},
                "cloud_user_id": "user-a",
            }
        }

        class Store:
            def find_cached_grading(self, *_args):
                return None

            def find_cached_scoring(self, *_args):
                return None

            def save_grading_cycle(self, user, **_kwargs):
                return {"essay_id": "essay-b", "grading_run_id": f"run-{user.id}"}

        user_b = SimpleNamespace(id="user-b")
        grade_submission(Store(), user_b, topic="topic", essay="one two")

        self.assertEqual(model_calls, [1])
        self.assertEqual(st.session_state.latest_cloud_ids["grading_run_id"], "run-user-b")
        self.assertIn(("user", "user-b", "f" * 64), st.session_state.grading_cache)

    def test_pending_access_is_scoped_to_the_current_guest_actor(self):
        model_calls = []
        grade_submission, st, _ = self.build(
            lambda **kwargs: model_calls.append(1) or valid_package()
        )
        st.session_state.user_id = "visitor-b"
        st.session_state.pending_first_report_access = {
            ("guest", "visitor-a", "f" * 64): {
                "kind": "guest",
                "flow_id": "flow-a",
            }
        }
        reservations = []
        completions = []

        grade_submission(
            object(),
            None,
            topic="topic",
            essay="one two",
            reserve_model_access=lambda _fingerprint: reservations.append(1) or {
                "kind": "guest",
                "flow_id": "flow-b",
            },
            complete_model_access=lambda ticket, _run_id: completions.append(ticket),
        )

        self.assertEqual(model_calls, [1])
        self.assertEqual(reservations, [1])
        self.assertEqual(completions[0]["flow_id"], "flow-b")
        self.assertIn(
            ("guest", "visitor-a", "f" * 64),
            st.session_state.pending_first_report_access,
        )

    def test_guest_claim_moves_cache_to_logged_in_actor(self):
        st = FakeStreamlit()
        fingerprint = "f" * 64
        guest_key = ("guest", "visitor", fingerprint)
        st.session_state.pending_guest_claim = {
            "topic": "topic",
            "essay": "one two",
            "word_count": 2,
            "fingerprint": fingerprint,
            "package": valid_package(),
            "actor_key": ("guest", "visitor"),
        }
        st.session_state.grading_cache = {
            guest_key: {"package": valid_package(), "cloud_ids": {}, "cloud_user_id": ""}
        }
        user = SimpleNamespace(id="user-a")

        class Store:
            def save_grading_cycle(self, *_args, **_kwargs):
                return {"essay_id": "essay-a", "grading_run_id": "run-a"}

        claim = load_guest_claim(
            {
                "CloudUser": object,
                "SupabaseStore": object,
                "CloudStoreError": CloudStoreError,
                "st": st,
                "ensure_learning_assets": lambda *_args: None,
            }
        )
        self.assertTrue(claim(Store(), user))
        self.assertNotIn(guest_key, st.session_state.grading_cache)
        claimed = st.session_state.grading_cache[("user", "user-a", fingerprint)]
        self.assertEqual(claimed["cloud_user_id"], "user-a")
        self.assertEqual(claimed["cloud_ids"]["grading_run_id"], "run-a")
        self.assertNotIn("pending_guest_claim", st.session_state)


if __name__ == "__main__":
    unittest.main()
