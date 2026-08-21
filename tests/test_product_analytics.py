import logging
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from src.admin_dashboard import admin_access_allowed, parse_admin_emails
from src.cloud_store import CloudUser, SupabaseStore
from src.product_analytics import (
    aggregate_event_rows,
    anonymous_user_id,
    build_dedupe_key,
    range_start,
    record_event_safely,
    sanitize_metadata,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20260821_product_analytics.sql"


class ProductAnalyticsTests(unittest.TestCase):
    def test_dedupe_key_is_stable_and_database_enforces_it(self):
        first = build_dedupe_key(
            "report_viewed", "11111111-1111-1111-1111-111111111111",
            run_id="22222222-2222-2222-2222-222222222222",
        )
        repeated = build_dedupe_key(
            "report_viewed", "11111111-1111-1111-1111-111111111111",
            run_id="22222222-2222-2222-2222-222222222222",
        )
        another_session = build_dedupe_key(
            "report_viewed", "33333333-3333-3333-3333-333333333333",
            run_id="22222222-2222-2222-2222-222222222222",
        )
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, another_session)
        sql = MIGRATION.read_text(encoding="utf-8").lower()
        self.assertIn("dedupe_key text not null unique", sql)
        self.assertIn("on conflict (dedupe_key) do nothing", sql)

    def test_analytics_failure_is_logged_and_never_raised(self):
        recorder = Mock(side_effect=RuntimeError("database unavailable"))
        logger = Mock(spec=logging.Logger)
        self.assertFalse(record_event_safely(recorder, logger=logger))
        recorder.assert_called_once_with()
        logger.warning.assert_called_once()

    def test_metadata_and_anonymous_identity_exclude_sensitive_values(self):
        visitor = "a" * 64
        self.assertEqual(anonymous_user_id(visitor), f"anon_{visitor}")
        self.assertEqual(anonymous_user_id("not-a-hash"), "")
        clean = sanitize_metadata({
            "cached": True,
            "source": "archive",
            "email": "person@example.com",
            "essay": "private draft",
        })
        self.assertEqual(clean, {"cached": True, "source": "archive"})

    def test_cloud_event_payload_contains_only_analytics_contract(self):
        store = SupabaseStore()
        store.url = "https://example.supabase.co"
        store.anon_key = "anon"
        store._request = Mock(return_value=True)
        user = CloudUser("11111111-1111-1111-1111-111111111111", "private@example.com", "token")
        store.record_analytics_event(
            "report_viewed",
            "22222222-2222-2222-2222-222222222222",
            "a" * 64,
            user=user,
            run_id="33333333-3333-3333-3333-333333333333",
            metadata={"source": "report"},
            event_id="44444444-4444-4444-4444-444444444444",
        )
        payload = store._request.call_args.kwargs["payload"]
        self.assertEqual(set(payload), {
            "p_event_id", "p_session_id", "p_run_id", "p_event_name",
            "p_metadata_json", "p_dedupe_key", "p_anonymous_user_id",
        })
        self.assertNotIn("private@example.com", str(payload))
        self.assertEqual(store._request.call_args.kwargs["timeout"], 2)

    def test_funnel_time_range_and_mature_retention_cohorts(self):
        now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)

        def event(user, session, name, day, hour=9):
            return {
                "user_id": user,
                "session_id": session,
                "event_name": name,
                "occurred_at": f"2026-08-{day:02d}T{hour:02d}:00:00+00:00",
            }

        rows = [
            event("u1", "s1", "session_started", 10),
            event("u1", "s1", "first_draft_submitted", 10, 10),
            event("u1", "s1", "report_viewed", 10, 11),
            event("u1", "s1", "training_started", 10, 12),
            event("u1", "s1", "sentence_training_completed", 10, 13),
            event("u1", "s1", "second_draft_submitted", 10, 14),
            event("u1", "s2", "session_started", 11),
            event("u1", "s3", "session_started", 17),
            event("u2", "s4", "session_started", 18),
            event("u2", "s4", "first_draft_submitted", 18, 10),
            event("u2", "s4", "report_viewed", 18, 11),
            event("u2", "s5", "session_started", 19),
            event("u3", "s6", "session_started", 20),
            event("u3", "s6", "first_draft_submitted", 20, 10),
            event("u3", "s6", "report_viewed", 20, 11),
            event("u3", "s6", "sentence_training_started", 20, 12),
        ]
        all_time = aggregate_event_rows(rows, now=now)
        self.assertEqual(all_time["funnel"], {
            "first_draft_submitted": 3,
            "report_viewed": 3,
            "training_started": 2,
            "sentence_training_completed": 1,
            "second_draft_submitted": 1,
        })
        self.assertEqual(all_time["retention"]["day_1"]["eligible_users"], 3)
        self.assertEqual(all_time["retention"]["day_1"]["retained_users"], 2)
        self.assertEqual(all_time["retention"]["day_7"], {
            "eligible_users": 1, "retained_users": 1, "rate": 1.0,
        })

        recent = aggregate_event_rows(rows, since=range_start(7, now), now=now)
        self.assertEqual(recent["unique_users"], 3)
        self.assertEqual(recent["new_users"], 2)
        self.assertEqual(recent["sessions"], 4)
        self.assertEqual(recent["funnel"]["first_draft_submitted"], 2)

    def test_admin_allowlist_takes_precedence_and_password_is_fallback(self):
        allowlist = parse_admin_emails("Admin@Example.com; owner@example.com")
        self.assertTrue(admin_access_allowed(
            email="admin@example.com", configured_admin_emails=allowlist,
        ))
        self.assertFalse(admin_access_allowed(
            email="other@example.com", configured_admin_emails=allowlist,
            password="right", expected_password="right",
        ))
        self.assertTrue(admin_access_allowed(password="right", expected_password="right"))
        self.assertFalse(admin_access_allowed(password="wrong", expected_password="right"))
        self.assertFalse(admin_access_allowed())

    def test_migration_is_private_minimal_and_aggregate_only(self):
        sql = MIGRATION.read_text(encoding="utf-8").lower()
        for field in (
            "event_id", "user_id", "session_id", "run_id", "event_name",
            "occurred_at", "metadata_json", "dedupe_key",
        ):
            self.assertIn(field, sql)
        self.assertIn("alter table public.analytics_events enable row level security", sql)
        self.assertIn("revoke all on public.analytics_events from public, anon, authenticated", sql)
        self.assertIn("grant execute on function public.get_analytics_dashboard(timestamptz)", sql)
        for forbidden in ("essay_text", "essay_content", "report_markdown", "report_json", "email", "ip_address", "api_key"):
            self.assertNotIn(forbidden, sql)


if __name__ == "__main__":
    unittest.main()
