import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from src.cloud_store import CloudUser, SupabaseStore
from src.visitor_identity import visitor_hash


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20260812_feedback_loop_funnel.sql"


class FeedbackLoopProductTests(unittest.TestCase):
    def test_browser_id_is_hashed_before_storage(self):
        raw = str(uuid.uuid4())
        hashed = visitor_hash(raw)
        self.assertEqual(len(hashed), 64)
        self.assertNotIn(raw, hashed)
        self.assertEqual(hashed, visitor_hash(raw))
        self.assertEqual(visitor_hash("not-a-browser-id"), "")

    def test_guest_trial_and_events_are_private_and_minimal(self):
        sql = MIGRATION.read_text(encoding="utf-8").lower()
        self.assertIn("create table if not exists public.guest_trials", sql)
        self.assertIn("create table if not exists public.product_events", sql)
        self.assertIn("unique (visitor_hash, event_name, flow_id)", sql)
        self.assertIn("enable row level security", sql)
        self.assertIn("grant execute on function public.get_product_funnel(timestamptz) to service_role", sql)
        for forbidden_column in ("essay_text", "essay_content", "report_json", "email", "ip_address", "raw_visitor"):
            self.assertNotIn(forbidden_column, sql)

    @patch("src.cloud_store.requests.request")
    def test_guest_reservation_sends_only_hash_and_flow(self, request):
        response = Mock(status_code=200, content=b'{"allowed":true}')
        response.json.return_value = {"allowed": True}
        request.return_value = response
        store = SupabaseStore()
        store.url = "https://example.supabase.co"
        store.anon_key = "anon"
        allowed = store.reserve_guest_trial("a" * 64, str(uuid.uuid4()))
        self.assertTrue(allowed)
        payload = request.call_args.kwargs["json"]
        self.assertEqual(set(payload), {"p_visitor_hash", "p_flow_id"})

    @patch("src.cloud_store.requests.request")
    def test_authenticated_event_uses_user_token_without_personal_content(self, request):
        response = Mock(status_code=200, content=b"true")
        response.json.return_value = True
        request.return_value = response
        store = SupabaseStore()
        store.url = "https://example.supabase.co"
        store.anon_key = "anon"
        user = CloudUser("user-a", "person@example.com", "user-token")
        store.record_product_event("report_viewed", "b" * 64, str(uuid.uuid4()), user=user)
        headers = request.call_args.kwargs["headers"]
        payload = request.call_args.kwargs["json"]
        self.assertEqual(headers["Authorization"], "Bearer user-token")
        self.assertEqual(set(payload), {"p_event_name", "p_visitor_hash", "p_flow_id"})
        self.assertNotIn("person@example.com", str(payload))

    def test_guest_soft_login_and_second_draft_primary_path_are_wired(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("cloud_user is None and not visitor_catalog", source)
        self.assertIn('if requested_page == "login" and cloud_user is None:', source)
        self.assertIn("pending_guest_claim", source)
        self.assertIn("登录并开始第二稿训练", source)
        self.assertIn("开始第二稿训练", source)
        self.assertIn('default_tab = "第二稿验证" if training_mode == "draft"', source)
        self.assertIn('"second_draft_generated"', source)
        self.assertIn('"second_draft_generation_failed"', source)
        self.assertNotIn("record_product_event(", source)

    def test_all_required_events_exist_in_the_server_contract(self):
        sql = MIGRATION.read_text(encoding="utf-8")
        for event in (
            "visitor_opened",
            "login_completed",
            "grading_started",
            "grading_completed",
            "report_viewed",
            "report_training_clicked",
            "second_draft_completed",
        ):
            self.assertIn(event, sql)


if __name__ == "__main__":
    unittest.main()
