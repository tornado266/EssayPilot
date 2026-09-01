"""Private product dashboard, manual membership review, and route detection."""

from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from src.cloud_store import CloudStoreError, SupabaseStore
from src.product_analytics import (
    EVENT_NAMES,
    build_optimization_recommendations,
    range_start,
)


EVENT_LABELS = {
    "session_started": "会话开始", "first_draft_submitted": "提交初稿",
    "login_completed": "完成登录",
    "report_generated": "报告生成成功", "report_generation_failed": "报告生成失败",
    "report_viewed": "查看报告", "tutorial_clicked": "点击教程 / 范文",
    "problem_map_viewed": "查看问题地图", "training_started": "进入训练页",
    "sentence_training_started": "开始单句训练",
    "sentence_training_completed": "完成单句训练",
    "logic_training_completed": "完成逻辑训练", "mistake_saved": "保存错题",
    "archive_viewed": "查看学习档案", "second_draft_submitted": "提交二稿",
    "second_draft_generated": "二稿生成成功",
    "second_draft_generation_failed": "二稿生成失败",
    "diff_viewed": "查看两稿差异", "dictionary_opened": "打开学习词典",
}

FEEDBACK_LABELS = {
    "report": "批改报告",
    "training": "专项训练",
    "second_draft": "二稿对比",
}

REASON_LABELS = {
    "inaccurate": "结果不准确",
    "too_generic": "建议太泛",
    "unclear": "说明不清楚",
    "not_actionable": "不知道下一步怎么做",
    "too_slow": "等待时间太长",
    "too_long": "内容或流程太长",
    "difficulty_mismatch": "训练难度不合适",
    "progress_unclear": "进步不明显",
    "other": "其他",
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


def is_production_runtime(hostname: str = "") -> bool:
    """Require email allowlisting on hosted deployments; passwords stay local-only."""
    host = hostname.strip().lower()
    if not host:
        try:
            host = str(urlsplit(st.context.url).hostname or "").lower()
        except (AttributeError, TypeError, ValueError):
            host = ""
    configured_environment = str(os.getenv("APP_ENV", "")).strip().casefold()
    return configured_environment == "production" or host.endswith(".streamlit.app")


def _authorize_admin() -> bool:
    allowlist = parse_admin_emails(_setting("ADMIN_EMAILS"))
    cloud_user = st.session_state.get("cloud_user")
    email = str(cloud_user.get("email") or "") if isinstance(cloud_user, dict) else ""
    if allowlist:
        if admin_access_allowed(email=email, configured_admin_emails=allowlist):
            return True
        st.error("无权访问统计后台。请先用管理员白名单邮箱登录普通应用。")
        return False
    if is_production_runtime():
        st.error("生产环境必须配置 ADMIN_EMAILS 白名单；共享密码入口已禁用。")
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


def period_delta(current: float, previous: float | None, *, rate: bool = False) -> str | None:
    """Return an honest previous-period delta; all-time callers pass ``None``."""
    if previous is None:
        return None
    difference = current - previous
    if rate:
        return f"{difference:+.1%} vs 上期"
    if float(difference).is_integer():
        return f"{int(difference):+d} vs 上期"
    return f"{difference:+.2f} vs 上期"


def visible_group_rows(
    rows: object, *, count_key: str = "count", minimum: int = 5
) -> list[dict[str, object]]:
    """Apply the dashboard's last-line small-sample suppression in Python too."""
    return [
        row for row in (rows or [])
        if isinstance(row, dict) and int(row.get(count_key) or 0) >= minimum
    ]


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _rows(value: object) -> list[dict[str, object]]:
    return [item for item in (value or []) if isinstance(item, dict)]


def _duration(value: object) -> str:
    try:
        milliseconds = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return "—"
    return f"{milliseconds / 1000:.1f} 秒" if milliseconds >= 1000 else f"{milliseconds} 毫秒"


def _shanghai_time(value: object) -> str:
    if not value:
        return "尚无精确事件"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(value)


def _render_priorities(data: dict[str, object]) -> None:
    st.subheader("下一步优先优化")
    recommendations = build_optimization_recommendations(data)
    if not recommendations:
        st.info("当前没有满足样本量≥5的确定性建议；继续收集后会自动排序。")
        return
    for index, item in enumerate(recommendations, start=1):
        with st.container(border=True):
            st.markdown(f"**{index}. {item['title']}**")
            st.write(str(item["detail"]))
            st.caption(f"建议动作：{item['action']}")


def _render_summary_v2(data: dict[str, object], *, compare: bool) -> None:
    summary = _mapping(data.get("summary"))
    previous = _mapping(data.get("previous_summary")) if compare else {}

    def count(key: str) -> int:
        return int(summary.get(key) or 0)

    def delta(key: str) -> str | None:
        return period_delta(
            count(key), int(previous.get(key) or 0) if compare else None
        )

    st.subheader("用户与使用规模")
    first_row = st.columns(4)
    first_row[0].metric("注册账户总数", count("registered_users_total"), delta("registered_users_total"))
    first_row[1].metric("期间新增注册", count("registered_users_new"), delta("registered_users_new"))
    first_row[2].metric("登录活跃用户", count("authenticated_active_users"), delta("authenticated_active_users"))
    first_row[3].metric("匿名活跃访客", count("anonymous_active_visitors"), delta("anonymous_active_visitors"))
    second_row = st.columns(4)
    second_row[0].metric("全部活跃主体", count("active_users"), delta("active_users"))
    second_row[1].metric("会话数", count("sessions"), delta("sessions"))
    second_row[2].metric("初稿尝试", count("first_draft_attempts"), delta("first_draft_attempts"))
    second_row[3].metric("二稿尝试", count("second_draft_attempts"), delta("second_draft_attempts"))


def _funnel_table(rows: object) -> list[dict[str, object]]:
    stages = _rows(rows)
    rendered: list[dict[str, object]] = []
    for index, stage in enumerate(stages):
        users = int(stage.get("users") or 0)
        denominator = users if index == 0 else int(stages[index - 1].get("users") or 0)
        conversion = _rate(users, denominator)
        rendered.append({
            "阶段": str(stage.get("label") or stage.get("stage") or ""),
            "用户数": users,
            "上一步转化率": "100.0%" if index == 0 else (f"{conversion:.1%}" if denominator else "—"),
            "流失人数": 0 if index == 0 else max(0, denominator - users),
            "流失率": "—" if index == 0 or not denominator else f"{max(0.0, 1 - conversion):.1%}",
        })
    return rendered


def _render_funnels(data: dict[str, object]) -> None:
    st.subheader("两条决策漏斗")
    experience_tab, learning_tab = st.tabs(["体验漏斗", "学习闭环"])
    with experience_tab:
        st.caption("先按同一会话关联访问与提交，再按同一 attempt_id 关联生成和查看。")
        st.dataframe(_funnel_table(data.get("experience_funnel")), hide_index=True, width="stretch")
        guest_login = _mapping(data.get("guest_report_login"))
        eligible = int(guest_login.get("eligible_users") or 0)
        converted = int(guest_login.get("converted_users") or 0)
        st.metric(
            "游客报告后登录转化",
            f"{_rate(converted, eligible):.1%}" if eligible else "—",
            f"{converted} / {eligible}",
        )
    with learning_tab:
        st.caption("按同一初稿 run_id 关联报告、训练、二稿和对比，不跨作文拼接。")
        st.dataframe(_funnel_table(data.get("learning_funnel")), hide_index=True, width="stretch")


def _quality_card(label: str, item: dict[str, object]) -> None:
    attempts = int(item.get("attempts") or 0)
    successes = int(item.get("successes") or 0)
    failures = int(item.get("failures") or 0)
    with st.container(border=True):
        st.markdown(f"**{label}**")
        columns = st.columns(3)
        columns[0].metric("生成成功率", f"{_rate(successes, attempts):.1%}" if attempts else "—", f"{successes} / {attempts}")
        columns[1].metric("P50 耗时", _duration(item.get("p50_duration_ms")))
        columns[2].metric("P95 耗时", _duration(item.get("p95_duration_ms")))
        st.caption(f"成功 {successes} 次 · 失败 {failures} 次")
        failure_rows = visible_group_rows(item.get("failure_types"))
        if failure_rows:
            st.dataframe([
                {"失败类型": str(row.get("failure_type") or "unknown"), "次数": int(row.get("count") or 0)}
                for row in failure_rows
            ], hide_index=True, width="stretch")
        elif failures:
            st.caption("各失败类型样本均少于 5，已隐藏具体分布。")


def _render_quality(data: dict[str, object]) -> None:
    st.subheader("生成质量与学习结果")
    quality = _mapping(data.get("quality"))
    report_col, second_col = st.columns(2)
    with report_col:
        _quality_card("初稿报告", _mapping(quality.get("report")))
    with second_col:
        _quality_card("二稿报告", _mapping(quality.get("second_draft")))

    training = _mapping(quality.get("training"))
    outcomes = _mapping(quality.get("draft_outcomes"))
    started = int(training.get("started_users") or 0)
    completed = int(training.get("completed_users") or 0)
    eligible = int(outcomes.get("eligible_users") or 0)
    improved = int(outcomes.get("improved_users") or 0)
    metrics = st.columns(4)
    metrics[0].metric("训练掌握率", f"{_rate(completed, started):.1%}" if started else "—", f"{completed} / {started}")
    if eligible >= 5:
        metrics[1].metric("二稿提分人数占比", f"{_rate(improved, eligible):.1%}", f"{improved} / {eligible}")
        metrics[2].metric("二稿平均分差", f"{float(outcomes.get('average_band_delta') or 0):+.2f}")
        metrics[3].metric("未提分人数", max(0, eligible - improved))
    else:
        metrics[1].metric("二稿提分人数占比", "样本不足")
        metrics[2].metric("二稿平均分差", "—")
        metrics[3].metric("有效二稿样本", eligible)


def _render_feedback(data: dict[str, object]) -> None:
    st.subheader("三个关键节点反馈")
    feedback = {str(row.get("touchpoint") or ""): row for row in _rows(data.get("feedback"))}
    for column, touchpoint in zip(st.columns(3), ("report", "training", "second_draft"), strict=False):
        row = _mapping(feedback.get(touchpoint))
        responses = int(row.get("responses") or 0)
        respondent_users = int(row.get("respondent_users") or 0)
        helpful = int(row.get("helpful") or 0)
        eligible = int(row.get("eligible_users") or 0)
        with column:
            with st.container(border=True):
                st.markdown(f"**{FEEDBACK_LABELS[touchpoint]}**")
                st.metric("反馈回收率", f"{_rate(respondent_users, eligible):.1%}" if eligible else "—", f"{responses} 份")
                if responses >= 5:
                    st.metric("有帮助率", f"{_rate(helpful, responses):.1%}", f"{helpful} / {responses}")
                    reasons = visible_group_rows(row.get("reason_counts"))
                    if reasons:
                        st.caption("主要负向原因")
                        for reason in reasons[:3]:
                            code = str(reason.get("reason_code") or "other")
                            st.write(f"· {REASON_LABELS.get(code, code)}：{int(reason.get('count') or 0)}")
                else:
                    st.caption("样本少于 5，不展示满意度和原因分布。")


def _render_learning_needs(data: dict[str, object]) -> None:
    st.subheader("用户最需要解决什么")
    needs = _mapping(data.get("learning_needs"))
    st.caption("来自已登录用户的聚合报告结果；任一分组少于 5 时隐藏。")
    groups = (
        ("主要薄弱维度", "criteria"),
        ("主要改进动作", "action_types"),
        ("主要题材分布", "topics"),
    )
    for column, (label, key) in zip(st.columns(3), groups, strict=False):
        rows = visible_group_rows(needs.get(key))
        with column:
            st.markdown(f"**{label}**")
            if rows:
                st.dataframe([
                    {"分组": str(row.get("key") or "未分类"), "人次": int(row.get("count") or 0)}
                    for row in rows[:8]
                ], hide_index=True, width="stretch")
            else:
                st.caption("尚无样本量≥5的分组。")


def _render_trends_and_retention(data: dict[str, object]) -> None:
    st.subheader("趋势与留存")
    daily = _rows(data.get("daily"))
    if daily:
        frame = pd.DataFrame(daily).rename(columns={
            "day": "日期", "active_users": "活跃主体", "reports": "成功报告", "failures": "生成失败",
        })
        st.line_chart(frame, x="日期", y=["活跃主体", "成功报告", "生成失败"])
    else:
        st.info("所选范围内还没有新版埋点数据。")
    retention = _mapping(data.get("retention"))
    columns = st.columns(2)
    for column, key, label in zip(columns, ("day_1", "day_7"), ("次日留存", "7 日留存"), strict=False):
        item = _mapping(retention.get(key))
        eligible = int(item.get("eligible_users") or 0)
        retained = int(item.get("retained_users") or 0)
        column.metric(label, f"{_rate(retained, eligible):.1%}" if eligible else "—", f"{retained} / {eligible} 个成熟 cohort")


def _render_history_and_health(data: dict[str, object]) -> None:
    with st.expander("历史业务量（不与新版精确漏斗混用）"):
        historical = _mapping(data.get("historical"))
        columns = st.columns(4)
        columns[0].metric("历史批改用户", int(historical.get("unique_users") or 0))
        columns[1].metric("历史成功报告", int(historical.get("successful_reports") or 0))
        columns[2].metric("历史训练开始用户", int(historical.get("training_started_users") or 0))
        columns[3].metric("历史二稿用户", int(historical.get("second_draft_users") or 0))
        st.caption("历史表只回溯可验证的业务量，不补造浏览、失败、反馈或精确漏斗。")
    with st.expander("数据健康"):
        health = _mapping(data.get("data_quality"))
        columns = st.columns(4)
        columns[0].metric("范围内事件", int(health.get("events_total") or 0))
        columns[1].metric("带尝试编号事件", int(health.get("attempt_linked_events") or 0))
        columns[2].metric("未关联尝试编号的事件", int(health.get("events_without_attempt_id") or 0))
        columns[3].metric("缺失 attempt_id 的结果事件", int(health.get("missing_attempt_outcomes") or 0))
        st.caption(f"尝试编号精确统计启用时间：{_shanghai_time(data.get('attempt_tracking_enabled_at'))}（Asia/Shanghai）")


def _render_membership_review(store: SupabaseStore) -> None:
    """Render manual founder-pass review without blocking aggregate analytics."""
    st.subheader("创始体验包人工核单")
    st.caption("仅处理待审核申请。请以收款渠道记录为准，不要仅依据用户备注批准。")
    if not getattr(store, "server_key", ""):
        st.warning(
            "尚未配置 SUPABASE_SECRET_KEY（或旧版 SUPABASE_SERVICE_ROLE_KEY），"
            "暂时无法读取或批准待核单申请。"
        )
        return

    try:
        pending_requests = _rows(store.list_pending_membership_requests())
    except CloudStoreError:
        st.warning("待核单列表暂时无法读取，请稍后重试；下方匿名统计不受影响。")
        return

    if not pending_requests:
        st.info("当前没有待审核的创始体验包申请。")
        return

    st.caption(f"待审核 {len(pending_requests)} 笔 · 时间均按 Asia/Shanghai 显示")
    selected_key = "membership_review_selected_request_id"
    selected_request_id = str(st.session_state.get(selected_key) or "")
    for request in pending_requests:
        request_id = str(request.get("id") or "")
        request_code = str(request.get("request_code") or "—")
        amount = float(request.get("amount_cny") or 7.50)
        currency = str(request.get("currency") or "CNY")
        with st.container(border=True):
            st.text(f"申请编号：{request_code}")
            st.text(f"用户 ID：{str(request.get('user_id') or '—')}")
            st.text(f"应核金额：¥{amount:.2f} {currency}")
            st.text(f"订单号：{str(request.get('payment_reference') or '—')}")
            st.text(f"付款时间：{_shanghai_time(request.get('paid_at'))}")
            st.text(f"备注：{str(request.get('note') or '（无）')}")
            st.text(f"提交时间：{_shanghai_time(request.get('created_at'))}")

            if not request_id:
                st.error("这条申请缺少内部编号，无法审批。")
                continue
            if st.button(
                "第一步：进入核对",
                key=f"membership_review_prepare_{request_id}",
                use_container_width=True,
            ):
                selected_request_id = request_id
                st.session_state[selected_key] = request_id

            if selected_request_id != request_id:
                continue
            st.warning("批准后会立即开始 30 天有效期并发放 3 篇完整训练额度。")
            confirmed = st.checkbox(
                "我已在收款记录中核对：订单号一致、实付 ¥7.50、付款时间合理。",
                key=f"membership_review_confirm_{request_id}",
            )
            if st.button(
                "第二步：确认批准并开通",
                key=f"membership_review_approve_{request_id}",
                type="primary",
                disabled=not confirmed,
                use_container_width=True,
            ):
                try:
                    result = store.approve_membership_request(request_id)
                except CloudStoreError:
                    st.error("审批请求暂时失败，未确认开通；请刷新后核对状态再重试。")
                else:
                    if isinstance(result, dict) and result.get("approved"):
                        st.success(f"申请 {request_code} 已批准，30 天有效期现已开始。")
                        st.session_state[selected_key] = ""
                    else:
                        reason = str((result or {}).get("reason") or "unknown")
                        st.error(f"本次未开通，请刷新申请状态后再处理（{reason}）。")


def render_admin_dashboard() -> None:
    """Render authorized manual review and aggregate product analytics."""
    st.title("EssayPilot 产品决策中心")
    if not _authorize_admin():
        return
    store = SupabaseStore()
    _render_membership_review(store)
    st.divider()
    st.subheader("匿名产品统计")
    if not store.analytics_enabled:
        st.warning(
            "请配置 SUPABASE_SECRET_KEY（或旧版 SUPABASE_SERVICE_ROLE_KEY），"
            "并先执行产品统计迁移。"
        )
        return
    selected_range = st.segmented_control(
        "时间范围", ["近7天", "近30天", "全部"], default="近30天", key="analytics_range"
    ) or "近30天"
    days = {"近7天": 7, "近30天": 30, "全部": None}[selected_range]
    until = datetime.now(timezone.utc)
    since = range_start(days, until)
    try:
        data = store.get_analytics_dashboard_v2(
            since.isoformat() if since else None,
            until.isoformat(),
        )
    except CloudStoreError as exc:
        st.warning(f"新版聚合接口暂不可用，已回退到旧版业务量视图：{exc}")
        try:
            legacy = store.get_analytics_dashboard(since.isoformat() if since else None)
        except CloudStoreError as legacy_exc:
            st.error(f"暂时无法读取聚合统计：{legacy_exc}")
            return
        _render_tracking_metrics(legacy)
        st.divider()
        _render_historical(legacy)
        return
    if int(data.get("schema_version") or 0) < 2:
        st.error("统计接口版本不匹配，请先应用 20260826 决策统计迁移。")
        return
    st.caption(
        f"当前范围：{selected_range} · 时区：Asia/Shanghai · "
        f"埋点启用：{_shanghai_time(data.get('tracking_enabled_at'))} · "
        "本区仅展示匿名聚合结果"
    )
    _render_priorities(data)
    st.divider()
    _render_summary_v2(data, compare=days is not None)
    st.divider()
    _render_funnels(data)
    st.divider()
    _render_quality(data)
    st.divider()
    _render_feedback(data)
    st.divider()
    _render_learning_needs(data)
    st.divider()
    _render_trends_and_retention(data)
    st.divider()
    _render_history_and_health(data)
