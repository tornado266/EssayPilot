import unittest
import base64
import json
import threading
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from src.auth_session import (
    AUTH_BROWSER_COMMAND_KEY,
    AUTH_BROWSER_VERSION_KEY,
    AUTH_LOGOUT_PENDING_KEY,
    AUTH_REQUEST_RERUN_KEY,
    acknowledge_browser_command,
    apply_browser_command_to_record,
    begin_logout,
    consume_auth_request_rerun,
    PersistedRefreshSession,
    queue_refresh_token_write,
    take_browser_command,
)
from src.cloud_store import CloudSessionExpiredError, CloudStoreError, CloudUser, SupabaseStore


class CloudStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = SupabaseStore()
        self.store.url = "https://example.supabase.co"
        self.store.anon_key = "public-anon-key"
        self.store.service_role_key = ""
        self.store.secret_key = ""
        self.store.beta_start_at = ""

    @staticmethod
    def response(status, data=None):
        body = b"{}" if data is not None else b""
        response = Mock(status_code=status, content=body)
        response.json.return_value = data or {}
        return response

    @staticmethod
    def jwt_with_exp(exp):
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": exp}).encode("utf-8")
        ).decode("ascii").rstrip("=")
        return f"header.{payload}.signature"

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
        self.assertEqual(user.expiry_source, "expires_at")

    @patch("src.cloud_store.time.time", return_value=2_000_000_000)
    @patch("src.cloud_store.requests.request")
    def test_expiry_falls_back_to_expires_in_then_jwt_then_unknown(self, request, _time):
        base = {
            "user": {"id": "user-a", "email": "a@example.com"},
            "refresh_token": "refresh-token",
        }
        request.side_effect = [
            self.response(200, {**base, "access_token": "opaque", "expires_at": "bad", "expires_in": 3600}),
            self.response(200, {**base, "access_token": self.jwt_with_exp(2_000_007_200)}),
            self.response(200, {**base, "access_token": "opaque"}),
        ]

        from_duration = self.store.verify_email_code("a@example.com", "111111")
        from_jwt = self.store.verify_email_code("a@example.com", "222222")
        unknown = self.store.verify_email_code("a@example.com", "333333")

        self.assertEqual((from_duration.expires_at, from_duration.expiry_source), (2_000_003_600, "expires_in"))
        self.assertEqual((from_jwt.expires_at, from_jwt.expiry_source), (2_000_007_200, "jwt"))
        self.assertEqual((unknown.expires_at, unknown.expiry_source), (0, "unknown"))

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
    def test_first_401_refreshes_persists_rotation_and_retries_once(self, request):
        old = CloudUser("user-a", "a@example.com", "access-old", "refresh-old")
        refreshed_data = {
            "user": {"id": "user-a", "email": "a@example.com"},
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "expires_at": 2_000_003_600,
            "expires_in": 3600,
        }
        request.side_effect = [
            self.response(401, {"message": "expired"}),
            self.response(200, refreshed_data),
            self.response(200, []),
        ]
        updated = Mock()
        invalidated = Mock()
        self.store.bind_auth_session(lambda: old, updated, invalidated)

        self.assertEqual(self.store.list_grading_runs(old), [])

        self.assertEqual(request.call_count, 3)
        updated.assert_called_once()
        self.assertEqual(updated.call_args.args[0].refresh_token, "refresh-new")
        invalidated.assert_not_called()
        self.assertEqual(
            request.call_args_list[2].kwargs["headers"]["Authorization"],
            "Bearer access-new",
        )

    @patch("src.cloud_store.requests.request")
    def test_401_retry_success_queues_write_and_matching_ack_completes_it(self, request):
        old = CloudUser("user-a", "a@example.com", "access-old", "refresh-old")
        refreshed_data = {
            "user": {"id": "user-a", "email": "a@example.com"},
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "expires_in": 3600,
        }
        request.side_effect = [
            self.response(401, {"message": "expired"}),
            self.response(200, refreshed_data),
            self.response(200, []),
        ]
        state = {AUTH_BROWSER_VERSION_KEY: 7}
        current = {"user": old}

        def updated(user):
            current["user"] = user
            queue_refresh_token_write(
                state, user.refresh_token, now=2_000_000_000, request_rerun=True
            )

        self.store.bind_auth_session(lambda: current["user"], updated, Mock())

        self.assertEqual(self.store.list_grading_runs(old), [])
        command = take_browser_command(state)
        self.assertEqual(command["action"], "write")
        self.assertEqual(command["refresh_token"], "refresh-new")
        self.assertTrue(state[AUTH_REQUEST_RERUN_KEY])
        existing = PersistedRefreshSession("refresh-old", 1_999_999_900, 7)
        _record, browser_value = apply_browser_command_to_record(existing, command)
        ack = acknowledge_browser_command(state, browser_value, now=2_000_000_000)
        self.assertEqual(ack.status, "written")
        self.assertNotIn(AUTH_BROWSER_COMMAND_KEY, state)

    @patch("src.cloud_store.requests.request")
    def test_second_401_stops_without_refresh_loop(self, request):
        old = CloudUser("user-a", "a@example.com", "access-old", "refresh-old")
        request.side_effect = [
            self.response(401, {"message": "expired"}),
            self.response(200, {
                "user": {"id": "user-a", "email": "a@example.com"},
                "access_token": "access-new",
                "refresh_token": "refresh-new",
            }),
            self.response(401, {"message": "still expired"}),
        ]
        updated = Mock()
        invalidated = Mock()
        self.store.bind_auth_session(lambda: old, updated, invalidated)

        with self.assertRaises(CloudSessionExpiredError):
            self.store.list_grading_runs(old)

        self.assertEqual(request.call_count, 3)
        updated.assert_called_once()
        self.assertEqual(updated.call_args.args[0].refresh_token, "refresh-new")
        invalidated.assert_called_once()

        with self.assertRaises(CloudSessionExpiredError):
            self.store.list_grading_runs(old)
        self.assertEqual(request.call_count, 3)

    @patch("src.cloud_store.requests.request")
    def test_second_401_clear_blocks_late_write_and_all_followup_requests(self, request):
        old = CloudUser("user-a", "a@example.com", "access-old", "refresh-old")
        request.side_effect = [
            self.response(401, {"message": "expired"}),
            self.response(200, {
                "user": {"id": "user-a", "email": "a@example.com"},
                "access_token": "access-new",
                "refresh_token": "refresh-new",
            }),
            self.response(401, {"message": "still expired"}),
        ]
        state = {AUTH_BROWSER_VERSION_KEY: 12}
        updates = []

        def updated(user):
            updates.append(user)
            queue_refresh_token_write(
                state, user.refresh_token, now=2_000_000_000, request_rerun=True
            )

        def invalidated(_user):
            begin_logout(state, reason="invalid", expected_version=12)

        self.store.bind_auth_session(lambda: old, updated, invalidated)
        with self.assertRaises(CloudSessionExpiredError):
            self.store.list_grading_runs(old)

        clear_command = take_browser_command(state)
        self.assertEqual([user.refresh_token for user in updates], ["refresh-new"])
        self.assertTrue(consume_auth_request_rerun(state))
        self.assertFalse(consume_auth_request_rerun(state))
        self.assertEqual(clear_command["action"], "clear")
        self.assertIn(AUTH_LOGOUT_PENDING_KEY, state)
        queue_refresh_token_write(
            state, "late-refresh", now=2_000_000_000, request_rerun=True
        )
        self.assertEqual(take_browser_command(state), clear_command)
        existing = PersistedRefreshSession("refresh-old", 1_999_999_900, 12)
        remaining, clear_value = apply_browser_command_to_record(existing, clear_command)
        clear_ack = acknowledge_browser_command(
            state, clear_value, now=2_000_000_000
        )
        self.assertIsNone(remaining)
        self.assertEqual(clear_ack.status, "cleared")
        with self.assertRaises(CloudSessionExpiredError):
            self.store.get_grading_run(old, "run-2")
        self.assertEqual(request.call_count, 3)

    @patch("src.cloud_store.requests.request")
    def test_timeout_and_5xx_do_not_refresh_or_replay(self, request):
        user = CloudUser("user-a", "a@example.com", "access", "refresh")
        updated = Mock()
        self.store.bind_auth_session(lambda: user, updated, Mock())
        request.side_effect = requests.Timeout("offline")

        with self.assertRaises(CloudStoreError):
            self.store.get_grading_run(user, "run-1")
        self.assertEqual(request.call_count, 1)
        updated.assert_not_called()

        request.reset_mock()
        request.side_effect = None
        request.return_value = self.response(429, {"message": "rate limited"})
        with self.assertRaises(CloudStoreError):
            self.store.get_grading_run(user, "run-1")
        self.assertEqual(request.call_count, 1)
        updated.assert_not_called()

        request.reset_mock()
        request.side_effect = None
        request.return_value = self.response(503, {"message": "unavailable"})
        with self.assertRaises(CloudStoreError):
            self.store.save_draft_revision(
                user,
                essay_id="essay-1",
                grading_run_id="run-1",
                content="revision",
                scores={},
                report_json={},
                report_markdown="",
                progress_report="",
                revised_grading_run_id="run-2",
            )
        self.assertEqual(request.call_count, 1)

        request.reset_mock()
        request.side_effect = None
        request.return_value = self.response(503, {"message": "unavailable"})
        with self.assertRaises(CloudStoreError):
            self.store.get_grading_run(user, "run-1")
        self.assertEqual(request.call_count, 1)
        updated.assert_not_called()

    @patch("src.cloud_store.requests.request")
    def test_background_analytics_401_never_uses_session_callbacks(self, request):
        user = CloudUser("user-a", "a@example.com", "access", "refresh")
        getter = Mock(side_effect=AssertionError("must stay off background session state"))
        updated = Mock()
        invalidated = Mock()
        self.store.bind_auth_session(getter, updated, invalidated)
        request.return_value = self.response(401, {"message": "expired"})

        with self.assertRaises(CloudStoreError):
            self.store.record_analytics_event("report_viewed", "session", "dedupe", user=user)

        self.assertEqual(request.call_count, 1)
        getter.assert_not_called()
        updated.assert_not_called()
        invalidated.assert_not_called()

    @patch("src.cloud_store.requests.request")
    def test_official_logout_uses_authorization_header_not_url(self, request):
        request.return_value = self.response(204)
        user = CloudUser("user-a", "a@example.com", "access-secret", "refresh-secret")

        self.store.sign_out(user)

        self.assertTrue(request.call_args.args[1].endswith("/auth/v1/logout"))
        self.assertNotIn("access-secret", request.call_args.args[1])
        self.assertEqual(
            request.call_args.kwargs["headers"]["Authorization"], "Bearer access-secret"
        )
        self.assertEqual(request.call_args.kwargs["params"], {"scope": "local"})

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
    def test_home_snapshot_fetches_only_display_fields_under_user_rls(self, request):
        runs = [{"id": "run-a", "overall_band": 7, "criteria": [], "created_at": "2026-09-01"}]
        pending = [{"id": "task-a", "grading_run_id": "run-b", "task_kind": "logic"}]
        request.side_effect = lambda method, url, **kwargs: self.response(
            200, runs if url.endswith("/grading_runs") else pending
        )
        user = CloudUser("user-a", "a@example.com", "user-access-token")

        self.assertEqual(self.store.get_home_snapshot(user), (runs, pending))

        self.assertEqual(request.call_count, 2)
        runs_call = next(call for call in request.call_args_list if call.args[1].endswith("/grading_runs"))
        pending_call = next(call for call in request.call_args_list if call.args[1].endswith("/practice_attempts"))
        self.assertTrue(runs_call.args[1].endswith("/rest/v1/grading_runs"))
        self.assertEqual(runs_call.kwargs["params"], {
            "select": "id,overall_band,criteria,created_at",
            "user_id": "eq.user-a", "order": "created_at.desc", "limit": "2",
        })
        self.assertTrue(pending_call.args[1].endswith("/rest/v1/practice_attempts"))
        self.assertEqual(pending_call.kwargs["params"], {
            "select": "id,grading_run_id,task_kind,task_index,original_text,updated_at",
            "user_id": "eq.user-a", "status": "eq.in_progress",
            "order": "updated_at.desc", "limit": "1",
        })
        for call in request.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 5)
            self.assertEqual(call.args[0], "GET")
            self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer user-access-token")
            self.assertEqual(call.kwargs["headers"]["apikey"], "public-anon-key")

    def test_home_reads_overlap_without_session_callbacks_in_workers(self):
        barrier = threading.Barrier(2, timeout=3)
        main_thread = threading.get_ident()
        user = CloudUser("user-a", "a@example.com", "old-token")
        current = CloudUser("user-a", "a@example.com", "current-token")

        def get_user():
            self.assertEqual(threading.get_ident(), main_thread)
            return current

        def read(method, path, **kwargs):
            self.assertNotEqual(threading.get_ident(), main_thread)
            self.assertEqual(kwargs["access_token"], "current-token")
            barrier.wait()  # Serial reads cannot pass this rendezvous.
            return [{"path": path}]

        self.store.bind_auth_session(get_user, Mock(), Mock())
        with patch.object(self.store, "_request", side_effect=read):
            runs, pending = self.store.get_home_snapshot(user)
        self.assertEqual(runs, [{"path": "/rest/v1/grading_runs"}])
        self.assertEqual(pending, [{"path": "/rest/v1/practice_attempts"}])

    def test_home_expired_reads_rotate_token_once_on_main_thread(self):
        main_thread = threading.get_ident()
        user = CloudUser("user-a", "a@example.com", "expired", "refresh-token")
        refreshed = CloudUser("user-a", "a@example.com", "fresh", "rotated-token")
        state = [user]

        def get_user():
            self.assertEqual(threading.get_ident(), main_thread)
            return state[0]

        def update(value):
            self.assertEqual(threading.get_ident(), main_thread)
            state[0] = value

        def read(method, path, **kwargs):
            if kwargs["access_token"] == "expired":
                raise CloudStoreError("expired", status_code=401)
            return [{"path": path}]

        def refresh(token):
            self.assertEqual(threading.get_ident(), main_thread)
            self.assertEqual(token, "refresh-token")
            return refreshed

        invalidated = Mock()
        self.store.bind_auth_session(get_user, update, invalidated)
        with patch.object(self.store, "_request", side_effect=read), patch.object(
            self.store, "refresh", side_effect=refresh,
        ) as rotate:
            runs, pending = self.store.get_home_snapshot(user)
        rotate.assert_called_once()
        invalidated.assert_not_called()
        self.assertEqual(state[0], refreshed)
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(pending), 1)

    def test_home_blocked_session_sends_no_parallel_reads(self):
        user = CloudUser("user-a", "a@example.com", "expired")
        self.store._auth_blocked_user_ids.add(user.id)
        with patch.object(self.store, "_request") as request:
            with self.assertRaises(CloudSessionExpiredError):
                self.store.get_home_snapshot(user)
        request.assert_not_called()

    @patch("src.cloud_store.requests.request")
    def test_home_snapshot_handles_empty_data_and_propagates_service_failure(self, request):
        user = CloudUser("user-a", "a@example.com", "user-access-token")
        request.side_effect = [self.response(200, []), self.response(200, {})]
        self.assertEqual(self.store.get_home_snapshot(user), ([], []))

        request.side_effect = [self.response(200, []), self.response(503, {"message": "unavailable"})]
        with self.assertRaises(CloudStoreError):
            self.store.get_home_snapshot(user)

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

    @patch("src.cloud_store.requests.request")
    def test_refresh_is_published_before_retry_503_or_timeout(self, request):
        old = CloudUser("user-a", "a@example.com", "access-old", "refresh-old")
        refreshed_data = {
            "user": {"id": "user-a", "email": "a@example.com"},
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "expires_at": 2_000_003_600,
            "expires_in": 3600,
        }

        for label, retry_failure in (
            ("503", self.response(503, {"message": "unavailable"})),
            ("timeout", requests.Timeout("retry timed out")),
        ):
            with self.subTest(retry_failure=label):
                store = SupabaseStore()
                store.url = "https://example.supabase.co"
                store.anon_key = "public-anon-key"
                updated = Mock()
                invalidated = Mock()
                store.bind_auth_session(lambda: old, updated, invalidated)
                request.reset_mock()
                request.side_effect = [
                    self.response(401, {"message": "expired"}),
                    self.response(200, refreshed_data),
                    retry_failure,
                ]

                with self.assertRaises(CloudStoreError):
                    store.list_grading_runs(old)

                self.assertEqual(request.call_count, 3)
                updated.assert_called_once()
                self.assertEqual(
                    updated.call_args.args[0].refresh_token, "refresh-new"
                )
                self.assertEqual(store._runtime_user.refresh_token, "refresh-new")
                invalidated.assert_not_called()


    @patch("src.cloud_store.requests.request")
    def test_temporary_refresh_failure_blocks_later_refreshes_in_same_run(self, request):
        old = CloudUser("user-a", "a@example.com", "access-old", "refresh-old")
        for label, refresh_failure in (
            ("503", self.response(503, {"message": "unavailable"})),
            ("timeout", requests.Timeout("refresh timed out")),
        ):
            with self.subTest(refresh_failure=label):
                store = SupabaseStore()
                store.url = "https://example.supabase.co"
                store.anon_key = "public-anon-key"
                updated = Mock()
                invalidated = Mock()
                store.bind_auth_session(lambda: old, updated, invalidated)
                request.reset_mock()
                request.side_effect = [
                    self.response(401, {"message": "expired"}),
                    refresh_failure,
                ]

                with self.assertRaises(CloudStoreError):
                    store.list_grading_runs(old)

                self.assertEqual(request.call_count, 2)
                self.assertIn(
                    old.id, store._auth_refresh_attempted_user_ids
                )
                self.assertIn(
                    old.id, store._auth_refresh_temporarily_failed_user_ids
                )
                updated.assert_not_called()
                invalidated.assert_not_called()

                request.reset_mock()
                request.side_effect = [
                    self.response(401, {"message": "expired again"}),
                    self.response(401, {"message": "expired once more"}),
                ]

                for _ in range(2):
                    with self.assertRaisesRegex(CloudStoreError, "temporarily"):
                        store.list_grading_runs(old)

                self.assertEqual(request.call_count, 2)
                self.assertFalse(
                    any(
                        "/auth/v1/token" in call.args[1]
                        for call in request.call_args_list
                    )
                )
                updated.assert_not_called()
                invalidated.assert_not_called()

if __name__ == "__main__":
    unittest.main()
