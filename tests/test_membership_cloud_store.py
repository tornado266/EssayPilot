import hashlib
import inspect
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.cloud_store import CloudStoreError, CloudUser, SupabaseStore


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20260901_founder_membership.sql"
SCHEMA = ROOT / "supabase" / "schema.sql"


class FounderMembershipSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.migration = MIGRATION.read_text(encoding="utf-8")
        cls.sql = cls.migration.lower()
        cls.schema = SCHEMA.read_text(encoding="utf-8")

    def test_consolidated_schema_contains_the_exact_migration(self):
        self.assertIn(self.migration.strip(), self.schema)
        self.assertEqual(self.schema.count("-- EssayPilot founder membership access."), 1)

    def test_offer_is_fixed_to_30_days_three_runs_and_bounded_actions(self):
        self.assertIn("founder_pass_30d_3runs", self.sql)
        self.assertIn("check (run_quota = 3)", self.sql)
        self.assertIn("check (training_actions_per_run = 3)", self.sql)
        self.assertIn("check (second_drafts_per_run = 1)", self.sql)
        self.assertIn("expires_at = starts_at + interval '30 days'", self.sql)
        self.assertIn(
            "amount_cny numeric(4,2) not null default 7.50 "
            "check (amount_cny = 7.50)",
            self.sql,
        )
        self.assertIn(
            "currency text not null default 'cny' check (currency = 'cny')",
            self.sql,
        )

    def test_private_tables_are_owner_read_only_for_authenticated_users(self):
        tables = (
            "memberships",
            "membership_requests",
            "membership_run_accesses",
            "membership_training_actions",
            "membership_second_draft_actions",
        )
        for table in tables:
            self.assertIn(
                f"alter table public.{table} enable row level security", self.sql
            )
            self.assertIn(f"auth.uid() = user_id", self.sql)
        self.assertIn("from public, anon, authenticated", self.sql)
        self.assertIn("grant select on", self.sql)
        self.assertNotIn("to authenticated;\ngrant insert", self.sql)
        self.assertNotIn("screenshot", self.sql)

    def test_approval_is_server_only_and_idempotent(self):
        self.assertIn(
            "revoke all on function public.approve_membership_request(uuid)\n"
            "  from public, anon, authenticated",
            self.sql,
        )
        self.assertIn(
            "grant execute on function public.approve_membership_request(uuid)\n"
            "  to service_role",
            self.sql,
        )
        self.assertIn("'reason', 'already_approved'", self.sql)
        self.assertIn("grant_reference = v_request.payment_reference", self.sql)
        self.assertIn(
            "pg_advisory_xact_lock(hashtextextended(v_user::text, 0))",
            self.sql,
        )
        self.assertIn("exception when unique_violation", self.sql)
        self.assertIn("coalesce(auth.role(), '') <> 'service_role'", self.sql)
        self.assertIn("reviewed_by = v_reviewer", self.sql)

    def test_security_definers_use_only_trusted_search_paths(self):
        function_count = self.sql.count("create or replace function public.")
        self.assertEqual(function_count, 14)
        self.assertEqual(
            self.sql.count("set search_path = pg_catalog, public"),
            function_count,
        )
        self.assertNotIn("set search_path = public, pg_temp", self.sql)
        self.assertIn(
            "revoke create on schema public from public, anon, authenticated",
            self.sql,
        )

    def test_run_quota_is_locked_reserved_completed_and_released(self):
        self.assertIn("create table if not exists public.membership_run_accesses", self.sql)
        self.assertIn("unique(user_id, flow_id)", self.sql)
        self.assertIn("unique(user_id, content_hash)", self.sql)
        self.assertGreaterEqual(self.sql.count("for update;"), 12)
        self.assertIn("status = 'released', released_at = v_now", self.sql)
        self.assertIn("reservation_expires_at <= v_now", self.sql)
        self.assertIn("'reason', 'run_quota_exhausted'", self.sql)
        self.assertIn("'reason', 'existing_result'", self.sql)
        self.assertIn(
            "v_completed_count + v_active_reserved_count + v_reconcilable_count",
            self.sql,
        )
        self.assertIn("g.created_at <= a.reservation_expires_at", self.sql)
        self.assertIn("v_other_usage >= v_membership.run_quota", self.sql)
        self.assertIn("'reason', 'reconciliation_required'", self.sql)
        self.assertIn("'reason', 'grading_run_outside_reservation'", self.sql)

    def test_run_training_and_second_draft_are_bound_to_owned_runs(self):
        self.assertIn("a.grading_run_id = p_grading_run_id", self.sql)
        self.assertIn(
            "grading_run_id uuid references public.grading_runs(id) on delete restrict",
            self.sql,
        )
        self.assertIn(
            "revised_grading_run_id uuid references public.grading_runs(id) on delete restrict",
            self.sql,
        )
        self.assertIn("unique(run_access_id, task_kind, task_key_hash)", self.sql)
        self.assertIn("'reason', 'training_limit_reached'", self.sql)
        self.assertIn("run_access_id uuid not null unique", self.sql)
        self.assertIn("from public.draft_revisions d", self.sql)
        self.assertIn("g.parent_run_id = v_access.grading_run_id", self.sql)
        self.assertNotIn("expression", " ".join(
            line for line in self.sql.splitlines()
            if "membership_training_actions" in line
        ))

        access_start = self.sql.index(
            "create or replace function public.get_membership_run_access"
        )
        access_end = self.sql.index(
            "create or replace function public.reserve_training_action",
            access_start,
        )
        access_function = self.sql[access_start:access_end]
        self.assertIn("'allowed', v_active", access_function)
        self.assertIn("'history_readable', true", access_function)

        second_start = self.sql.index(
            "create or replace function public.complete_second_draft_action"
        )
        second_end = self.sql.index(
            "create or replace function public.release_second_draft_action",
            second_start,
        )
        second_function = self.sql[second_start:second_end]
        self.assertIn("'revised_grading_run_required'", second_function)
        self.assertIn("join public.essays e on e.id = g.essay_id", second_function)
        self.assertIn("e.content_hash = v_action.content_hash", second_function)
        self.assertIn(
            "v_revised_created_at > v_action.reservation_expires_at",
            second_function,
        )

    def test_training_completion_requires_a_bound_persisted_attempt(self):
        for column in (
            "task_key_hash",
            "training_action_id",
            "training_flow_id",
            "feedback_persisted_at",
            "settled_at",
        ):
            self.assertIn(f"add column if not exists {column}", self.sql)
        self.assertIn("practice_training_action_once_idx", self.sql)
        self.assertIn("practice_training_flow_once_idx", self.sql)
        self.assertIn(
            "on public.practice_attempts(user_id, grading_run_id, task_kind, task_key_hash)",
            self.sql,
        )

        save_start = self.sql.index(
            "create or replace function public.save_training_practice_attempt"
        )
        save_end = self.sql.index(
            "create or replace function public.get_membership_run_access", save_start
        )
        save_function = self.sql[save_start:save_end]
        self.assertIn("coalesce(g.draft_role, 'ordinary') <> 'second'", save_function)
        self.assertIn("a.flow_id = v_flow_id", save_function)
        self.assertIn("a.task_key_hash = p_task_key_hash", save_function)
        self.assertIn("for update", save_function)
        self.assertIn("existing.training_action_id", save_function)
        self.assertIn("existing.training_flow_id", save_function)
        self.assertNotIn("settled_at =", save_function)

        complete_start = self.sql.index(
            "create or replace function public.complete_training_action"
        )
        complete_end = self.sql.index(
            "create or replace function public.release_training_action", complete_start
        )
        complete_function = self.sql[complete_start:complete_end]
        self.assertIn("p.training_action_id = v_action.id", complete_function)
        self.assertIn("p.training_flow_id = p_flow_id", complete_function)
        self.assertIn("p.user_id = v_user", complete_function)
        self.assertIn("p.grading_run_id = v_access.grading_run_id", complete_function)
        self.assertIn("p.task_kind = v_action.task_kind", complete_function)
        self.assertIn("p.task_key_hash = v_action.task_key_hash", complete_function)
        self.assertIn("'reason', 'practice_attempt_required'", complete_function)
        self.assertIn("v_action.reservation_expires_at <= v_now", complete_function)
        self.assertIn("then 'reconciled'", complete_function)

        access_start = self.sql.index(
            "create or replace function public.get_membership_run_access"
        )
        access_end = self.sql.index(
            "create or replace function public.reserve_training_action", access_start
        )
        access_function = self.sql[access_start:access_end]
        self.assertIn("p.training_action_id = a.id", access_function)
        self.assertIn("btrim(p.feedback) <> ''", access_function)

    def test_action_writers_lock_membership_before_run_and_action(self):
        function_names = (
            "reserve_training_action",
            "complete_training_action",
            "release_training_action",
            "reserve_second_draft_action",
            "complete_second_draft_action",
            "release_second_draft_action",
        )
        for index, name in enumerate(function_names):
            start = self.sql.index(f"create or replace function public.{name}")
            following = self.sql.find(
                "create or replace function public.", start + 1
            )
            body = self.sql[start:following if following >= 0 else None]
            membership_lock = body.index("from public.memberships m")
            run_lock = body.index("from public.membership_run_accesses")
            self.assertLess(
                membership_lock,
                run_lock,
                f"{name} must lock membership before run access",
            )

    def test_all_client_rpc_signatures_are_explicitly_granted(self):
        signatures = (
            "get_my_membership_entitlement()",
            "create_membership_request(text,text,text)",
            "reserve_membership_run(uuid,text,uuid)",
            "complete_membership_run(uuid,uuid)",
            "release_membership_run(uuid)",
            "get_membership_run_access(uuid)",
            "save_training_practice_attempt(uuid,uuid,uuid,text,text,integer,text,text,text,text,boolean,text[])",
            "reserve_training_action(uuid,uuid,text,text)",
            "complete_training_action(uuid)",
            "release_training_action(uuid)",
            "reserve_second_draft_action(uuid,uuid,text)",
            "complete_second_draft_action(uuid,uuid)",
            "release_second_draft_action(uuid)",
        )
        for signature in signatures:
            self.assertIn(f"grant execute on function public.{signature}", self.sql)


class FounderMembershipCloudStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = SupabaseStore()
        self.store.url = "https://example.supabase.co"
        self.store.anon_key = "public-anon-key"
        self.store.secret_key = ""
        self.store.service_role_key = ""
        self.user = CloudUser(
            "10000000-0000-0000-0000-000000000001",
            "learner@example.com",
            "user-access-token",
        )

    @staticmethod
    def response(data):
        response = Mock(status_code=200, content=b"{}")
        response.json.return_value = data
        return response

    @patch("src.cloud_store.requests.request")
    def test_entitlement_and_latest_request_use_user_rls(self, request):
        request.side_effect = [
            self.response({"active": True, "runs_remaining": 2}),
            self.response([
                {
                    "id": "request-1",
                    "application_code": "EP-ABC",
                    "status": "pending",
                    "payment_reference": "PAY-1234",
                    "submitted_at": "2026-09-01T12:00:00Z",
                    "reviewed_at": None,
                }
            ]),
        ]

        entitlement = self.store.get_membership_entitlement(self.user)
        latest = self.store.get_my_membership_request(self.user)

        self.assertEqual(entitlement["runs_remaining"], 2)
        self.assertEqual(latest["application_code"], "EP-ABC")
        self.assertTrue(request.call_args_list[0].args[1].endswith(
            "/rest/v1/rpc/get_my_membership_entitlement"
        ))
        params = request.call_args_list[1].kwargs["params"]
        self.assertIn("application_code:request_code", params["select"])
        self.assertIn("submitted_at:created_at", params["select"])
        for call in request.call_args_list:
            self.assertEqual(
                call.kwargs["headers"]["Authorization"], "Bearer user-access-token"
            )

    @patch("src.cloud_store.requests.request")
    def test_manual_request_sends_no_email_or_screenshot(self, request):
        request.return_value = self.response(
            {"created": True, "application_code": "EP-ABC", "status": "pending"}
        )

        result = self.store.create_membership_request(
            self.user,
            "  PAY-1234  ",
            paid_at=" 2026-09-01 20:30 ",
            note=" evening payment ",
        )

        self.assertEqual(result["application_code"], "EP-ABC")
        payload = request.call_args.kwargs["json"]
        self.assertEqual(payload, {
            "p_payment_reference": "PAY-1234",
            "p_paid_at": "2026-09-01 20:30",
            "p_note": "evening payment",
        })
        self.assertNotIn("email", str(payload).lower())
        self.assertNotIn("screenshot", str(payload).lower())

    @patch("src.cloud_store.requests.request")
    def test_run_reserve_complete_and_release_contracts(self, request):
        request.side_effect = [
            self.response({"allowed": True, "reason": "reserved"}),
            self.response({"completed": True, "reason": "completed"}),
            self.response({"released": True, "reason": "already_released"}),
            self.response({"allowed": True, "training_remaining": 3}),
        ]
        flow_id = "20000000-0000-0000-0000-000000000001"
        run_id = "30000000-0000-0000-0000-000000000001"
        content_hash = "a" * 64

        self.store.reserve_membership_run(
            self.user, flow_id, content_hash, grading_run_id=run_id
        )
        self.store.complete_membership_run(self.user, flow_id, run_id)
        self.store.release_membership_run(self.user, flow_id)
        access = self.store.get_membership_run_access(self.user, run_id)

        self.assertEqual(access["training_remaining"], 3)
        payloads = [call.kwargs["json"] for call in request.call_args_list]
        self.assertEqual(payloads[0], {
            "p_flow_id": flow_id,
            "p_content_hash": content_hash,
            "p_grading_run_id": run_id,
        })
        self.assertEqual(payloads[1], {
            "p_flow_id": flow_id,
            "p_grading_run_id": run_id,
        })
        self.assertEqual(payloads[2], {"p_flow_id": flow_id})
        self.assertEqual(payloads[3], {"p_grading_run_id": run_id})

    @patch("src.cloud_store.requests.request")
    def test_training_task_key_is_hashed_and_never_sent_raw(self, request):
        request.return_value = self.response({"allowed": True})
        raw_task = "The learner's complete private sentence."

        self.store.reserve_training_action(
            self.user,
            "run-1",
            "flow-1",
            "sentence",
            raw_task,
        )

        payload = request.call_args.kwargs["json"]
        expected = hashlib.sha256(f"sentence\0{raw_task}".encode("utf-8")).hexdigest()
        self.assertEqual(payload["p_task_key_hash"], expected)
        self.assertNotIn(raw_task, str(payload))
        self.assertEqual(set(payload), {
            "p_grading_run_id", "p_flow_id", "p_task_kind", "p_task_key_hash"
        })

    @patch("src.cloud_store.requests.request")
    def test_practice_feedback_is_saved_through_the_binding_rpc(self, request):
        request.return_value = self.response({
            "id": "attempt-1",
            "training_action_id": "action-1",
            "training_flow_id": "flow-1",
        })

        saved = self.store.save_practice_attempt(
            self.user,
            grading_run_id="run-1",
            task_kind="sentence",
            task_key="task-1",
            task_index=1,
            original_text="Original.",
            submitted_text="Rewrite.",
            feedback="Feedback.",
            training_action_id="action-1",
            training_flow_id="flow-1",
            error_tags=["grammar"],
        )

        self.assertEqual(saved["id"], "attempt-1")
        call = request.call_args
        self.assertTrue(call.args[1].endswith("/rpc/save_training_practice_attempt"))
        payload = call.kwargs["json"]
        self.assertEqual(payload["p_action_id"], "action-1")
        self.assertEqual(payload["p_flow_id"], "flow-1")
        self.assertEqual(
            payload["p_task_key_hash"],
            hashlib.sha256(b"sentence\0task-1").hexdigest(),
        )
        self.assertNotIn("user_id", payload)

    @patch("src.cloud_store.requests.request")
    def test_run_attempt_listing_is_one_owned_run_query(self, request):
        request.return_value = self.response([{"id": "attempt-1"}])

        rows = self.store.list_practice_attempts_for_run(self.user, "run-1")

        self.assertEqual(rows, [{"id": "attempt-1"}])
        params = request.call_args.kwargs["params"]
        self.assertEqual(params["user_id"], f"eq.{self.user.id}")
        self.assertEqual(params["grading_run_id"], "eq.run-1")
        self.assertIn("training_flow_id", params["select"])

    @patch("src.cloud_store.requests.request")
    def test_training_and_second_draft_completion_release_are_narrow(self, request):
        request.side_effect = [self.response({}) for _ in range(6)]

        self.store.complete_training_action(self.user, "training-flow")
        self.store.release_training_action(self.user, "training-flow")
        self.store.reserve_second_draft_action(
            self.user, "run-1", "draft-flow", "b" * 64
        )
        self.store.complete_second_draft_action(
            self.user, "draft-flow", "run-2"
        )
        self.store.release_second_draft_action(self.user, "draft-flow")

        paths = [call.args[1] for call in request.call_args_list]
        self.assertTrue(paths[0].endswith("/rpc/complete_training_action"))
        self.assertTrue(paths[1].endswith("/rpc/release_training_action"))
        self.assertTrue(paths[2].endswith("/rpc/reserve_second_draft_action"))
        self.assertTrue(paths[3].endswith("/rpc/complete_second_draft_action"))
        self.assertTrue(paths[4].endswith("/rpc/release_second_draft_action"))

    @patch("src.cloud_store.requests.request")
    def test_server_key_lists_and_idempotently_approves_requests(self, request):
        self.store.secret_key = "sb_secret_membership_review"
        request.side_effect = [
            self.response([{"id": "request-1", "status": "pending"}]),
            self.response({"approved": True, "reason": "already_approved"}),
        ]

        pending = self.store.list_pending_membership_requests(limit=999)
        approved = self.store.approve_membership_request("request-1")

        self.assertEqual(pending[0]["id"], "request-1")
        self.assertTrue(approved["approved"])
        list_call, approve_call = request.call_args_list
        self.assertEqual(list_call.kwargs["params"]["limit"], "200")
        self.assertEqual(approve_call.kwargs["json"], {"p_request_id": "request-1"})
        self.assertIn("amount_cny", list_call.kwargs["params"]["select"])
        self.assertIn("currency", list_call.kwargs["params"]["select"])
        for call in request.call_args_list:
            self.assertEqual(call.kwargs["headers"]["apikey"], self.store.secret_key)
            self.assertNotIn("Authorization", call.kwargs["headers"])

    def test_admin_review_requires_a_server_key(self):
        with self.assertRaises(CloudStoreError):
            self.store.list_pending_membership_requests()
        with self.assertRaises(CloudStoreError):
            self.store.approve_membership_request("request-1")

    def test_second_draft_completion_requires_a_persisted_run_id(self):
        parameter = inspect.signature(
            self.store.complete_second_draft_action
        ).parameters["revised_grading_run_id"]
        self.assertIs(parameter.default, inspect.Parameter.empty)


if __name__ == "__main__":
    unittest.main()
