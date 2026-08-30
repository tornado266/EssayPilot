"""Pure state derivation for the action-first home dashboard.

This module deliberately has no Streamlit or persistence dependencies.  Callers can
request only the small amount of cloud data needed by the home page, then turn it
into a safe display model here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any
from urllib.parse import urlencode


CRITERION_LABELS = {
    "Task Response": "TR 任务回应",
    "Coherence and Cohesion": "CC 连贯衔接",
    "Lexical Resource": "LR 词汇资源",
    "Grammatical Range and Accuracy": "GRA 语法准确性",
}

PRACTICE_KIND_LABELS = {
    "sentence": "单句改写",
    "logic": "逻辑训练",
}


@dataclass(frozen=True, slots=True)
class PendingPracticeSummary:
    """Small, UI-ready description of the most recent unfinished task."""

    id: str | None
    grading_run_id: str
    task_kind: str
    task_label: str
    task_index: int | None
    summary: str
    updated_at: str | None


@dataclass(frozen=True, slots=True)
class HomeFact:
    """One meaningful home-page metric; absent values never create a fact."""

    key: str
    label: str
    value: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class HomeSummary:
    """The complete read-only state needed by the signed-in home page."""

    latest_overall: float | None = None
    weakest_criterion: str | None = None
    weakest_score: float | None = None
    score_delta: float | None = None
    latest_grading_run_id: str | None = None
    pending: PendingPracticeSummary | None = None
    primary_label: str = "从剑雅真题开始"
    primary_href: str = "?page=write&mode=topics"
    facts: tuple[HomeFact, ...] = ()

    @property
    def has_history(self) -> bool:
        return self.latest_grading_run_id is not None

    @property
    def has_pending(self) -> bool:
        return self.pending is not None

    @property
    def continue_grading_run_id(self) -> str | None:
        """Run that the primary action should resume, if a task is pending."""

        return self.pending.grading_run_id if self.pending is not None else None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _score(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        score = float(value)  # Supabase JSON can expose numeric columns as strings.
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score) or not 0 <= score <= 9:
        return None
    return score


def _timestamp(value: object) -> float | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return None


def _newest_first(
    values: Iterable[object] | None,
    *,
    timestamp_field: str,
) -> list[Mapping[str, Any]]:
    """Sort valid mappings by time while preserving source order as a fallback."""

    if values is None or isinstance(values, (str, bytes, Mapping)):
        return []
    try:
        candidates = list(values)
    except TypeError:
        return []

    indexed = [
        (index, item, _timestamp(item.get(timestamp_field)))
        for index, item in enumerate(candidates)
        if isinstance(item, Mapping)
    ]
    indexed.sort(
        key=lambda entry: (
            entry[2] is not None,
            entry[2] if entry[2] is not None else float("-inf"),
            -entry[0],
        ),
        reverse=True,
    )
    return [item for _, item, _ in indexed]


def _weakest_criterion(run: Mapping[str, Any]) -> tuple[str | None, float | None]:
    criteria = run.get("criteria")
    if not isinstance(criteria, list):
        return None, None

    ranked: list[tuple[float, int, str]] = []
    for index, item in enumerate(criteria):
        if not isinstance(item, Mapping):
            continue
        name = _text(item.get("criterion"))
        score = _score(item.get("score"))
        if name is None or score is None:
            continue
        ranked.append((score, index, name))
    if not ranked:
        return None, None

    score, _, name = min(ranked, key=lambda item: (item[0], item[1]))
    return CRITERION_LABELS.get(name, name), score


def _practice_index(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    return index if index >= 0 else None


def _pending_summary(item: Mapping[str, Any]) -> PendingPracticeSummary | None:
    grading_run_id = _text(item.get("grading_run_id"))
    if grading_run_id is None:
        return None

    task_kind = _text(item.get("task_kind")) or ""
    task_label = PRACTICE_KIND_LABELS.get(task_kind, "专项训练")
    source = _text(item.get("original_text"))
    if source is not None and len(source) > 72:
        source = f"{source[:71].rstrip()}…"
    summary = f"{task_label}：{source}" if source else task_label

    return PendingPracticeSummary(
        id=_text(item.get("id")),
        grading_run_id=grading_run_id,
        task_kind=task_kind,
        task_label=task_label,
        task_index=_practice_index(item.get("task_index")),
        summary=summary,
        updated_at=_text(item.get("updated_at")),
    )


def _format_score(score: float) -> str:
    return f"{score:.1f}"


def _facts(
    latest_score: float | None,
    weakest: str | None,
    weakest_score: float | None,
    delta: float | None,
) -> tuple[HomeFact, ...]:
    facts: list[HomeFact] = []
    if latest_score is not None:
        facts.append(HomeFact("overall", "最新 Overall", _format_score(latest_score), "IELTS Task 2"))
    if weakest is not None:
        facts.append(
            HomeFact(
                "weakest",
                "当前薄弱项",
                weakest,
                f"Band {_format_score(weakest_score)}" if weakest_score is not None else None,
            )
        )
    if delta is not None:
        signed_delta = "0.0" if delta == 0 else f"{delta:+.1f}"
        facts.append(HomeFact("delta", "较上一次", signed_delta, "Overall 真实变化"))
    return tuple(facts[:3])


def build_home_summary(
    runs: Iterable[object] | None,
    pending_items: Iterable[object] | None,
) -> HomeSummary:
    """Build a resilient home model from recent grading runs and pending tasks.

    The most recent *usable* scored run becomes the history anchor.  This keeps one
    corrupt row from hiding otherwise valid history, while never inventing a score.
    Only the first two usable scores participate in the displayed delta.
    """

    ordered_runs = _newest_first(runs, timestamp_field="created_at")
    scored_runs = [
        (run, _score(run.get("overall_band")))
        for run in ordered_runs
        if _score(run.get("overall_band")) is not None and _text(run.get("id")) is not None
    ]

    latest: Mapping[str, Any] | None = None
    latest_score: float | None = None
    previous_score: float | None = None
    if scored_runs:
        latest, latest_score = scored_runs[0]
        if len(scored_runs) > 1:
            previous_score = scored_runs[1][1]

    weakest, weakest_score = _weakest_criterion(latest) if latest is not None else (None, None)
    delta = None
    if latest_score is not None and previous_score is not None:
        delta = round(latest_score - previous_score, 2)

    pending: PendingPracticeSummary | None = None
    for item in _newest_first(pending_items, timestamp_field="updated_at"):
        pending = _pending_summary(item)
        if pending is not None:
            break

    primary_label = "从剑雅真题开始"
    primary_href = "?page=write&mode=topics"
    if pending is not None:
        primary_label = "继续这项训练"
        primary_href = f"?{urlencode({'page': 'training', 'run_id': pending.grading_run_id})}"

    return HomeSummary(
        latest_overall=latest_score,
        weakest_criterion=weakest,
        weakest_score=weakest_score,
        score_delta=delta,
        latest_grading_run_id=_text(latest.get("id")) if latest is not None else None,
        pending=pending,
        primary_label=primary_label,
        primary_href=primary_href,
        facts=_facts(latest_score, weakest, weakest_score, delta),
    )


__all__ = [
    "CRITERION_LABELS",
    "HomeFact",
    "HomeSummary",
    "PendingPracticeSummary",
    "build_home_summary",
]
