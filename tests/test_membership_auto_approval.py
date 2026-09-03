import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260903094108_auto_approve_membership_requests.sql"
)
SCHEMA = ROOT / "supabase" / "schema.sql"


class MembershipAutoApprovalSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8")
        cls.schema = SCHEMA.read_text(encoding="utf-8")

    def test_consolidated_schema_contains_the_incremental_migration(self):
        self.assertIn(self.sql.strip(), self.schema)

    def test_pending_request_is_activated_in_the_same_write(self):
        self.assertIn("before insert or update on public.membership_requests", self.sql)
        self.assertIn("insert into public.memberships(", self.sql)
        self.assertIn("new.status := 'approved'", self.sql)
        self.assertIn("new.membership_id := v_membership.id", self.sql)
        self.assertIn("new.reviewed_at := v_now", self.sql)
        self.assertIn("new.reviewed_by := 'automatic'", self.sql)

    def test_existing_pending_requests_are_activated_during_migration(self):
        self.assertIn("update public.membership_requests", self.sql)
        self.assertIn("where status = 'pending'", self.sql)

    def test_trigger_function_is_not_a_public_rpc(self):
        self.assertIn("security invoker", self.sql)
        self.assertIn(
            "revoke all on function public.auto_activate_membership_request()\n"
            "  from public, anon, authenticated",
            self.sql,
        )


if __name__ == "__main__":
    unittest.main()
