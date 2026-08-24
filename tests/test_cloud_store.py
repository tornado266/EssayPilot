import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.cloud_store import CloudSessionExpiredError, CloudStoreError, CloudUser, SupabaseStore


class CloudStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = SupabaseStore()
        self.store.url = "https://example.supabase.co"
        self.store.anon_key = "public-anon-key"
        self.store.service_role_key = ""
        self.store.secret_key = ""
        self.store.beta_start_at = ""

    @patch("src.cloud_store.requests.request")
    def test_email_verification_keeps_supabase_expiry_fields(self, request):
        response = Mock(status_code=200, content=b"{}")
        response.json.return_value = {
            "user": {"id": "user-a", "email": "a@example.com"},
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": 2_000_003_600,
            "expires_in": 3600,
        }
        request.return_value = response

        user = self.store.verify_email_code("a@example.com", "123456")

        self.assertEqual(user.expires_at, 2_000_003_600)
        self.assertEqual(user.expires_in, 3600)

    @patch("src.cloud_store.requests.request")
    def test_refresh_rotates_token_without_putting_it_in_url_params(self, request):
        response = Mock(status_code=200, content=b"{}")
        response.json.return_value = {
            "user": {"id": "user-a", "email": "a@example.com"},
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "expires_at": 2_000_003_600,
            "expires_in": 3600,
        }
        request.return_value = response

        user = self.store.refresh("refresh-old")

        self.assertEqual(user.refresh_token, "refresh-new")
        self.assertEqual(request.call_args.kwargs["params"], {"grant_type": "refresh_token"})
        self.assertNotIn("refresh-old", str(request.call_args.kwargs["params"]))
        self.assertEqual(request.call_args.kwargs["json"], {"refresh_token": "refresh-old"})

    @patch("src.cloud_store.requests.request")
    def test_invalid_refresh_is_distinguished_from_temporary_failure(self, request):
        response = Mock(status_code=400, content=b"{}")
        response.json.return_value = {"message": "Invalid Refresh Token"}
        request.return_value = response

        with self.assertRaises(CloudSessionExpiredError):
            self.store.refresh("invalid-secret")

    @patch("src.cloud_store.requests.request")
    def test_user_token_is_used_for_row_level_security(self, request):
        response = Mock(status_code=200, content=b"[]")
        response.json.return_value = []
        request.return_value = response
        user = CloudUser("user-a", "a@example.com", "user-access-token")
        self.store.list_grading_runs(user)
        headers = request.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer user-access-token")
        self.assertEqual(headers["apikey"], "public-anon-key")

    @patch("src.cloud_store.requests.request")
    def test_history_is_owner_scoped_paginated_and_newest_first(self, request):
        response = Mock(status_code=200, content=b"[]")
        response.json.return_value = []
        request.return_value = response
        user = CloudUser("user-a", "a@example.com", "user-access-token")
        self.store.list_grading_runs(user, limit=10, offset=20)
        params = request.call_args.kwargs["params"]
        self.assertEqual(params["limit"], "10")
        self.assertEqual(params["offset"], "20")
        self.assertEqual(params["order"], "created_at.desc")
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer user-access-token")

    @patch("src.cloud_store.requests.request")
    def test_linked_second_draft_reuses_run_and_parent_without_extra_report_copy(self, request):
        response = Mock(status_code=200, content=b'{"essay_id":"essay-2","grading_run_id":"run-2"}')
        response.json.return_value = {"essay_id": "essay-2", "grading_run_id": "run-2"}
        request.return_value = response
        user = CloudUser("user-a", "a@example.com", "user-access-token")
        package = {
            "structured": {"overall_band": 7.0, "criteria": []},
            "report": "report", "model": "model", "prompt_version": "prompt", "skill_version": "skill",
        }
        result = self.store.save_linked_grading_cycle(
            user, question="q", essay="draft two", word_count=2, package=package,
            content_hash="hash", parent_run_id="run-1",
        )
        self.assertEqual(result["grading_run_id"], "run-2")
        payload = request.call_args.kwargs["json"]
        self.assertEqual(payload["p_parent_run_id"], "run-1")
        self.assertEqual(payload["p_draft_role"], "second")

    @patch("src.cloud_store.requests.request")
    def test_chinese_cache_is_scoped_to_prompt_version(self, request):
        response = Mock(status_code=200, content=b"[]")
        response.json.return_value = []
        request.return_value = response
        user = CloudUser("user-a", "a@example.com", "user-access-token")

        self.store.find_cached_grading(user, "content-hash", "task2-structured-zh-2026-08-09")

        params = request.call_args.kwargs["params"]
        self.assertEqual(params["essays.content_hash"], "eq.content-hash")
        self.assertEqual(params["prompt_version"], "eq.task2-structured-zh-2026-08-09")

    @patch("src.cloud_store.requests.request")
    def test_locked_scoring_cache_is_independent_of_feedback_version(self, request):
        response = Mock(status_code=200, content=b"[]")
        response.json.return_value = []
        request.return_value = response
        user = CloudUser("user-a", "a@example.com", "user-access-token")

        self.store.find_cached_scoring(user, "content-hash", "score-v10")

        params = request.call_args.kwargs["params"]
        self.assertEqual(params["essays.content_hash"], "eq.content-hash")
        self.assertEqual(params["report_json->>scoring_prompt_version"], "eq.score-v10")
        self.assertNotIn("feedback_prompt_version", str(params))

    def test_schema_enables_rls_for_every_private_table(self):
        schema = (Path(__file__).parents[1] / "supabase" / "schema.sql").read_text(encoding="utf-8")
        for table in ("essays", "grading_runs", "practice_attempts", "draft_revisions", "learning_items", "expression_attempts"):
            self.assertIn(f"alter table public.{table} enable row level security", schema)
        self.assertIn('create policy "owners manage learning items"', schema)
        self.assertGreaterEqual(schema.count("auth.uid() = user_id"), 4)

    def test_correction_history_migration_is_incremental_and_linked(self):
        migration = (Path(__file__).parents[1] / "supabase" / "migrations" / "20260816_correction_history.sql").read_text(encoding="utf-8")
        self.assertIn("add column if not exists draft_role", migration)
        self.assertIn("add column if not exists parent_run_id", migration)
        self.assertIn("add column if not exists revised_grading_run_id", migration)
        self.assertIn("save_linked_grading_cycle", migration)
        self.assertIn("auth.uid()", migration)

    def test_grading_cycle_is_transactional_rpc(self):
        schema = (Path(__file__).parents[1] / "supabase" / "schema.sql").read_text(encoding="utf-8")
        self.assertIn("function public.save_grading_cycle", schema)
        self.assertIn("insert into essays", schema)
        self.assertIn("insert into grading_runs", schema)
        self.assertIn("g.prompt_version = p_prompt_version", schema)

    @patch("src.cloud_store.requests.request")
    def test_beta_funnel_uses_server_only_service_role(self, request):
        response = Mock(status_code=200, content=b'{}')
        response.json.return_value = {}
        request.return_value = response
        self.store.service_role_key = "private-service-role-key"
        self.store.beta_start_at = "2026-08-09T12:00:00+08:00"

        self.store.get_beta_funnel()

        headers = request.call_args.kwargs["headers"]
        payload = request.call_args.kwargs["json"]
        self.assertEqual(headers["Authorization"], "Bearer private-service-role-key")
        self.assertEqual(headers["apikey"], "private-service-role-key")
        self.assertEqual(payload["p_since"], self.store.beta_start_at)

    def test_beta_funnel_is_disabled_without_private_configuration(self):
        with self.assertRaises(CloudStoreError):
            self.store.get_beta_funnel()

    @patch("src.cloud_store.requests.request")
    def test_current_secret_key_uses_apikey_without_invalid_bearer(self, request):
        response = Mock(status_code=200, content=b'{}')
        response.json.return_value = {}
        request.return_value = response
        self.store.secret_key = "sb_secret_product_analytics"
        self.store.beta_start_at = "2026-08-09T12:00:00+08:00"

        self.store.get_beta_funnel()

        headers = request.call_args.kwargs["headers"]
        self.assertEqual(headers["apikey"], self.store.secret_key)
        self.assertNotIn("Authorization", headers)

    def test_beta_funnel_rpc_is_aggregate_only_and_service_role_restricted(self):
        schema = (Path(__file__).parents[1] / "supabase" / "schema.sql").read_text(encoding="utf-8")
        self.assertIn("function public.get_beta_funnel", schema)
        self.assertIn("select distinct on (g.user_id)", schema)
        self.assertIn("p.status = 'mastered'", schema)
        self.assertIn(
            "revoke all on function public.get_beta_funnel(timestamptz) from public, anon, authenticated",
            schema,
        )
        self.assertIn(
            "grant execute on function public.get_beta_funnel(timestamptz) to service_role",
            schema,
        )


if __name__ == "__main__":
    unittest.main()
