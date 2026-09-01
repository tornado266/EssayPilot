import ast
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.cloud_store import CloudStoreError, CloudUser, SupabaseStore


ROOT = Path(__file__).resolve().parents[1]


def load_app_function(name, namespace=None):
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    loaded = dict(namespace or {})
    exec(compile(module, str(ROOT / "app.py"), "exec"), loaded)
    return loaded[name]


class DraftTwoSessionTests(unittest.TestCase):
    def test_cache_identity_changes_for_user_parent_or_text(self):
        cache_key = load_app_function("draft_2_cache_key")
        original = cache_key("user-a", "run-a", "hash-a")

        self.assertEqual(original, ("user-a", "run-a", "hash-a"))
        self.assertNotEqual(original, cache_key("user-b", "run-a", "hash-a"))
        self.assertNotEqual(original, cache_key("user-a", "run-b", "hash-a"))
        self.assertNotEqual(original, cache_key("user-a", "run-a", "hash-b"))

    def test_settlement_retry_reuses_persisted_ids(self):
        class Store:
            def __init__(self):
                self.save_calls = 0

            def save_second_draft_result(self, user, **kwargs):
                self.save_calls += 1
                return {
                    "essay_id": "essay-2",
                    "grading_run_id": "run-2",
                    "draft_revision_id": "revision-2",
                }

        store = Store()
        completion_calls = []

        def complete(store_arg, user_arg, ticket, *, revised_grading_run_id):
            completion_calls.append((ticket["flow_id"], revised_grading_run_id))
            if len(completion_calls) == 1:
                raise CloudStoreError("uncertain completion")

        persist = load_app_function(
            "persist_draft_2_cloud_result",
            {
                "SupabaseStore": object,
                "CloudUser": object,
                "CloudStoreError": CloudStoreError,
                "count_words": lambda value: len(value.split()),
                "submission_hash": lambda topic, text: "a" * 64,
                "complete_second_draft": complete,
                "clear_membership_cache": lambda: None,
            },
        )
        cache = {"access_ticket": {"flow_id": "flow-1"}}
        kwargs = {
            "draft_1": {"grading_run_id": "run-1", "topic": "topic"},
            "draft_2_text": "revised essay",
            "draft_2_package": {"structured": {}},
            "draft_2_scores": {"Overall Band": 7.0},
            "progress_report": "progress",
            "cached_generation": cache,
        }

        with self.assertRaisesRegex(CloudStoreError, "uncertain"):
            persist(store, object(), **kwargs)

        self.assertEqual(store.save_calls, 1)
        self.assertEqual(cache["cloud_ids"]["grading_run_id"], "run-2")

        result = persist(store, object(), **kwargs)

        self.assertEqual(store.save_calls, 1)
        self.assertEqual(len(completion_calls), 2)
        self.assertEqual(result["draft_revision_id"], "revision-2")
        self.assertTrue(cache["settled"])
        self.assertNotIn("access_ticket", cache)

    def test_persisted_revision_hydrates_a_displayable_result(self):
        normalize = load_app_function(
            "draft_2_result_from_revision",
            {"score_snapshot": lambda value: {"Overall Band": value["overall_band"]}},
        )
        result = normalize(
            {
                "id": "revision-2",
                "grading_run_id": "run-1",
                "progress_report": "progress",
                "revised_run": {
                    "id": "run-2",
                    "report_json": {"overall_band": 7.0},
                    "report_markdown": "report",
                    "essays": {"content": "second draft"},
                },
            },
            user_id="user-a",
            grading_run_id="run-1",
        )

        self.assertEqual(result["text"], "second draft")
        self.assertEqual(result["grading_run_id"], "run-2")
        self.assertEqual(result["parent_grading_run_id"], "run-1")
        self.assertFalse(result["settlement_pending"])

    def test_render_flow_is_scoped_atomic_and_read_only_aware(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        flow = source.split("def render_draft_2_training", 1)[1].split(
            "def extract_criteria_details", 1
        )[0]

        self.assertIn("read_only: bool = False", flow)
        self.assertIn("disabled=read_only or isinstance(current_draft_2_result, dict)", flow)
        self.assertIn("draft_2_cache.setdefault(scoped_cache_key", flow)
        self.assertIn("persist_draft_2_cloud_result(", flow)
        self.assertNotIn("save_linked_grading_cycle(", flow)
        self.assertNotIn("save_draft_revision(", flow)
        self.assertNotIn("draft_2_cache[draft_2_fingerprint]", flow)


class DraftTwoCloudStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = SupabaseStore()
        self.store.url = "https://example.supabase.co"
        self.store.anon_key = "anon-key"
        self.user = CloudUser("user-a", "a@example.com", "access-token")

    @staticmethod
    def response(data):
        response = Mock(status_code=200, content=b"{}")
        response.json.return_value = data
        return response

    @patch("src.cloud_store.requests.request")
    def test_atomic_save_uses_one_rpc(self, request):
        request.return_value = self.response(
            {
                "essay_id": "essay-2",
                "grading_run_id": "run-2",
                "draft_revision_id": "revision-2",
            }
        )
        package = {
            "structured": {"overall_band": 7.0, "criteria": []},
            "report": "report",
            "model": "model",
            "prompt_version": "prompt",
            "skill_version": "skill",
        }

        result = self.store.save_second_draft_result(
            self.user,
            grading_run_id="run-1",
            flow_id="flow-1",
            question="topic",
            content="second draft",
            word_count=2,
            content_hash="a" * 64,
            package=package,
            scores={"Overall Band": 7.0},
            progress_report="progress",
        )

        self.assertEqual(result["draft_revision_id"], "revision-2")
        self.assertEqual(request.call_count, 1)
        call = request.call_args
        self.assertTrue(call.args[1].endswith("/rpc/save_second_draft_result"))
        self.assertEqual(call.kwargs["json"]["p_grading_run_id"], "run-1")
        self.assertEqual(call.kwargs["json"]["p_flow_id"], "flow-1")

    @patch("src.cloud_store.requests.request")
    def test_single_revision_lookup_is_parent_scoped(self, request):
        request.return_value = self.response([{"id": "revision-2"}])

        result = self.store.get_draft_revision(self.user, "run-1")

        self.assertEqual(result["id"], "revision-2")
        params = request.call_args.kwargs["params"]
        self.assertEqual(params["grading_run_id"], "eq.run-1")
        self.assertEqual(params["draft_number"], "eq.2")
        self.assertEqual(params["limit"], "1")


class DraftTwoMigrationTests(unittest.TestCase):
    def test_migration_enforces_keys_and_atomic_writes(self):
        migration = (
            ROOT
            / "supabase"
            / "migrations"
            / "20260901130000_second_draft_idempotency.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("grading_runs_user_idempotency_key_idx", migration)
        self.assertIn("draft_revisions_user_idempotency_key_idx", migration)
        self.assertIn("function public.save_second_draft_result", migration)
        self.assertIn("for update", migration.lower())
        self.assertLess(
            migration.index("insert into public.grading_runs"),
            migration.index("insert into public.draft_revisions"),
        )
        self.assertIn("to authenticated", migration)

    def test_fresh_schema_contains_the_same_atomic_contract(self):
        schema = (ROOT / "supabase" / "schema.sql").read_text(encoding="utf-8")
        function = schema.split(
            "create or replace function public.save_second_draft_result", 1
        )[1].split("grant select, insert, update, delete", 1)[0]

        self.assertIn("p_flow_id uuid", function)
        self.assertIn("membership_second_draft_actions", function)
        self.assertIn("Matching second draft reservation required", function)
        self.assertIn("set search_path = pg_catalog, public", function)
        self.assertIn("insert into public.grading_runs", function)
        self.assertIn("insert into public.draft_revisions", function)
        self.assertLess(
            function.index("insert into public.grading_runs"),
            function.index("insert into public.draft_revisions"),
        )


if __name__ == "__main__":
    unittest.main()
