"""Private developer dashboard and robust Streamlit route detection."""

import hmac
import math
import os
from urllib.parse import parse_qs, urlsplit

import streamlit as st

from src.cloud_store import CloudStoreError, SupabaseStore


MINI_PROGRAM_USER_TARGET = 30
MINI_PROGRAM_COMPLETION_TARGET = 0.30


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
        request_url = st.context.url
        return _contains_admin_flag(parse_qs(urlsplit(request_url).query).get("admin"))
    except (AttributeError, TypeError, ValueError):
        return False


def _admin_password() -> str | None:
    """Read the admin password from Streamlit Secrets, with a local env fallback."""
    try:
        value = st.secrets["ADMIN_PASSWORD"]
    except (FileNotFoundError, KeyError):
        value = os.getenv("ADMIN_PASSWORD")
    return str(value) if value not in (None, "") else None


def render_admin_dashboard() -> None:
    """Render password protection followed by anonymous beta-funnel metrics."""
    st.title("EssayPilot 公开内测看板")
    expected_password = _admin_password()
    if not expected_password:
        st.error("尚未在 Streamlit Secrets 中配置 ADMIN_PASSWORD。")
        return

    if not st.session_state.get("admin_authenticated"):
        password = st.text_input("管理员密码", type="password")
        if not password:
            st.info("请输入管理员密码，查看匿名使用统计。")
            return
        if not hmac.compare_digest(password, expected_password):
            st.error("管理员密码不正确。")
            return
        st.session_state.admin_authenticated = True

    store = SupabaseStore()
    if not store.funnel_enabled:
        st.warning(
            "公开内测统计尚未启用。请在 Streamlit Secrets 配置 "
            "SUPABASE_SERVICE_ROLE_KEY 和 BETA_START_AT。普通用户功能不受影响。"
        )
        return
    try:
        funnel = store.get_beta_funnel()
    except CloudStoreError as exc:
        st.error(f"暂时无法读取匿名漏斗：{exc}")
        return

    first = int(funnel.get("first_grading_users") or 0)
    sentence = int(funnel.get("sentence_mastered_users") or 0)
    logic = int(funnel.get("logic_mastered_users") or 0)
    both = int(funnel.get("both_mastered_users") or 0)
    draft_2 = int(funnel.get("second_draft_users") or 0)

    def conversion(value: int) -> float:
        return value / first if first else 0.0

    st.caption(f"统计起点：{funnel.get('since') or store.beta_start_at} · 仅显示匿名聚合数据")
    first_row = st.columns(3)
    first_row[0].metric("首次批改用户", first)
    first_row[1].metric("单句训练掌握", sentence, f"{conversion(sentence):.0%}")
    first_row[2].metric("逻辑训练掌握", logic, f"{conversion(logic):.0%}")
    second_row = st.columns(2)
    second_row[0].metric("两项训练均掌握", both, f"{conversion(both):.0%}")
    second_row[1].metric("已提交第二稿", draft_2, f"{conversion(draft_2):.0%}")

    st.subheader("小程序启动门槛")
    completion_rate = conversion(both)
    required_completions = max(
        math.ceil(MINI_PROGRAM_USER_TARGET * MINI_PROGRAM_COMPLETION_TARGET),
        math.ceil(first * MINI_PROGRAM_COMPLETION_TARGET),
    )
    users_needed = max(0, MINI_PROGRAM_USER_TARGET - first)
    completions_needed = max(0, required_completions - both)
    gate_ready = (
        first >= MINI_PROGRAM_USER_TARGET
        and completion_rate >= MINI_PROGRAM_COMPLETION_TARGET
    )
    if gate_ready:
        st.success("已达到门槛：可以开始单独规划微信小程序架构。")
    else:
        st.info(
            f"还需 {users_needed} 名首次批改用户；按当前样本规模，还需 "
            f"{completions_needed} 名用户掌握两项训练。"
        )
    st.progress(min(first / MINI_PROGRAM_USER_TARGET, 1.0), text=f"用户门槛：{first} / 30")
    st.progress(
        min(completion_rate / MINI_PROGRAM_COMPLETION_TARGET, 1.0),
        text=f"训练完成率：{completion_rate:.1%} / 30.0%",
    )

    st.subheader("每日匿名趋势")
    daily_rows = [
        {
            "日期": row.get("day", ""),
            "首次批改": row.get("first_grading_users", 0),
            "单句掌握": row.get("sentence_mastered_users", 0),
            "逻辑掌握": row.get("logic_mastered_users", 0),
            "两项均掌握": row.get("both_mastered_users", 0),
            "第二稿": row.get("second_draft_users", 0),
        }
        for row in (funnel.get("daily") or [])
    ]
    if daily_rows:
        st.dataframe(daily_rows, width="stretch", hide_index=True)
    else:
        st.info("新版上线后还没有完成首次批改的用户。")
