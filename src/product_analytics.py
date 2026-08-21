"""Privacy-safe product analytics helpers shared by the app and tests."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable


EVENT_NAMES = (
    "session_started",
    "first_draft_submitted",
    "report_generated",
    "report_generation_failed",
    "report_viewed",
    "tutorial_clicked",
    "problem_map_viewed",
    "training_started",
    "sentence_training_started",
    "sentence_training_completed",
    "mistake_saved",
    "archive_viewed",
    "second_draft_submitted",
    "diff_viewed",
    "dictionary_opened",
)

_SAFE_METADATA_KEYS = {
    "cached",
    "draft_number",
    "entry_mode",
    "failure_type",
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
    occurrence_key: str = "",
) -> str:
    """Return a stable opaque key safe to persist across Streamlit reruns."""
    if event_name not in EVENT_NAMES:
        raise ValueError(f"Unsupported analytics event: {event_name}")
    material = "\0".join(("analytics-v1", event_name, session_id, run_id, occurrence_key))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def sanitize_metadata(metadata: dict[str, object] | None) -> dict[str, object]:
    """Keep only small, explicitly approved, non-sensitive metadata values."""
    clean: dict[str, object] = {}
    for key, value in (metadata or {}).items():
        if key not in _SAFE_METADATA_KEYS or not isinstance(value, (str, int, float, bool)):
            continue
        clean[key] = value if not isinstance(value, str) else value[:80]
    encoded = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
    return clean if len(encoded.encode("utf-8")) <= 1024 else {}


def record_event_safely(
    recorder: Callable[[], object],
    *,
    asynchronous: bool = False,
    logger: logging.Logger | None = None,
) -> bool:
    """Run or queue analytics without ever raising into the product flow."""
    event_logger = logger or logging.getLogger(__name__)

    def guarded() -> bool:
        try:
            recorder()
        except Exception as exc:  # Analytics must never interrupt grading or training.
            event_logger.warning("Product analytics event was not recorded: %s", exc)
            return False
        return True

    if asynchronous:
        try:
            _EXECUTOR.submit(guarded)
        except Exception as exc:
            event_logger.warning("Product analytics event could not be queued: %s", exc)
            return False
        return True
    return guarded()


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
