"""Privacy-safe product analytics helpers shared by the app and tests."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable


EVENT_NAMES = (
    "session_started",
    "login_completed",
    "first_draft_submitted",
    "report_generated",
    "report_generation_failed",
    "report_viewed",
    "tutorial_clicked",
    "problem_map_viewed",
    "training_started",
    "sentence_training_started",
    "sentence_training_completed",
    "logic_training_completed",
    "mistake_saved",
    "archive_viewed",
    "second_draft_submitted",
    "second_draft_generated",
    "second_draft_generation_failed",
    "diff_viewed",
    "dictionary_opened",
)

FEEDBACK_TOUCHPOINTS = ("report", "training", "second_draft")
FEEDBACK_REASON_CODES = (
    "inaccurate",
    "too_generic",
    "unclear",
    "not_actionable",
    "too_slow",
    "too_long",
    "difficulty_mismatch",
    "progress_unclear",
    "other",
)

_SAFE_METADATA_KEYS = {
    "cached",
    "draft_number",
    "duration_ms",
    "entry_mode",
    "failure_type",
    "identity_type",
    "item_index",
    "source",
    "task_kind",
}
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="essaypilot-analytics")


def anonymous_user_id(visitor_hash: str) -> str:
    """Turn the existing one-way browser hash into an explicit anonymous ID."""
    clean = str(visitor_hash or "").strip().lower()
    if len(clean) != 64 or any(character not in "0123456789abcdef" for character in clean):
        return ""
    return f"anon_{clean}"


def build_dedupe_key(
    event_name: str,
    session_id: str,
    *,
    run_id: str = "",
    attempt_id: str = "",
    occurrence_key: str = "",
) -> str:
    """Return a stable opaque key safe to persist across Streamlit reruns."""
    if event_name not in EVENT_NAMES:
        raise ValueError(f"Unsupported analytics event: {event_name}")
    material = "\0".join(
        ("analytics-v2", event_name, session_id, run_id, attempt_id, occurrence_key)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_feedback_dedupe_key(
    touchpoint: str,
    session_id: str,
    *,
    run_id: str = "",
    attempt_id: str = "",
) -> str:
    """Return one stable feedback key per milestone context."""
    if touchpoint not in FEEDBACK_TOUCHPOINTS:
        raise ValueError(f"Unsupported feedback touchpoint: {touchpoint}")
    material = "\0".join(
        ("product-feedback-v1", touchpoint, session_id, run_id, attempt_id)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def sanitize_metadata(metadata: dict[str, object] | None) -> dict[str, object]:
    """Keep only small, explicitly approved, non-sensitive metadata values."""
    clean: dict[str, object] = {}
    for key, value in (metadata or {}).items():
        if key not in _SAFE_METADATA_KEYS or not isinstance(value, (str, int, float, bool)):
            continue
        if key == "duration_ms":
            if isinstance(value, bool):
                continue
            try:
                value = max(0, min(3_600_000, int(value)))
            except (TypeError, ValueError, OverflowError):
                continue
        if key == "identity_type" and value not in {"anonymous", "authenticated"}:
            continue
        clean[key] = value if not isinstance(value, str) else value[:80]
    encoded = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
    return clean if len(encoded.encode("utf-8")) <= 1024 else {}


def record_event_safely(
    recorder: Callable[[], object],
    *,
    asynchronous: bool = False,
    max_retries: int = 0,
    retry_delay_seconds: float = 0.05,
    logger: logging.Logger | None = None,
) -> bool:
    """Run or queue analytics without ever raising into the product flow."""
    event_logger = logger or logging.getLogger(__name__)

    def guarded() -> bool:
        retries = max(0, min(2, int(max_retries)))
        for attempt in range(retries + 1):
            try:
                recorder()
                return True
            except Exception as exc:  # Analytics must never interrupt grading or training.
                if attempt >= retries:
                    event_logger.warning("Product analytics event was not recorded: %s", exc)
                    return False
                if retry_delay_seconds > 0:
                    time.sleep(min(float(retry_delay_seconds), 0.25) * (attempt + 1))
        return False

    if asynchronous:
        try:
            _EXECUTOR.submit(guarded)
        except Exception as exc:
            event_logger.warning("Product analytics event could not be queued: %s", exc)
            return False
        return True
    return guarded()


def validate_feedback(
    touchpoint: str,
    helpful: bool,
    reason_codes: Iterable[str] | None,
) -> tuple[str, bool, list[str]]:
    """Validate and normalize the structured, text-free feedback contract."""
    if touchpoint not in FEEDBACK_TOUCHPOINTS:
        raise ValueError(f"Unsupported feedback touchpoint: {touchpoint}")
    if not isinstance(helpful, bool):
        raise ValueError("Feedback helpfulness must be boolean")
    reasons = list(dict.fromkeys(str(item).strip() for item in (reason_codes or []) if str(item).strip()))
    if len(reasons) > 3 or any(item not in FEEDBACK_REASON_CODES for item in reasons):
        raise ValueError("Feedback reasons are invalid")
    if helpful and reasons:
        raise ValueError("Helpful feedback must not include negative reasons")
    if not helpful and not reasons:
        raise ValueError("Unhelpful feedback requires at least one reason")
    return touchpoint, helpful, reasons


def _optimization_candidate(
    *,
    category: str,
    title: str,
    detail: str,
    action: str,
    affected: int,
    sample: int,
) -> dict[str, object] | None:
    if sample < 5 or affected <= 0:
        return None
    return {
        "category": category,
        "title": title,
        "detail": detail,
        "action": action,
        "affected_users": affected,
        "sample_size": sample,
        "affected_rate": affected / sample if sample else 0.0,
    }


def build_optimization_recommendations(
    dashboard: dict[str, object], *, limit: int = 3
) -> list[dict[str, object]]:
    """Build deterministic, aggregate-only product priorities from dashboard data."""
    candidates: list[dict[str, object]] = []

    funnel_labels = {
        "session_started": "访问到提交初稿",
        "first_draft_submitted": "提交初稿到生成报告",
        "report_generated": "生成报告到查看报告",
        "report_viewed": "查看报告到登录",
        "training_started": "进入训练到完成训练",
        "training_completed": "完成训练到生成二稿",
        "second_draft_generated": "生成二稿到查看对比",
    }
    for funnel_name in ("experience_funnel", "learning_funnel"):
        rows = [row for row in dashboard.get(funnel_name, []) if isinstance(row, dict)]
        for current, following in zip(rows, rows[1:]):
            sample = int(current.get("users") or 0)
            converted = min(sample, int(following.get("users") or 0))
            affected = sample - converted
            label = funnel_labels.get(str(current.get("stage") or ""), str(current.get("label") or "核心步骤"))
            candidate = _optimization_candidate(
                category="funnel",
                title=f"优先优化{label}",
                detail=f"{affected}/{sample} 位用户未进入下一步",
                action="检查该步骤的价值说明、操作提示和主按钮是否足够清楚。",
                affected=affected,
                sample=sample,
            )
            if candidate:
                candidates.append(candidate)

    guest_login = (
        dashboard.get("guest_report_login")
        if isinstance(dashboard.get("guest_report_login"), dict)
        else {}
    )
    guest_eligible = int(guest_login.get("eligible_users") or 0)
    guest_converted = min(
        guest_eligible, int(guest_login.get("converted_users") or 0)
    )
    candidate = _optimization_candidate(
        category="funnel",
        title="优先优化游客报告后登录",
        detail=f"{guest_eligible - guest_converted}/{guest_eligible} 位游客查看报告后未登录",
        action="让登录承接清晰的档案保存、训练和二稿价值。",
        affected=guest_eligible - guest_converted,
        sample=guest_eligible,
    )
    if candidate:
        candidates.append(candidate)

    quality = dashboard.get("quality") if isinstance(dashboard.get("quality"), dict) else {}
    for key, label in (("report", "初稿报告"), ("second_draft", "二稿报告")):
        item = quality.get(key) if isinstance(quality.get(key), dict) else {}
        attempts = int(item.get("attempts") or 0)
        failures = int(item.get("failures") or 0)
        failure_types = [row for row in item.get("failure_types", []) if isinstance(row, dict)]
        primary = str(failure_types[0].get("failure_type") or "未知错误") if failure_types else "未知错误"
        candidate = _optimization_candidate(
            category="reliability",
            title=f"优先修复{label}生成失败",
            detail=f"{failures}/{attempts} 次失败，首要类型：{primary}",
            action="先复现最高频失败类型，再检查超时、模型返回与云端保存链路。",
            affected=failures,
            sample=attempts,
        )
        if candidate:
            candidates.append(candidate)

    for row in dashboard.get("feedback", []):
        if not isinstance(row, dict):
            continue
        responses = int(row.get("responses") or 0)
        unhelpful = int(row.get("unhelpful") or 0)
        reasons = [item for item in row.get("reason_counts", []) if isinstance(item, dict)]
        primary = str(reasons[0].get("reason_code") or "未说明") if reasons else "未说明"
        label = {"report": "批改报告", "training": "专项训练", "second_draft": "二稿对比"}.get(
            str(row.get("touchpoint") or ""), "产品体验"
        )
        candidate = _optimization_candidate(
            category="feedback",
            title=f"优先改善{label}的有用性",
            detail=f"{unhelpful}/{responses} 条反馈认为帮助不足，首因：{primary}",
            action="围绕最高频原因缩短说明、补足证据或增强下一步动作。",
            affected=unhelpful,
            sample=responses,
        )
        if candidate:
            candidates.append(candidate)

    outcomes = quality.get("draft_outcomes") if isinstance(quality.get("draft_outcomes"), dict) else {}
    eligible = int(outcomes.get("eligible_users") or 0)
    improved = int(outcomes.get("improved_users") or 0)
    candidate = _optimization_candidate(
        category="learning_outcome",
        title="优先提高二稿实际改善率",
        detail=f"{eligible - improved}/{eligible} 位二稿用户未获得分数提升",
        action="复核核心优先级与训练任务是否真正对应二稿最需要解决的问题。",
        affected=max(0, eligible - improved),
        sample=eligible,
    )
    if candidate:
        candidates.append(candidate)

    order = {"reliability": 0, "funnel": 1, "feedback": 2, "learning_outcome": 3}
    candidates.sort(
        key=lambda item: (
            -int(item["affected_users"]),
            -float(item["affected_rate"]),
            order.get(str(item["category"]), 99),
            str(item["title"]),
        )
    )
    return candidates[: max(0, min(3, int(limit)))]


def range_start(days: int | None, now: datetime | None = None) -> datetime | None:
    """Return an inclusive UTC start timestamp for a rolling dashboard range."""
    if days is None:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc) - timedelta(days=days)


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def aggregate_event_rows(
    rows: Iterable[dict[str, object]],
    *,
    since: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Reference aggregator used to verify funnel, ranges, and mature cohorts."""
    current = now or datetime.now(timezone.utc)
    current = current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)
    normalized = [
        {**row, "occurred_at": _timestamp(row["occurred_at"])}
        for row in rows
        if row.get("user_id") and row.get("session_id") and row.get("event_name") and row.get("occurred_at")
    ]
    selected = [row for row in normalized if since is None or row["occurred_at"] >= since]
    first_seen: dict[str, datetime] = {}
    active_days: dict[str, set] = defaultdict(set)
    for row in normalized:
        user = str(row["user_id"])
        occurred = row["occurred_at"]
        if user not in first_seen or occurred < first_seen[user]:
            first_seen[user] = occurred
        active_days[user].add(occurred.date())

    users = {str(row["user_id"]) for row in selected}
    usage: dict[str, dict[str, int]] = {}
    for event_name in EVENT_NAMES:
        matches = [row for row in selected if row["event_name"] == event_name]
        usage[event_name] = {
            "events": len(matches),
            "users": len({str(row["user_id"]) for row in matches}),
        }

    by_user: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in selected:
        by_user[str(row["user_id"])].append(row)
    funnel_counts = defaultdict(int)
    for user_rows in by_user.values():
        user_rows.sort(key=lambda row: row["occurred_at"])

        def after(names: set[str], moment: datetime | None) -> datetime | None:
            return next(
                (
                    row["occurred_at"]
                    for row in user_rows
                    if row["event_name"] in names and (moment is None or row["occurred_at"] >= moment)
                ),
                None,
            )

        draft = after({"first_draft_submitted"}, None)
        report = after({"report_viewed"}, draft) if draft else None
        training = after({"training_started", "sentence_training_started"}, report) if report else None
        completed = after({"sentence_training_completed"}, training) if training else None
        second = after({"second_draft_submitted"}, training) if training else None
        for name, moment in (
            ("first_draft_submitted", draft),
            ("report_viewed", report),
            ("training_started", training),
            ("sentence_training_completed", completed),
            ("second_draft_submitted", second),
        ):
            funnel_counts[name] += int(moment is not None)

    daily: dict[object, dict[str, object]] = {}
    for row in selected:
        day = row["occurred_at"].date()
        bucket = daily.setdefault(day, {"day": day.isoformat(), "users": set(), "gradings": 0})
        bucket["users"].add(str(row["user_id"]))
        if row["event_name"] == "report_generated":
            bucket["gradings"] += 1

    retention: dict[str, dict[str, int | float]] = {}
    for offset, label in ((1, "day_1"), (7, "day_7")):
        eligible = [
            user
            for user, first in first_seen.items()
            if (since is None or first >= since) and first.date() <= current.date() - timedelta(days=offset)
        ]
        retained = sum(
            1 for user in eligible if first_seen[user].date() + timedelta(days=offset) in active_days[user]
        )
        retention[label] = {
            "eligible_users": len(eligible),
            "retained_users": retained,
            "rate": retained / len(eligible) if eligible else 0.0,
        }

    return {
        "unique_users": len(users),
        "new_users": sum(1 for first in first_seen.values() if since is None or first >= since),
        "sessions": len({str(row["session_id"]) for row in selected}),
        "event_usage": usage,
        "funnel": dict(funnel_counts),
        "daily": [
            {"day": bucket["day"], "active_users": len(bucket["users"]), "gradings": bucket["gradings"]}
            for _, bucket in sorted(daily.items())
        ],
        "retention": retention,
    }
