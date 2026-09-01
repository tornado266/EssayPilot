import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260901140000_membership_renewal_packs.sql"
)
SCHEMA = ROOT / "supabase" / "schema.sql"
CLOUD_STORE = ROOT / "src" / "cloud_store.py"


class MembershipRenewalSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.migration = MIGRATION.read_text(encoding="utf-8")
        cls.sql = cls.migration.lower()
        cls.schema = SCHEMA.read_text(encoding="utf-8")

    def function(self, name: str, next_name: str | None = None) -> str:
        body = self.sql.split(
            f"create or replace function public.{name}", 1
        )[1]
        if next_name:
            body = body.split(
                f"create or replace function public.{next_name}", 1
            )[0]
        return body

    def test_consolidated_schema_contains_exact_incremental_migration(self):
        self.assertIn(self.migration.strip(), self.schema)
        self.assertEqual(
            self.schema.count("-- EssayPilot repeatable manual-review packs."), 1
        )

    def test_each_purchase_is_a_distinct_pack_and_old_data_is_preserved(self):
        self.assertIn(
            "drop constraint if exists memberships_user_id_key", self.sql
        )
        self.assertIn("memberships_user_pack_order_idx", self.sql)
        approval = self.function("approve_membership_request")
        self.assertIn("insert into public.memberships(", approval)
        self.assertNotIn("update public.memberships", approval)
        self.assertIn("membership_id = v_membership.id", approval)
        self.assertNotIn("delete from public.membership", self.sql)

    def test_server_sets_founder_then_renewal_price_without_client_plan(self):
        self.assertIn("founder_pass_30d_3runs", self.sql)
        self.assertIn("renewal_pass_30d_3runs", self.sql)
        self.assertIn("amount_cny = 7.50", self.sql)
        self.assertIn("amount_cny = 9.90", self.sql)
        create_header = self.sql.split(
            "create or replace function public.create_membership_request", 1
        )[1].split("returns jsonb", 1)[0]
        self.assertNotIn("p_plan", create_header)
        create = self.function(
            "create_membership_request", "approve_membership_request"
        )
        self.assertIn("select count(*) into v_purchase_count", create)
        self.assertIn("v_plan := 'renewal_pass_30d_3runs'", create)
        self.assertIn("v_amount := 9.90", create)

    def test_price_constraint_is_safe_when_schema_is_reapplied(self):
        drop = "drop constraint if exists membership_requests_plan_price_check"
        add = "add constraint membership_requests_plan_price_check"
        self.assertIn(drop, self.sql)
        self.assertIn(add, self.sql)
        self.assertLess(self.sql.index(drop), self.sql.index(add))

    def test_active_pack_requires_three_completed_runs_not_reservations(self):
        create = self.function(
            "create_membership_request", "approve_membership_request"
        )
        approve = self.function("approve_membership_request")
        for body in (create, approve):
            self.assertIn("a.status = 'completed'", body)
            self.assertIn("a.reservation_expires_at > v_now", body)
            self.assertRegex(
                body,
                r"v_completed < v_(?:membership|existing)\.run_quota\s+or "
                r"v_reserved > 0",
            )
            self.assertIn("'reason', 'active_membership'", body)
        self.assertNotIn("v_completed + v_reserved >=", create)

    def test_entitlement_exposes_verified_next_offer(self):
        entitlement = self.function(
            "get_my_membership_entitlement", "reserve_membership_run"
        )
        for field in (
            "purchase_count",
            "has_previous_purchase",
            "next_plan_code",
            "next_amount_cny",
            "can_purchase",
        ):
            self.assertIn(f"'{field}'", entitlement)
        self.assertIn("v_completed >= v_membership.run_quota", entitlement)
        self.assertIn("v_reserved = 0", entitlement)

    def test_latest_refunded_or_revoked_pack_cannot_fall_back_to_an_older_pack(self):
        entitlement = self.function(
            "get_my_membership_entitlement", "reserve_membership_run"
        )
        current_pack = entitlement.split(
            "select m.* into v_membership", 1
        )[1].split("limit 1", 1)[0]
        self.assertIn("order by m.starts_at desc, m.created_at desc", current_pack)
        self.assertNotIn("case when m.status = 'active'", current_pack)
        self.assertIn("v_membership.status = 'active'", entitlement)

    def test_refund_or_revocation_invalidates_unproved_live_leases(self):
        run_reserve = self.function(
            "reserve_membership_run", "complete_membership_run"
        )
        training_reserve = self.function(
            "reserve_training_action", "complete_training_action"
        )
        draft_reserve = self.function(
            "reserve_second_draft_action", "complete_second_draft_action"
        )
        self.assertIn("m.status in ('revoked', 'refunded')", run_reserve)
        self.assertIn("set status = 'released', released_at = v_now", run_reserve)
        for body in (training_reserve, draft_reserve):
            self.assertIn(
                "v_membership.status in ('revoked', 'refunded')", body
            )
            self.assertIn("'reason', 'membership_inactive'", body)

    def test_new_first_draft_uses_latest_pack_and_rehomes_only_released_access(self):
        reserve = self.function(
            "reserve_membership_run", "complete_membership_run"
        )
        self.assertIn("m.starts_at desc", reserve)
        self.assertIn("m.created_at desc", reserve)
        self.assertIn("set membership_id = v_membership.id", reserve)
        self.assertLess(
            reserve.index("if v_has_access then"),
            reserve.index("set membership_id = v_membership.id"),
        )
        self.assertIn("if v_has_access and v_access.status = 'completed'", reserve)
        completed_branch = reserve.split(
            "if v_has_access and v_access.status = 'completed'", 1
        )[1].split("if v_has_access and v_access.status = 'reserved'", 1)[0]
        self.assertNotIn("set membership_id", completed_branch)

    def test_persisted_old_reservation_settles_against_its_original_pack(self):
        reserve = self.function(
            "reserve_membership_run", "complete_membership_run"
        )
        reconcile = reserve.index("v_reconcile_result := public.complete_membership_run")
        current_pack = reserve.index("select m.* into v_membership")
        self.assertLess(reconcile, current_pack)
        self.assertIn("v_other_access.flow_id, v_proof_run_id", reserve)
        self.assertIn("'reason', 'reconciliation_required'", reserve)

    def test_all_mutating_run_action_rpcs_lock_the_pack_from_run_access(self):
        expected = {
            "complete_membership_run": "release_membership_run",
            "release_membership_run": "save_training_practice_attempt",
            "save_training_practice_attempt": "get_membership_run_access",
            "reserve_training_action": "complete_training_action",
            "complete_training_action": "release_training_action",
            "release_training_action": "reserve_second_draft_action",
            "reserve_second_draft_action": "complete_second_draft_action",
            "complete_second_draft_action": "release_second_draft_action",
            "release_second_draft_action": "create_membership_request",
        }
        for name, next_name in expected.items():
            body = self.function(name, next_name)
            self.assertIn("v_membership_id uuid", body, name)
            self.assertIn(
                "where m.id = v_membership_id and m.user_id = v_user", body, name
            )
            self.assertIn("for update", body, name)

    def test_read_access_remains_bound_to_the_historical_pack(self):
        original_access = self.schema.split(
            "create or replace function public.get_membership_run_access", 1
        )[1].split(
            "create or replace function public.reserve_training_action", 1
        )[0]
        self.assertIn("where m.id = v_access.membership_id", original_access)
        self.assertIn("v_active := found", original_access)
        self.assertIn("v_membership.expires_at > v_now", original_access)

    def test_security_definers_and_approval_grants_remain_narrow(self):
        function_count = self.sql.count("create or replace function public.")
        self.assertEqual(function_count, 13)
        self.assertEqual(
            self.sql.count("set search_path = pg_catalog, public"),
            function_count,
        )
        self.assertIn("coalesce(auth.role(), '') <> 'service_role'", self.sql)
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

    def test_approval_uses_the_same_user_lock_order_as_request_creation(self):
        approval = self.function("approve_membership_request")
        advisory = approval.index("perform pg_advisory_xact_lock(")
        request_row_lock = approval.index(
            "where r.id = p_request_id\n  for update"
        )
        self.assertLess(advisory, request_row_lock)


class MembershipRenewalCloudStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = CLOUD_STORE.read_text(encoding="utf-8")

    def test_latest_request_reads_server_priced_offer_fields(self):
        method = self.source.split("def get_my_membership_request", 1)[1].split(
            "def create_membership_request", 1
        )[0]
        for field in ("plan_code", "amount_cny", "currency"):
            self.assertIn(field, method)

    def test_admin_queue_includes_plan_and_price(self):
        method = self.source.split(
            "def list_pending_membership_requests", 1
        )[1].split("def approve_membership_request", 1)[0]
        for field in ("plan_code", "amount_cny", "currency"):
            self.assertIn(field, method)


if __name__ == "__main__":
    unittest.main()
