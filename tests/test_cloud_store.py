import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.cloud_store import CloudUser, SupabaseStore


class CloudStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = SupabaseStore()
        self.store.url = "https://example.supabase.co"
        self.store.anon_key = "public-anon-key"

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

    def test_schema_enables_rls_for_every_private_table(self):
        schema = (Path(__file__).parents[1] / "supabase" / "schema.sql").read_text(encoding="utf-8")
        for table in ("essays", "grading_runs", "practice_attempts", "draft_revisions"):
            self.assertIn(f"alter table public.{table} enable row level security", schema)
        self.assertGreaterEqual(schema.count("auth.uid() = user_id"), 4)

    def test_grading_cycle_is_transactional_rpc(self):
        schema = (Path(__file__).parents[1] / "supabase" / "schema.sql").read_text(encoding="utf-8")
        self.assertIn("function public.save_grading_cycle", schema)
        self.assertIn("insert into essays", schema)
        self.assertIn("insert into grading_runs", schema)


if __name__ == "__main__":
    unittest.main()
