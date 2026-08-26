"""Reusable, privacy-safe feedback UI for the three product milestones."""

from __future__ import annotations

import uuid

import streamlit as st

from src.cloud_store import CloudStoreError, CloudUser, SupabaseStore
from src.product_analytics import (
    anonymous_user_id,
    build_feedback_dedupe_key,
    validate_feedback,
)


FEEDBACK_REASON_LABELS = {
    "inaccurate": "结果看起来不准确",
    "too_generic": "建议太泛",
    "unclear": "说明不够清楚",
    "not_actionable": "不知道下一步怎么做",
    "too_slow": "等待时间太长",
    "too_long": "内容或流程太长",
    "difficulty_mismatch": "训练难度不合适",
    "progress_unclear": "没有看清具体进步",
    "other": "其他原因",
}

FEEDBACK_REASON_OPTIONS = {
    "report": (
        "inaccurate", "too_generic", "unclear", "not_actionable",
        "too_slow", "too_long", "other",
    ),
    "training": (
        "too_generic", "unclear", "not_actionable", "difficulty_mismatch",
        "too_long", "other",
    ),
    "second_draft": (
        "inaccurate", "unclear", "not_actionable", "progress_unclear",
        "too_slow", "other",
    ),
}

FEEDBACK_TITLES = {
    "report": "这份批改报告对你有帮助吗？",
    "training": "这轮专项训练对你有帮助吗？",
    "second_draft": "这份二稿对比对你有帮助吗？",
}


def render_product_feedback(
    store: SupabaseStore,
    user: CloudUser | None,
    *,
    touchpoint: str,
    run_id: str = "",
    attempt_id: str = "",
) -> None:
    """Render one aggregate-only, text-free feedback prompt per milestone."""
    session_id = str(st.session_state.get("flow_id") or "")
    anonymous_id = anonymous_user_id(str(st.session_state.get("visitor_hash") or ""))
    if not store.enabled or not session_id or (user is None and not anonymous_id):
        return
    try:
        normalized_run_id = str(uuid.UUID(run_id)) if run_id else ""
        normalized_attempt_id = str(uuid.UUID(attempt_id)) if attempt_id else ""
        dedupe_key = build_feedback_dedupe_key(
            touchpoint,
            session_id,
            run_id=normalized_run_id,
            attempt_id=normalized_attempt_id,
        )
    except (ValueError, TypeError, AttributeError):
        return

    submitted = st.session_state.setdefault("submitted_product_feedback", set())
    if dedupe_key in submitted:
        st.caption("感谢反馈，这条意见只会进入匿名聚合统计。")
        return

    widget_key = f"product_feedback_{touchpoint}_{dedupe_key[:12]}"

    def save(helpful: bool, reasons: list[str]) -> bool:
        try:
            _, normalized_helpful, normalized_reasons = validate_feedback(
                touchpoint, helpful, reasons
            )
            store.record_product_feedback(
                touchpoint,
                session_id,
                normalized_helpful,
                normalized_reasons,
                dedupe_key,
                anonymous_user_id=anonymous_id,
                run_id=normalized_run_id,
                attempt_id=normalized_attempt_id,
                user=user,
            )
        except (CloudStoreError, ValueError, TypeError, AttributeError):
            st.warning("反馈暂时没有保存成功，请稍后再试。")
            return False
        submitted.add(dedupe_key)
        st.session_state.pop(f"{widget_key}_negative", None)
        st.success("谢谢，你的反馈已匿名计入产品改进统计。")
        return True

    with st.container(border=True):
        st.markdown(f"**{FEEDBACK_TITLES[touchpoint]}**")
        st.caption("不收集作文、邮箱或自由文本；你也可以直接跳过。")
        helpful_col, unhelpful_col = st.columns(2)
        if helpful_col.button("有帮助", key=f"{widget_key}_helpful", use_container_width=True):
            save(True, [])
        if unhelpful_col.button("暂时没有", key=f"{widget_key}_unhelpful", use_container_width=True):
            st.session_state[f"{widget_key}_negative"] = True
        if st.session_state.get(f"{widget_key}_negative"):
            codes = FEEDBACK_REASON_OPTIONS[touchpoint]
            labels = [FEEDBACK_REASON_LABELS[code] for code in codes]
            selected_labels = st.multiselect(
                "主要原因（最多 3 项）",
                labels,
                max_selections=3,
                key=f"{widget_key}_reasons",
            )
            label_to_code = {FEEDBACK_REASON_LABELS[code]: code for code in codes}
            if st.button("提交原因", key=f"{widget_key}_submit", use_container_width=True):
                selected_codes = [label_to_code[label] for label in selected_labels]
                if not selected_codes:
                    st.warning("请至少选择一个原因。")
                else:
                    save(False, selected_codes)
