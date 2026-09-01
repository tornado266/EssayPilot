import unittest
from decimal import Decimal

from src.membership import (
    FOUNDER_OFFER,
    action_reason_message,
    entitlement_caption,
    normalize_entitlement,
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
