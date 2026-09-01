import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]


EMPTY_DASHBOARD = {
    "schema_version": 2,
    "tracking_enabled_at": None,
    "attempt_tracking_enabled_at": None,
    "summary": {},
    "previous_summary": {},
    "experience_funnel": [
        {"stage": "session_started", "label": "访问", "users": 0},
        {"stage": "first_draft_submitted", "label": "提交初稿", "users": 0},
        {"stage": "report_generated", "label": "生成报告", "users": 0},
        {"stage": "report_viewed", "label": "查看报告", "users": 0},
    ],
    "guest_report_login": {"eligible_users": 0, "converted_users": 0},
    "learning_funnel": [
        {"stage": "report_viewed", "label": "查看报告", "users": 0},
        {"stage": "training_started", "label": "进入训练", "users": 0},
        {"stage": "training_completed", "label": "完成至少一项训练", "users": 0},
        {"stage": "second_draft_generated", "label": "生成二稿", "users": 0},
        {"stage": "diff_viewed", "label": "查看两稿对比", "users": 0},
    ],
    "quality": {},
    "feedback": [],
    "learning_needs": {},
    "daily": [],
    "retention": {},
    "historical": {},
    "data_quality": {},
}


class AdminDashboardAppTests(unittest.TestCase):
    def test_direct_admin_route_restores_session_then_shows_login(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        app.query_params["admin"] = "1"

        def auth_component(*_args, **kwargs):
            data = kwargs["data"]
            return SimpleNamespace(
                auth_session={
                    "status": "empty",
                    "source": "read",
                    "read_epoch": data.get("read_epoch"),
                },
                auth_wake=0,
            )

        with (
            patch("src.auth_session._AUTH_COMPONENT", side_effect=auth_component),
            patch("src.cloud_store._setting", return_value=""),
        ):
            app.run()

        self.assertEqual(len(app.exception), 0)
        self.assertIn("邮箱", [item.label for item in app.text_input])
        self.assertNotIn("EssayPilot 产品决策中心", [item.value for item in app.title])

    def test_admin_allowlist_rejects_non_admin_in_rendered_ui(self):
        script = """
import streamlit as st
import src.admin_dashboard as dashboard
st.session_state.cloud_user = {"email": "outsider@example.com"}
dashboard._setting = lambda name: "admin@example.com" if name == "ADMIN_EMAILS" else ""
dashboard.render_admin_dashboard()
"""
        app = AppTest.from_string(script, default_timeout=10).run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("无权访问统计后台" in item.value for item in app.error))

    def test_empty_dashboard_and_range_switch(self):
        script = f"""
import streamlit as st
import src.admin_dashboard as dashboard

DATA = {EMPTY_DASHBOARD!r}

class Store:
    analytics_enabled = True
    def get_analytics_dashboard_v2(self, since=None, until=None):
        st.session_state.last_dashboard_range = {{"since": since, "until": until}}
        return DATA

dashboard._authorize_admin = lambda: True
dashboard.SupabaseStore = Store
dashboard.render_admin_dashboard()
"""
        app = AppTest.from_string(script, default_timeout=15).run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("还没有新版埋点数据" in item.value for item in app.info))
        self.assertIsNotNone(app.session_state["last_dashboard_range"]["since"])

        app.segmented_control[0].set_value("全部").run()
        self.assertEqual(len(app.exception), 0)
        self.assertIsNone(app.session_state["last_dashboard_range"]["since"])
        self.assertTrue(any("当前范围：全部" in item.value for item in app.caption))
        self.assertTrue(all(
            "vs 上期" not in str(getattr(item, "delta", ""))
            for item in app.metric[:8]
        ))

    def test_membership_review_explains_missing_server_key(self):
        script = """
import src.admin_dashboard as dashboard

class Store:
    server_key = ""
    analytics_enabled = False

dashboard._authorize_admin = lambda: True
dashboard.SupabaseStore = Store
dashboard.render_admin_dashboard()
"""
        app = AppTest.from_string(script, default_timeout=10).run()

        self.assertEqual(len(app.exception), 0)
        self.assertIn(
            "3 篇训练包人工核单", [item.value for item in app.subheader]
        )
        self.assertTrue(any(
            "暂时无法读取或批准待核单申请" in item.value
            for item in app.warning
        ))

    def test_membership_review_read_failure_does_not_block_analytics(self):
        script = f"""
import streamlit as st
import src.admin_dashboard as dashboard

DATA = {EMPTY_DASHBOARD!r}

class Store:
    server_key = "sb_secret_test"
    analytics_enabled = True
    def list_pending_membership_requests(self):
        st.session_state.review_attempted = True
        raise dashboard.CloudStoreError("review unavailable")
    def get_analytics_dashboard_v2(self, since=None, until=None):
        st.session_state.analytics_loaded = True
        return DATA

dashboard._authorize_admin = lambda: True
dashboard.SupabaseStore = Store
dashboard.render_admin_dashboard()
"""
        app = AppTest.from_string(script, default_timeout=15).run()

        self.assertEqual(len(app.exception), 0)
        self.assertTrue(app.session_state["review_attempted"])
        self.assertTrue(app.session_state["analytics_loaded"])
        self.assertTrue(any(
            "下方匿名统计不受影响" in item.value for item in app.warning
        ))
        self.assertTrue(any(
            "还没有新版埋点数据" in item.value for item in app.info
        ))

    def test_membership_approval_requires_two_steps_and_shows_request_details(self):
        script = """
import streamlit as st
import src.admin_dashboard as dashboard

REQUEST = {
    "id": "10000000-0000-0000-0000-000000000001",
    "request_code": "EP-ABC123",
    "user_id": "20000000-0000-0000-0000-000000000002",
    "plan_code": "renewal_pass_30d_3runs",
    "amount_cny": 9.90,
    "currency": "CNY",
    "payment_reference": "ORDER-7788",
    "paid_at": "2026-09-01T12:30:00Z",
    "note": "微信收款",
    "created_at": "2026-09-01T12:45:00Z",
}
st.session_state.setdefault("approval_calls", [])

class Store:
    server_key = "sb_secret_test"
    analytics_enabled = False
    def list_pending_membership_requests(self):
        return [REQUEST]
    def approve_membership_request(self, request_id):
        st.session_state.approval_calls.append(request_id)
        return {"approved": True, "reason": "approved"}

dashboard._authorize_admin = lambda: True
dashboard.SupabaseStore = Store
dashboard.render_admin_dashboard()
"""
        app = AppTest.from_string(script, default_timeout=10).run()

        self.assertEqual(len(app.exception), 0)
        visible_text = [item.value for item in app.text]
        for expected in (
            "申请编号：EP-ABC123",
            "用户 ID：20000000-0000-0000-0000-000000000002",
            "申请套餐：3 篇续包",
            "应核金额：¥9.90 CNY",
            "订单号：ORDER-7788",
            "付款时间：2026-09-01 20:30",
            "备注：微信收款",
            "提交时间：2026-09-01 20:45",
        ):
            self.assertIn(expected, visible_text)
        self.assertEqual(app.session_state["approval_calls"], [])
        self.assertNotIn(
            "第二步：确认批准并开通", [button.label for button in app.button]
        )

        app = next(
            button for button in app.button if button.label == "第一步：进入核对"
        ).click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["approval_calls"], [])
        self.assertIn(
            "第二步：确认批准并开通", [button.label for button in app.button]
        )

        app = next(
            checkbox for checkbox in app.checkbox
            if "实付 ¥9.90" in checkbox.label
        ).set_value(True).run()
        self.assertEqual(app.session_state["approval_calls"], [])
        app = next(
            button for button in app.button
            if button.label == "第二步：确认批准并开通"
        ).click().run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(
            app.session_state["approval_calls"],
            ["10000000-0000-0000-0000-000000000001"],
        )
        self.assertTrue(any("已批准" in item.value for item in app.success))

    def test_three_feedback_touchpoints_can_each_submit_once(self):
        script = """
import streamlit as st
from src.product_feedback import render_product_feedback

class Store:
    enabled = True
    def record_product_feedback(self, touchpoint, session_id, helpful, reasons, dedupe_key, **kwargs):
        calls = st.session_state.setdefault("feedback_calls", [])
        calls.append({"touchpoint": touchpoint, "helpful": helpful, "reasons": reasons})
        return True

st.session_state.setdefault("flow_id", "11111111-1111-1111-1111-111111111111")
st.session_state.setdefault("visitor_hash", "a" * 64)
store = Store()
render_product_feedback(
    store, None, touchpoint="report",
    run_id="22222222-2222-2222-2222-222222222222",
    attempt_id="33333333-3333-3333-3333-333333333333",
)
render_product_feedback(
    store, None, touchpoint="training",
    run_id="22222222-2222-2222-2222-222222222222",
)
render_product_feedback(
    store, None, touchpoint="second_draft",
    run_id="22222222-2222-2222-2222-222222222222",
    attempt_id="44444444-4444-4444-4444-444444444444",
)
"""
        app = AppTest.from_string(script, default_timeout=10).run()
        self.assertEqual([button.label for button in app.button].count("有帮助"), 3)
        for _ in range(3):
            app = next(
                button for button in app.button if button.label == "有帮助"
            ).click().run()
            self.assertEqual(len(app.exception), 0)
            app = app.run()
        calls = app.session_state["feedback_calls"]
        self.assertEqual([call["touchpoint"] for call in calls], ["report", "training", "second_draft"])
        self.assertTrue(all(call["helpful"] for call in calls))


if __name__ == "__main__":
    unittest.main()
