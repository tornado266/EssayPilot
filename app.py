"""Streamlit app entry point for the IELTS Writing Correction Skill."""

import base64
import binascii
import hashlib
import html
import json
import logging
import os
import re
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

import streamlit as st
from dotenv import load_dotenv

from src.ai_grader import (
    AIGraderError,
    PRODUCTION_MODEL,
    build_client,
    compare_draft_progress,
    get_provider_config,
    grade_essay_package,
    grade_scoring_decision,
    review_logic_rewrite,
    review_sentence_rewrite,
)
from src.admin_dashboard import is_admin_request, render_admin_dashboard
from src.auth_session import (
    AUTH_BROWSER_COMMAND_KEY,
    AUTH_BROWSER_RECOVERY_KEY,
    AUTH_BROWSER_READ_EPOCH_KEY,
    AUTH_PERSIST_WARNING_KEY,
    AUTH_BROWSER_VERSION_KEY,
    AUTH_LISTENER_RERUN_KEY,
    AUTH_LOGOUT_PENDING_KEY,
    AUTH_RECOVERY_STATE_KEY,
    AUTH_REQUEST_RERUN_KEY,
    AUTH_USER_VERSION_KEY,
    acknowledge_browser_command,
    begin_logout,
    browser_ack_needs_listener_rerun,
    browser_bootstrap_transition,
    browser_signaled_logout,
    browser_refresh_session,
    cloud_user_from_state,
    cloud_user_to_state,
    consume_auth_request_rerun,
    mark_browser_listener_stable,
    parse_persisted_refresh_session,
    queue_refresh_token_clear,
    queue_refresh_token_write,
    resolve_auth_session,
    start_logout_with_remote_best_effort,
    take_browser_command,
)
from src.analytics import record_grading_event
from src.cloud_store import CloudStoreError, CloudUser, SupabaseStore
from src.dictionary_provider import DictionaryProvider, get_default_dictionary_provider
from src.demo_package import DemoPackageError, load_demo_package
from src.draft_training import list_draft_training_history, save_draft_training_record
from src.error_book import append_error_book
from src.learning_assets import (
    CATEGORY_LABELS,
    EXPRESSION_VIEW_CURATED,
    EXPRESSION_VIEW_PRACTICE,
    EXPRESSION_VIEW_REPORT,
    build_learning_items,
    catalog_learning_item,
    expression_status_label,
    report_expression_items,
    resolve_expression_view,
)
from src.membership import (
    FOUNDER_OFFER,
    RENEWAL_OFFER,
    action_reason_message,
    entitlement_caption,
    normalize_entitlement,
    offer_for_entitlement,
)
from src.issue_map import (
    CRITERION_LABELS as ISSUE_MAP_CRITERION_LABELS,
    build_issue_map_html,
    correction_issue_type,
    grouped_corrections,
    learning_replacements,
    map_essay_issues,
    report_essay_from_state,
)
from src.expression_catalog import FUNCTION_LABELS, TOPIC_LABELS, load_expression_catalog
from src.home_dashboard import build_home_summary
from src.share_card import build_result_card_svg
from src.problem_spans import contextual_collocation, highlight_problem_text
from src.product_analytics import (
    anonymous_user_id,
    build_dedupe_key,
    record_event_safely,
    sanitize_metadata,
)
from src.product_feedback import render_product_feedback
from src.vocabulary_cards import build_vocabulary_cards_html, report_vocabulary_items
from src.storage import markdown_to_pdf, save_markdown_record
from src.report_schema import (
    ExaminerResultError,
    REPORT_PROMPT_VERSION,
    SCORING_PROMPT_VERSION,
    SCORING_SKILL_VERSION,
    calculate_overall,
    format_overall_band,
    learner_safe_report_markdown,
    score_snapshot,
    submission_hash,
)
from src.text_utils import count_words, word_count_warning
from src.topic_bank import (
    QUESTION_TYPE_LABELS,
    TopicBankError,
    apply_topic_selection,
    filter_topics_by_category,
    load_topic_bank,
)
from src.visitor_identity import browser_visitor_id, visitor_hash
from ui.alpine import (
    inject_alpine_theme,
    render_guest_home_intro,
    render_home_action_card,
    render_home_heading,
    render_home_preview_link,
    render_hero as render_alpine_hero,
    render_scoring_loader,
    paragraph_diff_html,
    render_text_diff,
    render_training_stepper,
)


load_dotenv()

BASE_DIR = Path(__file__).parent
SCORE_PATTERN = re.compile(r"(?:最可能分数|Likely Score|Overall Band Score|Overall Band|Overall|总分|likely score)[^\d]*(\d(?:\.\d)?)")
CRITERION_DISPLAY_NAMES = {
    "Task Response": "任务回应（TR）",
    "Coherence and Cohesion": "连贯与衔接（CC）",
    "Lexical Resource": "词汇资源（LR）",
    "Grammatical Range and Accuracy": "语法多样性与准确性（GRA）",
}
CRITERION_COMPACT_NAMES = {
    "Task Response": "TR 任务回应",
    "Coherence and Cohesion": "CC 连贯衔接",
    "Lexical Resource": "LR 词汇资源",
    "Grammatical Range and Accuracy": "GRA 语法准确性",
}
SCORE_DISPLAY_NAMES = {
    "Overall Band": "总分",
    "Task Response": "任务回应（TR）",
    "Coherence & Cohesion": "连贯与衔接（CC）",
    "Lexical Resource": "词汇资源（LR）",
    "Grammar Range & Accuracy": "语法多样性与准确性（GRA）",
}
CHART_CRITERION_DOMAIN = list(CRITERION_COMPACT_NAMES.values())
ALPINE_CHART_COLORS = ["#0B2545", "#00796B", "#C45100", "#A52464"]
ALPINE_CHART_DASHES = [[1, 0], [9, 4], [3, 3], [11, 3, 2, 3]]
ALPINE_CHART_SHAPES = ["circle", "square", "diamond", "triangle-up"]
SAMPLE_POPOVER_TITLE = "试用作文"
LOGGER = logging.getLogger(__name__)
MEMBERSHIP_CACHE_SECONDS = 20


class GradingAccessError(RuntimeError):
    """A learner-facing access decision raised before any model request."""

    def __init__(self, reason: str, *, existing_run_id: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.existing_run_id = existing_run_id


class GradingSettlementError(RuntimeError):
    """A valid report exists, but its paid reservation is not settled yet."""


def load_sample_essay() -> bool:
    """Load the validated static example into the writing fields."""
    try:
        package = load_demo_package()
    except DemoPackageError:
        st.session_state.demo_load_error = True
        return False
    st.session_state.topic_input = package.question
    st.session_state.essay_input = package.essay
    st.session_state.pop("demo_load_error", None)
    return True


def show_workspace() -> None:
    """Return to the live grading workspace."""
    st.session_state.page_mode = "write"
    st.query_params["page"] = "write"
    st.session_state.scroll_target = "workspace-top"


def show_demo() -> None:
    """Open the zero-token walkthrough."""
    st.session_state.page_mode = "demo"
    st.query_params["page"] = "demo"
    st.query_params.pop("run_id", None)
    st.query_params.pop("mode", None)
    st.session_state.scroll_target = "demo-top"
    st.session_state.tutorial_clicked_pending = True


def load_sample_and_show_workspace() -> None:
    """Load the sample without running the grader and return to the workspace."""
    if not load_sample_essay():
        return
    st.session_state.page_mode = "write"
    st.query_params["page"] = "write"
    st.session_state.scroll_target = "writing-input"


def select_topic_from_bank(topic: dict[str, str]) -> None:
    """Select a local practice topic, asking first when an essay already exists."""
    result = apply_topic_selection(st.session_state, topic)
    st.session_state.topic_bank_expanded = result == "confirmation_required"
    if result == "selected":
        st.session_state.topic_selection_notice = True
        st.session_state.scroll_target = "writing-input"


def confirm_pending_topic_selection() -> None:
    """Keep the existing essay and apply the topic chosen in the bank."""
    pending = st.session_state.get("pending_topic_selection")
    if not isinstance(pending, dict):
        return
    apply_topic_selection(st.session_state, pending, confirm_existing_essay=True)
    st.session_state.topic_selection_notice = True
    st.session_state.scroll_target = "writing-input"


def cancel_pending_topic_selection() -> None:
    """Leave both writing fields untouched and dismiss a pending selection."""
    st.session_state.pop("pending_topic_selection", None)
    st.session_state.topic_bank_expanded = True


def session_cloud_user() -> CloudUser | None:
    return cloud_user_from_state(st.session_state.get("cloud_user"))


_ACCOUNT_PRIVATE_LEARNING_KEYS = frozenset(
    {
        "topic_input",
        "essay_input",
        "active_run_id",
        "draft_1_snapshot",
        "grading_cache",
        "pending_first_report_access",
        "pending_guest_claim",
        "guest_claim_failed",
        "queued_sentence_training",
        "pending_training_feedback_run_id",
        "grading_failed",
        "reused_result_notice",
        "first_report_settlement_warning",
        "guest_trial_completion_warning",
        "cloud_cache_warning",
        "cloud_save_warning",
    }
)
_ACCOUNT_PRIVATE_LEARNING_PREFIXES = (
    "latest_",
    "draft_2_",
    "sentence_",
    "logic_",
    "expression_",
    "membership_",
    "learning_assets_",
)


def clear_account_private_learning_state() -> None:
    """Remove learning data that must never survive an account boundary."""
    for key in list(st.session_state.keys()):
        if key in _ACCOUNT_PRIVATE_LEARNING_KEYS or any(
            str(key).startswith(prefix)
            for prefix in _ACCOUNT_PRIVATE_LEARNING_PREFIXES
        ):
            st.session_state.pop(key, None)


def first_report_actor_key(
    user: CloudUser | None, guest_user_id: str
) -> tuple[str, str]:
    """Identify the principal allowed to reuse one in-session first report."""
    if user is not None:
        return ("user", str(user.id))
    return ("guest", str(guest_user_id))


def first_report_cache_key(
    actor_key: tuple[str, str], fingerprint: str
) -> tuple[str, str, str]:
    return (actor_key[0], actor_key[1], str(fingerprint))


def write_cloud_user_state(
    user: CloudUser, *, persist: bool, request_rerun: bool = False
) -> None:
    """Update login state without running first-login product side effects."""
    previous_user = session_cloud_user()
    previous_guest_id = str(st.session_state.get("user_id") or "")
    identity_changed = previous_user is None or previous_user.id != user.id
    pending_claim = st.session_state.get("pending_guest_claim")
    pending_actor = pending_claim.get("actor_key") if isinstance(pending_claim, dict) else None
    preserve_guest_claim = bool(
        previous_user is None
        and isinstance(pending_claim, dict)
        and (
            pending_actor is None
            or tuple(pending_actor) == ("guest", previous_guest_id)
        )
    )
    if identity_changed and not preserve_guest_claim:
        clear_account_private_learning_state()
    st.session_state.cloud_user = cloud_user_to_state(user)
    st.session_state.user_id = user.id
    if persist:
        queue_refresh_token_write(
            st.session_state,
            user.refresh_token,
            request_rerun=request_rerun,
        )


def finish_local_logout(*, reason: str) -> None:
    """Finish phase two only after the browser has confirmed localStorage cleanup."""
    previous_route = str(st.session_state.get("page_mode") or "home")
    clear_account_private_learning_state()
    for key in (
        "cloud_user",
        AUTH_USER_VERSION_KEY,
        AUTH_BROWSER_VERSION_KEY,
        AUTH_BROWSER_COMMAND_KEY,
        AUTH_BROWSER_RECOVERY_KEY,
        AUTH_BROWSER_READ_EPOCH_KEY,
        AUTH_PERSIST_WARNING_KEY,
        AUTH_LISTENER_RERUN_KEY,
        AUTH_LOGOUT_PENDING_KEY,
        AUTH_RECOVERY_STATE_KEY,
        AUTH_REQUEST_RERUN_KEY,
    ):
        st.session_state.pop(key, None)
    st.session_state.user_id = str(uuid.uuid4())
    st.query_params.clear()
    if reason == "invalid":
        st.session_state.login_return_route = (
            previous_route if previous_route in APP_ROUTES else "home"
        )
        st.session_state.page_mode = "login"
        st.query_params["page"] = "login"
    else:
        st.session_state.page_mode = "home"


def mark_cloud_session_invalid(user: CloudUser) -> None:
    """Start a conditional clear so a stale tab cannot erase a newer session."""
    current = session_cloud_user()
    if current is None or current.id != user.id:
        return
    begin_logout(
        st.session_state,
        reason="invalid",
        expected_version=int(st.session_state.get(AUTH_BROWSER_VERSION_KEY) or 0),
    )
    st.session_state[AUTH_REQUEST_RERUN_KEY] = True
    st.rerun()


def _render_auth_wait(message: str, *, retry_label: str) -> None:
    st.info(message)
    if st.button(retry_label, key=f"auth_wait_{retry_label}"):
        recovery = st.session_state.get(AUTH_RECOVERY_STATE_KEY)
        if isinstance(recovery, dict):
            recovery["attempts"] = 0
        pending = st.session_state.get(AUTH_LOGOUT_PENDING_KEY)
        if isinstance(pending, dict) and AUTH_BROWSER_COMMAND_KEY not in st.session_state:
            queue_refresh_token_clear(
                st.session_state,
                expected_version=int(st.session_state.get(AUTH_BROWSER_VERSION_KEY) or 0),
            )
        st.rerun()
    st.stop()


def _render_auth_debug(
    command: object,
    browser_value: object,
    ack: object,
    current_user: CloudUser | None,
) -> None:
    """Show a bounded, token-free auth trace only when explicitly requested."""
    if str(st.query_params.get("auth_debug", "") or "") != "1":
        return
    command_data = command if isinstance(command, dict) else {}
    browser_data = browser_value if isinstance(browser_value, dict) else {}
    pending = st.session_state.get(AUTH_BROWSER_COMMAND_KEY)
    pending_data = pending if isinstance(pending, dict) else {}
    action = str(command_data.get("action") or "")
    snapshot = {
        "command": action,
        "browser_status": str(browser_data.get("status") or ""),
        "browser_source": str(browser_data.get("source") or ""),
        "ack": str(getattr(ack, "status", "") or ""),
        "pending": str(pending_data.get("action") or ""),
        "current_user": current_user is not None,
        "persist_warning": bool(st.session_state.get(AUTH_PERSIST_WARNING_KEY)),
        "logout_pending": isinstance(
            st.session_state.get(AUTH_LOGOUT_PENDING_KEY), dict
        ),
        "command_matches": bool(
            action in {"write", "clear"}
            and browser_data.get("command_id")
            and browser_data.get("command_id") == command_data.get("command_id")
        ),
        "read_matches": bool(
            action == "read"
            and browser_data.get("read_epoch")
            and browser_data.get("read_epoch") == command_data.get("read_epoch")
        ),
    }
    history = st.session_state.setdefault("_auth_debug_history", [])
    if not history or history[-1] != snapshot:
        history.append(snapshot)
        del history[:-8]
    st.caption("登录诊断（不包含令牌）")
    st.json({"build": "auth-state-v1", "history": list(history)})


def restore_cloud_user_session(store: SupabaseStore) -> CloudUser | None:
    """Restore or rotate one auth session without changing the product route."""
    command = take_browser_command(st.session_state)
    browser_value = browser_refresh_session(command)
    ack = acknowledge_browser_command(st.session_state, browser_value)
    current_user = session_cloud_user()
    force_browser_refresh = False

    logout_pending = st.session_state.get(AUTH_LOGOUT_PENDING_KEY)
    if isinstance(logout_pending, dict):
        reason = str(logout_pending.get("reason") or "user")
        if ack is not None and ack.status == "cleared":
            finish_local_logout(reason=reason)
            st.rerun()
        if ack is not None and ack.status == "skipped_newer" and ack.record is not None:
            if reason == "invalid":
                st.session_state.pop(AUTH_LOGOUT_PENDING_KEY, None)
                if current_user is not None:
                    current_user = replace(
                        current_user, refresh_token=ack.record.refresh_token
                    )
                    write_cloud_user_state(current_user, persist=False)
                st.session_state[AUTH_USER_VERSION_KEY] = ack.record.version
                st.session_state[AUTH_BROWSER_VERSION_KEY] = ack.record.version
                recovery = st.session_state.setdefault(
                    AUTH_RECOVERY_STATE_KEY, {"attempts": 0}
                )
                recovery["force_browser_refresh"] = True
                force_browser_refresh = True
                browser_value = {
                    "status": "loaded",
                    "source": "ack",
                    "refresh_token": ack.record.refresh_token,
                    "saved_at": ack.record.saved_at,
                    "version": ack.record.version,
                }
            else:
                retries = int(logout_pending.get("clear_retries") or 0) + 1
                logout_pending["clear_retries"] = retries
                queue_refresh_token_clear(
                    st.session_state, expected_version=ack.record.version
                )
                if retries <= 1:
                    st.rerun()
        if isinstance(st.session_state.get(AUTH_LOGOUT_PENDING_KEY), dict):
            bootstrap = browser_bootstrap_transition(st.session_state, browser_value)
            if bootstrap == "retry":
                st.info("正在退出登录…")
                st.rerun()
            if bootstrap == "degraded":
                finish_local_logout(reason=reason)
                st.warning("浏览器存储未能清理，请关闭其他标签页或清除本站点数据。")
                return None
            _render_auth_wait("正在退出登录…", retry_label="重试退出")

    _render_auth_debug(command, browser_value, ack, current_user)
    if browser_ack_needs_listener_rerun(st.session_state, ack):
        st.rerun()
    listener_stable = mark_browser_listener_stable(st.session_state, command, browser_value)

    bootstrap = browser_bootstrap_transition(st.session_state, browser_value)
    if bootstrap == "wait":
        st.info("正在恢复登录…")
        st.stop()
    if bootstrap == "retry":
        st.info("正在恢复登录…")
        st.rerun()
    if bootstrap == "degraded":
        st.warning("浏览器暂时无法保存登录状态，本次会话仍可继续使用。")

    if (
        bootstrap != "degraded"
        and isinstance(command, dict)
        and command.get("action") == "write"
        and ack is None
    ):
        st.info("正在保存登录状态…")
        st.stop()
    if st.session_state.get(AUTH_PERSIST_WARNING_KEY):
        st.warning("本次登录无法持久保存；关闭此页面后可能需要重新登录。")
    if browser_signaled_logout(
        browser_value,
        ack=ack,
        current_version=int(st.session_state.get(AUTH_USER_VERSION_KEY) or 0),
        has_current_user=current_user is not None,
        command=command,
        listener_stable=listener_stable,
        has_pending_command=AUTH_BROWSER_COMMAND_KEY in st.session_state,
        persistence_failed=bool(st.session_state.get(AUTH_PERSIST_WARNING_KEY)),
    ):
        finish_local_logout(reason="user")
        st.rerun()

    browser_record, _ = parse_persisted_refresh_session(browser_value)
    if browser_record is not None:
        st.session_state[AUTH_BROWSER_VERSION_KEY] = max(
            int(st.session_state.get(AUTH_BROWSER_VERSION_KEY) or 0),
            browser_record.version,
        )
    recovery_gate = st.session_state.get(AUTH_RECOVERY_STATE_KEY)
    if isinstance(recovery_gate, dict):
        force_browser_refresh = force_browser_refresh or bool(
            recovery_gate.get("force_browser_refresh")
        )
    if isinstance(recovery_gate, dict) and int(recovery_gate.get("attempts") or 0) >= 1:
        if recovery_gate.pop("retry_due", False):
            pass
        elif not (
            isinstance(browser_value, dict)
            and browser_value.get("source") == "storage"
        ):
            _render_auth_wait("正在恢复登录…", retry_label="重试恢复")
    resolution = resolve_auth_session(
        store,
        current_user,
        browser_value,
        current_version=int(st.session_state.get(AUTH_USER_VERSION_KEY) or 0),
        force_browser_refresh=force_browser_refresh,
    )
    if resolution.clear_persisted:
        begin_logout(
            st.session_state,
            reason="invalid",
            expected_version=resolution.clear_expected_version,
        )
        st.rerun()
    if resolution.user is None and current_user is not None and resolution.state_changed:
        clear_account_private_learning_state()
        st.session_state.pop("cloud_user", None)
        st.session_state.pop(AUTH_USER_VERSION_KEY, None)
        st.session_state.user_id = str(uuid.uuid4())
    elif resolution.user is not None and resolution.state_changed:
        write_cloud_user_state(resolution.user, persist=resolution.persist_refresh)
    if resolution.user is not None and resolution.browser_version:
        st.session_state[AUTH_USER_VERSION_KEY] = max(
            int(st.session_state.get(AUTH_USER_VERSION_KEY) or 0),
            resolution.browser_version,
        )
    if resolution.recovery_pending:
        recovery = st.session_state.setdefault(
            AUTH_RECOVERY_STATE_KEY, {"attempts": 0}
        )
        attempts = int(recovery.get("attempts") or 0)
        if attempts < 1:
            recovery["attempts"] = attempts + 1
            recovery["retry_due"] = True
            st.rerun()
        _render_auth_wait("正在恢复登录…", retry_label="重试恢复")
    st.session_state.pop(AUTH_RECOVERY_STATE_KEY, None)
    if resolution.state_changed:
        st.rerun()
    return resolution.user


def record_usage_event(
    store: SupabaseStore,
    event_name: str,
    *,
    user: CloudUser | None = None,
    run_id: str = "",
    attempt_id: str = "",
    occurrence_key: str = "",
    metadata: dict[str, object] | None = None,
) -> None:
    """Queue one privacy-safe event once per stable dedupe key."""
    session_id = str(st.session_state.get("flow_id") or "")
    anonymous_id = anonymous_user_id(str(st.session_state.get("visitor_hash") or ""))
    if not store.enabled or not session_id or (user is None and not anonymous_id):
        return
    try:
        normalized_run_id = str(uuid.UUID(run_id)) if run_id else ""
        normalized_attempt_id = str(uuid.UUID(attempt_id)) if attempt_id else ""
        dedupe_key = build_dedupe_key(
            event_name,
            session_id,
            run_id=normalized_run_id,
            attempt_id=normalized_attempt_id,
            occurrence_key=occurrence_key,
        )
    except (ValueError, TypeError, AttributeError):
        LOGGER.warning("Invalid product analytics context for %s", event_name)
        return
    recorded = st.session_state.setdefault("analytics_recorded_keys", set())
    if dedupe_key in recorded:
        return
    recorded.add(dedupe_key)
    event_metadata = dict(metadata or {})
    event_metadata["identity_type"] = "authenticated" if user is not None else "anonymous"
    clean_metadata = sanitize_metadata(event_metadata)
    record_event_safely(
        lambda: store.record_analytics_event(
            event_name,
            session_id,
            dedupe_key,
            anonymous_user_id=anonymous_id,
            run_id=normalized_run_id,
            attempt_id=normalized_attempt_id,
            metadata=clean_metadata,
            user=user,
        ),
        asynchronous=True,
        max_retries=2,
        logger=LOGGER,
    )


def _app_setting(name: str) -> str:
    """Read one server-side app setting from Streamlit or the environment."""
    try:
        value = st.secrets.get(name, "")
    except (FileNotFoundError, KeyError):
        value = ""
    return str(value or os.getenv(name, "")).strip()


def _nested_secret_setting(section: str, name: str) -> str:
    """Read a setting kept in a non-environment Streamlit Secrets section."""
    try:
        section_values = st.secrets.get(section, {})
        value = section_values.get(name, "")
    except (AttributeError, FileNotFoundError, KeyError, TypeError):
        value = ""
    return str(value or "").strip()


def _payment_qr_bytes(name: str) -> bytes | None:
    """Decode one payment QR stored outside Git in Streamlit Secrets."""
    encoded = _nested_secret_setting("founder_payment_qr", name)
    if not encoded:
        return None
    if encoded.startswith("data:"):
        header, separator, encoded = encoded.partition(",")
        if not separator or ";base64" not in header.lower():
            LOGGER.warning("Ignoring invalid payment QR data URL for %s", name)
            return None
    try:
        image_bytes = base64.b64decode(re.sub(r"\s+", "", encoded), validate=True)
    except (binascii.Error, ValueError, TypeError):
        LOGGER.warning("Ignoring invalid base64 payment QR for %s", name)
        return None
    if len(image_bytes) > 2_000_000:
        LOGGER.warning("Ignoring oversized payment QR for %s", name)
        return None
    is_jpeg = image_bytes.startswith(b"\xff\xd8\xff")
    is_png = image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    is_webp = image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP"
    if not (is_jpeg or is_png or is_webp):
        LOGGER.warning("Ignoring unsupported payment QR image for %s", name)
        return None
    return image_bytes


def local_unmetered_ai_enabled() -> bool:
    """Allow quota-free model calls only when a developer opts in explicitly."""
    return _app_setting("ALLOW_LOCAL_UNMETERED_AI").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def require_ai_access_backend(store: SupabaseStore) -> bool:
    """Fail closed when the production quota backend is missing."""
    if store.enabled:
        return True
    if local_unmetered_ai_enabled():
        return False
    raise CloudStoreError(
        "AI 权益服务尚未配置。公开环境不会在缺少 Supabase 时放行模型请求。"
    )


def clear_membership_cache() -> None:
    st.session_state.pop("membership_entitlement_cache", None)
    st.session_state.pop("membership_request_cache", None)


def load_membership_entitlement(
    store: SupabaseStore,
    user: CloudUser,
    *,
    refresh: bool = False,
) -> tuple[dict[str, object], str]:
    """Load a short-lived display cache; access RPCs still decide every AI action."""
    if not store.enabled and local_unmetered_ai_enabled():
        return normalize_entitlement(
            {
                "active": True,
                "status": "local",
                "run_quota": FOUNDER_OFFER.run_quota,
                "runs_remaining": FOUNDER_OFFER.run_quota,
            }
        ), ""
    if not store.enabled:
        return normalize_entitlement({"status": "unavailable"}), "AI 权益服务尚未配置"
    cached = st.session_state.get("membership_entitlement_cache")
    if (
        not refresh
        and isinstance(cached, dict)
        and cached.get("user_id") == user.id
        and time.time() - float(cached.get("fetched_at") or 0) < MEMBERSHIP_CACHE_SECONDS
    ):
        return normalize_entitlement(cached.get("entitlement")), str(cached.get("error") or "")
    try:
        entitlement = normalize_entitlement(store.get_membership_entitlement(user))
        error = ""
    except (CloudStoreError, AttributeError) as exc:
        entitlement = normalize_entitlement({"status": "unavailable"})
        error = str(exc)
    st.session_state.membership_entitlement_cache = {
        "user_id": user.id,
        "fetched_at": time.time(),
        "entitlement": entitlement,
        "error": error,
    }
    return entitlement, error


def load_membership_request(
    store: SupabaseStore,
    user: CloudUser,
    *,
    refresh: bool = False,
) -> dict[str, object]:
    if not store.enabled:
        return {}
    cached = st.session_state.get("membership_request_cache")
    if not refresh and isinstance(cached, dict) and cached.get("user_id") == user.id:
        return dict(cached.get("request") or {})
    getter = getattr(store, "get_my_membership_request", None)
    if not callable(getter):
        return {}
    try:
        request = getter(user)
    except CloudStoreError:
        request = {}
    normalized = dict(request) if isinstance(request, dict) else {}
    st.session_state.membership_request_cache = {"user_id": user.id, "request": normalized}
    return normalized


def render_founder_offer(
    store: SupabaseStore,
    user: CloudUser | None,
    *,
    key: str,
    intro: str = "",
) -> None:
    """Render the server-selected first pack or renewal payment offer."""
    with st.container(border=True):
        entitlement = normalize_entitlement({"status": "none"})
        entitlement_error = ""
        if user is not None:
            entitlement, entitlement_error = load_membership_entitlement(store, user)
        offer = offer_for_entitlement(entitlement)
        is_renewal = offer.plan_code == RENEWAL_OFFER.plan_code
        offer_name = "3 篇续包" if is_renewal else "创始体验首包"
        price_label = format(offer.price_cny, ".1f")

        offer_heading = (
            f"下一包：{offer_name}"
            if entitlement.get("active") and int(entitlement.get("runs_remaining") or 0) > 0
            else offer_name
        )
        st.markdown(f"### {offer_heading}")
        if user is None:
            st.markdown("## 首包 ¥7.5，之后每包 ¥9.9")
        else:
            st.markdown(f"## ¥{price_label} / 30 天 / 3 篇")
        if intro:
            st.write(intro)
        st.write(
            "最多选择 3 篇作文完成训练闭环；每篇包含首稿报告、最多 3 次专项 AI 点评，"
            "以及 1 次二稿评分与两稿对比。"
        )
        st.caption(
            "30 天与 3 篇任一先达到即结束 · 绑定当前账号 · 不自动续费 · "
            "表达库独立造句 AI 点评暂不包含"
        )
        if is_renewal:
            st.caption("首包之后每包 ¥9.9 / 3 篇 · 可重复续包 · 提交订单号后自动开通")
        else:
            st.caption("每个账号仅有 1 个 ¥7.5 首包 · 之后每个 3 篇续包 ¥9.9")
        st.caption("权益不可转移 · 未使用权益不自动延期、转赠或折现")
        st.caption("查看、下载已生成内容不计次数；模型失败不扣次数。AI 估分不等同于 IELTS 官方成绩。")
        if user is None:
            st.button(
                "登录后查看开通方式",
                key=f"offer_login_{key}",
                type="primary",
                use_container_width=True,
                on_click=open_cloud_login,
                args=(str(st.session_state.get("page_mode") or "report"), str(st.query_params.get("mode", "") or "")),
            )
            return

        if entitlement_error:
            st.warning("暂时无法读取开通状态，请稍后刷新；系统不会因此扣除篇数。")
            return
        if entitlement.get("active") and int(entitlement.get("runs_remaining") or 0) > 0:
            st.success(entitlement_caption(entitlement))
            st.caption("当前包用完 3 篇或到期后，才会开放 ¥9.9 / 3 篇续包申请。")
            return
        if not entitlement.get("can_purchase"):
            st.info(entitlement_caption(entitlement))
            return

        # Display must agree with the authoritative offer metadata. If a
        # partially deployed backend returns an unknown plan or price, do not
        # accept a payment reference.
        if not entitlement.get("server_offer_verified"):
            st.warning("套餐价格暂时无法安全确认，付费申请未开放。请稍后刷新。")
            return

        wechat_payment_qr = _payment_qr_bytes("wechat_base64")
        alipay_payment_qr = _payment_qr_bytes("alipay_base64")
        payment_qr = _app_setting("FOUNDER_PAYMENT_QR_URL")
        payment_instructions = _app_setting("FOUNDER_PAYMENT_INSTRUCTIONS")
        support_contact = _app_setting("FOUNDER_SUPPORT_CONTACT")
        refund_policy = _app_setting("FOUNDER_REFUND_POLICY")
        payment_ready = bool(payment_instructions and support_contact and refund_policy)
        request = load_membership_request(store, user)
        request_status = str(request.get("status") or "")
        if request_status in {"pending", "reviewing", "needs_info"}:
            application_code = str(request.get("application_code") or "")
            request_plan = str(request.get("plan_code") or offer.plan_code)
            request_name = "3 篇续包" if request_plan == RENEWAL_OFFER.plan_code else "创始体验首包"
            request_amount = str(request.get("amount_cny") or price_label)
            label = "付款信息已提交，系统正在自动开通。请刷新开通状态。"
            if request_status == "needs_info":
                label = "这笔申请需要补充信息，请按下方联系方式与管理员确认。"
            st.info(label)
            st.caption(f"申请套餐：{request_name} · 应核金额 ¥{request_amount}")
            if application_code:
                st.code(application_code, language="text")
            if support_contact:
                st.caption(f"退款或异常联系：{support_contact}")
            if refund_policy:
                st.caption(f"退款说明：{refund_policy}")
        else:
            if not payment_ready:
                st.warning("收款说明、真实联系方式或退款说明尚未配置完整，付费申请暂未开放。")
            if payment_ready:
                st.caption(f"当前绑定邮箱：{user.email}")
                private_qr_codes = [
                    ("微信支付", wechat_payment_qr),
                    ("支付宝", alipay_payment_qr),
                ]
                private_qr_codes = [item for item in private_qr_codes if item[1]]
                if private_qr_codes:
                    qr_columns = st.columns(len(private_qr_codes))
                    for column, (payment_name, qr_image) in zip(
                        qr_columns,
                        private_qr_codes,
                        strict=True,
                    ):
                        column.image(
                            qr_image,
                            caption=f"{payment_name} · {offer_name}",
                            width=260,
                        )
                elif payment_qr:
                    st.image(payment_qr, caption=f"{offer_name}收款入口", width=260)
                st.info(payment_instructions)
                st.caption(f"退款或异常联系：{support_contact}")
                st.caption(f"退款说明：{refund_policy}")
                with st.form(f"membership_request_{key}"):
                    payment_reference = st.text_input(
                        "支付订单号",
                        placeholder="请填写支付平台中的订单号，不要上传包含余额或其他交易的截图。",
                    )
                    paid_at = st.text_input("付款时间（选填）", placeholder="例如：2026-09-01 20:30")
                    note = st.text_input("付款备注（选填）", placeholder="可填写付款渠道中的备注")
                    terms_confirmed = st.checkbox(
                        f"我已确认：{offer_name} ¥{price_label}、30 天、最多 3 篇、"
                        "不自动续费，并同意上述退款说明。"
                    )
                    submitted = st.form_submit_button("提交并开通", type="primary", use_container_width=True)
                if submitted:
                    if not terms_confirmed:
                        st.warning("请先确认体验包范围与退款说明。")
                    elif len(payment_reference.strip()) < 4:
                        st.warning("请填写支付平台中的订单号。")
                    else:
                        try:
                            created = store.create_membership_request(
                                user,
                                payment_reference.strip(),
                                paid_at=paid_at.strip(),
                                note=note.strip(),
                            )
                        except (CloudStoreError, AttributeError) as exc:
                            st.error(f"付款信息暂时无法提交：{exc}")
                        else:
                            clear_membership_cache()
                            code = str((created or {}).get("application_code") or "")
                            reason = str((created or {}).get("reason") or "")
                            status = str((created or {}).get("status") or "")
                            if (created or {}).get("created") and status == "approved":
                                st.success(f"{offer_name}已自动开通，30 天有效期现在开始。")
                                st.rerun()
                            elif status == "approved":
                                st.rerun()
                            elif (created or {}).get("created"):
                                st.info("开通申请已提交，系统正在自动处理，请刷新开通状态。")
                            elif reason in {"already_submitted", "pending_request_exists"}:
                                st.info("这笔付款或当前账号已有开通申请，请勿重复付款。")
                            elif reason in {
                                "membership_exists",
                                "active_membership",
                                "purchase_not_allowed",
                            }:
                                st.info("当前权益仍可使用，暂时不能提交续包申请。")
                            else:
                                st.error("本次没有建立开通申请，请联系管理员确认。")
                            if code and reason not in {
                                "membership_exists",
                                "active_membership",
                                "purchase_not_allowed",
                            }:
                                st.code(code, language="text")
        if st.button("刷新开通状态", key=f"refresh_membership_{key}", use_container_width=True):
            clear_membership_cache()
            st.rerun()


def render_training_access_gate(
    store: SupabaseStore,
    user: CloudUser,
    *,
    grading_run_id: str,
) -> dict[str, object] | None:
    """Require an active pass and bind the current report to one paid run."""
    if not store.enabled and local_unmetered_ai_enabled():
        return {
            "allowed": True,
            "reason": "local",
            "training_limit": FOUNDER_OFFER.training_limit_per_run,
            "training_remaining": FOUNDER_OFFER.training_limit_per_run,
            "second_draft_completed": False,
        }
    if not store.enabled:
        st.error("AI 权益服务尚未配置，当前不会发起模型请求。")
        return None
    entitlement, error = load_membership_entitlement(store, user)
    if error:
        st.error("暂时无法安全确认训练权益，请稍后刷新。")
        return None
    access: dict[str, object] = {}
    if grading_run_id:
        try:
            raw_access = store.get_membership_run_access(user, grading_run_id)
            access = dict(raw_access) if isinstance(raw_access, dict) else {}
        except (CloudStoreError, AttributeError):
            st.error("暂时无法确认这篇作文的训练状态，请稍后重试。")
            return None
    if entitlement.get("active") and access.get("allowed"):
        st.caption(
            f"本篇专项 AI 点评剩余 {int(access.get('training_remaining') or 0)}/"
            f"{int(access.get('training_limit') or FOUNDER_OFFER.training_limit_per_run)} · "
            f"二稿验证 {'已完成' if access.get('second_draft_completed') else '可用 1 次'}"
        )
        return {**access, "read_only": False}

    if access.get("history_readable"):
        st.info("体验包已结束；这篇已生成的训练与二稿记录仍可查看，但不能再发起新的 AI 请求。")
        return {**access, "read_only": True}

    if not entitlement.get("active"):
        render_founder_offer(
            store,
            user,
            key=f"training_{grading_run_id or 'none'}",
            intro="首稿报告已经保留。开通后可选择将这篇作文加入完整训练。",
        )
        return None
    if not grading_run_id:
        st.warning("请先保存这份首稿报告，再将它加入完整训练。")
        return None
    if int(entitlement.get("runs_remaining") or 0) <= 0:
        st.warning("3 篇完整训练额度已用完；已有报告和训练记录仍可查看。")
        return None

    with st.container(border=True):
        st.markdown("### 将本篇加入完整训练")
        st.write(
            "确认后会占用 1 篇额度。本篇将获得最多 3 次专项 AI 点评，以及 1 次二稿评分与两稿对比。"
        )
        st.caption(entitlement_caption(entitlement))
        if st.button(
            "确认使用 1 篇额度",
            key=f"activate_training_{grading_run_id}",
            type="primary",
            use_container_width=True,
        ):
            snapshot = st.session_state.get("draft_1_snapshot")
            topic = str(snapshot.get("topic") or "") if isinstance(snapshot, dict) else ""
            essay = str(snapshot.get("text") or "") if isinstance(snapshot, dict) else ""
            content_hash = submission_hash(topic, essay) if topic and essay else grading_run_id
            flow_key = f"membership_activation_flow_{grading_run_id}"
            flow_id = str(st.session_state.setdefault(flow_key, str(uuid.uuid4())))
            try:
                reserved = store.reserve_membership_run(
                    user,
                    flow_id,
                    content_hash,
                    grading_run_id=grading_run_id,
                )
                if reserved.get("allowed") and reserved.get("reason") != "already_completed":
                    completed = store.complete_membership_run(user, flow_id, grading_run_id)
                    if not completed.get("completed"):
                        raise CloudStoreError(action_reason_message(completed.get("reason")))
                elif not reserved.get("allowed") and not reserved.get("cached"):
                    raise GradingAccessError(str(reserved.get("reason") or "membership_inactive"))
            except GradingAccessError as exc:
                st.warning(action_reason_message(exc.reason))
            except (CloudStoreError, AttributeError) as exc:
                st.error(f"暂时无法加入训练：{exc}")
            else:
                st.session_state.pop(flow_key, None)
                clear_membership_cache()
                st.rerun()
    return None


def reserve_training_feedback_action(
    store: SupabaseStore,
    user: CloudUser,
    *,
    grading_run_id: str,
    task_kind: str,
    task_key: str,
) -> dict[str, object]:
    """Reserve one of the three per-essay feedback actions before calling AI."""
    if not require_ai_access_backend(store):
        return {"allowed": True, "local": True, "flow_id": ""}
    flow_id = str(uuid.uuid4())
    result = store.reserve_training_action(
        user,
        grading_run_id,
        flow_id,
        task_kind,
        task_key,
    )
    if not isinstance(result, dict) or not result.get("allowed"):
        raise GradingAccessError(str((result or {}).get("reason") or "membership_inactive"))
    return {**result, "flow_id": flow_id}


def complete_training_feedback_action(
    store: SupabaseStore,
    user: CloudUser,
    ticket: dict[str, object],
) -> None:
    if ticket.get("local"):
        return
    result = store.complete_training_action(user, str(ticket.get("flow_id") or ""))
    if not isinstance(result, dict) or not result.get("completed"):
        raise CloudStoreError(action_reason_message((result or {}).get("reason")))
    clear_membership_cache()


def release_training_feedback_action(
    store: SupabaseStore,
    user: CloudUser,
    ticket: dict[str, object],
) -> None:
    if ticket.get("local"):
        return
    result = store.release_training_action(user, str(ticket.get("flow_id") or ""))
    if not isinstance(result, dict) or not result.get("released"):
        raise CloudStoreError(action_reason_message((result or {}).get("reason")))


def reserve_second_draft(
    store: SupabaseStore,
    user: CloudUser,
    *,
    grading_run_id: str,
    draft_text: str,
    topic: str,
) -> dict[str, object]:
    """Reserve the sole second-draft action for one paid essay run."""
    if not require_ai_access_backend(store):
        return {"allowed": True, "local": True, "flow_id": ""}
    flow_id = str(uuid.uuid4())
    result = store.reserve_second_draft_action(
        user,
        grading_run_id,
        flow_id,
        submission_hash(topic, draft_text),
    )
    if not isinstance(result, dict) or not result.get("allowed"):
        raise GradingAccessError(str((result or {}).get("reason") or "membership_inactive"))
    return {**result, "flow_id": flow_id}


def complete_second_draft(
    store: SupabaseStore,
    user: CloudUser,
    ticket: dict[str, object],
    *,
    revised_grading_run_id: str = "",
) -> None:
    if ticket.get("local"):
        return
    result = store.complete_second_draft_action(
        user,
        str(ticket.get("flow_id") or ""),
        revised_grading_run_id=revised_grading_run_id,
    )
    if not isinstance(result, dict) or not result.get("completed"):
        raise CloudStoreError(action_reason_message((result or {}).get("reason")))


def release_second_draft(
    store: SupabaseStore,
    user: CloudUser,
    ticket: dict[str, object],
) -> None:
    if ticket.get("local"):
        return
    result = store.release_second_draft_action(user, str(ticket.get("flow_id") or ""))
    if not isinstance(result, dict) or not result.get("released"):
        raise CloudStoreError(action_reason_message((result or {}).get("reason")))


def claim_guest_result(store: SupabaseStore, user: CloudUser) -> bool:
    """Attach the current guest result to a new login without another model call."""
    pending = st.session_state.get("pending_guest_claim")
    if not isinstance(pending, dict):
        return True
    try:
        cloud_ids = store.save_grading_cycle(
            user,
            question=str(pending["topic"]),
            essay=str(pending["essay"]),
            word_count=int(pending["word_count"]),
            package=dict(pending["package"]),
            content_hash=str(pending["fingerprint"]),
        )
    except (CloudStoreError, KeyError, TypeError, ValueError):
        st.session_state.guest_claim_failed = True
        return False
    st.session_state.latest_cloud_ids = cloud_ids
    snapshot = st.session_state.get("draft_1_snapshot")
    if isinstance(snapshot, dict):
        snapshot["essay_id"] = cloud_ids.get("essay_id", "")
        snapshot["grading_run_id"] = cloud_ids.get("grading_run_id", "")
    cache = st.session_state.get("grading_cache")
    if isinstance(cache, dict):
        fingerprint = str(pending["fingerprint"])
        pending_actor = pending.get("actor_key")
        source_key = (
            first_report_cache_key(tuple(pending_actor), fingerprint)
            if isinstance(pending_actor, (list, tuple)) and len(pending_actor) == 2
            else fingerprint
        )
        entry = cache.get(source_key)
        if isinstance(entry, dict):
            claimed_entry = dict(entry)
            claimed_entry["cloud_ids"] = cloud_ids
            claimed_entry["cloud_user_id"] = user.id
            target_key = first_report_cache_key(
                first_report_actor_key(user, ""), fingerprint
            )
            cache[target_key] = claimed_entry
            if source_key != target_key:
                cache.pop(source_key, None)
    st.session_state.pop("pending_guest_claim", None)
    st.session_state.pop("guest_claim_failed", None)
    ensure_learning_assets(store, user)
    return True


def complete_login(store: SupabaseStore, user: CloudUser) -> None:
    write_cloud_user_state(user, persist=True)
    claim_guest_result(store, user)
    record_usage_event(
        store,
        "login_completed",
        user=user,
        attempt_id=str(st.session_state.get("latest_grading_attempt_id") or ""),
    )
    route = str(st.session_state.pop("login_return_route", "home") or "home")
    mode = str(st.session_state.pop("login_return_mode", "") or "")
    navigate(route, str(st.session_state.get("latest_cloud_ids", {}).get("grading_run_id", "")), mode)


def render_login_page(store: SupabaseStore) -> None:
    """Render passwordless email-code authentication without exposing provider details."""
    render_alpine_hero(variant="login")
    email = st.text_input("邮箱", key="login_email", placeholder="name@example.com")
    send_col, demo_col = st.columns(2)
    with send_col:
        if st.button("发送验证码", type="primary", use_container_width=True):
            if "@" not in email:
                st.error("请输入有效邮箱地址。")
            else:
                try:
                    store.send_email_code(email.strip())
                    st.session_state.login_code_sent = True
                    st.success("验证码已发送，请检查邮箱。")
                except CloudStoreError as exc:
                    st.error(f"验证码发送失败：{exc}")
    with demo_col:
        st.button("先看零 Token 完整示例", on_click=show_demo, use_container_width=True)
    if st.session_state.get("login_code_sent"):
        code = st.text_input("请输入邮箱验证码", key="login_code")
        if st.button("登录并进入学习档案", use_container_width=True):
            try:
                user = store.verify_email_code(email.strip(), code.strip())
                complete_login(store, user)
                st.rerun()
            except CloudStoreError as exc:
                st.error(f"登录失败：{exc}")


def logout_cloud_user() -> None:
    """Start two-phase logout; local state remains blocked until browser ACK."""
    user = session_cloud_user()
    store = SupabaseStore()
    start_logout_with_remote_best_effort(
        st.session_state,
        user,
        store.sign_out,
        expected_version=int(st.session_state.get(AUTH_USER_VERSION_KEY) or 0),
    )


def open_cloud_login(return_route: str = "home", return_mode: str = "") -> None:
    """Open soft login and remember the learning action that prompted it."""
    st.session_state.page_mode = "login"
    st.session_state.login_return_route = return_route
    st.session_state.login_return_mode = return_mode
    st.session_state.pop("login_code_sent", None)
    st.query_params["page"] = "login"


def sync_learning_item_status(
    store: SupabaseStore,
    user: CloudUser,
    *,
    grading_run_id: str,
    source_text: str,
    mastered: bool,
) -> None:
    """Keep practice completion resilient when a SQL migration is still pending."""
    try:
        store.update_learning_item_for_practice(
            user,
            grading_run_id=grading_run_id,
            source_text=source_text,
            mastered=mastered,
        )
    except (CloudStoreError, AttributeError):
        st.session_state.learning_assets_sync_error = True


def apply_pending_scroll() -> None:
    """Move the parent page to the requested section after a Streamlit rerun."""
    target = st.session_state.pop("scroll_target", None)
    if not target:
        return
    st.html(
        f"""
        <script>
        requestAnimationFrame(() => {{
            const target = document.getElementById("{target}");
            if (target) target.scrollIntoView({{ behavior: "instant", block: "start" }});
        }});
        </script>
        """,
        unsafe_allow_javascript=True,
    )


def render_anchor(anchor_id: str) -> None:
    """Create a stable in-page scroll target."""
    st.markdown(f'<div id="{anchor_id}" class="scroll-anchor"></div>', unsafe_allow_html=True)


def render_bookmark_rail(items: list[tuple[str, str]]) -> None:
    """Render a compact floating table of contents."""
    links = "".join(
        f'<a href="#{anchor}">{label}</a>' for label, anchor in items
    )
    active_rules = "".join(
        f'body:has(#{anchor}:target) .bookmark-rail a[href="#{anchor}"]'
        "{background:var(--ep-surface);color:var(--ep-primary-hover);box-shadow:var(--ep-shadow-sm);}"
        for _, anchor in items
    )
    st.markdown(
        f'<style>{active_rules}</style><nav class="bookmark-rail"><span>快速索引</span>{links}</nav>',
        unsafe_allow_html=True,
    )


st.set_page_config(
    page_title="EssayPilot 雅思写作训练",
    page_icon=":memo:",
    layout="wide",
)

if "user_id" not in st.session_state:
    st.session_state.user_id = str(uuid.uuid4())
if "page_mode" not in st.session_state:
    st.session_state.page_mode = "home"
if "flow_id" not in st.session_state:
    st.session_state.flow_id = str(uuid.uuid4())

user_id = st.session_state.user_id

inject_alpine_theme()


def show_markdown_file(path: Path) -> None:
    """Offer the complete record as Markdown and PDF downloads."""
    markdown = path.read_text(encoding="utf-8")
    pdf = markdown_to_pdf(markdown)
    markdown_column, pdf_column = st.columns(2)
    with markdown_column:
        st.download_button(
            label="下载 Markdown 报告",
            data=markdown,
            file_name=path.name,
            mime="text/markdown",
            use_container_width=True,
        )
    with pdf_column:
        st.download_button(
            label="下载 PDF 报告",
            data=pdf,
            file_name=f"{path.stem}.pdf",
            mime="application/pdf",
            use_container_width=True,
        )


def render_score_card(label: str, value: str, note: str = "") -> None:
    """Render a compact portfolio-style score card."""
    safe_label = html.escape(str(label))
    safe_value = html.escape(str(value))
    safe_note = html.escape(str(note))
    st.markdown(
        f"""
        <div class="score-card">
            <div class="score-label">{safe_label}</div>
            <div class="score-value">{safe_value}</div>
            <div class="score-label">{safe_note}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_dashboard_stats(items: list[tuple[str, object, str]], columns: int) -> None:
    """Render compact dashboard statistics that reflow cleanly on narrow screens."""
    cards = []
    for label, value, note in items:
        cards.append(
            '<div class="dashboard-stat-card">'
            f'<div class="dashboard-stat-label">{html.escape(str(label))}</div>'
            f'<div class="dashboard-stat-value">{html.escape(str(value))}</div>'
            f'<div class="dashboard-stat-note">{html.escape(str(note))}</div>'
            "</div>"
        )
    st.markdown(
        f'<div class="dashboard-stat-grid" style="--stat-columns: {max(1, columns)}">'
        + "".join(cards)
        + "</div>",
        unsafe_allow_html=True,
    )


def extract_overall_score(markdown: str) -> float | None:
    """Extract the likely overall band score from the latest report text."""
    match = SCORE_PATTERN.search(markdown)
    if not match:
        return None

    return float(match.group(1))


def extract_report_section(markdown: str, number: int) -> str:
    """Safely extract a numbered report section from Markdown."""
    heading = rf"#{{1,3}}\s*{number}\s*[.:、-]?\s+"
    next_heading = r"\n#{1,3}\s*\d+\s*[.:、-]?\s+"
    match = re.search(
        rf"{heading}.*?(?={next_heading}|\Z)",
        markdown,
        flags=re.DOTALL,
    )
    return match.group(0).strip() if match else ""


def clean_markdown_text(text: str) -> str:
    """Remove simple Markdown markers for compact card display."""
    cleaned = re.sub(r"^#{1,6}\s*", "", text.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"^[-*]\s*", "", cleaned, flags=re.MULTILINE)
    return cleaned.strip()


def extract_bullets(section: str, limit: int = 4) -> list[str]:
    """Extract short bullet-like items from a report section."""
    if not section:
        return []

    items: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("|") or re.fullmatch(r"[-:| ]+", stripped):
            continue
        if re.match(r"^[-*]\s+", stripped) or re.match(r"^\d+\.\s+", stripped):
            stripped = re.sub(r"^[-*]\s+|^\d+\.\s+", "", stripped)
            items.append(clean_markdown_text(stripped))
        elif any(prefix in stripped.lower() for prefix in ("problem", "priority", "why", "how")):
            items.append(clean_markdown_text(stripped))

    deduped: list[str] = []
    for item in items:
        if item and item not in deduped:
            deduped.append(item)

    return deduped[:limit]


def extract_criteria_scores(markdown: str) -> dict[str, str]:
    """Extract likely criterion scores from the four-criteria table."""
    section = extract_report_section(markdown, 2)
    scores = {
        "Task Response": "-",
        "Coherence": "-",
        "Lexical Resource": "-",
        "Grammar": "-",
    }

    for line in section.splitlines():
        if not line.strip().startswith("|") or "---" in line:
            continue

        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3:
            continue

        criterion = cells[0].lower()
        if criterion in {"criterion", "评分项"}:
            continue
        likely_score = (cells[2] if len(cells) >= 4 else cells[1]) or "-"
        score_match = re.search(r"\d(?:\.\d)?", likely_score)
        likely_score = score_match.group(0) if score_match else likely_score
        if "task" in criterion or "任务回应" in criterion:
            scores["Task Response"] = likely_score
        elif "coherence" in criterion or "连贯与衔接" in criterion:
            scores["Coherence"] = likely_score
        elif "lexical" in criterion or "词汇资源" in criterion:
            scores["Lexical Resource"] = likely_score
        elif "grammar" in criterion or "grammatical" in criterion or "语法多样性" in criterion:
            scores["Grammar"] = likely_score

    return scores


def calculate_overall_band(markdown: str) -> float | None:
    """Read legacy Markdown but delegate rounding and validation to the schema authority."""
    criteria = extract_criteria_scores(markdown)
    criterion_rows: list[dict[str, int]] = []
    for value in criteria.values():
        match = re.search(r"\d(?:\.\d)?", value)
        if not match:
            return extract_overall_score(markdown)
        score = float(match.group(0))
        if not score.is_integer():
            return None
        criterion_rows.append({"score": int(score)})
    try:
        return calculate_overall(criterion_rows)
    except ExaminerResultError:
        return None


def draft_training_focus(scores: dict[str, float | None]) -> list[str]:
    """Choose one or two practical priorities from the lowest criteria."""
    guidance = {
        "Task Response": "加强论证深度，补充具体解释或例子，避免观点停留在概括层面。",
        "Coherence & Cohesion": "优化主题句、段落衔接和逻辑推进，让每段围绕一个中心展开。",
        "Lexical Resource": "减少重复词，优先使用准确、自然的学术表达和搭配。",
        "Grammar Range & Accuracy": "增加可控的句式变化，同时检查语法、标点和从句准确性。",
    }
    available = [
        (label, score)
        for label, score in scores.items()
        if label != "Overall Band" and score is not None
    ]
    available.sort(key=lambda item: (item[1], list(guidance).index(item[0])))
    return [f"**{label}：** {guidance[label]}" for label, _ in available[:2]]


def render_score_change(
    draft_1_scores: dict[str, float | None],
    draft_2_scores: dict[str, float | None],
) -> None:
    """Show compact Draft 1 to Draft 2 score changes."""
    st.subheader("Overall 与四项变化")
    for label in draft_1_scores:
        before = draft_1_scores.get(label)
        after = draft_2_scores.get(label)
        if label == "Overall Band":
            before_text = format_overall_band(before)
            after_text = format_overall_band(after)
        else:
            before_text = f"{before:.0f}" if before is not None else "-"
            after_text = f"{after:.0f}" if after is not None else "-"
        st.write(f"**{SCORE_DISPLAY_NAMES.get(label, label)}：** {before_text} → {after_text}")


def draft_2_cache_key(
    user_id: str, grading_run_id: str, draft_content_hash: str
) -> tuple[str, str, str]:
    """Scope generated Draft 2 data to one user, one original run, and one text."""
    return (str(user_id), str(grading_run_id), str(draft_content_hash))


def draft_2_result_from_revision(
    revision: dict[str, object],
    *,
    user_id: str,
    grading_run_id: str,
) -> dict[str, object]:
    """Normalize a persisted revision into the same shape as a fresh result."""
    revised_run = (
        revision.get("revised_run")
        if isinstance(revision.get("revised_run"), dict)
        else {}
    )
    revised_essay = (
        revised_run.get("essays")
        if isinstance(revised_run.get("essays"), dict)
        else {}
    )
    structured = (
        revised_run.get("report_json")
        if isinstance(revised_run.get("report_json"), dict)
        else revision.get("report_json")
    )
    structured = structured if isinstance(structured, dict) else {}
    stored_scores = revision.get("score_snapshot")
    scores = (
        score_snapshot(structured)
        if structured.get("overall_band") is not None
        else (dict(stored_scores) if isinstance(stored_scores, dict) else {})
    )
    return {
        "scores": scores,
        "report": str(
            revised_run.get("report_markdown")
            or revision.get("report_markdown")
            or ""
        ),
        "progress_report": str(revision.get("progress_report") or ""),
        "text": str(revised_essay.get("content") or revision.get("content") or ""),
        "grading_run_id": str(
            revised_run.get("id") or revision.get("revised_grading_run_id") or ""
        ),
        "attempt_id": f"revision:{revision.get('id') or grading_run_id}",
        "settlement_pending": False,
        "persisted": True,
        "user_id": str(user_id),
        "parent_grading_run_id": str(grading_run_id),
    }


def generate_draft_2_feedback(
    *,
    provider: str,
    model: str,
    task_type: str,
    topic: str,
    draft_1_text: str,
    draft_1_scores: dict[str, float | None],
    draft_2_text: str,
    cached_generation: dict[str, object],
) -> tuple[dict[str, object], str]:
    """Generate Draft 2 teaching and comparison concurrently after score lock."""
    cached_package = cached_generation.get("package")
    cached_progress = cached_generation.get("progress_report")
    if isinstance(cached_package, dict):
        if cached_progress:
            return dict(cached_package), str(cached_progress)
        draft_2_scores = score_snapshot(dict(cached_package["structured"]))
        progress_report = compare_draft_progress(
            provider=provider,
            task_question=topic,
            draft_1_text=draft_1_text,
            draft_1_scores=draft_1_scores,
            draft_2_text=draft_2_text,
            draft_2_scores=draft_2_scores,
            model=model,
        )
        cached_generation["progress_report"] = progress_report
        return dict(cached_package), progress_report

    cached_scoring = cached_generation.get("scoring_package")
    if isinstance(cached_scoring, dict):
        scoring_package = dict(cached_scoring)
    else:
        scoring_package = grade_scoring_decision(
            task_type=task_type,
            topic=topic,
            essay=draft_2_text,
        )
        cached_generation["scoring_package"] = scoring_package

    draft_2_scores = score_snapshot(dict(scoring_package["structured"]))
    comparison_executor = None
    comparison_future = None
    comparison_setup_error = None
    if not cached_progress:
        try:
            comparison_provider_config = get_provider_config(provider)
            comparison_client = build_client(provider)
            comparison_executor = ThreadPoolExecutor(max_workers=1)
            comparison_future = comparison_executor.submit(
                compare_draft_progress,
                provider=provider,
                task_question=topic,
                draft_1_text=draft_1_text,
                draft_1_scores=draft_1_scores,
                draft_2_text=draft_2_text,
                draft_2_scores=draft_2_scores,
                model=model,
                client=comparison_client,
                provider_config=comparison_provider_config,
            )
        except Exception as exc:
            comparison_setup_error = exc

    branch_errors: list[Exception] = []
    try:
        try:
            cached_generation["package"] = grade_essay_package(
                task_type=task_type,
                topic=topic,
                essay=draft_2_text,
                locked_scoring_package=scoring_package,
            )
        except Exception as exc:
            branch_errors.append(exc)
        if comparison_future is not None:
            try:
                cached_generation["progress_report"] = comparison_future.result()
            except Exception as exc:
                branch_errors.append(exc)
    finally:
        if comparison_executor is not None:
            comparison_executor.shutdown(wait=True)
    if comparison_setup_error is not None:
        branch_errors.append(comparison_setup_error)
    if branch_errors:
        raise branch_errors[0]

    draft_2_package = cached_generation.get("package")
    if not isinstance(draft_2_package, dict):
        raise RuntimeError("Draft 2 teaching feedback did not return a package.")
    return dict(draft_2_package), str(cached_generation.get("progress_report") or "")


def persist_draft_2_cloud_result(
    store: SupabaseStore,
    user: CloudUser,
    *,
    draft_1: dict[str, object],
    draft_2_text: str,
    draft_2_package: dict[str, object],
    draft_2_scores: dict[str, float | None],
    progress_report: str,
    cached_generation: dict[str, object],
) -> dict[str, object]:
    """Persist once, then make uncertain retries perform settlement only."""
    linked_ids = (
        dict(cached_generation.get("cloud_ids") or {})
        if isinstance(cached_generation.get("cloud_ids"), dict)
        else {}
    )
    revised_run_id = str(linked_ids.get("grading_run_id") or "")
    saved_ticket = cached_generation.get("access_ticket")
    if not isinstance(saved_ticket, dict) or not str(saved_ticket.get("flow_id") or ""):
        raise CloudStoreError("第二稿云端保存缺少原始预留凭证。")
    if not revised_run_id:
        linked_ids = store.save_second_draft_result(
            user,
            grading_run_id=str(draft_1.get("grading_run_id") or ""),
            flow_id=str(saved_ticket.get("flow_id") or ""),
            question=str(draft_1.get("topic") or ""),
            content=draft_2_text,
            word_count=count_words(draft_2_text),
            content_hash=submission_hash(str(draft_1.get("topic") or ""), draft_2_text),
            package=draft_2_package,
            scores=draft_2_scores,
            progress_report=progress_report,
        )
        revised_run_id = str(linked_ids.get("grading_run_id") or "")
        if not revised_run_id:
            raise CloudStoreError("第二稿云端保存未返回可确认的批改记录。")
        # Cache the committed ids before quota settlement. A timeout while
        # completing the action must not insert either row again.
        cached_generation["cloud_ids"] = linked_ids

    if isinstance(saved_ticket, dict) and not cached_generation.get("settled"):
        complete_second_draft(
            store,
            user,
            dict(saved_ticket),
            revised_grading_run_id=revised_run_id,
        )
        cached_generation["settled"] = True
        cached_generation.pop("access_ticket", None)
        clear_membership_cache()
    return linked_ids


def render_draft_2_training(
    *,
    provider: str,
    model: str,
    task_type: str,
    user_id: str,
    cloud_store: SupabaseStore | None = None,
    cloud_user: CloudUser | None = None,
    read_only: bool = False,
) -> None:
    """Render and process the complete Draft 2 learning cycle."""
    draft_1 = st.session_state.get("draft_1_snapshot")
    if not isinstance(draft_1, dict):
        st.info("请先完成第一稿批改。")
        return

    st.subheader("第二稿训练")
    with st.expander("查看第一稿", expanded=False):
        st.write(draft_1["text"])

    st.markdown("#### 第一稿简要结果")
    score_columns = st.columns(5)
    short_labels = ["Overall", "TR", "CC", "LR", "GRA"]
    for column, short_label, score in zip(
        score_columns,
        short_labels,
        draft_1["scores"].values(),
        strict=False,
    ):
        value = (
            format_overall_band(score)
            if short_label == "Overall"
            else (f"{score:.0f}" if score is not None else "-")
        )
        column.metric(short_label, value)

    st.markdown("#### 本次重写重点")
    for focus in draft_training_focus(draft_1["scores"]):
        st.markdown(f"- {focus}")

    cache_user_id = str(cloud_user.id if cloud_user is not None else user_id)
    original_run_id = str(draft_1.get("grading_run_id") or "")
    if not original_run_id:
        original_run_id = "local:" + submission_hash(
            str(draft_1.get("topic") or ""), str(draft_1.get("text") or "")
        )
    draft_2_context_key = (cache_user_id, original_run_id)
    if st.session_state.get("draft_2_context_key") != draft_2_context_key:
        st.session_state.draft_2_context_key = draft_2_context_key
        st.session_state.pop("draft_2_result", None)
        st.session_state.pop("draft_2_text", None)

    current_draft_2_result = st.session_state.get("draft_2_result")
    if isinstance(current_draft_2_result, dict):
        result_owner = str(current_draft_2_result.get("user_id") or cache_user_id)
        result_parent = str(
            current_draft_2_result.get("parent_grading_run_id") or original_run_id
        )
        if (result_owner, result_parent) != draft_2_context_key:
            st.session_state.pop("draft_2_result", None)
            st.session_state.pop("draft_2_text", None)
            current_draft_2_result = None
        elif "draft_2_text" not in st.session_state:
            st.session_state.draft_2_text = str(
                current_draft_2_result.get("text") or ""
            )

    draft_2_text = st.text_area(
        "请根据上方反馈写第二稿",
        height=360,
        key="draft_2_text",
        disabled=read_only or isinstance(current_draft_2_result, dict),
    )
    draft_2_settlement_pending = bool(
        isinstance(current_draft_2_result, dict)
        and current_draft_2_result.get("settlement_pending")
    )
    draft_2_already_generated = bool(
        isinstance(current_draft_2_result, dict) and not draft_2_settlement_pending
    )
    draft_2_button_label = (
        "本篇二稿验证已完成"
        if draft_2_already_generated
        else ("重试同步二稿结果（不重新调用模型）" if draft_2_settlement_pending else "提交第二稿")
    )
    submit_draft_2 = st.button(
        draft_2_button_label,
        type="primary",
        key="submit_draft_2",
        use_container_width=True,
        disabled=read_only or draft_2_already_generated,
    )
    if read_only and not isinstance(current_draft_2_result, dict):
        st.info("这份记录当前为只读，且没有已保存的第二稿结果；不能在此新生成二稿。")

    if submit_draft_2 and not read_only:
        if not draft_2_text.strip():
            st.warning("请先完成第二稿。")
        elif draft_2_text.strip() == draft_1["text"].strip():
            st.warning("第二稿与第一稿完全相同，请根据反馈完成修改后再提交。")
        else:
            draft_2_started_at = time.perf_counter()
            draft_2_fingerprint = submission_hash(str(draft_1["topic"]), draft_2_text)
            scoped_cache_key = draft_2_cache_key(
                cache_user_id, original_run_id, draft_2_fingerprint
            )
            draft_2_cache = st.session_state.setdefault("draft_2_generation_cache", {})
            if not isinstance(draft_2_cache, dict):
                draft_2_cache = {}
                st.session_state.draft_2_generation_cache = draft_2_cache
            cached_generation = draft_2_cache.setdefault(scoped_cache_key, {})
            if not isinstance(cached_generation, dict):
                cached_generation = {}
                draft_2_cache[scoped_cache_key] = cached_generation
            draft_2_attempt_id = str(
                cached_generation.get("attempt_id") or uuid.uuid4()
            )
            cached_generation["attempt_id"] = draft_2_attempt_id
            saved_ticket = cached_generation.get("access_ticket")
            draft_2_ticket = dict(saved_ticket) if isinstance(saved_ticket, dict) else None
            if (
                draft_2_ticket is None
                and cloud_store
                and cloud_user
                and draft_1.get("grading_run_id")
            ):
                try:
                    draft_2_ticket = reserve_second_draft(
                        cloud_store,
                        cloud_user,
                        grading_run_id=str(draft_1["grading_run_id"]),
                        draft_text=draft_2_text,
                        topic=str(draft_1["topic"]),
                    )
                    cached_generation["access_ticket"] = draft_2_ticket
                except GradingAccessError as exc:
                    st.warning(action_reason_message(exc.reason))
                    return
                except (CloudStoreError, AttributeError):
                    st.error("暂时无法安全预留二稿机会，请稍后重试；当前没有扣除次数。")
                    return
            if cloud_store is not None and not cached_generation.get("submitted_recorded"):
                record_usage_event(
                    cloud_store,
                    "second_draft_submitted",
                    user=cloud_user,
                    run_id=str(draft_1.get("grading_run_id") or ""),
                    attempt_id=draft_2_attempt_id,
                    occurrence_key=draft_2_attempt_id,
                    metadata={"draft_number": 2},
                )
                cached_generation["submitted_recorded"] = True
            with st.spinner("正在评分第二稿并生成两稿对比报告..."):
                render_scoring_loader()
                try:
                    draft_2_package, progress_report = generate_draft_2_feedback(
                        provider=provider,
                        model=model,
                        task_type=task_type,
                        topic=draft_1["topic"],
                        draft_1_text=draft_1["text"],
                        draft_1_scores=draft_1["scores"],
                        draft_2_text=draft_2_text,
                        cached_generation=cached_generation,
                    )
                    draft_2_report = str(draft_2_package["report"])
                    draft_2_structured = dict(draft_2_package["structured"])
                    draft_2_scores = score_snapshot(draft_2_structured)
                    training_path = cached_generation.get("training_path")
                    if not cached_generation.get("local_saved"):
                        save_markdown_record(
                            task_type=task_type,
                            topic=draft_1["topic"],
                            essay=draft_2_text,
                            report=draft_2_report,
                            word_count=count_words(draft_2_text),
                            user_id=user_id,
                            examiner_data=draft_2_structured,
                            grading_metadata={
                                "model": draft_2_package["model"],
                                "prompt_version": draft_2_package["prompt_version"],
                                "skill_version": draft_2_package["skill_version"],
                                "schema_version": draft_2_package["schema_version"],
                                "graded_at": draft_2_package["graded_at"],
                            },
                        )
                        training_path = save_draft_training_record(
                            user_id=user_id,
                            task_question=draft_1["topic"],
                            draft_1_text=draft_1["text"],
                            draft_1_scores=draft_1["scores"],
                            draft_1_feedback=draft_1["feedback"],
                            draft_2_text=draft_2_text,
                            draft_2_scores=draft_2_scores,
                            draft_2_feedback=draft_2_report,
                            progress_report=progress_report,
                        )
                        record_grading_event(
                            user_id=user_id,
                            overall_band=draft_2_scores["Overall Band"],
                            essay_word_count=count_words(draft_2_text),
                            model_name=model,
                        )
                        cached_generation["training_path"] = training_path
                        cached_generation["local_saved"] = True

                    linked_ids = (
                        dict(cached_generation.get("cloud_ids") or {})
                        if isinstance(cached_generation.get("cloud_ids"), dict)
                        else {}
                    )
                    settlement_pending = False
                    if cloud_store and cloud_user and draft_1.get("grading_run_id"):
                        try:
                            linked_ids = persist_draft_2_cloud_result(
                                cloud_store,
                                cloud_user,
                                draft_1=draft_1,
                                draft_2_text=draft_2_text,
                                draft_2_package=draft_2_package,
                                draft_2_scores=draft_2_scores,
                                progress_report=progress_report,
                                cached_generation=cached_generation,
                            )
                        except (CloudStoreError, AttributeError) as exc:
                            settlement_pending = True
                            st.warning(
                                "二稿结果已经生成并保留；云端保存或额度状态正在确认。"
                                "可点击上方按钮重试同步，不会重新调用模型，也不会重复保存。"
                            )
                            logging.warning("Draft 2 settlement remains pending: %s", exc)
                    st.session_state.draft_2_result = {
                        "scores": draft_2_scores,
                        "report": draft_2_report,
                        "progress_report": progress_report,
                        "path": training_path,
                        "text": draft_2_text,
                        "grading_run_id": str(linked_ids.get("grading_run_id") or ""),
                        "attempt_id": draft_2_attempt_id,
                        "settlement_pending": settlement_pending,
                        "user_id": cache_user_id,
                        "parent_grading_run_id": original_run_id,
                    }
                    if cloud_store is not None and not cached_generation.get("generated_recorded"):
                        record_usage_event(
                            cloud_store,
                            "second_draft_generated",
                            user=cloud_user,
                            run_id=str(draft_1.get("grading_run_id") or ""),
                            attempt_id=draft_2_attempt_id,
                            occurrence_key=draft_2_attempt_id,
                            metadata={
                                "draft_number": 2,
                                "duration_ms": int((time.perf_counter() - draft_2_started_at) * 1000),
                            },
                        )
                        cached_generation["generated_recorded"] = True
                except AIGraderError as exc:
                    if draft_2_ticket and cloud_store and cloud_user:
                        try:
                            release_second_draft(cloud_store, cloud_user, draft_2_ticket)
                        except CloudStoreError:
                            pass
                        else:
                            cached_generation.pop("access_ticket", None)
                    if cloud_store is not None:
                        record_usage_event(
                            cloud_store,
                            "second_draft_generation_failed",
                            user=cloud_user,
                            run_id=str(draft_1.get("grading_run_id") or ""),
                            attempt_id=draft_2_attempt_id,
                            occurrence_key=draft_2_attempt_id,
                            metadata={
                                "draft_number": 2,
                                "failure_type": type(exc).__name__,
                                "duration_ms": int((time.perf_counter() - draft_2_started_at) * 1000),
                            },
                        )
                    st.error("第二稿评分失败。完整诊断信息如下。")
                    st.code(str(exc), language="text")
                except Exception as exc:
                    if draft_2_ticket and cloud_store and cloud_user:
                        try:
                            release_second_draft(cloud_store, cloud_user, draft_2_ticket)
                        except CloudStoreError:
                            pass
                        else:
                            cached_generation.pop("access_ticket", None)
                    if cloud_store is not None:
                        record_usage_event(
                            cloud_store,
                            "second_draft_generation_failed",
                            user=cloud_user,
                            run_id=str(draft_1.get("grading_run_id") or ""),
                            attempt_id=draft_2_attempt_id,
                            occurrence_key=draft_2_attempt_id,
                            metadata={
                                "draft_number": 2,
                                "failure_type": type(exc).__name__,
                                "duration_ms": int((time.perf_counter() - draft_2_started_at) * 1000),
                            },
                        )
                    st.error("第二稿训练出现意外错误。")
                    st.code(f"{type(exc).__name__}: {exc}", language="text")

    result = st.session_state.get("draft_2_result")
    if isinstance(result, dict):
        if cloud_store is not None:
            record_usage_event(
                cloud_store,
                "diff_viewed",
                user=cloud_user,
                run_id=str(draft_1.get("grading_run_id") or ""),
                attempt_id=str(result.get("attempt_id") or ""),
                occurrence_key=str(
                    result.get("attempt_id") or result.get("grading_run_id") or "current-result"
                ),
                metadata={"source": "second_draft_result"},
            )
        st.divider()
        st.header("两稿对比进步报告")
        render_training_stepper(active=5)
        render_score_change(draft_1["scores"], result["scores"])
        revised_text = str(result.get("text") or "")
        if revised_text:
            st.subheader("真实文本变化")
            st.caption("先对齐段落，再比较段内词语；复制按钮只复制干净第二稿。")
            compare_tab, clean_tab, full_revision_tab = st.tabs(
                ["逐段对照", "清爽阅读", "全文修订"], default="逐段对照"
            )
            with compare_tab:
                st.markdown(
                    paragraph_diff_html(str(draft_1["text"]), revised_text),
                    unsafe_allow_html=True,
                )
            with clean_tab:
                st.code(revised_text.replace("\r\n", "\n").replace("\r", "\n"), language=None, wrap_lines=True)
            with full_revision_tab:
                render_text_diff(str(draft_1["text"]), revised_text)
        st.markdown(result["progress_report"])
        with st.expander("查看第二稿完整评分", expanded=False):
            st.markdown(result["report"])
        if cloud_store is not None:
            render_product_feedback(
                cloud_store,
                cloud_user,
                touchpoint="second_draft",
                run_id=str(draft_1.get("grading_run_id") or ""),
                attempt_id=str(result.get("attempt_id") or ""),
            )


def extract_criteria_details(markdown: str) -> dict[str, dict[str, str]]:
    """Extract per-criterion comments for expandable score details."""
    section = extract_report_section(markdown, 2) or markdown
    details = {
        "Task Response": {"score": "-", "good": "", "problem": ""},
        "Coherence": {"score": "-", "good": "", "problem": ""},
        "Lexical Resource": {"score": "-", "good": "", "problem": ""},
        "Grammar": {"score": "-", "good": "", "problem": ""},
    }

    for line in section.splitlines():
        if not line.strip().startswith("|") or "---" in line:
            continue

        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 4 or cells[0].lower() == "criterion":
            continue

        criterion = cells[0].lower()
        score = cells[2] or cells[1] or "-"
        score_match = re.search(r"\d(?:\.\d)?", score)
        score = score_match.group(0) if score_match else score
        why = clean_markdown_text(cells[3])

        if "task" in criterion:
            key = "Task Response"
        elif "coherence" in criterion:
            key = "Coherence"
        elif "lexical" in criterion:
            key = "Lexical Resource"
        elif "grammar" in criterion or "grammatical" in criterion:
            key = "Grammar"
        else:
            continue

        detail_parts = re.split(r"(?<=[.!?])\s+|;\s+", why)
        positive_markers = (
            "clear",
            "relevant",
            "logical",
            "accurate",
            "appropriate",
            "effective",
            "good",
            "varied",
            "well",
        )
        positive_parts: list[str] = []
        problem_parts: list[str] = []
        for part in detail_parts:
            clauses = re.split(
                r"\s+(?:but|however|although|yet|while)\s+",
                part,
                maxsplit=1,
                flags=re.IGNORECASE,
            )
            positive_clause = clauses[0].strip()
            if any(marker in positive_clause.lower() for marker in positive_markers):
                positive_parts.append(positive_clause.rstrip(","))
            else:
                problem_parts.append(positive_clause)
            if len(clauses) > 1 and clauses[1].strip():
                problem_parts.append(clauses[1].strip())

        details[key]["score"] = score
        details[key]["good"] = positive_parts[0] if positive_parts else ""
        details[key]["problem"] = " ".join(problem_parts) or why

    return details


def extract_problem_evidence_by_criterion(markdown: str) -> dict[str, list[str]]:
    """Extract quoted problem evidence and map it loosely to IELTS criteria."""
    section = extract_report_section(markdown, 4)
    evidence = {
        "Task Response": [],
        "Coherence": [],
        "Lexical Resource": [],
        "Grammar": [],
    }

    problem_blocks = re.split(r"#{2,4}\s*Problem\s*\d+:", section)
    for block in problem_blocks[1:]:
        lower_block = block.lower()
        quotes = re.findall(r'["“]([^"”]+)["”]', block)
        original_match = re.search(r"\*\*Original sentence:\*\*\s*(.+)", block)
        if original_match:
            original = clean_markdown_text(original_match.group(1)).strip('"“”')
            if original:
                quotes.insert(0, original)

        if not quotes:
            continue

        if any(word in lower_block for word in ("grammar", "verb", "sentence structure")):
            key = "Grammar"
        elif any(word in lower_block for word in ("vocabulary", "lexical", "word", "phrase")):
            key = "Lexical Resource"
        elif any(word in lower_block for word in ("coherence", "paragraph", "logic", "progression")):
            key = "Coherence"
        else:
            key = "Task Response"

        for quote in quotes:
            cleaned = quote.strip()
            if cleaned and cleaned not in evidence[key]:
                evidence[key].append(cleaned)

    return evidence


def extract_paragraph_strengths(markdown: str) -> list[str]:
    """Extract positive paragraph-level observations from the existing report."""
    section = extract_report_section(markdown, 6) or markdown
    strengths: list[str] = []
    for match in re.finditer(
        r"(?:\*\*)?(?:What works|加分项)(?:\*\*)?\s*:\s*"
        r"(.+?)(?=\n\s*(?:[-*]\s*)?\*\*|\n#{1,4}|\Z)",
        section,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        item = clean_markdown_text(match.group(1)).lstrip("*- ")
        if item and item not in strengths:
            strengths.append(item)
    return strengths


def render_overall_band(score: float | None) -> None:
    """Render the program-calculated point Overall for the learner."""
    score_text = html.escape(format_overall_band(score))
    st.markdown(
        f"""
        <div class="ep-overall-card">
            <div class="ep-overall-card__label">雅思写作练习 Overall</div>
            <div class="ep-overall-card__value">{score_text}</div>
            <div class="ep-overall-card__note">AI 练习估分，不是 IELTS 官方成绩；四项整数分可继续查看</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_criteria_overview(markdown: str) -> None:
    """Render four expandable IELTS criterion score cards in a readable grid."""
    details = extract_criteria_details(markdown)
    problem_evidence = extract_problem_evidence_by_criterion(markdown)
    strengths = extract_paragraph_strengths(markdown)
    detail_items = list(details.items())

    for row_start in range(0, len(detail_items), 2):
        columns = st.columns(2)
        for index, (column, (label, detail)) in enumerate(
            zip(columns, detail_items[row_start : row_start + 2], strict=False),
            start=row_start,
        ):
            with column:
                render_score_card(label, detail.get("score", "-"), "点击下方查看详情")
                with st.expander("查看详情", expanded=False):
                    good_text = detail.get("good") or (
                        strengths[index % len(strengths)] if strengths else ""
                    )
                    problem_text = detail.get("problem") or "暂未提取到主要问题。"
                    evidence_items = problem_evidence.get(label, [])

                    st.success(f"加分项：{good_text or '暂未提取到明确加分项。'}")

                    if evidence_items:
                        for item in evidence_items[:2]:
                            st.error(f"主要问题依据：{item}")
                    else:
                        st.error(f"主要问题：{problem_text}")


def render_structured_criteria_overview(data: dict[str, object]) -> None:
    """直接使用结构化评分结果展示四项评分，不解析报告标题。"""
    criteria = [item for item in data.get("criteria", []) if isinstance(item, dict)]
    for row_start in range(0, len(criteria), 2):
        columns = st.columns(2)
        for column, item in zip(columns, criteria[row_start : row_start + 2], strict=False):
            label = CRITERION_DISPLAY_NAMES.get(str(item.get("criterion", "")), str(item.get("criterion", "")))
            with column:
                render_score_card(label, str(item.get("score", "-")), "点击下方查看评分依据")
                with st.expander("查看详情", expanded=False):
                    st.markdown(f"**评分说明：** {item.get('reason', '暂无说明。')}")
                    for evidence in item.get("evidence", [])[:2]:
                        st.info(f"原文依据：{evidence}")
                    st.warning(f"下一档限制：{item.get('next_band_limit', '暂无说明。')}")


def render_problem_cards(markdown: str) -> None:
    """Render main problems as warning cards."""
    st.subheader("主要问题")
    problems = extract_bullets(extract_report_section(markdown, 4), limit=5)
    if not problems:
        st.info("这份报告暂未提取到主要问题。")
        return

    for problem in problems:
        st.warning(problem)


def render_suggestion_cards(markdown: str) -> None:
    """Render improvement suggestions as success cards."""
    st.subheader("提分建议")
    suggestions = extract_bullets(extract_report_section(markdown, 3), limit=5)
    if not suggestions:
        suggestions = extract_bullets(extract_report_section(markdown, 10), limit=3)
    if not suggestions:
        st.info("这份报告暂未提取到提分建议。")
        return

    for suggestion in suggestions:
        st.success(suggestion)


def report_before_interactive_practice(markdown: str) -> str:
    """Return the static report content before interactive practice sections."""
    parts = re.split(
        r"\n#{1,3}\s*11\.\s*单句提分训练",
        markdown,
        maxsplit=1,
    )
    report = parts[0].rstrip()
    report = re.sub(
        r"\n#{1,3}\s*9\s*[.:、-]?\s+Seven-Day Training Plan.*?"
        r"(?=\n#{1,3}\s*10\s*[.:、-]?\s+|\Z)",
        "",
        report,
        flags=re.DOTALL | re.IGNORECASE,
    )
    report = re.sub(
        r"(#{1,3}\s*)10(\s*[.:、-]?\s+Next Practice Task)",
        r"\g<1>9\g<2>",
        report,
        count=1,
        flags=re.IGNORECASE,
    )
    report = re.sub(
        r"^#\s*(?:IELTS Writing Examiner Report|雅思写作批改报告|雅思写作练习估分与反馈)\s*",
        "",
        report,
    ).strip()
    return report


def render_grouped_examiner_report(markdown: str) -> None:
    """Render the static examiner report as focused learning sections."""
    report = report_before_interactive_practice(markdown)
    groups = [
        ("评分依据", "report-basis", (1, 2)),
        ("核心提分方向", "report-priorities", (3, 4)),
        ("逐句与段落批改", "report-corrections", (5, 6)),
        ("Band 7.5 示范改写", "report-rewrite", (7,)),
        ("本篇可迁移表达与下一步", "report-next", (8, 9)),
    ]

    rendered_any = False
    for label, anchor_id, section_numbers in groups:
        sections = [
            extract_report_section(report, number)
            for number in section_numbers
        ]
        content = "\n\n".join(section for section in sections if section)
        if not content:
            continue
        rendered_any = True
        render_anchor(anchor_id)
        with st.expander(label, expanded=False):
            st.markdown(content)

    if not rendered_any:
        with st.expander("完整评分报告", expanded=False):
            st.markdown(report)


def extract_practice_sentences(markdown: str) -> list[str]:
    """Extract original sentences from the single-sentence practice section."""
    section_match = re.search(
        r"#{1,3}\s*11\.\s*单句提分训练(?P<section>.*)",
        markdown,
        flags=re.DOTALL,
    )
    if not section_match:
        section_match = re.search(
            r"单句提分训练(?P<section>.*)",
            markdown,
            flags=re.DOTALL,
        )
    if not section_match:
        section_match = re.search(
            r"【练习任务】(?P<section>.*)",
            markdown,
            flags=re.DOTALL,
        )
    if not section_match:
        return []

    section = section_match.group("section")
    section = re.split(r"#{1,3}\s*12\.\s*写作提升验证", section, maxsplit=1)[0]
    exercise_part = section.split("【参考改写】", 1)[0]
    candidates = re.findall(r'["“]([^"”]+)["”]', exercise_part)

    sentences: list[str] = []
    for candidate in candidates:
        cleaned = candidate.strip()
        if cleaned and cleaned not in {"（原句）", "(原句)"} and cleaned not in sentences:
            sentences.append(cleaned)

    return sentences[:5]


def extract_sentence_references(markdown: str) -> dict[str, str]:
    """Extract sentence-level reference rewrites from the existing correction table."""
    section = extract_report_section(markdown, 5)
    references: dict[str, str] = {}

    for line in section.splitlines():
        if not line.strip().startswith("|") or "---" in line:
            continue

        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3 or cells[0].lower() == "original":
            continue

        original = clean_markdown_text(cells[0]).strip('"“”')
        improved = clean_markdown_text(cells[-1]).strip('"“”')
        if original and improved:
            references[original] = improved

    return references


def find_sentence_reference(sentence: str, references: dict[str, str]) -> str | None:
    """Find a compatible reference rewrite for a practice sentence."""
    normalized_sentence = re.sub(r"\s+", " ", sentence).strip().lower()
    for original, improved in references.items():
        normalized_original = re.sub(r"\s+", " ", original).strip().lower()
        if normalized_sentence == normalized_original:
            return improved
        if normalized_sentence in normalized_original or normalized_original in normalized_sentence:
            return improved
    return None


def _normalize_practice_original_text(value: object) -> str:
    """Collapse Unicode whitespace without weakening the legacy task identity."""
    return " ".join(str(value or "").split())


def _match_practice_attempt(
    rows: list[dict[str, object]],
    *,
    task_kind: str,
    task_key_hash: str,
    task_index: int,
    original_text: str,
) -> tuple[dict[str, object] | None, bool]:
    """Prefer a current hash, then strictly match one pre-hash history row."""
    normalized_original = _normalize_practice_original_text(original_text)
    legacy_match: dict[str, object] | None = None

    for row in rows:
        if str(row.get("task_kind") or "") != task_kind:
            continue

        stored_hash = str(row.get("task_key_hash") or "").strip()
        if stored_hash:
            if stored_hash == task_key_hash:
                return row, False
            continue

        if row.get("task_index") != task_index:
            continue
        if not normalized_original or _normalize_practice_original_text(
            row.get("original_text")
        ) != normalized_original:
            continue
        legacy_match = row

    return legacy_match, legacy_match is not None


def render_sentence_practice(
    sentences: list[str],
    provider: str,
    model: str,
    references: dict[str, str] | None = None,
    cloud_store: SupabaseStore | None = None,
    cloud_user: CloudUser | None = None,
    grading_run_id: str = "",
    error_tags: list[str] | None = None,
    read_only: bool = False,
    persisted_attempts: list[dict[str, object]] | None = None,
    goals: dict[str, str] | None = None,
) -> None:
    """Render the interactive sentence rewrite practice."""
    st.subheader("单句提分训练")

    if not sentences:
        st.info("还没有识别到可练习的原句。请先重新生成一次报告。")
        return

    st.caption("先自己改写，再点击点评。AI 会根据你的版本给出具体建议。")
    references = references or {}
    goals = goals or {}
    if persisted_attempts is None and cloud_store and cloud_user and grading_run_id:
        try:
            persisted_attempts = cloud_store.list_practice_attempts_for_run(
                cloud_user, grading_run_id
            )
        except (CloudStoreError, AttributeError):
            st.caption("已保存的训练点评暂时无法读取，请稍后刷新。")
    persisted_attempts = persisted_attempts or []

    for index, original_sentence in enumerate(sentences, start=1):
        sentence_id = hashlib.md5(original_sentence.encode("utf-8")).hexdigest()[:10]
        task_key_hash = hashlib.sha256(
            f"sentence\0{sentence_id}".encode("utf-8")
        ).hexdigest()
        scope_id = hashlib.md5(
            f"{grading_run_id or 'local'}|sentence|{sentence_id}".encode("utf-8")
        ).hexdigest()[:12]
        rewrite_key = f"sentence_rewrite_{scope_id}"
        reference_key = f"sentence_reference_{scope_id}"
        button_key = f"sentence_review_button_{scope_id}"
        feedback_key = f"sentence_feedback_{scope_id}"
        access_key = f"sentence_feedback_access_{scope_id}"
        revision_key = f"sentence_revision_{scope_id}"
        mastered_key = f"sentence_mastered_{scope_id}"
        saved_key = f"sentence_saved_{scope_id}"

        with st.container(border=True):
            persisted_attempt, legacy_restore = _match_practice_attempt(
                persisted_attempts,
                task_kind="sentence",
                task_key_hash=task_key_hash,
                task_index=index,
                original_text=original_sentence,
            )

            if persisted_attempt and str(persisted_attempt.get("feedback") or "").strip():
                st.session_state[feedback_key] = str(persisted_attempt["feedback"])
                st.session_state.setdefault(
                    rewrite_key, str(persisted_attempt.get("submitted_text") or "")
                )
                if str(persisted_attempt.get("revision_text") or ""):
                    st.session_state.setdefault(
                        revision_key, str(persisted_attempt["revision_text"])
                    )
                st.session_state[saved_key] = True

            pending_ticket = st.session_state.get(access_key)
            if (
                st.session_state.get(feedback_key)
                and isinstance(pending_ticket, dict)
                and st.session_state.get(saved_key) is False
                and cloud_store
                and cloud_user
                and grading_run_id
            ):
                try:
                    persisted_attempt = cloud_store.save_practice_attempt(
                        cloud_user,
                        grading_run_id=grading_run_id,
                        task_kind="sentence",
                        task_key=sentence_id,
                        task_index=index,
                        original_text=original_sentence,
                        submitted_text=str(st.session_state.get(rewrite_key) or ""),
                        feedback=str(st.session_state[feedback_key]),
                        training_action_id=str(pending_ticket.get("action_id") or ""),
                        training_flow_id=str(pending_ticket.get("flow_id") or ""),
                        error_tags=error_tags,
                    )
                except CloudStoreError:
                    st.caption("点评仍在等待安全保存；保存成功前不会结算额度。")
                else:
                    legacy_restore = False
                    st.session_state[saved_key] = True

            proof_ticket: dict[str, object] | None = None
            if not legacy_restore:
                if isinstance(pending_ticket, dict) and st.session_state.get(saved_key) is True:
                    proof_ticket = pending_ticket
                elif (
                    persisted_attempt
                    and persisted_attempt.get("training_flow_id")
                    and not persisted_attempt.get("settled_at")
                ):
                    proof_ticket = {
                        "flow_id": str(persisted_attempt["training_flow_id"]),
                        "action_id": str(persisted_attempt.get("training_action_id") or ""),
                    }
            if (
                st.session_state.get(feedback_key)
                and proof_ticket
                and cloud_store
                and cloud_user
            ):
                try:
                    complete_training_feedback_action(
                        cloud_store, cloud_user, proof_ticket
                    )
                except CloudStoreError:
                    st.caption("点评已保留，额度状态仍在确认；本题不会再次调用模型。")
                else:
                    st.session_state.pop(access_key, None)
            st.markdown(f"**原句 {index}:** {original_sentence}")
            if goal := str(goals.get(original_sentence) or "").strip():
                st.markdown(f"**本题目标：** {goal}")

            if (
                legacy_restore
                and persisted_attempt
                and str(persisted_attempt.get("feedback") or "").strip()
            ):
                st.caption("这是升级前保存的历史点评，已按题号与原文严格匹配，只读展示。")
                st.markdown("**你的改写：**")
                st.info(str(persisted_attempt.get("submitted_text") or ""))
                st.markdown(str(persisted_attempt["feedback"]))
                if str(persisted_attempt.get("revision_text") or "").strip():
                    st.markdown("**点评后的改写：**")
                    st.info(str(persisted_attempt["revision_text"]))
                if persisted_attempt.get("status") == "mastered":
                    st.success("已标记为掌握。")
                continue

            if read_only:
                if persisted_attempt and str(persisted_attempt.get("feedback") or "").strip():
                    st.markdown("**你的改写：**")
                    st.info(str(persisted_attempt.get("submitted_text") or ""))
                    st.markdown(str(persisted_attempt["feedback"]))
                    if str(persisted_attempt.get("revision_text") or "").strip():
                        st.markdown("**点评后的改写：**")
                        st.info(str(persisted_attempt["revision_text"]))
                    if persisted_attempt.get("status") == "mastered":
                        st.success("已标记为掌握。")
                else:
                    st.caption("本题还没有已保存的 AI 点评。")
                continue

            rewrite = st.text_area(
                "你的改写",
                key=rewrite_key,
                height=90,
                placeholder="在这里输入你改写后的完整句子。",
                disabled=bool(st.session_state.get(feedback_key)),
            )

            if st.button("显示参考答案", key=reference_key):
                reference = find_sentence_reference(original_sentence, references)
                if reference:
                    st.info(reference)
                else:
                    st.info("暂时没有匹配到参考答案。你提交改写后，AI 点评会给出更自然的版本。")

            if st.button(
                "已获得本题点评" if st.session_state.get(feedback_key) else "点评我的改写",
                key=button_key,
                disabled=bool(st.session_state.get(feedback_key)),
            ):
                if not rewrite.strip():
                    st.warning("请先输入你的改写句子。")
                else:
                    saved_ticket = st.session_state.get(access_key)
                    ticket = dict(saved_ticket) if isinstance(saved_ticket, dict) else None
                    try:
                        if ticket is None and cloud_store and cloud_user and grading_run_id:
                            ticket = reserve_training_feedback_action(
                                cloud_store,
                                cloud_user,
                                grading_run_id=grading_run_id,
                                task_kind="sentence",
                                task_key=sentence_id,
                            )
                            st.session_state[access_key] = ticket
                    except GradingAccessError as exc:
                        st.warning(action_reason_message(exc.reason))
                    except (CloudStoreError, AttributeError):
                        st.error("暂时无法安全预留本次点评，请稍后重试；当前没有扣除次数。")
                    else:
                        with st.spinner("AI 正在点评你的句子..."):
                            try:
                                feedback = review_sentence_rewrite(
                                    provider=provider,
                                    original_sentence=original_sentence,
                                    student_rewrite=rewrite,
                                    model=model,
                                )
                            except AIGraderError as exc:
                                if ticket and cloud_store and cloud_user:
                                    try:
                                        release_training_feedback_action(cloud_store, cloud_user, ticket)
                                    except CloudStoreError:
                                        pass
                                    else:
                                        st.session_state.pop(access_key, None)
                                st.error("点评失败，本次不扣次数。完整诊断信息如下。")
                                st.code(str(exc), language="text")
                            except Exception as exc:
                                if ticket and cloud_store and cloud_user:
                                    try:
                                        release_training_feedback_action(cloud_store, cloud_user, ticket)
                                    except CloudStoreError:
                                        pass
                                    else:
                                        st.session_state.pop(access_key, None)
                                st.error("点评时出现意外错误，本次不扣次数。")
                                st.code(
                                    f"Exception Type: {type(exc).__name__}\n\n{exc}",
                                    language="text",
                                )
                            else:
                                st.session_state[feedback_key] = feedback
                                st.session_state[saved_key] = not bool(
                                    cloud_store and cloud_user and grading_run_id
                                )
                                if cloud_store and cloud_user and grading_run_id:
                                    try:
                                        persisted_attempt = cloud_store.save_practice_attempt(
                                            cloud_user,
                                            grading_run_id=grading_run_id,
                                            task_kind="sentence",
                                            task_key=sentence_id,
                                            task_index=index,
                                            original_text=original_sentence,
                                            submitted_text=rewrite,
                                            feedback=feedback,
                                            training_action_id=str(
                                                (ticket or {}).get("action_id") or ""
                                            ),
                                            training_flow_id=str(
                                                (ticket or {}).get("flow_id") or ""
                                            ),
                                            error_tags=error_tags,
                                        )
                                        legacy_restore = False
                                        st.session_state[saved_key] = True
                                    except CloudStoreError as exc:
                                        st.warning(
                                            f"点评已生成，但尚未安全保存：{exc}。保存成功前不会结算额度。"
                                        )
                                    else:
                                        try:
                                            sync_learning_item_status(
                                                cloud_store,
                                                cloud_user,
                                                grading_run_id=grading_run_id,
                                                source_text=original_sentence,
                                                mastered=False,
                                            )
                                        except CloudStoreError as exc:
                                            st.warning(f"点评已保存，但学习卡片暂时无法同步：{exc}")
                                if (
                                    ticket
                                    and st.session_state.get(saved_key) is True
                                    and cloud_store
                                    and cloud_user
                                    and not legacy_restore
                                ):
                                    try:
                                        complete_training_feedback_action(cloud_store, cloud_user, ticket)
                                    except CloudStoreError:
                                        st.warning("点评已生成并保留；额度状态正在确认，请不要重复提交本题。")
                                    else:
                                        st.session_state.pop(access_key, None)

            if st.session_state.get(feedback_key):
                st.markdown(st.session_state[feedback_key])
                st.markdown("**再改一次：** 根据点评写出你的最终版本。")
                revision = st.text_area(
                    "第二次改写",
                    key=revision_key,
                    height=90,
                    placeholder="吸收点评后再写一次，完成后标记掌握。",
                )
                if st.button(
                    "标记为已掌握",
                    key=mastered_key,
                    use_container_width=True,
                    disabled=st.session_state.get(saved_key) is False,
                ):
                    if not revision.strip():
                        st.warning("请先完成第二次改写。")
                    elif revision.strip() == rewrite.strip():
                        st.warning("第二次改写需要体现你根据点评做出的调整。")
                    else:
                        if cloud_store and cloud_user and grading_run_id:
                            try:
                                cloud_store.save_practice_attempt(
                                    cloud_user,
                                    grading_run_id=grading_run_id,
                                    task_kind="sentence",
                                    task_key=sentence_id,
                                    task_index=index,
                                    original_text=original_sentence,
                                    submitted_text=rewrite,
                                    feedback=st.session_state[feedback_key],
                                    revision_text=revision,
                                    mastered=True,
                                    error_tags=error_tags,
                                )
                                sync_learning_item_status(
                                    cloud_store,
                                    cloud_user,
                                    grading_run_id=grading_run_id,
                                    source_text=original_sentence,
                                    mastered=True,
                                )
                            except CloudStoreError as exc:
                                st.warning(f"已完成练习，但云端同步失败：{exc}")
                        if cloud_store is not None:
                            record_usage_event(
                                cloud_store,
                                "sentence_training_completed",
                                user=cloud_user,
                                run_id=grading_run_id,
                                occurrence_key=f"sentence-{index}",
                                metadata={"item_index": index, "task_kind": "sentence"},
                            )
                        st.session_state.pending_training_feedback_run_id = grading_run_id
                        st.success("已掌握。本次改写会计入你的学习档案。")


def extract_logic_practice_tasks(markdown: str) -> list[dict[str, str]]:
    """Extract logic-level writing practice tasks from the report."""
    section_match = re.search(
        r"#{1,3}\s*12\.\s*写作提升验证(?P<section>.*)",
        markdown,
        flags=re.DOTALL,
    )
    if not section_match:
        section_match = re.search(
            r"【提升练习】(?P<section>.*)",
            markdown,
            flags=re.DOTALL,
        )
    if not section_match:
        return []

    section = section_match.group("section")
    blocks = re.split(r"#{2,4}\s*任务\s*\d+", section)
    tasks: list[dict[str, str]] = []

    for block in blocks[1:]:
        problem_match = re.search(r"问题：\s*(.+)", block)
        quotes = re.findall(r'["“]([^"”]+)["”]', block, flags=re.DOTALL)
        if not quotes:
            continue

        problem = problem_match.group(1).strip() if problem_match else "逻辑/结构问题"
        original_fragment = quotes[0].strip()
        if original_fragment:
            tasks.append(
                {
                    "problem": problem,
                    "original": original_fragment,
                }
            )

    return tasks[:3]


def render_logic_practice(
    tasks: list[dict[str, str]],
    provider: str,
    model: str,
    cloud_store: SupabaseStore | None = None,
    cloud_user: CloudUser | None = None,
    grading_run_id: str = "",
    error_tags: list[str] | None = None,
    read_only: bool = False,
    persisted_attempts: list[dict[str, object]] | None = None,
) -> None:
    """Render interactive logic and structure rewrite practice."""
    st.subheader("写作提升验证")

    if not tasks:
        st.info("还没有识别到可练习的思路提升任务。请先重新生成一次报告。")
        return

    st.caption("重写一个关键片段，再让 AI 对比原文和你的版本。")
    if persisted_attempts is None and cloud_store and cloud_user and grading_run_id:
        try:
            persisted_attempts = cloud_store.list_practice_attempts_for_run(
                cloud_user, grading_run_id
            )
        except (CloudStoreError, AttributeError):
            st.caption("已保存的训练点评暂时无法读取，请稍后刷新。")
    persisted_attempts = persisted_attempts or []

    for index, task in enumerate(tasks, start=1):
        logic_source = f"{task['problem']}|{task['original']}"
        logic_id = hashlib.md5(logic_source.encode("utf-8")).hexdigest()[:10]
        task_key_hash = hashlib.sha256(f"logic\0{logic_id}".encode("utf-8")).hexdigest()
        scope_id = hashlib.md5(
            f"{grading_run_id or 'local'}|logic|{logic_id}".encode("utf-8")
        ).hexdigest()[:12]
        rewrite_key = f"logic_rewrite_{scope_id}"
        button_key = f"logic_review_button_{scope_id}"
        feedback_key = f"logic_feedback_{scope_id}"
        access_key = f"logic_feedback_access_{scope_id}"
        revision_key = f"logic_revision_{scope_id}"
        mastered_key = f"logic_mastered_{scope_id}"
        saved_key = f"logic_saved_{scope_id}"

        with st.container(border=True):
            persisted_attempt, legacy_restore = _match_practice_attempt(
                persisted_attempts,
                task_kind="logic",
                task_key_hash=task_key_hash,
                task_index=index,
                original_text=task["original"],
            )
            if persisted_attempt and str(persisted_attempt.get("feedback") or "").strip():
                st.session_state[feedback_key] = str(persisted_attempt["feedback"])
                st.session_state.setdefault(
                    rewrite_key, str(persisted_attempt.get("submitted_text") or "")
                )
                if str(persisted_attempt.get("revision_text") or ""):
                    st.session_state.setdefault(
                        revision_key, str(persisted_attempt["revision_text"])
                    )
                st.session_state[saved_key] = True

            pending_ticket = st.session_state.get(access_key)
            if (
                st.session_state.get(feedback_key)
                and isinstance(pending_ticket, dict)
                and st.session_state.get(saved_key) is False
                and cloud_store
                and cloud_user
                and grading_run_id
            ):
                try:
                    persisted_attempt = cloud_store.save_practice_attempt(
                        cloud_user,
                        grading_run_id=grading_run_id,
                        task_kind="logic",
                        task_key=logic_id,
                        task_index=index,
                        original_text=task["original"],
                        submitted_text=str(st.session_state.get(rewrite_key) or ""),
                        feedback=str(st.session_state[feedback_key]),
                        training_action_id=str(pending_ticket.get("action_id") or ""),
                        training_flow_id=str(pending_ticket.get("flow_id") or ""),
                        error_tags=error_tags,
                    )
                except CloudStoreError:
                    st.caption("点评仍在等待安全保存；保存成功前不会结算额度。")
                else:
                    legacy_restore = False
                    st.session_state[saved_key] = True

            proof_ticket: dict[str, object] | None = None
            if not legacy_restore:
                if isinstance(pending_ticket, dict) and st.session_state.get(saved_key) is True:
                    proof_ticket = pending_ticket
                elif (
                    persisted_attempt
                    and persisted_attempt.get("training_flow_id")
                    and not persisted_attempt.get("settled_at")
                ):
                    proof_ticket = {
                        "flow_id": str(persisted_attempt["training_flow_id"]),
                        "action_id": str(persisted_attempt.get("training_action_id") or ""),
                    }
            if (
                st.session_state.get(feedback_key)
                and proof_ticket
                and cloud_store
                and cloud_user
            ):
                try:
                    complete_training_feedback_action(
                        cloud_store, cloud_user, proof_ticket
                    )
                except CloudStoreError:
                    st.caption("点评已保留，额度状态仍在确认；本题不会再次调用模型。")
                else:
                    st.session_state.pop(access_key, None)
            st.markdown(f"**任务 {index}:** {task['problem']}")
            st.markdown(str(task.get("task") or "改写下面内容，补足本题指出的逻辑问题。"))
            st.markdown(f"> {task['original']}")
            requirements = task.get("requirements")
            if isinstance(requirements, list):
                for requirement in requirements:
                    if str(requirement).strip():
                        st.markdown(f"**完成检查：** {requirement}")

            if (
                legacy_restore
                and persisted_attempt
                and str(persisted_attempt.get("feedback") or "").strip()
            ):
                st.caption("这是升级前保存的历史点评，已按题号与原文严格匹配，只读展示。")
                st.markdown("**你的重写：**")
                st.info(str(persisted_attempt.get("submitted_text") or ""))
                st.markdown(str(persisted_attempt["feedback"]))
                if str(persisted_attempt.get("revision_text") or "").strip():
                    st.markdown("**点评后的重写：**")
                    st.info(str(persisted_attempt["revision_text"]))
                if persisted_attempt.get("status") == "mastered":
                    st.success("已标记为掌握。")
                continue

            if read_only:
                if persisted_attempt and str(persisted_attempt.get("feedback") or "").strip():
                    st.markdown("**你的重写：**")
                    st.info(str(persisted_attempt.get("submitted_text") or ""))
                    st.markdown(str(persisted_attempt["feedback"]))
                    if str(persisted_attempt.get("revision_text") or "").strip():
                        st.markdown("**点评后的重写：**")
                        st.info(str(persisted_attempt["revision_text"]))
                    if persisted_attempt.get("status") == "mastered":
                        st.success("已标记为掌握。")
                else:
                    st.caption("本题还没有已保存的 AI 点评。")
                continue

            rewrite = st.text_area(
                "你的重写",
                key=rewrite_key,
                height=130,
                placeholder="按照本题目标，在这里输入你的重写版本。",
                disabled=bool(st.session_state.get(feedback_key)),
            )

            if st.button(
                "已获得本题点评" if st.session_state.get(feedback_key) else "点评我的思路重写",
                key=button_key,
                disabled=bool(st.session_state.get(feedback_key)),
            ):
                if not rewrite.strip():
                    st.warning("请先输入你的重写内容。")
                else:
                    saved_ticket = st.session_state.get(access_key)
                    ticket = dict(saved_ticket) if isinstance(saved_ticket, dict) else None
                    try:
                        if ticket is None and cloud_store and cloud_user and grading_run_id:
                            ticket = reserve_training_feedback_action(
                                cloud_store,
                                cloud_user,
                                grading_run_id=grading_run_id,
                                task_kind="logic",
                                task_key=logic_id,
                            )
                            st.session_state[access_key] = ticket
                    except GradingAccessError as exc:
                        st.warning(action_reason_message(exc.reason))
                    except (CloudStoreError, AttributeError):
                        st.error("暂时无法安全预留本次点评，请稍后重试；当前没有扣除次数。")
                    else:
                        with st.spinner("AI 正在对比你的逻辑结构..."):
                            try:
                                feedback = review_logic_rewrite(
                                    provider=provider,
                                    problem=task["problem"],
                                    original_fragment=task["original"],
                                    student_rewrite=rewrite,
                                    model=model,
                                )
                            except AIGraderError as exc:
                                if ticket and cloud_store and cloud_user:
                                    try:
                                        release_training_feedback_action(cloud_store, cloud_user, ticket)
                                    except CloudStoreError:
                                        pass
                                    else:
                                        st.session_state.pop(access_key, None)
                                st.error("点评失败，本次不扣次数。完整诊断信息如下。")
                                st.code(str(exc), language="text")
                            except Exception as exc:
                                if ticket and cloud_store and cloud_user:
                                    try:
                                        release_training_feedback_action(cloud_store, cloud_user, ticket)
                                    except CloudStoreError:
                                        pass
                                    else:
                                        st.session_state.pop(access_key, None)
                                st.error("点评时出现意外错误，本次不扣次数。")
                                st.code(
                                    f"Exception Type: {type(exc).__name__}\n\n{exc}",
                                    language="text",
                                )
                            else:
                                st.session_state[feedback_key] = feedback
                                st.session_state[saved_key] = not bool(
                                    cloud_store and cloud_user and grading_run_id
                                )
                                if cloud_store and cloud_user and grading_run_id:
                                    try:
                                        persisted_attempt = cloud_store.save_practice_attempt(
                                            cloud_user,
                                            grading_run_id=grading_run_id,
                                            task_kind="logic",
                                            task_key=logic_id,
                                            task_index=index,
                                            original_text=task["original"],
                                            submitted_text=rewrite,
                                            feedback=feedback,
                                            training_action_id=str(
                                                (ticket or {}).get("action_id") or ""
                                            ),
                                            training_flow_id=str(
                                                (ticket or {}).get("flow_id") or ""
                                            ),
                                            error_tags=error_tags,
                                        )
                                        legacy_restore = False
                                        st.session_state[saved_key] = True
                                    except CloudStoreError as exc:
                                        st.warning(
                                            f"点评已生成，但尚未安全保存：{exc}。保存成功前不会结算额度。"
                                        )
                                    else:
                                        try:
                                            sync_learning_item_status(
                                                cloud_store,
                                                cloud_user,
                                                grading_run_id=grading_run_id,
                                                source_text=task["original"],
                                                mastered=False,
                                            )
                                        except CloudStoreError as exc:
                                            st.warning(f"点评已保存，但学习卡片暂时无法同步：{exc}")
                                if (
                                    ticket
                                    and st.session_state.get(saved_key) is True
                                    and cloud_store
                                    and cloud_user
                                    and not legacy_restore
                                ):
                                    try:
                                        complete_training_feedback_action(cloud_store, cloud_user, ticket)
                                    except CloudStoreError:
                                        st.warning("点评已生成并保留；额度状态正在确认，请不要重复提交本题。")
                                    else:
                                        st.session_state.pop(access_key, None)

            if st.session_state.get(feedback_key):
                st.markdown(st.session_state[feedback_key])
                st.markdown("**再写一次：** 把点评落实到完整的论点—解释—例子链条。")
                revision = st.text_area(
                    "第二次重写",
                    key=revision_key,
                    height=130,
                    placeholder="根据点评重写最终版本。",
                )
                if st.button(
                    "标记逻辑训练为已掌握",
                    key=mastered_key,
                    use_container_width=True,
                    disabled=st.session_state.get(saved_key) is False,
                ):
                    if not revision.strip():
                        st.warning("请先完成第二次重写。")
                    elif revision.strip() == rewrite.strip():
                        st.warning("第二次重写需要体现点评后的调整。")
                    else:
                        if cloud_store and cloud_user and grading_run_id:
                            try:
                                cloud_store.save_practice_attempt(
                                    cloud_user,
                                    grading_run_id=grading_run_id,
                                    task_kind="logic",
                                    task_key=logic_id,
                                    task_index=index,
                                    original_text=task["original"],
                                    submitted_text=rewrite,
                                    feedback=st.session_state[feedback_key],
                                    revision_text=revision,
                                    mastered=True,
                                    error_tags=error_tags,
                                )
                                sync_learning_item_status(
                                    cloud_store,
                                    cloud_user,
                                    grading_run_id=grading_run_id,
                                    source_text=task["original"],
                                    mastered=True,
                                )
                            except CloudStoreError as exc:
                                st.warning(f"已完成练习，但云端同步失败：{exc}")
                        if cloud_store is not None:
                            record_usage_event(
                                cloud_store,
                                "logic_training_completed",
                                user=cloud_user,
                                run_id=grading_run_id,
                                occurrence_key=f"logic-{index}",
                                metadata={"item_index": index, "task_kind": "logic"},
                            )
                        st.session_state.pending_training_feedback_run_id = grading_run_id
                        st.success("已掌握。这次逻辑重写已加入学习档案。")


def list_correction_history(user_id: str) -> list[dict[str, object]]:
    """Read saved records for the dashboard trend chart."""
    records_dir = BASE_DIR / "records" / user_id
    if not records_dir.exists():
        return []

    history: list[dict[str, object]] = []
    for path in sorted(records_dir.glob("ielts_*.md")):
        markdown = path.read_text(encoding="utf-8")
        created_match = re.search(r"- (?:创建时间|Created At):\s*(.+)", markdown)
        task_match = re.search(r"- (?:任务类型|Task Type):\s*(.+)", markdown)
        words_match = re.search(r"- Word Count:\s*(\d+)", markdown)

        score = calculate_overall_band(markdown)
        json_path = path.with_suffix(".json")
        if score is None and json_path.exists():
            try:
                metadata = json.loads(json_path.read_text(encoding="utf-8"))
                stored_score = metadata.get("overall_band") if isinstance(metadata, dict) else None
                score = float(stored_score) if isinstance(stored_score, (int, float)) else None
            except (OSError, ValueError, json.JSONDecodeError):
                score = None
        history.append(
            {
                "file": path.name,
                "path": path,
                "created_at": created_match.group(1) if created_match else path.stem,
                "task_type": task_match.group(1) if task_match else "未知",
                "word_count": int(words_match.group(1)) if words_match else None,
                "score": score,
            }
        )

    return history


def render_history(user_id: str) -> None:
    """Render local history with the point Overall estimate."""
    history = list_correction_history(user_id)
    scored_history = [item for item in history if item["score"] is not None]
    training_history = list_draft_training_history(user_id)

    st.subheader("历史练习估分")
    if not scored_history:
        st.info("还没有评分记录。完成一次批改后，这里会显示你的分数趋势。")
    else:
        for item in reversed(scored_history[-10:]):
            st.caption(
                f"{item['created_at']} · Overall "
                f"{format_overall_band(item['score'])}"
            )

    if training_history:
        st.subheader("第二稿训练历史")
        for record in reversed(training_history[-5:]):
            draft_1_score = record.get("draft_1_scores", {}).get("Overall Band")
            draft_2_score = record.get("draft_2_scores", {}).get("Overall Band")
            before = format_overall_band(draft_1_score) if isinstance(draft_1_score, (int, float)) else "-"
            after = format_overall_band(draft_2_score) if isinstance(draft_2_score, (int, float)) else "-"
            with st.expander(
                f"第一稿 → 第二稿 · Overall：{before} → {after}",
                expanded=False,
            ):
                st.caption(str(record.get("timestamp", "")))
                st.markdown(str(record.get("progress_report", "")))


def render_learning_dashboard(store: SupabaseStore, user: CloudUser) -> bool:
    """Render the action-first signed-in home page from a minimal cloud snapshot."""
    # Browser components trigger an extra rerun when their initial value arrives.
    # Reuse this display-only snapshot briefly within one user's page visit.
    cached = st.session_state.get("latest_home_snapshot")
    try:
        if (
            isinstance(cached, dict)
            and cached.get("user_id") == user.id
            and 0 <= time.monotonic() - cached["fetched_at"] < 15
        ):
            runs, pending = cached["snapshot"]
        else:
            st.session_state.pop("latest_home_snapshot", None)
            runs, pending = store.get_home_snapshot(user)
            st.session_state.latest_home_snapshot = {
                "user_id": user.id,
                "fetched_at": time.monotonic(),
                "snapshot": (runs, pending),
            }
    except CloudStoreError as exc:
        st.warning(f"云端学习档案暂时不可用：{exc}")
        render_home_heading(
            title="学习首页",
            subtitle="暂时无法读取上次进度，你仍可正常开始新的写作练习。",
        )
        render_home_action_card(
            eyebrow="继续练习",
            title="先选一道剑雅真题开始",
            body="题库与写作输入不依赖学习档案，当前内容不会被自动覆盖。",
            primary_label="从剑雅真题开始",
            primary_href="?page=write&mode=topics",
            secondary_actions=(("粘贴自己的题目", "?page=write"),),
        )
        return True

    summary = build_home_summary(runs, pending)
    if not summary.has_history:
        return False

    render_home_heading(
        title="学习首页",
        subtitle="继续未完成的训练，或从剑雅真题开始下一篇。",
    )

    if summary.has_pending and summary.pending is not None:
        title = "把上次没完成的一步做完"
        body = summary.pending.summary
        secondary_actions = (
            ("从剑雅真题选题", "?page=write&mode=topics"),
            ("查看学习档案", "?page=growth"),
        )
        eyebrow = "继续当前训练"
    else:
        title = "选一道剑雅真题，开始下一篇"
        body = "当前没有未完成训练，可以换一道题继续积累同题材观点与表达。"
        latest_run_id = summary.latest_grading_run_id or ""
        secondary_actions = (
            ("粘贴自己的题目", "?page=write"),
            ("查看上次报告", f"?{urlencode({'page': 'report', 'run_id': latest_run_id})}"),
        )
        eyebrow = "开始下一轮"

    render_home_action_card(
        eyebrow=eyebrow,
        title=title,
        body=body,
        primary_label=summary.primary_label,
        primary_href=summary.primary_href,
        secondary_actions=secondary_actions,
        facts=tuple((fact.label, fact.value) for fact in summary.facts),
    )
    return True


def render_demo_page() -> None:
    """Render the current report and training flow from validated local data."""
    try:
        package = load_demo_package()
    except DemoPackageError:
        st.error("零 Token 示例暂时无法读取，你仍可正常开始自己的写作练习。")
        st.button("去写作批改", type="primary", on_click=navigate, args=("write",))
        return

    render_anchor("demo-top")
    apply_pending_scroll()
    st.markdown(
        """
        <section class="ep-demo-intro">
            <span>0 TOKEN · CURRENT PRODUCT WALKTHROUGH</span>
            <h1>先看一篇作文，如何走完整个提分闭环</h1>
            <p>这份示例使用当前评分结构与页面组件，展示原文证据、问题地图、词汇卡、训练和第二稿；浏览不会调用模型。</p>
            <div><b>输入</b><i>→</i><b>报告</b><i>→</i><b>训练</b><i>→</i><b>第二稿</b></div>
        </section>
        """,
        unsafe_allow_html=True,
    )

    back_column, fill_column, spacer_column = st.columns([1.05, 1.7, 3.25])
    with back_column:
        st.button(
            "← 返回首页",
            on_click=navigate,
            args=("home",),
            use_container_width=True,
            key="demo_back_top",
        )
    with fill_column:
        st.button(
            "把示例原稿填入写作区",
            type="primary",
            on_click=load_sample_and_show_workspace,
            use_container_width=True,
            key="demo_fill_top",
        )

    st.caption("静态示例 · 当前报告格式 · 不调用模型 · 不写入学习档案")
    input_tab, report_tab, training_tab, draft_tab = st.tabs(
        ["① 输入", "② 报告", "③ 训练", "④ 第二稿"]
    )
    with input_tab:
        st.subheader("题目与第一稿")
        question_column, essay_column = st.columns([0.82, 1.38], gap="large")
        with question_column:
            with st.container(border=True):
                st.markdown("**英文作文题目**")
                st.write(package.question)
                st.caption("Task 2 · 双边讨论并给出观点")
        with essay_column:
            with st.container(border=True):
                st.markdown(f"**学生第一稿 · {package.word_count} 词**")
                st.write(package.essay)
    with report_tab:
        structured = package.structured
        st.subheader("先看分数，再核对原文证据")
        render_overall_band(float(structured.get("overall_band") or 0))
        priorities = [
            item for item in structured.get("priorities", [])
            if isinstance(item, dict)
        ]
        if priorities:
            st.markdown(
                '<div class="ep-result-summary"><strong>本轮最重要：</strong> '
                f'{html.escape(str(priorities[0].get("title") or ""))}</div>',
                unsafe_allow_html=True,
            )
        render_structured_criteria_overview(structured)
        diagnosis_tab, map_tab, full_tab = st.tabs(
            ["重点诊断", "原文问题地图", "完整报告"]
        )
        with diagnosis_tab:
            if priorities:
                st.markdown("### 本轮只优先解决这两项")
                columns = st.columns(min(2, len(priorities)))
                for index, item in enumerate(priorities[:2]):
                    with columns[index]:
                        with st.container(border=True):
                            st.markdown(f"#### {item.get('title', '提分重点')}")
                            st.write(item.get("why", ""))
                            st.success(str(item.get("action", "")))
                            st.caption(f"完成检查：{item.get('success_check', '')}")
            render_problem_cards(package.report)
            render_suggestion_cards(package.report)
        with map_tab:
            corrections = [
                item for item in structured.get("sentence_corrections", [])
                if isinstance(item, dict)
            ]
            st.caption("问题地图、原文定位与词汇卡均由本地静态数据渲染，不会额外调用模型。")
            if corrections:
                st.markdown("### 这篇文章的问题路径")
                st.markdown(build_issue_map_html(corrections), unsafe_allow_html=True)
                st.markdown("### 原文定位")
                marked_essay, unmatched_nodes = map_essay_issues(package.essay, corrections)
                st.markdown(f'<div class="issue-map">{marked_essay}</div>', unsafe_allow_html=True)
                if unmatched_nodes:
                    st.caption("未可靠定位的问题节点：" + "、".join(f"#{item}" for item in unmatched_nodes))
            vocabulary_items = report_vocabulary_items(structured, package.essay)
            st.markdown("### 原文词汇推荐与可优化词")
            if vocabulary_items:
                st.markdown(build_vocabulary_cards_html(vocabulary_items), unsafe_allow_html=True)
        with full_tab:
            render_grouped_examiner_report(package.report)
    with training_tab:
        st.subheader("把报告变成可以动手完成的训练")
        st.caption("这里展示正式流程会生成的训练任务；示例页不提交答案，也不会触发评审模型。")
        sentence_tab, logic_tab, expression_tab = st.tabs(
            ["单句训练", "逻辑训练", "本篇可迁移表达"]
        )
        with sentence_tab:
            for index, task in enumerate(structured.get("sentence_training", []), start=1):
                if not isinstance(task, dict):
                    continue
                with st.container(border=True):
                    st.markdown(f"**练习 {index} · {task.get('goal', '改写句子')}**")
                    st.write(task.get("original", ""))
                    with st.expander("查看参考改写", expanded=False):
                        st.write(task.get("reference", ""))
        with logic_tab:
            for index, task in enumerate(structured.get("logic_training", []), start=1):
                if not isinstance(task, dict):
                    continue
                with st.container(border=True):
                    st.markdown(f"**逻辑任务 {index} · {task.get('problem', '补足论证')}**")
                    st.write(task.get("original", ""))
                    st.info(str(task.get("task", "")))
                    requirements = task.get("requirements", [])
                    if isinstance(requirements, list):
                        st.caption("完成要求：" + "；".join(str(item) for item in requirements))
        with expression_tab:
            expressions = [
                item for item in structured.get("useful_expressions", [])
                if isinstance(item, dict)
            ]
            for item in expressions:
                with st.container(border=True):
                    st.markdown(f"**{item.get('expression', '')}** · {item.get('meaning', '')}")
                    st.write(item.get("example", ""))
                    st.caption(str(item.get("usage_note", "")))
    with draft_tab:
        st.subheader("第二稿：把反馈真正写回自己的文章")
        st.caption("正式流程会保留第一稿基线，再对比真实第二稿；这里用静态第二稿展示最终效果。")
        compare_draft_1, compare_draft_2, compare_result = st.tabs(
            ["第一稿", "第二稿示范", "两稿变化"]
        )
        with compare_draft_1:
            st.markdown(package.essay)
        with compare_draft_2:
            st.markdown(package.draft_2)
        with compare_result:
            labels = {"retained": "保留", "improved": "已改善", "next": "下一步"}
            for key in ("retained", "improved", "next"):
                value = package.draft_changes.get(key)
                if value:
                    st.markdown(f"- **{labels[key]}：** {value}")

    st.success("这份静态示例已按当前产品流程更新；查看全过程不消耗 Token。")


APP_ROUTES = {
    "home": "学习首页",
    "write": "写作批改",
    "report": "批改报告",
    "training": "专项训练",
    "growth": "学习档案",
}


def navigate(route: str, run_id: str = "", mode: str = "") -> None:
    """Switch the visible product page and preserve a shareable run context."""
    route = route if route in APP_ROUTES else "home"
    st.session_state.page_mode = route
    st.query_params["page"] = route
    if run_id:
        st.session_state.active_run_id = run_id
        st.query_params["run_id"] = run_id
    elif route == "write":
        st.query_params.pop("run_id", None)
    if mode:
        st.query_params["mode"] = mode
    else:
        st.query_params.pop("mode", None)


def open_purchase_offer(authenticated: bool) -> None:
    """Open the purchase offer, preserving it as the post-login destination."""
    if authenticated:
        navigate("home", "", "purchase")
        return
    open_cloud_login("home", "purchase")


def hydrate_grading_run(
    run: dict[str, object],
    *,
    user_id: str = "",
    draft_revision: dict[str, object] | None = None,
) -> None:
    """Make a cloud record the current cross-page learning context."""
    essay_data = run.get("essays") if isinstance(run.get("essays"), dict) else {}
    structured = run.get("report_json") if isinstance(run.get("report_json"), dict) else {}
    report = str(run.get("report_markdown") or "")
    run_id = str(run.get("id") or "")
    essay_id = str(run.get("essay_id") or "")
    topic = str(essay_data.get("question") or "")
    essay = str(essay_data.get("content") or "")
    st.session_state.latest_report = report
    st.session_state.latest_structured = structured
    st.session_state.latest_prompt_version = str(run.get("prompt_version") or "")
    st.session_state.latest_cloud_ids = {"essay_id": essay_id, "grading_run_id": run_id}
    st.session_state.active_run_id = run_id
    st.session_state.topic_input = topic
    st.session_state.essay_input = essay
    draft_context = (str(user_id), run_id)
    if st.session_state.get("draft_2_context_key") != draft_context:
        st.session_state.pop("draft_2_result", None)
        st.session_state.pop("draft_2_text", None)
    st.session_state.draft_2_context_key = draft_context
    if structured:
        st.session_state.draft_1_snapshot = {
            "topic": topic,
            "text": essay,
            "feedback": report,
            "scores": score_snapshot(structured),
            "structured": structured,
            "essay_id": essay_id,
            "grading_run_id": run_id,
        }
    if (
        isinstance(draft_revision, dict)
        and str(draft_revision.get("grading_run_id") or "") == run_id
    ):
        restored = draft_2_result_from_revision(
            draft_revision,
            user_id=str(user_id),
            grading_run_id=run_id,
        )
        st.session_state.draft_2_result = restored
        st.session_state.draft_2_text = str(restored.get("text") or "")


def ensure_run_context(store: SupabaseStore, user: CloudUser | None) -> None:
    # A saved home/archive URL can carry a run_id, but these pages do not
    # need the full essay and second draft before they can be displayed.
    if st.session_state.get("page_mode", "home") not in {"write", "report", "training"}:
        return
    requested = str(st.query_params.get("run_id", "") or "")
    current = str(st.session_state.get("active_run_id", "") or "")
    if not requested or requested == current or user is None:
        return
    try:
        run = store.get_grading_run(user, requested)
    except CloudStoreError as exc:
        st.warning(f"暂时无法恢复这份批改记录：{exc}")
        return
    revision = None
    if run:
        try:
            revision = store.get_draft_revision(user, requested)
        except (CloudStoreError, AttributeError):
            st.session_state.draft_2_restore_warning = True
    if run:
        hydrate_grading_run(run, user_id=user.id, draft_revision=revision)


def ensure_learning_assets(store: SupabaseStore, user: CloudUser | None) -> None:
    structured = st.session_state.get("latest_structured")
    run_id = str(st.session_state.get("latest_cloud_ids", {}).get("grading_run_id", ""))
    if user is None or not run_id or not isinstance(structured, dict) or not structured:
        return
    rows = build_learning_items(
        structured, user_id=user.id, grading_run_id=run_id,
        question=str(st.session_state.get("topic_input") or ""),
    )
    try:
        store.upsert_learning_items(user, rows)
        st.session_state.learning_assets_ready = True
    except (CloudStoreError, AttributeError):
        st.session_state.learning_assets_ready = False


def render_app_navigation(user: CloudUser | None, *, store: SupabaseStore) -> None:
    """Render the persistent product-level navigation instead of a long-page index."""
    with st.sidebar:
        st.markdown("## EssayPilot")
        st.caption("Task 2 学习工作台")
        active = str(st.session_state.get("page_mode", "home"))
        for route, label in APP_ROUTES.items():
            prefix = "● " if route == active else ""
            st.button(
                f"{prefix}{label}",
                key=f"nav_{route}",
                use_container_width=True,
                on_click=navigate,
                args=(route,),
            )
        if store.enabled:
            st.button(
                "购买批改包",
                key="sidebar_purchase",
                type="primary",
                use_container_width=True,
                on_click=open_purchase_offer,
                args=(user is not None,),
            )
        st.divider()
        st.caption(f"固定评分模型 · {PRODUCTION_MODEL}")
        st.markdown(
            '<small>学习词典卡：EssayPilot 按作文语境整理；旧报告可由 '
            '<a href="https://github.com/globalwordnet/english-wordnet" '
            'target="_blank" rel="noopener noreferrer">Open English WordNet 2025</a> · '
            'CC BY 4.0 补充（非朗文原文）</small>',
            unsafe_allow_html=True,
        )
        if user is not None:
            st.caption(f"已登录：{user.email}")
            cached_membership = st.session_state.get("membership_entitlement_cache")
            if (
                isinstance(cached_membership, dict)
                and cached_membership.get("user_id") == user.id
            ):
                st.caption(
                    entitlement_caption(
                        cached_membership.get("entitlement")
                        if isinstance(cached_membership.get("entitlement"), dict)
                        else {}
                    )
                )
            else:
                st.caption("训练权益可在报告或训练页查看")
            st.button("我的学习档案", key="sidebar_profile", on_click=navigate, args=("growth",), use_container_width=True)
            st.button("退出登录", on_click=logout_cloud_user, use_container_width=True)
        elif store.enabled:
            st.caption("访客模式 · 当前浏览器可免费生成 1 次首稿完整报告")
            st.button("登录 / 保存学习档案", on_click=open_cloud_login, use_container_width=True)
        else:
            st.caption("本地开发模式")
    with st.container(key="desktop_account_bar", border=True):
        if user is not None:
            account_col, purchase_col, profile_col = st.columns([3, 1, 1])
            account_col.caption(f"已登录：{user.email}")
            purchase_col.button(
                "购买批改包",
                key="desktop_purchase",
                type="primary",
                on_click=open_purchase_offer,
                args=(True,),
                use_container_width=True,
            )
            profile_col.button("我的学习档案", key="desktop_profile", on_click=navigate, args=("growth",), use_container_width=True)
        elif store.enabled:
            account_col, purchase_col, login_col = st.columns([3, 1, 1])
            account_col.caption("当前浏览器可先免费生成 1 次首稿报告；登录后保存报告，AI 训练和二稿需开通体验包。")
            purchase_col.button(
                "购买批改包",
                key="desktop_purchase",
                type="primary",
                on_click=open_purchase_offer,
                args=(False,),
                use_container_width=True,
            )
            login_col.button("登录 / 保存档案", key="desktop_login", on_click=open_cloud_login, use_container_width=True)
    if user is not None and st.session_state.get("pending_guest_claim"):
        st.warning("这次游客批改仍在当前页面中，尚未保存到学习档案。")
        if st.button("重试保存这次批改", key="retry_guest_claim", use_container_width=True):
            retry_store = SupabaseStore()
            retry_store.bind_auth_session(
                session_cloud_user,
                lambda refreshed: write_cloud_user_state(
                    refreshed, persist=True, request_rerun=True
                ),
                mark_cloud_session_invalid,
            )
            if claim_guest_result(retry_store, user):
                st.success("已保存到学习档案。")
                st.rerun()
    with st.container(key="mobile_account_bar", border=True):
        if user is not None:
            st.caption(f"已登录：{user.email}")
            st.button(
                "购买批改包",
                key="mobile_purchase",
                type="primary",
                on_click=open_purchase_offer,
                args=(True,),
                use_container_width=True,
            )
            st.button(
                "退出登录",
                key="mobile_logout",
                on_click=logout_cloud_user,
                use_container_width=True,
            )
        elif store.enabled:
            st.caption("当前为访客浏览；登录后可跨设备保存报告和成长记录。")
            st.button(
                "购买批改包",
                key="mobile_purchase",
                type="primary",
                on_click=open_purchase_offer,
                args=(False,),
                use_container_width=True,
            )
            st.button(
                "登录并同步进度",
                key="mobile_login",
                on_click=open_cloud_login,
                use_container_width=True,
            )
        else:
            st.caption("本地开发模式")
    active = str(st.session_state.get("page_mode", "home"))
    short_labels = {"home": "首页", "write": "写作", "report": "报告", "training": "训练", "growth": "档案"}
    run_id = str(st.query_params.get("run_id") or st.session_state.get("active_run_id") or st.session_state.get("latest_cloud_ids", {}).get("grading_run_id", ""))
    links: list[str] = []
    for route in APP_ROUTES:
        query = f"?page={route}" + (f"&run_id={html.escape(run_id)}" if run_id else "")
        active_class = " active" if route == active else ""
        links.append(f'<a class="{active_class.strip()}" href="{query}">{short_labels[route]}</a>')
    st.markdown(f'<nav class="mobile-product-nav">{"".join(links)}</nav>', unsafe_allow_html=True)


def _run_priority(structured: dict[str, object]) -> str:
    priorities = structured.get("priorities") if isinstance(structured, dict) else []
    if isinstance(priorities, list) and priorities and isinstance(priorities[0], dict):
        return str(priorities[0].get("title") or priorities[0].get("action") or "")
    return "继续完成本轮专项训练"


def render_home_page(store: SupabaseStore, user: CloudUser | None) -> None:
    if str(st.query_params.get("mode", "") or "") == "purchase":
        render_home_heading(
            title="购买批改包",
            subtitle="查看当前账号可购买的套餐，付款后提交订单号即可自动开通。",
        )
        render_founder_offer(
            store,
            user,
            key="home_purchase",
            intro="提交订单号后自动开通并开始计算 30 天有效期；付款前请确认套餐与退款说明。",
        )
        return

    if user is not None:
        if render_learning_dashboard(store, user):
            return
        render_home_heading(
            title="学习首页",
            subtitle="先完成第一篇，再根据真实反馈安排下一步训练。",
        )
        render_home_action_card(
            eyebrow="开始第一轮",
            title="从一道剑雅真题开始",
            body="选择题目只会带入写作区，不会自动生成作文、批改或调用模型。",
            primary_label="从剑雅真题开始",
            primary_href="?page=write&mode=topics",
            secondary_actions=(("粘贴自己的题目", "?page=write"),),
        )
        render_home_preview_link()
        return

    render_guest_home_intro(
        title="先完成一篇，再决定练什么",
        body=(
            "提交 IELTS Writing Task 2 作文，获得四项评分、原文证据和清楚的下一步。"
            "流程：评分定位 → 针对训练 → 第二稿验证。"
        ),
        steps=(),
    )


def grade_submission(
    store: SupabaseStore,
    user: CloudUser | None,
    *,
    topic: str,
    essay: str,
    reserve_model_access: Callable[[str], dict[str, object]] | None = None,
    complete_model_access: Callable[[dict[str, object], str], None] | None = None,
    release_model_access: Callable[[dict[str, object]], None] | None = None,
) -> None:
    """Run the existing fixed-model grading workflow and open its report page."""
    word_count = count_words(essay)
    fingerprint = submission_hash(topic, essay)
    actor_key = first_report_actor_key(
        user, str(st.session_state.get("user_id") or "")
    )
    scoped_cache_key = first_report_cache_key(actor_key, fingerprint)
    grading_cache = st.session_state.setdefault("grading_cache", {})
    if not isinstance(grading_cache, dict):
        grading_cache = {}
        st.session_state.grading_cache = grading_cache
    cached_entry = grading_cache.get(scoped_cache_key)
    package: dict[str, object] | None = None
    locked_scoring_package: dict[str, object] | None = None
    cloud_ids: dict[str, str] = {}
    reused_result = False
    pending_accesses = st.session_state.setdefault("pending_first_report_access", {})
    if not isinstance(pending_accesses, dict):
        pending_accesses = {}
        st.session_state.pending_first_report_access = pending_accesses
    access_ticket = (
        dict(pending_accesses.get(scoped_cache_key) or {})
        if isinstance(pending_accesses, dict)
        else {}
    )
    if isinstance(cached_entry, dict):
        cached_scoring = cached_entry.get("scoring_package")
        if (
            isinstance(cached_scoring, dict)
            and cached_scoring.get("prompt_version") == SCORING_PROMPT_VERSION
            and cached_scoring.get("skill_version") == SCORING_SKILL_VERSION
            and isinstance(cached_scoring.get("scoring"), dict)
            and cached_entry.get("scoring_topic") == topic
            and cached_entry.get("scoring_essay") == essay
        ):
            locked_scoring_package = dict(cached_scoring)
        candidate = dict(cached_entry.get("package") or {})
        if candidate.get("prompt_version") == REPORT_PROMPT_VERSION:
            package = candidate
            if user is not None and cached_entry.get("cloud_user_id") == user.id:
                cloud_ids = dict(cached_entry.get("cloud_ids") or {})
            reused_result = bool(package)
        elif (
            candidate.get("scoring_prompt_version") == SCORING_PROMPT_VERSION
            and candidate.get("skill_version") == SCORING_SKILL_VERSION
            and isinstance(candidate.get("scoring"), dict)
        ):
            locked_scoring_package = {
                "provider": candidate.get("provider") or "OpenAI",
                "model": candidate.get("model") or PRODUCTION_MODEL,
                "response_model": candidate.get("response_model"),
                "system_fingerprint": candidate.get("system_fingerprint"),
                "reasoning_effort": candidate.get("reasoning_effort") or "none",
                "prompt_version": SCORING_PROMPT_VERSION,
                "skill_version": SCORING_SKILL_VERSION,
                "scoring": candidate["scoring"],
                "usage": {},
            }
    if package is None and user is not None:
        try:
            cached_cloud = store.find_cached_grading(user, fingerprint, REPORT_PROMPT_VERSION)
        except CloudStoreError:
            cached_cloud = None
            st.session_state.cloud_cache_warning = True
        if cached_cloud:
            structured_cloud = dict(cached_cloud.get("report_json") or {})
            package = {
                "model": str(cached_cloud.get("model") or PRODUCTION_MODEL),
                "schema_version": str(structured_cloud.get("schema_version") or "2.0"),
                "prompt_version": str(cached_cloud.get("prompt_version") or ""),
                "skill_version": str(cached_cloud.get("skill_version") or ""),
                "graded_at": str(cached_cloud.get("created_at") or ""),
                "structured": structured_cloud,
                "report": str(cached_cloud.get("report_markdown") or ""),
                "usage": {},
            }
            cloud_ids = {
                "essay_id": str(cached_cloud.get("essay_id") or ""),
                "grading_run_id": str(cached_cloud.get("id") or ""),
            }
            reused_result = True
        elif locked_scoring_package is None:
            try:
                cached_score = store.find_cached_scoring(user, fingerprint, SCORING_PROMPT_VERSION)
            except CloudStoreError:
                cached_score = None
            if cached_score:
                cached_json = cached_score.get("report_json") or {}
                if isinstance(cached_json, dict):
                    locked = cached_json.get("locked_scoring_decision")
                    if isinstance(locked, dict):
                        locked_scoring_package = {
                            "provider": "OpenAI",
                            "model": str(cached_score.get("model") or PRODUCTION_MODEL),
                            "prompt_version": SCORING_PROMPT_VERSION,
                            "skill_version": SCORING_SKILL_VERSION,
                            "scoring": locked,
                            "usage": {},
                        }
    if package is None:
        if not access_ticket and reserve_model_access is not None:
            access_ticket = dict(reserve_model_access(fingerprint) or {})
            if access_ticket and not access_ticket.get("local"):
                pending_accesses[scoped_cache_key] = access_ticket
        try:
            if locked_scoring_package is None:
                locked_scoring_package = grade_scoring_decision(
                    task_type="Task 2", topic=topic, essay=essay,
                )
            # Keep the validated score if teaching fails; retries only regenerate
            # teaching. A score-only entry must never count as a complete report.
            grading_cache[scoped_cache_key] = {
                "scoring_package": locked_scoring_package,
                "scoring_topic": topic,
                "scoring_essay": essay,
            }
            package = grade_essay_package(
                task_type="Task 2",
                topic=topic,
                essay=essay,
                locked_scoring_package=locked_scoring_package,
            )
        except Exception:
            if access_ticket and release_model_access is not None:
                try:
                    release_model_access(access_ticket)
                except (CloudStoreError, AttributeError):
                    # Keep the same reservation for a safe retry when release is uncertain.
                    pass
                else:
                    pending_accesses.pop(scoped_cache_key, None)
            raise
        # Persist the valid model result in session before any filesystem/cloud write.
        # A later retry can finish settlement without calling the model again.
        grading_cache[scoped_cache_key] = {
            "package": package,
            "cloud_ids": {},
            "cloud_user_id": user.id if user is not None else "",
        }
    report = str(package["report"])
    structured = dict(package["structured"])
    scores = score_snapshot(structured)
    saved_path = None
    error_book_path = None
    if user is not None:
        saved_path = save_markdown_record(
            task_type="Task 2", topic=topic, essay=essay, report=report,
            word_count=word_count, user_id=user.id,
            parsed_result={"ok": True, "data": {"overall_band": structured["overall_band"], "criteria_scores": {k: v for k, v in scores.items() if k != "Overall Band"}}, "raw": report, "error": ""},
            examiner_data=structured,
            grading_metadata={
                "model": package["model"], "prompt_version": package["prompt_version"],
                "skill_version": package["skill_version"], "schema_version": package["schema_version"],
                "graded_at": package["graded_at"], "usage": package["usage"],
            }, content_hash=fingerprint,
        )
        error_book_path = append_error_book(
            task_type="Task 2", topic=topic, report=report, user_id=user.id,
        )
    if user is not None and not cloud_ids:
        try:
            cloud_ids = store.save_grading_cycle(
                user, question=topic, essay=essay, word_count=word_count,
                package=package, content_hash=fingerprint,
            )
        except CloudStoreError:
            st.session_state.cloud_save_warning = True
    grading_cache[scoped_cache_key] = {
        "package": package,
        "cloud_ids": cloud_ids,
        "cloud_user_id": user.id if user is not None else "",
    }
    if access_ticket and complete_model_access is not None:
        grading_run_id = str(cloud_ids.get("grading_run_id") or "")
        if access_ticket.get("kind") == "membership" and not grading_run_id:
            raise GradingSettlementError(
                "报告已经生成，但云端保存尚未完成。再次提交会复用本次结果，不会重新调用模型。"
            )
        try:
            complete_model_access(access_ticket, grading_run_id)
        except (CloudStoreError, AttributeError) as exc:
            st.session_state.first_report_settlement_warning = True
            raise GradingSettlementError(
                "报告已经生成，但权益状态暂时无法确认。请稍后用相同内容重试；不会重新调用模型。"
            ) from exc
        else:
            pending_accesses.pop(scoped_cache_key, None)
            clear_membership_cache()
    st.session_state.latest_report = report
    st.session_state.latest_structured = structured
    st.session_state.latest_prompt_version = str(package["prompt_version"])
    st.session_state.latest_cloud_ids = cloud_ids
    st.session_state.latest_saved_path = saved_path
    st.session_state.latest_error_book_path = error_book_path
    st.session_state.draft_1_snapshot = {
        "topic": topic, "text": essay, "feedback": report, "scores": scores,
        "structured": structured, "essay_id": cloud_ids.get("essay_id", ""),
        "grading_run_id": cloud_ids.get("grading_run_id", ""),
    }
    st.session_state.draft_2_active = False
    st.session_state.draft_2_result = None
    st.session_state.grading_failed = False
    if user is None:
        st.session_state.pending_guest_claim = {
            "topic": topic,
            "essay": essay,
            "word_count": word_count,
            "fingerprint": fingerprint,
            "package": package,
            "actor_key": actor_key,
        }
    if reused_result:
        st.session_state.reused_result_notice = True
    record_grading_event(
        user_id=user.id if user is not None else st.session_state.user_id,
        overall_band=float(structured["overall_band"]),
        essay_word_count=word_count,
        model_name=PRODUCTION_MODEL,
    )
    ensure_learning_assets(store, user)
    navigate("report", str(cloud_ids.get("grading_run_id", "")))


def render_topic_bank_picker() -> None:
    """Render the secondary topic picker before the essay editor."""
    topics_requested = str(st.query_params.get("mode", "") or "") == "topics"
    topics_expanded = topics_requested or bool(st.session_state.pop("topic_bank_expanded", False))
    with st.container(key="topic_bank_panel"):
        with st.expander("从主题题库选题", expanded=topics_expanded):
            st.markdown("### 主题连练")
            st.info(
                "收录 Cambridge IELTS 16–20 Academic Writing Test 1–4，"
                "按 IELTS Task 2 常见题材整理，仅供练习，不代表考题预测。"
            )
            try:
                topic_bank = load_topic_bank()
            except TopicBankError:
                st.warning("主题题库暂时无法加载。你仍可在上方直接粘贴题目并正常批改。")
                topic_bank = []

            pending = st.session_state.get("pending_topic_selection")
            if isinstance(pending, dict):
                st.warning(
                    "作文输入区已有内容。是否保留现有作文，并将题目更换为刚才选择的练习题？"
                )
                st.caption(str(pending.get("question") or ""))
                confirm_col, cancel_col = st.columns(2)
                confirm_col.button(
                    "确认：保留作文并更换题目",
                    key="confirm_topic_selection",
                    use_container_width=True,
                    on_click=confirm_pending_topic_selection,
                )
                cancel_col.button(
                    "取消换题",
                    key="cancel_topic_selection",
                    use_container_width=True,
                    on_click=cancel_pending_topic_selection,
                )

            if topic_bank:
                available_categories = list(TOPIC_LABELS)
                stored_category = str(st.session_state.get("topic_bank_category") or "")
                if stored_category not in TOPIC_LABELS:
                    st.session_state.topic_bank_category = available_categories[0]
                selected_category = st.selectbox(
                    "选择练习题材",
                    options=available_categories,
                    format_func=lambda key: TOPIC_LABELS[key],
                    key="topic_bank_category",
                )
                for item in filter_topics_by_category(topic_bank, selected_category):
                    with st.container(key=f"topic_card_{item['id']}", border=True):
                        st.markdown(
                            f'<span class="ep-topic-type">'
                            f'{html.escape(QUESTION_TYPE_LABELS[item["question_type"]])}</span>',
                            unsafe_allow_html=True,
                        )
                        st.markdown(f"**{item['question']}**")
                        st.caption(
                            f"来源：{item['source_book']} · {item['source_test']}"
                        )
                        st.caption(f"练习重点：{item['practice_focus']}")
                        st.button(
                            "用这题开始写",
                            key=f"choose_topic_{item['id']}",
                            use_container_width=True,
                            on_click=select_topic_from_bank,
                            args=(item,),
                        )


def render_write_page(store: SupabaseStore, user: CloudUser | None) -> None:
    st.markdown('<div class="section-kicker">写作批改</div>', unsafe_allow_html=True)
    st.title("提交 IELTS Writing Task 2 作文")
    render_training_stepper(active=1)
    st.caption(f"评分固定使用 {PRODUCTION_MODEL}；失败后保留题目和正文，不切换模型。")
    if st.session_state.pop("cloud_cache_warning", False):
        st.warning("云端历史暂时无法读取，本次仍可继续批改。")
    if st.session_state.pop("cloud_save_warning", False):
        st.warning("报告已保存在当前设备，但云端同步暂时失败；请稍后重试。")

    render_topic_bank_picker()
    render_anchor("writing-input")
    apply_pending_scroll()
    if st.session_state.pop("topic_selection_notice", False):
        st.success("题目已带入写作区；你的作文和已有训练记录均未改动。")
    with st.container(key="essay_editor"):
        st.markdown(
            '<div class="ep-editor-note"><span>Task 2 · 题目与正文保持原始段落</span>'
            '<span>唯一主操作：开始批改</span></div>',
            unsafe_allow_html=True,
        )
        topic = st.text_area(
            "英文作文题目",
            height=120,
            placeholder="请粘贴完整的 Task 2 英文题目。",
            key="topic_input",
        )
        essay = st.text_area(
            "你的英文作文",
            height=420,
            placeholder="请粘贴完整英文作文。",
            key="essay_input",
        )
        word_count = count_words(essay)
        col_words, col_model = st.columns(2)
        with col_words:
            render_score_card("当前词数", str(word_count), "Task 2 建议 250 词以上")
        with col_model:
            render_score_card("固定模型", PRODUCTION_MODEL, "评分标准保持一致")
        warning = word_count_warning("Task 2", word_count) if essay.strip() else ""
        if warning:
            st.warning(warning)
        label = (
            f"使用 {PRODUCTION_MODEL} 重新评分"
            if st.session_state.get("grading_failed")
            else "开始批改作文"
        )
        if st.button(label, type="primary", use_container_width=True):
            if not topic.strip() or not essay.strip():
                st.error("请同时填写英文作文题目和作文正文。")
                return
            hashed = str(st.session_state.get("visitor_hash") or "")
            grading_attempt_id = str(uuid.uuid4())
            st.session_state.latest_grading_attempt_id = grading_attempt_id

            def reserve_first_report_access(content_hash: str) -> dict[str, object]:
                if not require_ai_access_backend(store):
                    return {"allowed": True, "local": True, "kind": "local"}
                if not hashed:
                    raise CloudStoreError("访客身份尚未准备完成，请刷新页面后重试。")
                guest_allowed = store.reserve_guest_trial(hashed, grading_attempt_id)
                if guest_allowed:
                    return {
                        "allowed": True,
                        "kind": "guest",
                        "visitor_hash": hashed,
                        "flow_id": grading_attempt_id,
                    }
                if user is None:
                    raise GradingAccessError("free_report_used")
                result = store.reserve_membership_run(
                    user,
                    grading_attempt_id,
                    content_hash,
                )
                existing_run_id = str(
                    result.get("existing_run_id") or result.get("grading_run_id") or ""
                )
                if result.get("cached") and existing_run_id:
                    raise GradingAccessError(
                        str(result.get("reason") or "existing_result"),
                        existing_run_id=existing_run_id,
                    )
                if not result.get("allowed"):
                    raise GradingAccessError(
                        str(result.get("reason") or "membership_inactive")
                    )
                return {
                    **result,
                    "kind": "membership",
                    "flow_id": grading_attempt_id,
                }

            def complete_first_report_access(
                ticket: dict[str, object], grading_run_id: str
            ) -> None:
                kind = str(ticket.get("kind") or "")
                flow_id = str(ticket.get("flow_id") or "")
                if kind == "guest":
                    if not store.complete_guest_trial(
                        str(ticket.get("visitor_hash") or ""), flow_id
                    ):
                        raise CloudStoreError("免费报告额度状态暂时无法确认。")
                    return
                if kind == "membership":
                    result = store.complete_membership_run(user, flow_id, grading_run_id)
                    if not result.get("completed"):
                        raise CloudStoreError(
                            action_reason_message(result.get("reason"))
                        )

            def release_first_report_access(ticket: dict[str, object]) -> None:
                kind = str(ticket.get("kind") or "")
                flow_id = str(ticket.get("flow_id") or "")
                if kind == "guest":
                    if not store.release_guest_trial(
                        str(ticket.get("visitor_hash") or ""), flow_id
                    ):
                        raise CloudStoreError("免费报告额度释放状态暂时无法确认。")
                    return
                if kind == "membership":
                    result = store.release_membership_run(user, flow_id)
                    if not result.get("released"):
                        raise CloudStoreError(
                            action_reason_message(result.get("reason"))
                        )

            grading_started_at = time.perf_counter()
            record_usage_event(
                store,
                "first_draft_submitted",
                user=user,
                attempt_id=grading_attempt_id,
                occurrence_key=grading_attempt_id,
                metadata={"draft_number": 1},
            )
            with st.spinner("正在评分、核对原文证据并生成训练任务……"):
                render_scoring_loader()
                try:
                    grade_submission(
                        store,
                        user,
                        topic=topic,
                        essay=essay,
                        reserve_model_access=reserve_first_report_access,
                        complete_model_access=complete_first_report_access,
                        release_model_access=release_first_report_access,
                    )
                    grading_duration_ms = int((time.perf_counter() - grading_started_at) * 1000)
                    generated_run_id = str(
                        st.session_state.get("latest_cloud_ids", {}).get("grading_run_id", "")
                    )
                    record_usage_event(
                        store,
                        "report_generated",
                        user=user,
                        run_id=generated_run_id,
                        attempt_id=grading_attempt_id,
                        occurrence_key=grading_attempt_id,
                        metadata={
                            "cached": bool(st.session_state.get("reused_result_notice")),
                            "duration_ms": grading_duration_ms,
                        },
                    )
                    st.rerun()
                except GradingAccessError as exc:
                    if exc.existing_run_id and user is not None:
                        try:
                            existing_run = store.get_grading_run(user, exc.existing_run_id)
                        except CloudStoreError as cloud_exc:
                            st.error(f"已有报告暂时无法打开：{cloud_exc}")
                        else:
                            if existing_run:
                                try:
                                    existing_revision = store.get_draft_revision(
                                        user, exc.existing_run_id
                                    )
                                except CloudStoreError:
                                    existing_revision = None
                                hydrate_grading_run(
                                    existing_run,
                                    user_id=user.id,
                                    draft_revision=existing_revision,
                                )
                                st.session_state.reused_result_notice = True
                                navigate("report", exc.existing_run_id)
                                st.rerun()
                            else:
                                st.warning(action_reason_message(exc.reason))
                    elif user is None:
                        st.info(action_reason_message(exc.reason))
                        st.button(
                            "登录后查看 3 篇训练包",
                            type="primary",
                            on_click=open_cloud_login,
                            args=("write",),
                            use_container_width=True,
                        )
                    else:
                        st.warning(action_reason_message(exc.reason))
                        render_founder_offer(
                            store,
                            user,
                            key="write_access_gate",
                            intro="当前浏览器的免费首稿额度已经使用；开通后可继续生成新报告并完成训练闭环。",
                        )
                except GradingSettlementError as exc:
                    grading_duration_ms = int((time.perf_counter() - grading_started_at) * 1000)
                    st.session_state.grading_failed = False
                    record_usage_event(
                        store,
                        "report_generated",
                        user=user,
                        attempt_id=grading_attempt_id,
                        occurrence_key=grading_attempt_id,
                        metadata={
                            "settlement_pending": True,
                            "duration_ms": grading_duration_ms,
                        },
                    )
                    st.warning(str(exc))
                except AIGraderError as exc:
                    grading_duration_ms = int((time.perf_counter() - grading_started_at) * 1000)
                    st.session_state.grading_failed = True
                    record_usage_event(
                        store,
                        "report_generation_failed",
                        user=user,
                        attempt_id=grading_attempt_id,
                        occurrence_key=grading_attempt_id,
                        metadata={
                            "failure_type": type(exc).__name__,
                            "duration_ms": grading_duration_ms,
                        },
                    )
                    st.error("评分服务暂时不可用。题目和作文已经保留，可以直接重试。")
                    with st.expander("查看技术诊断"):
                        st.code(str(exc), language="text")
                except CloudStoreError as exc:
                    grading_duration_ms = int((time.perf_counter() - grading_started_at) * 1000)
                    st.session_state.grading_failed = True
                    record_usage_event(
                        store,
                        "report_generation_failed",
                        user=user,
                        attempt_id=grading_attempt_id,
                        occurrence_key=grading_attempt_id,
                        metadata={
                            "failure_type": type(exc).__name__,
                            "duration_ms": grading_duration_ms,
                        },
                    )
                    st.error(f"暂时无法安全确认额度或同步报告，题目和作文未清空：{exc}")
                except Exception as exc:
                    grading_duration_ms = int((time.perf_counter() - grading_started_at) * 1000)
                    st.session_state.grading_failed = True
                    record_usage_event(
                        store,
                        "report_generation_failed",
                        user=user,
                        attempt_id=grading_attempt_id,
                        occurrence_key=grading_attempt_id,
                        metadata={
                            "failure_type": type(exc).__name__,
                            "duration_ms": grading_duration_ms,
                        },
                    )
                    st.error("评分没有完成，没有产生半份记录。请稍后重试。")
                    with st.expander("查看技术诊断"):
                        st.code(f"{type(exc).__name__}: {exc}", language="text")

    selected_topic_category = str(st.session_state.get("selected_topic_category") or "")
    selected_topic_question = str(st.session_state.get("selected_topic_question") or "")
    if (
        selected_topic_category in TOPIC_LABELS
        and selected_topic_question == str(st.session_state.get("topic_input") or "")
    ):
        expressions = [
            item
            for item in load_expression_catalog()
            if item["topic_category"] == selected_topic_category
        ][:5]
        with st.container(key="topic_expression_panel"):
            with st.expander("本主题可复用表达", expanded=False):
                st.caption(f"{TOPIC_LABELS[selected_topic_category]} · 精选前 5 个现有表达")
                for expression in expressions:
                    st.markdown(
                        '<article class="ep-topic-expression">'
                        f'<strong>{html.escape(str(expression["expression"]))}</strong>'
                        f'<span>{html.escape(str(expression["meaning"]))}</span>'
                        f'<p>{html.escape(str(expression["example"]))}</p>'
                        '</article>',
                        unsafe_allow_html=True,
                    )


def render_correction_original(correction: dict[str, object]) -> None:
    st.markdown(
        f'<div class="correction-original">{highlight_problem_text(correction)}</div>',
        unsafe_allow_html=True,
    )


def render_dictionary_card(
    correction: dict[str, object], provider: DictionaryProvider | None = None
) -> bool:
    """Render replacement words as learner-dictionary cards linked to one map node."""
    replacements = learning_replacements(correction)
    if not replacements:
        return False
    provider = provider or get_default_dictionary_provider()
    improved = str(correction.get("improved") or "")
    for replacement in replacements:
        target = str(replacement.get("target") or "").strip()
        headword = str(replacement.get("headword") or target).strip()
        source = str(replacement.get("source") or "原表达").strip()
        definition = str(replacement.get("simple_definition") or "").strip()
        part_of_speech = str(replacement.get("part_of_speech") or "").strip()
        legacy = bool(replacement.get("legacy"))
        entry = None
        if not definition or not part_of_speech:
            entry = provider.lookup(headword or target)
        if entry is not None:
            definition = definition or entry.definition
            part_of_speech = part_of_speech or entry.part_of_speech
        meaning = str(replacement.get("meaning_zh") or "").strip()
        pattern = str(replacement.get("pattern") or "").strip()
        raw_collocations = replacement.get("collocations")
        collocations = [
            str(value).strip() for value in raw_collocations or [] if str(value).strip()
        ] if isinstance(raw_collocations, list) else []
        if not collocations:
            contextual = contextual_collocation(improved, target)
            collocations = [contextual] if contextual else []
        usage_note = str(
            replacement.get("usage_note_zh") or correction.get("problem") or ""
        ).strip()
        if legacy and entry is not None:
            source_note = "Open English WordNet 2025 · CC BY 4.0 · 旧报告语境补充"
        elif legacy:
            source_note = "旧报告替换路径 · 重新生成报告可获得完整学习词典讲解"
        else:
            source_note = "学习词典式讲解 · EssayPilot 按本句语境整理（非朗文原文）"
        fields = [
            f'<div class="dictionary-card__route"><span>{html.escape(source)}</span>'
            f'<b>→</b><strong>{html.escape(target)}</strong></div>',
            f'<h4>{html.escape(headword or target)}'
            + (f' <span>{html.escape(part_of_speech)}</span>' if part_of_speech else '')
            + '</h4>',
        ]
        if meaning:
            fields.append(f'<p><strong>本句义：</strong>{html.escape(meaning)}</p>')
        if definition:
            fields.append(f'<p><strong>简明英文释义：</strong>{html.escape(definition)}</p>')
        if pattern:
            fields.append(f'<p><strong>搭配 / 句型：</strong><code>{html.escape(pattern)}</code></p>')
        if collocations:
            fields.append(
                f'<p><strong>常用搭配：</strong>{html.escape(" · ".join(collocations))}</p>'
            )
        if usage_note:
            fields.append(f'<p><strong>用法区别：</strong>{html.escape(usage_note)}</p>')
        if improved:
            fields.append(f'<p><strong>本文例句：</strong>{html.escape(improved)}</p>')
        st.markdown(
            '<aside class="dictionary-card">'
            f'<div class="dictionary-card__source">{html.escape(source_note)}</div>'
            + "".join(fields)
            + '</aside>',
            unsafe_allow_html=True,
        )
    return True


def queue_correction_for_training(
    correction: dict[str, object],
    store: SupabaseStore,
    user: CloudUser | None,
) -> None:
    st.session_state.queued_sentence_training = {
        "original": str(correction.get("original") or ""),
        "reference": str(correction.get("improved") or ""),
    }
    if user is not None:
        ensure_learning_assets(store, user)
        try:
            for item in store.list_learning_items(user):
                if item.get("source_text") == correction.get("original"):
                    store.update_learning_item(user, str(item.get("id")), status="practicing")
                    break
        except (CloudStoreError, AttributeError):
            pass
    navigate("training", str(st.session_state.get("active_run_id", "")))


def render_report_downloads(report: str) -> None:
    markdown_col, pdf_col = st.columns(2)
    with markdown_col:
        st.download_button("下载 Markdown 报告", report, "essaypilot-report.md", "text/markdown", use_container_width=True)
    with pdf_col:
        st.download_button("下载 PDF 报告", markdown_to_pdf(report), "essaypilot-report.pdf", "application/pdf", use_container_width=True)


def render_report_page(store: SupabaseStore, user: CloudUser | None) -> None:
    report = str(st.session_state.get("latest_report") or "")
    structured = st.session_state.get("latest_structured")
    if not report or not isinstance(structured, dict) or not structured:
        st.info("还没有可显示的批改报告。")
        st.button("去提交作文", type="primary", on_click=navigate, args=("write",))
        return
    report = learner_safe_report_markdown(report, structured.get("overall_band"))
    ensure_learning_assets(store, user)
    run_id = str(
        st.session_state.get("active_run_id")
        or st.session_state.get("latest_cloud_ids", {}).get("grading_run_id", "")
    )
    latest_run_id = str(st.session_state.get("latest_cloud_ids", {}).get("grading_run_id", ""))
    attempt_id = (
        str(st.session_state.get("latest_grading_attempt_id") or "")
        if not run_id or run_id == latest_run_id else ""
    )
    record_usage_event(
        store, "report_viewed", user=user, run_id=run_id, attempt_id=attempt_id
    )
    st.markdown('<div class="section-kicker">批改报告</div>', unsafe_allow_html=True)
    st.title("先看最影响提分的问题")
    render_training_stepper(active=2)
    if st.session_state.pop("reused_result_notice", False):
        st.info("已复用相同作文的当前中文版评分结果，本次未消耗 Token。")
    if st.session_state.pop("guest_trial_completion_warning", False):
        st.warning("报告已经完整保留。游客额度状态暂时无法同步；请登录保存本次结果，不会重新评分。")
    if st.session_state.pop("first_report_settlement_warning", False):
        st.warning("报告内容已经保留，权益状态仍在确认；使用相同内容重试不会再次调用模型。")
    priorities = [item for item in structured.get("priorities", []) if isinstance(item, dict)]
    render_overall_band(float(structured.get("overall_band") or 0))
    if priorities:
        summary = str(priorities[0].get("title") or priorities[0].get("why") or "")
        if summary:
            st.markdown(
                f'<div class="ep-result-summary"><strong>本轮最重要：</strong> '
                f'{html.escape(summary)}</div>',
                unsafe_allow_html=True,
            )
    render_structured_criteria_overview(structured)
    if priorities:
        st.subheader("本轮只优先解决这两项")
        cols = st.columns(min(2, len(priorities)))
        for index, item in enumerate(priorities[:2]):
            if not isinstance(item, dict):
                continue
            with cols[index]:
                with st.container(border=True):
                    st.markdown(f"### {item.get('title', '提分重点')}")
                    st.write(item.get("why", ""))
                    st.success(str(item.get("action", "")))
                    if item.get("success_check"):
                        st.caption(f"完成检查：{item['success_check']}")
    raw_corrections = structured.get("sentence_corrections")
    corrections = (
        [item for item in raw_corrections if isinstance(item, dict)]
        if isinstance(raw_corrections, list) else []
    )
    essay = report_essay_from_state(st.session_state)
    vocabulary_items = report_vocabulary_items(structured, essay)
    report_sections = ["重点诊断", "原文问题地图", "完整报告与下载"]
    overview_tab, correction_tab, full_tab = st.tabs(
        report_sections,
        key="report_sections",
        on_change="rerun",
    )
    with overview_tab:
        render_problem_cards(report)
        render_suggestion_cards(report)
    with correction_tab:
        if st.session_state.get("report_sections") == "原文问题地图":
            record_usage_event(store, "problem_map_viewed", user=user, run_id=run_id)
        st.caption("先看问题路径，再回到原文定位；地图和词典卡展开都不会额外调用模型。")
        if corrections:
            st.markdown("### 这篇文章的问题路径")
            st.markdown(build_issue_map_html(corrections), unsafe_allow_html=True)
        else:
            st.info("本篇没有可精确定位的句子级问题，可继续查看重点诊断或进入逻辑训练。")

        st.markdown("### 原文定位")
        if essay:
            marked_essay, unmatched_nodes = map_essay_issues(essay, corrections)
            st.markdown(
                f'<div class="issue-map">{marked_essay}</div>',
                unsafe_allow_html=True,
            )
            if unmatched_nodes:
                st.caption(
                    "以下节点未在原文中可靠定位，因此没有强行编号："
                    + "、".join(f"#{index}" for index in unmatched_nodes)
                )
        else:
            st.warning("暂时无法恢复这份报告的完整原文；问题节点仍可查看，重新打开批改记录后会再次尝试恢复。")

        st.markdown("### 原文词汇推荐与可优化词")
        st.caption(
            "这里从整篇原文独立选词：既保留已经用得好的表达，也给普通、模糊或不够自然的词提供升级方案。"
        )
        if vocabulary_items:
            st.markdown(
                build_vocabulary_cards_html(vocabulary_items),
                unsafe_allow_html=True,
            )
        else:
            st.info(
                "这份旧报告没有可可靠关联到原文的独立词汇条目；新生成的报告会提供 4–6 条词汇推荐。"
            )

        if corrections:
            st.markdown("### 节点详情与目标表达")
        latest_cloud_ids = st.session_state.get("latest_cloud_ids")
        latest_run_id = (
            str(latest_cloud_ids.get("grading_run_id") or "")
            if isinstance(latest_cloud_ids, dict) else ""
        )
        raw_report_context = "\0".join(
            (
                str(st.session_state.get("active_run_id") or latest_run_id),
                essay or json.dumps(corrections, sort_keys=True, ensure_ascii=False),
            )
        )
        report_context_key = hashlib.sha256(raw_report_context.encode("utf-8")).hexdigest()[:12]
        dictionary_default_opened = False
        for criterion, grouped_items in grouped_corrections(corrections):
            st.markdown(
                f"#### {criterion} · {ISSUE_MAP_CRITERION_LABELS[criterion]}（{len(grouped_items)}）"
            )
            for index, correction in grouped_items:
                replacements = learning_replacements(correction)
                with st.container(border=True):
                    st.markdown(
                        f'<h3>#{index}&ensp;{html.escape(correction_issue_type(correction))}</h3>',
                        unsafe_allow_html=True,
                    )
                    st.caption("原文证据｜红线处是本节点要处理的问题")
                    render_correction_original(correction)
                    st.write(str(correction.get("problem", "")))
                    st.caption("修改后")
                    st.success(str(correction.get("improved", "")))
                    if replacements:
                        targets = "、".join(
                            str(item.get("target") or "").strip()
                            for item in replacements if str(item.get("target") or "").strip()
                        )
                        st.markdown(
                            '<div class="dictionary-trigger"><span>从该问题节点引出的替换词 / 短语</span>'
                            f'<strong>{html.escape(targets)}</strong></div>',
                            unsafe_allow_html=True,
                        )
                        open_by_default = not dictionary_default_opened
                        dictionary_default_opened = True
                        accessible_target = targets[:36] + ("…" if len(targets) > 36 else "")
                        show_dictionary = st.toggle(
                            f"展开 #{index}「{accessible_target}」的学习词典式讲解",
                            value=open_by_default,
                            key=f"show_dictionary_{report_context_key}_{index}",
                        )
                        if show_dictionary:
                            if st.session_state.get("report_sections") == "原文问题地图":
                                record_usage_event(
                                    store,
                                    "dictionary_opened",
                                    user=user,
                                    run_id=run_id,
                                    occurrence_key=f"node-{index}",
                                    metadata={"item_index": index},
                                )
                            render_dictionary_card(correction)
                    else:
                        st.caption("该节点属于结构或语法修复，没有可靠的词汇替换，因此不强行生成词条。")
                    if user is None:
                        st.caption("登录后可保存这条问题；AI 单句训练需开通体验包。")
                        st.button(
                            f"登录后保存 #{index}",
                            key=f"login_correction_{report_context_key}_{index}",
                            use_container_width=True,
                            on_click=open_cloud_login,
                            args=("report", ""),
                        )
                    else:
                        train_col, book_col = st.columns(2)
                        with train_col:
                            st.button(
                                f"把 #{index} 加入单句训练",
                                key=f"queue_correction_{report_context_key}_{index}",
                                use_container_width=True,
                                on_click=queue_correction_for_training, args=(correction, store, user),
                            )
                        with book_col:
                            if st.button(
                                f"把 #{index} 收入错题本",
                                key=f"save_correction_{report_context_key}_{index}",
                                use_container_width=True,
                            ):
                                ensure_learning_assets(store, user)
                                record_usage_event(
                                    store,
                                    "mistake_saved",
                                    user=user,
                                    run_id=run_id,
                                    occurrence_key=f"node-{index}",
                                    metadata={"item_index": index, "source": "problem_map"},
                                )
                                st.success(f"已确认 #{index} 收入错题本，不会产生模型请求。")
    with full_tab:
        render_grouped_examiner_report(report)
        if user is not None:
            render_report_downloads(report)
        else:
            st.info("登录后可下载报告，并把本次结果保存到个人学习档案。")
            st.button(
                "登录并保存报告",
                key="login_report_download",
                use_container_width=True,
                on_click=open_cloud_login,
                args=("report", ""),
            )

    render_product_feedback(
        store,
        user,
        touchpoint="report",
        run_id=run_id,
        attempt_id=attempt_id,
    )

    if user is not None:
        render_founder_offer(
            store,
            user,
            key=f"report_{run_id or 'local'}",
            intro=(
                "首稿报告已经完整生成。开通后可把这篇作文加入训练，"
                "继续做专项点评和第二稿验证；加入时才占用 1 篇额度。"
            ),
        )

    st.markdown("### 下一步：用第二稿验证这次反馈")
    primary_col, practice_col = st.columns([1.25, 1])
    if user is None:
        with primary_col:
            if st.button("登录并保存本次报告", type="primary", use_container_width=True):
                open_cloud_login("training", "draft")
                st.rerun()
        with practice_col:
            if st.button("登录后查看训练权益", use_container_width=True):
                open_cloud_login("training", "practice")
                st.rerun()
        if st.button("练习本篇表达", key="guest_report_expressions", use_container_width=True):
            open_cloud_login("growth", "expressions-from-report")
            st.rerun()
    else:
        with primary_col:
            if st.button("开始第二稿训练", type="primary", use_container_width=True):
                navigate("training", run_id, "draft")
                st.rerun()
        with practice_col:
            if st.button("先做专项训练", use_container_width=True):
                navigate("training", run_id, "practice")
                st.rerun()
        auxiliary_col, expression_col = st.columns(2)
        with auxiliary_col:
            st.caption("报告下载在“完整报告与下载”中")
        with expression_col:
            if st.button("练习本篇表达", key="report_expressions", use_container_width=True):
                navigate("growth", run_id, "expressions-from-report")
                st.rerun()


def render_training_page(store: SupabaseStore, user: CloudUser | None) -> None:
    if user is None:
        st.markdown('<div class="section-kicker">专项训练</div>', unsafe_allow_html=True)
        st.title("登录后保存报告并查看训练权益")
        st.info("游客首稿报告仍保留在当前会话中。登录后会自动保存，不会重新评分；AI 训练和二稿需开通体验包。")
        mode = str(st.query_params.get("mode", "practice") or "practice")
        label = "登录并查看体验包"
        st.button(
            label,
            type="primary",
            use_container_width=True,
            on_click=open_cloud_login,
            args=("training", mode),
        )
        return
    structured = st.session_state.get("latest_structured")
    if not isinstance(structured, dict) or not structured:
        st.info("请先完成一次作文批改，再开始专项训练。")
        st.button("去写作批改", type="primary", on_click=navigate, args=("write",))
        return
    st.markdown('<div class="section-kicker">专项训练</div>', unsafe_allow_html=True)
    st.title("把本轮问题真正练会")
    render_training_stepper(active=3)
    run_id = str(st.session_state.get("latest_cloud_ids", {}).get("grading_run_id", ""))
    membership_access = render_training_access_gate(
        store,
        user,
        grading_run_id=run_id,
    )
    if membership_access is None:
        return
    read_only = bool(membership_access.get("read_only"))
    if user is not None:
        try:
            pending = store.list_pending_practice(user)
        except CloudStoreError:
            pending = []
        if pending:
            st.info(f"你有 {len(pending)} 项未完成训练，本页已优先显示当前作文的任务。")
    sentence_data = [item for item in structured.get("sentence_training", []) if isinstance(item, dict)]
    sentences = [str(item.get("original") or "") for item in sentence_data]
    references = {
        str(item.get("original") or ""): str(item.get("reference") or "")
        for item in sentence_data
    }
    goals = {
        str(item.get("original") or ""): str(item.get("goal") or "")
        for item in sentence_data
    }
    queued = st.session_state.get("queued_sentence_training")
    if isinstance(queued, dict) and queued.get("original"):
        if queued["original"] not in sentences:
            sentences.insert(0, str(queued["original"]))
            references[str(queued["original"])] = str(queued.get("reference") or "")
    logic_tasks = [item for item in structured.get("logic_training", []) if isinstance(item, dict)]
    # Both tabs execute in one Streamlit run. Share this read, not a cross-rerun
    # cache, so a saved attempt or account/run change is visible immediately.
    persisted_attempts: list[dict[str, object]] = []
    if run_id and (sentences or logic_tasks):
        try:
            persisted_attempts = store.list_practice_attempts_for_run(user, run_id)
        except (CloudStoreError, AttributeError):
            st.caption("已保存的训练点评暂时无法读取，请稍后刷新。")
    training_mode = str(st.query_params.get("mode", "practice") or "practice")
    default_tab = "第二稿验证" if training_mode == "draft" else "单句训练"
    sentence_tab, logic_tab, draft_tab = st.tabs(
        ["单句训练", "逻辑训练", "第二稿验证"],
        default=default_tab,
        key="training_mode_tabs",
        on_change="rerun",
    )
    record_usage_event(
        store,
        "training_started",
        user=user,
        run_id=run_id,
        occurrence_key=training_mode,
        metadata={"entry_mode": training_mode},
    )
    if st.session_state.get("training_mode_tabs", default_tab) == "单句训练":
        record_usage_event(
            store,
            "sentence_training_started",
            user=user,
            run_id=run_id,
            occurrence_key="sentence-tab",
            metadata={"task_kind": "sentence"},
        )
    with sentence_tab:
        render_sentence_practice(
            sentences, "OpenAI", PRODUCTION_MODEL, references=references,
            cloud_store=store if user is not None else None, cloud_user=user,
            grading_run_id=run_id, error_tags=list(structured.get("error_tags", [])),
            read_only=read_only,
            persisted_attempts=persisted_attempts,
            goals=goals,
        )
    with logic_tab:
        render_logic_practice(
            logic_tasks,
            "OpenAI", PRODUCTION_MODEL,
            cloud_store=store if user is not None else None, cloud_user=user,
            grading_run_id=run_id, error_tags=list(structured.get("error_tags", [])),
            read_only=read_only,
            persisted_attempts=persisted_attempts,
        )
    with draft_tab:
        st.session_state.draft_2_active = True
        render_draft_2_training(
            provider="OpenAI", model=PRODUCTION_MODEL, task_type="Task 2",
            user_id=user.id if user is not None else st.session_state.user_id,
            cloud_store=store if user is not None else None, cloud_user=user,
            read_only=read_only,
        )
    if st.session_state.pop("learning_assets_sync_error", False):
        st.caption("训练已保存；错题掌握状态将在数据库升级后自动联动。")
    feedback_run_id = str(st.session_state.get("pending_training_feedback_run_id") or "")
    if feedback_run_id and feedback_run_id == run_id:
        render_product_feedback(
            store,
            user,
            touchpoint="training",
            run_id=run_id,
        )


def _normalise_expression(item: dict[str, object]) -> dict[str, object]:
    """Give catalog and cloud expressions one display shape."""
    if item.get("catalog_id"):
        return dict(item)
    run = item.get("grading_runs") if isinstance(item.get("grading_runs"), dict) else {}
    essay = run.get("essays") if isinstance(run.get("essays"), dict) else {}
    return {
        "learning_item_id": item.get("id"),
        "grading_run_id": item.get("grading_run_id"),
        "item_type": item.get("item_type") or "expression",
        "item_key": item.get("item_key"),
        "origin": item.get("origin") or "report",
        "topic_category": item.get("topic_category") or "society_family",
        "function_category": item.get("function_category") or "core_collocation",
        "expression": item.get("source_text") or "",
        "meaning": item.get("explanation") or "",
        "usage_note": item.get("usage_note") or "",
        "example": item.get("target_text") or "",
        "favorite": bool(item.get("favorite")),
        "status": item.get("status") or "new",
        "created_at": item.get("created_at") or "",
        "source_question": essay.get("question") or "",
    }


def _persist_catalog_expression(
    store: SupabaseStore, user: CloudUser, item: dict[str, object]
) -> dict[str, object]:
    row = catalog_learning_item(item, user_id=user.id)
    saved = store.upsert_learning_item(user, row)
    return _normalise_expression(saved or row)


def _render_expression_card(
    item: dict[str, object], *, store: SupabaseStore, user: CloudUser | None, key: str
) -> None:
    expression = _normalise_expression(item)
    topic = TOPIC_LABELS.get(str(expression.get("topic_category")), "其他")
    function = FUNCTION_LABELS.get(str(expression.get("function_category")), "核心搭配")
    with st.container(border=True):
        st.markdown(f"### {expression.get('expression', '')}")
        st.caption(f"{topic} · {function} · {expression_status_label(expression.get('status'))}")
        st.write(str(expression.get("meaning") or ""))
        if expression.get("usage_note"):
            st.info(str(expression.get("usage_note")))
        st.markdown(f"**例句：** {expression.get('example', '')}")
        favorite_col, practice_col = st.columns(2)
        with favorite_col:
            favorite = bool(expression.get("favorite"))
            if st.button("取消收藏" if favorite else "收藏", key=f"fav_{key}", use_container_width=True):
                if user is None:
                    open_cloud_login("growth", "")
                    st.rerun()
                else:
                    try:
                        if not expression.get("learning_item_id"):
                            expression = _persist_catalog_expression(store, user, expression)
                        store.update_learning_item(
                            user, str(expression.get("learning_item_id")), favorite=not favorite
                        )
                        st.rerun()
                    except (CloudStoreError, AttributeError) as exc:
                        st.warning(f"收藏暂时无法保存：{exc}")
        with practice_col:
            if st.button("开始造句", key=f"practice_{key}", type="primary", use_container_width=True):
                if user is None:
                    open_cloud_login("growth", "practice")
                    st.rerun()
                if user is not None and not expression.get("learning_item_id"):
                    try:
                        expression = _persist_catalog_expression(store, user, expression)
                    except (CloudStoreError, AttributeError) as exc:
                        st.warning(f"练习条目暂时无法保存：{exc}")
                        return
                st.session_state.expression_practice_item = expression
                st.session_state.expression_open_practice = True
                st.session_state.pop("expression_practice_result", None)
                st.session_state.pop("expression_student_sentence", None)
                if user is not None and expression.get("learning_item_id"):
                    try:
                        store.update_learning_item(
                            user, str(expression.get("learning_item_id")), status="practicing"
                        )
                    except (CloudStoreError, AttributeError):
                        pass
                st.rerun()


def render_expression_library(
    store: SupabaseStore,
    user: CloudUser | None,
    personal_items: list[dict[str, object]] | None = None,
    *,
    mode: str = "",
) -> None:
    """Render the static catalog, personal assets, and opt-in AI practice."""
    catalog = load_expression_catalog()
    personal_items = personal_items or []
    personal = [_normalise_expression(item) for item in personal_items if item.get("item_type") == "expression"]
    current_run_id = str(st.session_state.get("active_run_id") or "")
    report_personal = report_expression_items(personal, grading_run_id=current_run_id)
    by_key = {str(item.get("item_key")): item for item in personal}
    for item in catalog:
        saved = by_key.get(f"catalog:{item['catalog_id']}")
        if saved:
            item.update({
                "learning_item_id": saved.get("learning_item_id"), "favorite": saved.get("favorite"),
                "status": saved.get("status"), "item_key": saved.get("item_key"),
            })

    if st.session_state.pop("expression_open_practice", False):
        mode = "practice"
    view_options = [EXPRESSION_VIEW_CURATED]
    if user is not None:
        view_options.extend([EXPRESSION_VIEW_REPORT, EXPRESSION_VIEW_PRACTICE])
    st.session_state.expression_library_view = resolve_expression_view(
        stored_view=st.session_state.get("expression_library_view"),
        authenticated=user is not None,
        has_report_expressions=bool(report_personal),
        mode=mode,
    )

    with st.container(border=True):
        st.markdown("#### 表达库怎么用？")
        st.write("不要一次背很多。先从自己的作文或题材精选中选 1 个表达，用它写一个属于自己的英文句子；通过点评后，再尝试在下一篇作文中主动使用。")
        st.caption("1. 选一个表达　　2. 用自己的意思造句　　3. 在下一篇作文中再次使用")

    view = st.radio(
        "表达库视图", view_options, horizontal=True,
        key="expression_library_view", label_visibility="collapsed",
    )
    if view == EXPRESSION_VIEW_CURATED:
        st.caption("10 个 Task 2 高频题材，共 150 条内置精选表达；浏览、搜索和查看例句均为 0 Token。")
        mastered_topics = Counter(
            str(item.get("topic_category")) for item in personal if item.get("status") == "mastered"
        )
        st.markdown(
            '<div class="feature-strip">' + "".join(
                f'<div class="feature-chip"><strong>{html.escape(label)}</strong>'
                f'{mastered_topics.get(key, 0)}/15 已练习</div>'
                for key, label in TOPIC_LABELS.items()
            ) + "</div>",
            unsafe_allow_html=True,
        )
        query = st.text_input("搜索表达或中文释义", placeholder="例如：public transport / 公共交通")
        filter_cols = st.columns(2)
        topic_label = filter_cols[0].selectbox("题材", ["全部题材", *TOPIC_LABELS.values()])
        function_label = filter_cols[1].selectbox("写作功能", ["全部功能", *FUNCTION_LABELS.values()])
        topic_key = next((key for key, label in TOPIC_LABELS.items() if label == topic_label), "")
        function_key = next((key for key, label in FUNCTION_LABELS.items() if label == function_label), "")
        filtered = [item for item in catalog if not topic_key or item["topic_category"] == topic_key]
        filtered = [item for item in filtered if not function_key or item["function_category"] == function_key]
        if user is not None:
            personal_filters = st.columns(2)
            favorite_only = personal_filters[0].checkbox("只看收藏", key="catalog_favorite_only")
            status_label = personal_filters[1].selectbox(
                "练习状态", ["全部状态", "未练习", "继续练习", "已正确使用一次"], key="catalog_status"
            )
            status_key = {"未练习": "new", "继续练习": "practicing", "已正确使用一次": "mastered"}.get(status_label, "")
            filtered = [item for item in filtered if not favorite_only or item.get("favorite")]
            filtered = [item for item in filtered if not status_key or item.get("status", "new") == status_key]
        if query.strip():
            needle = query.strip().casefold()
            filtered = [
                item for item in filtered
                if needle in f"{item['expression']} {item['meaning']} {item['usage_note']} {item['example']}".casefold()
            ]
        st.write(f"共找到 {len(filtered)} 条")
        for item in filtered[:45]:
            _render_expression_card(item, store=store, user=user, key=str(item["catalog_id"]))
        if len(filtered) > 45:
            st.info("结果较多，请选择题材或继续搜索以缩小范围。")
    elif view == EXPRESSION_VIEW_REPORT:
        if user is None:
            st.info("登录后，每次批改生成的 6–8 条可迁移表达会显示在这里。")
            return
        if not report_personal:
            st.info("完成一次作文批改后，这里会显示从你的作文中生成的可迁移表达。")
            return
        filters = st.columns(3)
        topic_label = filters[0].selectbox("题材筛选", ["全部题材", *TOPIC_LABELS.values()], key="mine_topic")
        status_label = filters[1].selectbox("练习状态", ["全部状态", "未练习", "继续练习", "已正确使用一次"])
        favorite_only = filters[2].checkbox("只看收藏")
        topic_key = next((key for key, label in TOPIC_LABELS.items() if label == topic_label), "")
        status_key = {"未练习": "new", "继续练习": "practicing", "已正确使用一次": "mastered"}.get(status_label, "")
        shown = [item for item in report_personal if not topic_key or item.get("topic_category") == topic_key]
        shown = [item for item in shown if not status_key or item.get("status") == status_key]
        shown = [item for item in shown if not favorite_only or item.get("favorite")]
        for index, item in enumerate(shown):
            if item.get("origin") == "report":
                source = str(item.get("source_question") or "作文题目未记录")
                st.caption(f"来自作文：{source[:100]} · {str(item.get('created_at') or '')[:10]}")
            _render_expression_card(item, store=store, user=user, key=f"mine_{index}_{item.get('learning_item_id')}")
    else:
        item = st.session_state.get("expression_practice_item")
        if not isinstance(item, dict) or not item:
            st.info("请先在题材精选或来自我的作文中选择“开始造句”。")
            return
        expression = _normalise_expression(item)
        st.markdown(f"### 使用 `{expression.get('expression', '')}` 写一个英文句子")
        st.write(str(expression.get("meaning") or ""))
        st.caption(str(expression.get("usage_note") or ""))
        sentence = st.text_area("你的英文句子", key="expression_student_sentence", height=130)
        st.caption("静态表达、释义和例句可以继续使用；独立造句 AI 点评不包含在当前 3 篇训练包中。")
        st.button("独立造句 AI 点评暂未开放", disabled=True, use_container_width=True)
        result = st.session_state.get("expression_practice_result")
        if isinstance(result, dict):
            if result.get("mastered"):
                st.success("已正确使用一次：表达使用准确、语法基本正确且语境自然。")
            else:
                st.warning("还需要继续练习：根据点评修改后再试一次。")
            st.write(str(result.get("feedback_zh") or ""))
            st.info(f"优化句：{result.get('improved_sentence_en', '')}")


def render_score_trend(runs: list[dict[str, object]]) -> None:
    """Render a compact, optional score trend inside the learning archive."""
    chart_rows: list[dict[str, object]] = []
    practice_dates: set[str] = set()
    for run in reversed(runs):
        created = str(run.get("created_at") or "")[:10]
        if not created:
            continue
        for item in run.get("criteria") or []:
            if not isinstance(item, dict) or not isinstance(item.get("score"), (int, float)):
                continue
            practice_dates.add(created)
            chart_rows.append(
                {
                    "练习日期": created,
                    "能力维度": CRITERION_COMPACT_NAMES.get(
                        str(item.get("criterion")), str(item.get("criterion"))
                    ),
                    "分数": item.get("score"),
                }
            )
    if len(practice_dates) < 2 or not chart_rows:
        return

    import altair as alt
    import pandas as pd

    with st.expander("成绩趋势", expanded=False):
        chart = (
            alt.Chart(pd.DataFrame(chart_rows))
            .mark_line(
                strokeWidth=3.5,
                point=alt.OverlayMarkDef(
                    filled=True, size=95, stroke="#FFFFFF", strokeWidth=1.5
                ),
            )
            .encode(
                x=alt.X("练习日期:N", title="练习日期", axis=alt.Axis(labelAngle=-35)),
                y=alt.Y("分数:Q", scale=alt.Scale(domain=[3, 9]), title="分数"),
                color=alt.Color(
                    "能力维度:N",
                    title="能力维度",
                    scale=alt.Scale(
                        domain=CHART_CRITERION_DOMAIN, range=ALPINE_CHART_COLORS
                    ),
                    legend=alt.Legend(labelLimit=180, orient="bottom", columns=2),
                ),
                strokeDash=alt.StrokeDash(
                    "能力维度:N",
                    scale=alt.Scale(
                        domain=CHART_CRITERION_DOMAIN, range=ALPINE_CHART_DASHES
                    ),
                    legend=None,
                ),
                shape=alt.Shape(
                    "能力维度:N",
                    scale=alt.Scale(
                        domain=CHART_CRITERION_DOMAIN, range=ALPINE_CHART_SHAPES
                    ),
                    legend=None,
                ),
                tooltip=["练习日期", "能力维度", "分数"],
            )
            .properties(height=250)
            .configure_axis(
                labelColor="#31485A",
                titleColor="#172B3A",
                labelFontSize=13,
                titleFontSize=14,
                gridColor="#CCD9E2",
                domainColor="#91A8B8",
            )
            .configure_legend(
                labelColor="#263F52",
                titleColor="#172B3A",
                labelFontSize=13,
                titleFontSize=14,
                symbolSize=150,
                padding=8,
            )
        )
        st.altair_chart(chart, use_container_width=True)


def _criterion_history_scores(run: dict[str, object]) -> dict[str, str]:
    scores = {"TR": "-", "CC": "-", "LR": "-", "GRA": "-"}
    aliases = {
        "Task Response": "TR", "Coherence and Cohesion": "CC",
        "Lexical Resource": "LR", "Grammatical Range and Accuracy": "GRA",
    }
    for item in run.get("criteria") or []:
        if isinstance(item, dict) and str(item.get("criterion")) in aliases:
            scores[aliases[str(item["criterion"])]] = str(item.get("score", "-"))
    return scores


def _open_draft_comparison(
    store: SupabaseStore,
    user: CloudUser,
    original_run_id: str,
    revised_run: dict[str, object] | None,
    revision: dict[str, object] | None,
) -> None:
    original = store.get_grading_run(user, original_run_id)
    if not original:
        return
    hydrate_grading_run(original, user_id=user.id)
    original_essay = original.get("essays") if isinstance(original.get("essays"), dict) else {}
    revised_run = revised_run or {}
    revised_essay = revised_run.get("essays") if isinstance(revised_run.get("essays"), dict) else {}
    revision = revision or {}
    revised_structured = revised_run.get("report_json") or revision.get("report_json") or {}
    revised_report = revised_run.get("report_markdown") or revision.get("report_markdown") or ""
    revised_text = revised_essay.get("content") or revision.get("content") or ""
    revised_scores = score_snapshot(revised_structured) if isinstance(revised_structured, dict) and revised_structured.get("overall_band") is not None else revision.get("score_snapshot") or {}
    st.session_state.draft_1_snapshot = {
        "topic": str(original_essay.get("question") or ""),
        "text": str(original_essay.get("content") or ""),
        "feedback": str(original.get("report_markdown") or ""),
        "scores": score_snapshot(dict(original.get("report_json") or {})),
        "structured": dict(original.get("report_json") or {}),
        "essay_id": str(original.get("essay_id") or ""),
        "grading_run_id": original_run_id,
    }
    st.session_state.draft_2_result = {
        "scores": revised_scores, "report": str(revised_report),
        "progress_report": str(revision.get("progress_report") or ""),
        "text": str(revised_text), "grading_run_id": str(revised_run.get("id") or ""),
        "user_id": user.id, "parent_grading_run_id": original_run_id,
        "settlement_pending": False,
    }
    st.session_state.draft_2_text = str(revised_text)
    navigate("training", original_run_id, "draft")


def render_correction_history(
    store: SupabaseStore,
    user: CloudUser,
    runs: list[dict[str, object]],
    revisions: list[dict[str, object]],
    *,
    has_more: bool,
) -> None:
    """Render owner-scoped grading runs without copying stored reports."""
    revision_by_original = {str(item.get("grading_run_id") or ""): item for item in revisions}
    revision_by_revised = {str(item.get("revised_grading_run_id") or ""): item for item in revisions}
    runs_by_id = {str(item.get("id") or ""): item for item in runs}
    child_by_parent = {
        str(item.get("parent_run_id") or ""): item
        for item in runs if item.get("parent_run_id")
    }
    if not runs:
        st.info("完成一次批改后，这里会保存批改记录。")
        return
    for run in runs:
        run_id = str(run.get("id") or "")
        essay = run.get("essays") if isinstance(run.get("essays"), dict) else {}
        question = " ".join(str(essay.get("question") or "未记录题目").split())
        content = str(essay.get("content") or "")
        role = str(run.get("draft_role") or "ordinary")
        if role == "second" or run.get("parent_run_id"):
            role_label = "第二稿"
        elif role == "first" or run_id in revision_by_original or run_id in child_by_parent:
            role_label = "第一稿"
        else:
            role_label = "普通批改"
        scores = _criterion_history_scores(run)
        with st.container(border=True, key=f"correction_history_card_{run_id}"):
            created_at = str(run.get("created_at") or "")[:16].replace("T", " ")
            st.markdown(
                '<div class="correction-history-meta">'
                f'<span>{html.escape(created_at)}</span><strong>{html.escape(role_label)}</strong>'
                "</div>",
                unsafe_allow_html=True,
            )
            question_summary = question[:120] + ("…" if len(question) > 120 else "")
            st.markdown(
                f'<div class="correction-history-question">{html.escape(question_summary)}</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                '<div class="correction-history-scores">'
                f'<strong>Overall {html.escape(format_overall_band(run.get("overall_band")))}</strong>'
                f'<span>TR {html.escape(scores["TR"])}</span>'
                f'<span>CC {html.escape(scores["CC"])}</span>'
                f'<span>LR {html.escape(scores["LR"])}</span>'
                f'<span>GRA {html.escape(scores["GRA"])}</span>'
                "</div>",
                unsafe_allow_html=True,
            )
            preview = " ".join(content.split())
            preview_text = preview[:180] + ("…" if len(preview) > 180 else "")
            st.markdown(
                f'<div class="correction-history-preview">{html.escape(preview_text)}</div>',
                unsafe_allow_html=True,
            )
            with st.expander("查看完整原文", expanded=False):
                st.text(content)
            report_col, diff_col = st.columns(2)
            stored_revision = revision_by_original.get(run_id)
            if report_col.button("打开完整批改报告", key=f"history_report_{run_id}", use_container_width=True):
                hydrate_grading_run(
                    run,
                    user_id=user.id,
                    draft_revision=stored_revision,
                )
                navigate("report", run_id)
                st.rerun()
            revision = revision_by_revised.get(run_id) or revision_by_original.get(run_id)
            revised_run = run if run_id in revision_by_revised else child_by_parent.get(run_id)
            original_id = str(run.get("parent_run_id") or run_id)
            if revision or revised_run:
                if diff_col.button("查看二稿变化", key=f"history_diff_{run_id}", use_container_width=True):
                    try:
                        _open_draft_comparison(store, user, original_id, revised_run, revision)
                    except CloudStoreError as exc:
                        st.warning(f"暂时无法打开二稿变化：{exc}")
                    else:
                        record_usage_event(
                            store,
                            "diff_viewed",
                            user=user,
                            run_id=original_id,
                            occurrence_key=f"archive-{run_id}",
                            metadata={"source": "archive"},
                        )
                        st.rerun()
    if has_more and st.button("继续加载", key="load_more_corrections", use_container_width=True):
        st.session_state.correction_history_limit = int(st.session_state.get("correction_history_limit", 10)) + 10
        st.rerun()


def render_growth_page(store: SupabaseStore, user: CloudUser | None) -> None:
    st.markdown('<div class="section-kicker">学习档案</div>', unsafe_allow_html=True)
    st.title("在这里复习错题、练习表达、查看二稿记录")
    growth_mode = str(st.query_params.get("mode", "") or "")
    if growth_mode in {"expressions", "expressions-from-report", "practice"}:
        st.query_params.pop("mode", None)
    if user is None:
        st.info("题材精选可直接浏览；登录后可收藏、练习并跨设备同步进度。")
        render_expression_library(store, None, [], mode=growth_mode)
        return
    record_usage_event(
        store,
        "archive_viewed",
        user=user,
        run_id=str(st.session_state.get("active_run_id") or ""),
    )
    history_limit = int(st.session_state.get("correction_history_limit", 10))
    try:
        loaded_runs = store.list_grading_runs(user, limit=history_limit + 1)
        has_more_runs = len(loaded_runs) > history_limit
        runs = loaded_runs[:history_limit]
        revisions = store.list_draft_revisions(user)
    except CloudStoreError as exc:
        st.warning(f"历史与成长记录暂时无法读取：{exc}")
        runs, revisions, has_more_runs = [], [], False
    try:
        items = store.list_learning_items(user)
    except (CloudStoreError, AttributeError):
        st.warning("学习资产模块正在升级，历史和成长趋势仍可正常查看。请稍后刷新页面。")
        items = []
    if runs and not items:
        latest = runs[0]
        latest_id = str(latest.get("id") or "")
        latest_revision = next(
            (
                item
                for item in revisions
                if str(item.get("grading_run_id") or "") == latest_id
            ),
            None,
        )
        hydrate_grading_run(
            latest,
            user_id=user.id,
            draft_revision=latest_revision,
        )
        ensure_learning_assets(store, user)
        try:
            items = store.list_learning_items(user)
        except (CloudStoreError, AttributeError):
            items = []
    mastered = [item for item in items if item.get("status") == "mastered"]
    errors = [item for item in items if item.get("item_type") == "error"]
    expressions = [item for item in items if item.get("item_type") == "expression"]
    completed_expression_practice = [item for item in expressions if item.get("status") == "mastered"]
    metrics = st.columns(4)
    metrics[0].metric("累计批改", len(runs))
    metrics[1].metric("待复习错误", len([item for item in errors if item.get("status") != "mastered"]))
    metrics[2].metric("已完成表达练习", len(completed_expression_practice))
    metrics[3].metric("第二稿", len(revisions))
    default_section = "表达库" if growth_mode in {"expressions", "expressions-from-report", "practice"} else "批改记录"
    history_tab, error_tab, expression_tab, draft_tab, share_tab = st.tabs(
        ["批改记录", "错题本", "表达库", "二稿记录", "成果卡"],
        default=default_section,
        key="growth_sections",
    )
    with history_tab:
        render_score_trend(runs)
        render_correction_history(store, user, runs, revisions, has_more=has_more_runs)
    with error_tab:
        category_counts = Counter(str(item.get("category") or "grammar") for item in errors)
        if category_counts:
            st.caption("高频问题：" + " · ".join(f"{CATEGORY_LABELS.get(k, k)} × {v}" for k, v in category_counts.most_common()))
        for item in errors:
            with st.container(border=True):
                label = CATEGORY_LABELS.get(str(item.get("category")), str(item.get("category")))
                st.markdown(f"**{label} · {str(item.get('status', 'new')).replace('new', '待学习').replace('practicing', '练习中').replace('mastered', '已掌握')}**")
                st.error(str(item.get("source_text") or ""))
                st.write(str(item.get("explanation") or ""))
                st.success(str(item.get("target_text") or ""))
                if item.get("status") != "mastered" and st.button("标记为已掌握", key=f"master_asset_{item.get('id')}"):
                    try:
                        store.update_learning_item(user, str(item.get("id")), status="mastered", review_count=int(item.get("review_count") or 0) + 1)
                    except (CloudStoreError, AttributeError):
                        st.warning("云端学习资产仍在升级，请稍后重试。")
                    else:
                        st.rerun()
    with expression_tab:
        render_expression_library(store, user, expressions, mode=growth_mode)
    with draft_tab:
        if not revisions:
            st.info("完成第二稿训练后，这里会显示第一稿与第二稿的变化。")
        for revision in revisions:
            original = revision.get("grading_runs") if isinstance(revision.get("grading_runs"), dict) else {}
            revised = revision.get("score_snapshot") if isinstance(revision.get("score_snapshot"), dict) else {}
            before = float(original.get("overall_band") or 0)
            after = float(revised.get("Overall Band") or 0)
            with st.expander(
                f"Overall {format_overall_band(before)} → "
                f"{format_overall_band(after)}"
            ):
                st.markdown(str(revision.get("progress_report") or ""))
    with share_tab:
        if not runs:
            st.info("完成第一篇批改后即可生成匿名成果卡。")
        else:
            latest = runs[0]
            latest_revision_gain = None
            if revisions:
                original = revisions[0].get("grading_runs") or {}
                revised = revisions[0].get("score_snapshot") or {}
                if isinstance(original, dict) and isinstance(revised, dict):
                    latest_revision_gain = float(revised.get("Overall Band") or 0) - float(original.get("overall_band") or 0)
            structured = latest.get("report_json") if isinstance(latest.get("report_json"), dict) else {}
            svg = build_result_card_svg(
                overall_band=float(latest.get("overall_band") or 0),
                criteria=[item for item in latest.get("criteria") or [] if isinstance(item, dict)],
                priority=_run_priority(structured),
                mastered_count=len(mastered),
                draft_gain=latest_revision_gain,
            )
            card_uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
            st.image(card_uri, use_container_width=True)
            st.download_button("下载匿名成果卡", svg, "essaypilot-result.svg", "image/svg+xml", use_container_width=True)


def render_product_route(store: SupabaseStore, user: CloudUser | None) -> None:
    route = str(st.session_state.get("page_mode", "home"))
    if route == "workspace":
        route = "home"
        st.session_state.page_mode = route
    render_app_navigation(user, store=store)
    if route == "home":
        render_home_page(store, user)
    elif route == "write":
        render_write_page(store, user)
    elif route == "report":
        render_report_page(store, user)
    elif route == "training":
        render_training_page(store, user)
    elif route == "growth":
        render_growth_page(store, user)


cloud_store = SupabaseStore()
cloud_store.bind_auth_session(
    session_cloud_user,
    lambda refreshed: write_cloud_user_state(
        refreshed, persist=True, request_rerun=True
    ),
    mark_cloud_session_invalid,
)
cloud_user = restore_cloud_user_session(cloud_store)

requested_page = str(st.query_params.get("page", "") or "")
admin_requested = is_admin_request()
if admin_requested or (requested_page or st.session_state.page_mode) != "home":
    # Leaving home (including demo/login/admin) must refresh pending work on return.
    st.session_state.pop("latest_home_snapshot", None)

if admin_requested:
    if cloud_user is None:
        st.session_state.login_return_route = "home"
        render_login_page(cloud_store)
    else:
        render_admin_dashboard()
    st.stop()

if requested_page == "demo" and st.session_state.page_mode != "demo":
    st.session_state.page_mode = "demo"
    st.session_state.tutorial_clicked_pending = True

if st.session_state.page_mode == "demo":
    demo_visitor_id = browser_visitor_id()
    if demo_visitor_id:
        st.session_state.visitor_hash = visitor_hash(demo_visitor_id)
        record_usage_event(cloud_store, "session_started", user=cloud_user)
    if st.session_state.get("tutorial_clicked_pending") and (
        cloud_user is not None or st.session_state.get("visitor_hash")
    ):
        record_usage_event(cloud_store, "tutorial_clicked", user=cloud_user)
        st.session_state.pop("tutorial_clicked_pending", None)
    render_demo_page()
    st.stop()

raw_visitor_id = browser_visitor_id()
if raw_visitor_id:
    st.session_state.visitor_hash = visitor_hash(raw_visitor_id)
    record_usage_event(cloud_store, "session_started", user=cloud_user)

if requested_page == "login" and cloud_user is None:
    render_login_page(cloud_store)
    st.stop()
if cloud_user is not None:
    user_id = cloud_user.id

if requested_page in APP_ROUTES:
    st.session_state.page_mode = requested_page
elif st.session_state.page_mode not in APP_ROUTES:
    st.session_state.page_mode = "home"
ensure_run_context(cloud_store, cloud_user)
render_product_route(cloud_store, cloud_user)
if consume_auth_request_rerun(st.session_state):
    st.rerun()
st.stop()
