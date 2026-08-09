import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.cloud_store import CloudStoreError, CloudUser, SupabaseStore


class CloudStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = SupabaseStore()
        self.store.url = "https://example.supabase.co"
        self.store.anon_key = "public-anon-key"
        self.store.service_role_key = ""
        self.store.beta_start_at = ""

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
    def test_chinese_cache_is_scoped_to_prompt_version(self, request):
        response = Mock(status_code=200, content=b"[]")
        response.json.return_value = []
        request.return_value = response
        user = CloudUser("user-a", "a@example.com", "user-access-token")

        self.store.find_cached_grading(user, "content-hash", "task2-structured-zh-2026-08-09")

        params = request.call_args.kwargs["params"]
        self.assertEqual(params["essays.content_hash"], "eq.content-hash")
        self.assertEqual(params["prompt_version"], "eq.task2-structured-zh-2026-08-09")

    def test_schema_enables_rls_for_every_private_table(self):
        schema = (Path(__file__).parents[1] / "supabase" / "schema.sql").read_text(encoding="utf-8")
        for table in ("essays", "grading_runs", "practice_attempts", "draft_revisions", "learning_items", "expression_attempts"):
            self.assertIn(f"alter table public.{table} enable row level security", schema)
        self.assertIn('create policy "owners manage learning items"', schema)
        self.assertGreaterEqual(schema.count("auth.uid() = user_id"), 4)

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
