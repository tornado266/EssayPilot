"""Private aggregate product dashboard and robust Streamlit route detection."""

from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import streamlit as st

from src.cloud_store import CloudStoreError, SupabaseStore
from src.product_analytics import EVENT_NAMES, range_start


EVENT_LABELS = {
    "session_started": "会话开始", "first_draft_submitted": "提交初稿",
    "report_generated": "报告生成成功", "report_generation_failed": "报告生成失败",
    "report_viewed": "查看报告", "tutorial_clicked": "点击教程 / 范文",
    "problem_map_viewed": "查看问题地图", "training_started": "进入训练页",
    "sentence_training_started": "开始单句训练",
    "sentence_training_completed": "完成单句训练", "mistake_saved": "保存错题",
    "archive_viewed": "查看学习档案", "second_draft_submitted": "提交二稿",
    "diff_viewed": "查看两稿差异", "dictionary_opened": "打开学习词典",
}


def _contains_admin_flag(value: object) -> bool:
    if isinstance(value, (list, tuple)):
        return any(str(item) == "1" for item in value)
    return str(value) == "1"


def is_admin_request() -> bool:
    """Detect ?admin=1 across current and legacy Streamlit query APIs."""
    try:
        if _contains_admin_flag(st.query_params.get("admin")):
            return True
    except (AttributeError, KeyError, TypeError):
        pass
    legacy_reader = getattr(st, "experimental_get_query_params", None)
    if callable(legacy_reader):
        try:
            if _contains_admin_flag(legacy_reader().get("admin")):
                return True
        except (AttributeError, KeyError, TypeError):
            pass
    try:
        return _contains_admin_flag(parse_qs(urlsplit(st.context.url).query).get("admin"))
    except (AttributeError, TypeError, ValueError):
        return False


def _setting(name: str) -> str:
    try:
        value = st.secrets.get(name, "")
    except (FileNotFoundError, KeyError):
        value = ""
    return str(value or os.getenv(name, "")).strip()


def parse_admin_emails(value: str) -> set[str]:
    """Parse a comma/semicolon/newline separated administrator allowlist."""
    normalized = value.replace(";", ",").replace("\n", ",")
    return {item.strip().casefold() for item in normalized.split(",") if item.strip()}


def admin_access_allowed(
    *, email: str = "", configured_admin_emails: set[str] | None = None,
    password: str = "", expected_password: str = "",
) -> bool:
    """Pure authorization rule: allowlist first, password only as fallback."""
    allowlist = configured_admin_emails or set()
    if allowlist:
        return bool(email and email.strip().casefold() in allowlist)
    return bool(expected_password and password and hmac.compare_digest(password, expected_password))


def _authorize_admin() -> bool:
    allowlist = parse_admin_emails(_setting("ADMIN_EMAILS"))
    cloud_user = st.session_state.get("cloud_user")
    email = str(cloud_user.get("email") or "") if isinstance(cloud_user, dict) else ""
    if allowlist:
        if admin_access_allowed(email=email, configured_admin_emails=allowlist):
            return True
        st.error("无权访问统计后台。请先用管理员白名单邮箱登录普通应用。")
        return False
    expected_password = _setting("ADMIN_PASSWORD")
    if not expected_password:
        st.error("尚未配置 ADMIN_EMAILS 或 ADMIN_PASSWORD；统计后台已拒绝访问。")
        return False
    if st.session_state.get("admin_authenticated"):
        return True
    password = st.text_input("管理员密码", type="password")
    if not password:
        st.info("请输入管理员密码。")
        return False
    if not admin_access_allowed(password=password, expected_password=expected_password):
        st.error("管理员密码不正确。")
        return False
    st.session_state.admin_authenticated = True
    return True


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _render_tracking_metrics(data: dict[str, object]) -> None:
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    users = int(summary.get("unique_users") or 0)
    new_users = int(summary.get("new_users") or 0)
    sessions = int(summary.get("sessions") or 0)
    first_drafts = int(summary.get("first_drafts") or 0)
    successes = int(summary.get("successful_reports") or 0)
    failures = int(summary.get("failed_reports") or 0)
    row_one = st.columns(3)
    row_one[0].metric("独立用户", users)
    row_one[1].metric("新用户", new_users)
    row_one[2].metric("会话数", sessions)
    row_two = st.columns(4)
    row_two[0].metric("初稿提交", first_drafts)
    row_two[1].metric("成功报告", successes)
    row_two[2].metric("报告失败率", f"{_rate(failures, successes + failures):.1%}")
    row_two[3].metric("人均批改", f"{_rate(successes, users):.2f}")

    usage_rows = {
        str(item.get("event_name")): item for item in (data.get("event_usage") or [])
        if isinstance(item, dict)
    }
    st.subheader("功能使用")
    st.dataframe([
        {"功能": EVENT_LABELS[name],
         "使用次数": int(usage_rows.get(name, {}).get("event_count") or 0),
         "独立使用人数": int(usage_rows.get(name, {}).get("user_count") or 0)}
        for name in EVENT_NAMES
    ], hide_index=True, width="stretch")

    funnel = data.get("funnel") if isinstance(data.get("funnel"), dict) else {}
    first = int(funnel.get("first_draft_submitted") or 0)
    report = int(funnel.get("report_viewed") or 0)
    training = int(funnel.get("training_started") or 0)
    completed = int(funnel.get("sentence_training_completed") or 0)
    second = int(funnel.get("second_draft_submitted") or 0)
    funnel_rows = []
    for label, value, denominator in (
        ("提交初稿", first, first), ("查看报告", report, first),
        ("进入训练", training, report), ("完成单句训练", completed, training),
        ("提交二稿（相对进入训练）", second, training),
    ):
        conversion = _rate(value, denominator)
        funnel_rows.append({"步骤": label, "独立用户": value,
                            "转化率": f"{conversion:.1%}",
                            "流失率": f"{1 - conversion:.1%}" if denominator else "—"})
    st.subheader("核心漏斗")
    st.caption("核心完成率 = 完成“提交初稿 → 查看报告 → 进入训练 → 提交二稿”的独立用户数 / 初稿独立用户数。")
    st.metric("核心完成用户 / 完成率", f"{second} / {first}", f"{_rate(second, first):.1%}")
    st.dataframe(funnel_rows, hide_index=True, width="stretch")

    daily = [row for row in (data.get("daily") or []) if isinstance(row, dict)]
    st.subheader("每日趋势")
    if daily:
        frame = pd.DataFrame(daily).rename(columns={"day": "日期", "active_users": "活跃用户", "gradings": "批改量"})
        st.line_chart(frame, x="日期", y=["活跃用户", "批改量"], color=["#0B4F8A", "#4D9BE6"])
    else:
        st.info("所选范围内还没有埋点数据。")

    retention = data.get("retention") if isinstance(data.get("retention"), dict) else {}
    retention_cols = st.columns(2)
    for column, key, label in zip(retention_cols, ("day_1", "day_7"), ("次日复访率", "7 日复访率"), strict=False):
        item = retention.get(key) if isinstance(retention.get(key), dict) else {}
        eligible = int(item.get("eligible_users") or 0)
        retained = int(item.get("retained_users") or 0)
        column.metric(label, f"{_rate(retained, eligible):.1%}", f"{retained} / {eligible} 个成熟 cohort 用户")


def _render_historical(data: dict[str, object]) -> None:
    historical = data.get("historical") if isinstance(data.get("historical"), dict) else {}
    st.subheader("历史记录推算")
    st.caption("来自作文、报告、训练和二稿业务表，可可靠回溯；报告查看、会话、失败、教程、问题地图、词典和档案浏览无法回填。")
    rows = st.columns(4)
    rows[0].metric("历史独立批改用户", int(historical.get("unique_users") or 0))
    rows[1].metric("历史成功报告", int(historical.get("successful_reports") or 0))
    rows[2].metric("历史训练开始用户", int(historical.get("training_started_users") or 0))
    rows[3].metric("历史二稿用户", int(historical.get("second_draft_users") or 0))
    st.caption(f"初稿 {int(historical.get('first_drafts') or 0)} · "
               f"训练完成人数 {int(historical.get('training_completed_users') or 0)} · "
               f"二稿记录 {int(historical.get('second_drafts') or 0)}")


def render_admin_dashboard() -> None:
    """Render the authorized aggregate-only product dashboard."""
    st.title("EssayPilot 产品数据")
    if not _authorize_admin():
        return
    store = SupabaseStore()
    if not store.analytics_enabled:
        st.warning("请配置 SUPABASE_SERVICE_ROLE_KEY，并先执行产品统计迁移。")
        return
    selected_range = st.segmented_control(
        "时间范围", ["近7天", "近30天", "全部"], default="近30天", key="analytics_range"
    ) or "近30天"
    days = {"近7天": 7, "近30天": 30, "全部": None}[selected_range]
    since = range_start(days, datetime.now(timezone.utc))
    try:
        data = store.get_analytics_dashboard(since.isoformat() if since else None)
    except CloudStoreError as exc:
        st.error(f"暂时无法读取聚合统计：{exc}")
        return
    tracking_started = str(data.get("tracking_enabled_at") or "")
    st.caption(f"当前范围：{selected_range} · 埋点启用：{tracking_started[:19].replace('T', ' ') or '尚无事件'} · 默认仅展示聚合结果")
    st.subheader("埋点启用后数据")
    _render_tracking_metrics(data)
    st.divider()
    _render_historical(data)
