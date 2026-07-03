"""Persistence helpers for Draft 1 to Draft 2 training records."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.storage import get_user_records_dir


def save_draft_training_record(
    *,
    user_id: str,
    task_question: str,
    draft_1_text: str,
    draft_1_scores: dict[str, float | None],
    draft_1_feedback: str,
    draft_2_text: str,
    draft_2_scores: dict[str, float | None],
    draft_2_feedback: str,
    progress_report: str,
) -> Path:
    """Save a complete two-draft learning cycle for one anonymous user."""
    records_dir = get_user_records_dir(user_id)
    records_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now().astimezone()
    path = records_dir / f"draft_training_{created_at.strftime('%Y%m%d_%H%M%S_%f')}.json"
    payload = {
        "record_type": "draft_training",
        "user_id": user_id,
        "task_question": task_question,
        "draft_1_text": draft_1_text,
        "draft_1_scores": draft_1_scores,
        "draft_1_feedback": draft_1_feedback,
        "draft_2_text": draft_2_text,
        "draft_2_scores": draft_2_scores,
        "draft_2_feedback": draft_2_feedback,
        "progress_report": progress_report,
        "timestamp": created_at.isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def list_draft_training_history(user_id: str) -> list[dict[str, Any]]:
    """Return valid two-draft records belonging only to the requested user."""
    records_dir = get_user_records_dir(user_id)
    if not records_dir.exists():
        return []

    records: list[dict[str, Any]] = []
    for path in sorted(records_dir.glob("draft_training_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("user_id") != user_id:
            continue
        data["path"] = path
        records.append(data)
    return records
