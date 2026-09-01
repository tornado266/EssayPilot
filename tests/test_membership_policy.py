import unittest
from decimal import Decimal

from src.membership import (
    FOUNDER_OFFER,
    RENEWAL_OFFER,
    action_reason_message,
    entitlement_caption,
    normalize_entitlement,
    offer_for_entitlement,
)


class MembershipPolicyTests(unittest.TestCase):
    def test_founder_offer_is_the_frozen_limited_beta(self):
        self.assertEqual(FOUNDER_OFFER.price_cny, Decimal("7.5"))
        self.assertEqual(FOUNDER_OFFER.duration_days, 30)
        self.assertEqual(FOUNDER_OFFER.run_quota, 3)
        self.assertEqual(FOUNDER_OFFER.training_limit_per_run, 3)
        self.assertEqual(FOUNDER_OFFER.second_draft_limit_per_run, 1)
        self.assertFalse(FOUNDER_OFFER.auto_renews)
        self.assertFalse(FOUNDER_OFFER.expression_ai_included)

    def test_renewal_offer_keeps_scope_but_costs_9_9(self):
        self.assertEqual(RENEWAL_OFFER.price_cny, Decimal("9.9"))
        self.assertEqual(RENEWAL_OFFER.duration_days, 30)
        self.assertEqual(RENEWAL_OFFER.run_quota, 3)
        self.assertEqual(
            RENEWAL_OFFER.training_limit_per_run,
            FOUNDER_OFFER.training_limit_per_run,
        )
        self.assertEqual(
            RENEWAL_OFFER.second_draft_limit_per_run,
            FOUNDER_OFFER.second_draft_limit_per_run,
        )
        self.assertFalse(RENEWAL_OFFER.auto_renews)

    def test_server_history_selects_first_or_renewal_copy(self):
        self.assertIs(offer_for_entitlement({"purchase_count": 0}), FOUNDER_OFFER)
        self.assertIs(offer_for_entitlement({"purchase_count": 1}), RENEWAL_OFFER)
        self.assertIs(
            offer_for_entitlement({"next_plan_code": RENEWAL_OFFER.plan_code}),
            RENEWAL_OFFER,
        )

    def test_missing_renewal_eligibility_fails_closed(self):
        legacy_expired = normalize_entitlement(
            {"active": False, "status": "expired", "purchase_count": 1}
        )
        self.assertFalse(legacy_expired["can_purchase"])
        self.assertFalse(legacy_expired["server_offer_verified"])
        self.assertFalse(
            normalize_entitlement(
                {"status": "expired", "purchase_count": 1, "can_purchase": "true"}
            )["can_purchase"]
        )
        migrated_expired = normalize_entitlement(
            {
                "active": False,
                "status": "expired",
                "purchase_count": 1,
                "can_purchase": True,
                "next_plan_code": RENEWAL_OFFER.plan_code,
                "next_amount_cny": "9.90",
            }
        )
        self.assertTrue(migrated_expired["can_purchase"])
        self.assertTrue(migrated_expired["server_offer_verified"])
        self.assertEqual(migrated_expired["next_amount_cny"], Decimal("9.9"))

    def test_partial_or_mismatched_server_offer_never_verifies(self):
        partial = normalize_entitlement(
            {"purchase_count": 1, "can_purchase": True}
        )
        self.assertTrue(partial["can_purchase"])
        self.assertFalse(partial["server_offer_verified"])
        wrong_price = normalize_entitlement(
            {
                "purchase_count": 1,
                "can_purchase": True,
                "next_plan_code": RENEWAL_OFFER.plan_code,
                "next_amount_cny": "7.50",
            }
        )
        self.assertFalse(wrong_price["server_offer_verified"])

    def test_normalize_entitlement_defaults_to_no_access(self):
        entitlement = normalize_entitlement(None)
        self.assertFalse(entitlement["active"])
        self.assertEqual(entitlement["runs_remaining"], 0)
        self.assertEqual(entitlement["run_quota"], 3)

    def test_caption_discloses_limit_expiry_and_no_renewal(self):
        caption = entitlement_caption(
            {
                "active": True,
                "status": "active",
                "expires_at": "2026-10-01T12:00:00+00:00",
                "run_quota": 3,
                "runs_remaining": 2,
            }
        )
        self.assertIn("剩余 2/3 篇", caption)
        self.assertIn("2026-10-01", caption)
        self.assertIn("不自动续费", caption)

    def test_server_reason_is_translated_for_the_learner(self):
        self.assertIn("3 次", action_reason_message("training_limit_reached"))
        self.assertNotIn("reservation", action_reason_message("reservation_conflict"))

    def test_free_caption_discloses_browser_scope(self):
        caption = entitlement_caption({"active": False, "status": "none"})
        self.assertIn("当前浏览器", caption)

    def test_inactive_statuses_keep_history_message(self):
        for status in ("expired", "refunded", "revoked"):
            caption = entitlement_caption({"active": False, "status": status})
            self.assertIn("已生成内容仍可查看", caption)


if __name__ == "__main__":
    unittest.main()
